#!/usr/bin/env python3
"""One-command max-speed search for the Exlink RP2040 JTAG bridge."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, asdict
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import statistics
import subprocess
import sys
import time
from typing import Iterable

from exlink_jtag_test import BridgeError, ExlinkJtagBridge, bits_to_bytes, words32_lsb_first


DEFAULT_DMA_CHUNKS = (2048, 4096, 8192, 16384, 32768)
IDCODE_READS = 10


@dataclass
class ProgramRun:
    run: int
    type: str
    status: str
    elapsed_ms: int
    reason: str = ""


@dataclass
class CandidateResult:
    engine: str
    dma_chunk_bits: int
    requested_tck_hz: int
    actual_tck_hz: int
    pio_cycles_per_bit: int
    idcode: str = ""
    idcode_pass_count: int = 0
    idcode_pass: bool = False
    vivado_pass: bool = False
    confirmed: bool = False
    download_times_s: list[float] | None = None
    warmup_time_s: float | None = None
    failure_reason: str = ""
    xvc_effective_bit_s: float = 0.0
    firmware_effective_bit_s: float = 0.0
    bottleneck: str = "not measured"

    def times(self) -> list[float]:
        return self.download_times_s or []


def calculate_max_tck_hz(clk_sys_hz: int, cycles_per_bit: int) -> int:
    if clk_sys_hz <= 0 or cycles_per_bit <= 0:
        raise ValueError("clk_sys_hz and cycles_per_bit must be positive")
    return clk_sys_hz // cycles_per_bit


def calculate_pio_divider(clk_sys_hz: int, requested_tck_hz: int, cycles_per_bit: int) -> tuple[float, int]:
    if requested_tck_hz <= 0:
        raise ValueError("requested_tck_hz must be positive")
    divider = clk_sys_hz / (requested_tck_hz * cycles_per_bit)
    if divider < 1.0 or divider > 65535.0:
        raise ValueError("PIO divider outside hardware range")
    actual = int(clk_sys_hz / (divider * cycles_per_bit))
    return divider, actual


def top_down_frequency_sequence(maximum_khz: int, minimum_khz: int, step_khz: int) -> list[int]:
    if maximum_khz < minimum_khz:
        return []
    if step_khz <= 0:
        raise ValueError("step_khz must be positive")
    values = list(range(maximum_khz, minimum_khz - 1, -step_khz))
    if values[-1] != minimum_khz:
        values.append(minimum_khz)
    return values


def binary_search_points(fail_khz: int, pass_khz: int, resolution_khz: int) -> list[int]:
    if fail_khz <= pass_khz:
        raise ValueError("fail_khz must be greater than pass_khz")
    if resolution_khz <= 0:
        raise ValueError("resolution_khz must be positive")
    points: list[int] = []
    low = pass_khz
    high = fail_khz
    while high - low > resolution_khz:
        mid = (high + low) // 2
        points.append(mid)
        low = mid
    return points


def idcodes_consistent(values: Iterable[int]) -> bool:
    values = list(values)
    return bool(values) and all(v == values[0] for v in values) and values[0] not in (0, 0xFFFFFFFF) and (values[0] & 1)


def parse_vivado_program_output(text: str) -> list[ProgramRun]:
    runs: list[ProgramRun] = []
    pattern = re.compile(
        r"^EXLINK_PROGRAM\s+run=(?P<run>\d+)\s+type=(?P<type>\S+)\s+"
        r"status=(?P<status>PASS|FAIL)\s+elapsed_ms=(?P<elapsed>\d+)(?:\s+reason=(?P<reason>.*))?"
    )
    for line in text.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        runs.append(
            ProgramRun(
                run=int(match.group("run")),
                type=match.group("type"),
                status=match.group("status"),
                elapsed_ms=int(match.group("elapsed")),
                reason=(match.group("reason") or "").strip(),
            )
        )
    return runs


def all_test_runs_pass(runs: Iterable[ProgramRun]) -> bool:
    selected = [run for run in runs if run.type == "test"]
    return bool(selected) and all(run.status == "PASS" for run in selected)


def summarize_times(values: Iterable[float]) -> dict[str, float]:
    values = sorted(values)
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0, "p95": 0.0, "stdev": 0.0}
    p95_index = min(len(values) - 1, math.ceil(len(values) * 0.95) - 1)
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": values[p95_index],
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def parse_profile_text(text: str) -> dict[str, int | float | str]:
    values: dict[str, int | float | str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        try:
            values[key.strip()] = int(value)
        except ValueError:
            values[key.strip()] = value
    return values


def infer_bottleneck(profile: dict[str, int | float | str], xvc_rate: float, actual_tck_hz: int) -> str:
    if not profile:
        return "not measured"
    if actual_tck_hz and xvc_rate and xvc_rate < actual_tck_hz * 0.25:
        rx_wait = int(profile.get("usb_rx_incomplete_waits", 0) or 0)
        tx_wait = int(profile.get("usb_tx_space_waits", 0) or 0)
        parse_count = int(profile.get("request_parse_us: count", 0) or 0)
        if tx_wait > rx_wait:
            return "USB TX"
        if rx_wait > 0:
            return "USB RX"
        if parse_count > 0:
            return "XVC request roundtrip"
    return "PIO/TCK"


def write_report(report_dir: Path, results: list[CandidateResult], environment: dict[str, object],
                 baseline_seconds: float, target_seconds: float) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
    (report_dir / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")

    with (report_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()) if results else ["engine"])
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["download_times_s"] = " ".join(f"{v:.6f}" for v in result.times())
            writer.writerow(row)

    downloadable = [r for r in results if r.vivado_pass and r.times()]
    fastest = min(downloadable, key=lambda r: summarize_times(r.times())["median"], default=None)
    highest_id = max((r for r in results if r.idcode_pass), key=lambda r: r.actual_tck_hz, default=None)
    highest_download = max(downloadable, key=lambda r: r.actual_tck_hz, default=None)
    confirmed = max((r for r in results if r.confirmed), key=lambda r: r.actual_tck_hz, default=None)

    lines = ["# Exlink Maxspeed Summary", ""]
    lines.append(f"Highest theoretical TCK: {environment.get('maximum_theoretical_tck_hz', 0)} Hz")
    lines.append(f"Highest IDCODE stable TCK: {highest_id.actual_tck_hz if highest_id else 0} Hz")
    lines.append(f"Highest downloadable TCK: {highest_download.actual_tck_hz if highest_download else 0} Hz")
    lines.append(f"10-run confirmed stable TCK: {confirmed.actual_tck_hz if confirmed else 0} Hz")
    if fastest:
        stats = summarize_times(fastest.times())
        improvement = ((baseline_seconds - stats["median"]) / baseline_seconds) * 100.0 if baseline_seconds else 0.0
        gap = stats["median"] / target_seconds if target_seconds else 0.0
        lines.append(f"Fastest download config: engine={fastest.engine} chunk={fastest.dma_chunk_bits} actual_tck={fastest.actual_tck_hz} Hz")
        lines.append(f"Fastest single run: {stats['min']:.3f} s")
        lines.append(f"Fastest mean: {stats['mean']:.3f} s")
        lines.append(f"Fastest median: {stats['median']:.3f} s")
        lines.append(f"Improvement vs {baseline_seconds:.3f} s: {improvement:.1f}%")
        lines.append(f"Gap vs {target_seconds:.3f} s: {gap:.2f}x")
        lines.append(f"Recommended default TCK: {confirmed.actual_tck_hz if confirmed else fastest.actual_tck_hz} Hz")
        lines.append(f"Primary bottleneck: {fastest.bottleneck}")
    else:
        lines.append("Fastest download config: not measured")
        lines.append("Fastest single run: not measured")
        lines.append("Fastest mean: not measured")
        lines.append("Fastest median: not measured")
        lines.append("Improvement: not measured")
        lines.append("Gap: not measured")
        lines.append("Recommended default TCK: not measured")
        lines.append("Primary bottleneck: not measured")
    lines.append("Next optimization: use this report's profile stage percentages to choose USB CDC vs DMA packing work.")
    lines.append("")
    lines.append("Hardware tests not executed in this run are intentionally reported as not measured, not PASS.")
    (report_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_idcode_once(bridge: ExlinkJtagBridge) -> int:
    bridge.reset_tap()
    bridge.shift(3, bits_to_bytes([1, 0, 0]), bits_to_bytes([0, 0, 0]))
    tms_bits = [0] * 32
    tms_bits[-1] = 1
    tdo = bridge.shift(32, bits_to_bytes(tms_bits), bits_to_bytes([0] * 32))
    bridge.shift(2, bits_to_bytes([1, 0]), bits_to_bytes([0, 0]))
    return words32_lsb_first(tdo, 32)[0]


def idcode_scan(bridge: ExlinkJtagBridge, count: int = IDCODE_READS) -> tuple[bool, list[int]]:
    values = [read_idcode_once(bridge) for _ in range(count)]
    return idcodes_consistent(values), values


def run_vivado(vivado: Path, script: Path, bitstream: Path, xvc_port: int, device_index: int,
               warmup: int, runs: int, log_path: Path) -> list[ProgramRun]:
    cmd = [
        str(vivado), "-mode", "batch",
        "-source", str(script),
        "-tclargs",
        "-bitstream", str(bitstream),
        "-xvc_port", str(xvc_port),
        "-device_index", str(device_index),
        "-warmup", str(warmup),
        "-runs", str(runs),
    ]
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log_path.write_text(completed.stdout, encoding="utf-8", errors="replace")
    runs_out = parse_vivado_program_output(completed.stdout)
    if completed.returncode != 0 and all_test_runs_pass(runs_out):
        raise RuntimeError(f"Vivado returned {completed.returncode} despite PASS output")
    return runs_out


def wait_tcp_port(port: int, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--bitstream")
    parser.add_argument("--vivado")
    parser.add_argument("--xvc-port", type=int, default=2542)
    parser.add_argument("--coarse-step-khz", type=int, default=2500)
    parser.add_argument("--binary-resolution-khz", type=int, default=250)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--confirm-runs", type=int, default=10)
    parser.add_argument("--baseline-seconds", type=float, default=27.245)
    parser.add_argument("--target-seconds", type=float, default=3.400)
    parser.add_argument("--minimum-khz", type=int, default=50)
    parser.add_argument("--maximum-khz", type=int)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--engine", choices=["pio_safe", "pio_fast"], default="pio_safe")
    parser.add_argument("--dma-chunks", default="8192", help="comma-separated chunk bits")
    parser.add_argument("--skip-fast-engine", action="store_true")
    parser.add_argument("--skip-vivado", action="store_true")
    parser.add_argument("--report-dir")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    start_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = Path(args.report_dir or Path("reports") / "maxspeed" / start_time)
    report_dir.mkdir(parents=True, exist_ok=True)
    results: list[CandidateResult] = []

    bridge: ExlinkJtagBridge | None = ExlinkJtagBridge(args.port, args.baudrate, args.timeout)
    try:
        assert bridge is not None
        info = bridge.info_dict()
        clk_sys_hz = int(info.get("clk_sys_hz", info.get("clk_sys", "0")))
        cycles = int(info.get("PIO cycles per bit", "5"))
        theoretical_hz = calculate_max_tck_hz(clk_sys_hz, cycles) if clk_sys_hz else int(info.get("Maximum theoretical TCK", "0"))
        maximum_khz = args.maximum_khz or max(args.minimum_khz, theoretical_hz // 1000)
        chunks = [int(value) for value in args.dma_chunks.split(",") if value.strip()]
        frequencies = top_down_frequency_sequence(maximum_khz, args.minimum_khz, args.coarse_step_khz)

        def run_with_xvc(chunk: int, khz: int, warmup: int, runs: int, log_stem: str) -> list[ProgramRun]:
            nonlocal bridge
            if bridge is not None:
                bridge.close()
                bridge = None
            xvc_log = report_dir / f"xvc_{log_stem}.log"
            json_summary = report_dir / f"xvc_{log_stem}.jsonl"
            xvc_cmd = [
                sys.executable, str(Path(__file__).with_name("exlink_xvc_server.py")),
                "--port", args.port,
                "--xvc-port", str(args.xvc_port),
                "--force-tck-khz", str(khz),
                "--engine", args.engine,
                "--dma-chunk-bits", str(chunk),
                "--profile",
                "--json-summary", str(json_summary),
                "--log-file", str(xvc_log),
            ]
            xvc = subprocess.Popen(xvc_cmd)
            try:
                if not wait_tcp_port(args.xvc_port):
                    raise RuntimeError("XVC server did not start")
                return run_vivado(
                    Path(args.vivado),
                    Path(__file__).with_name("exlink_vivado_program.tcl"),
                    Path(args.bitstream),
                    args.xvc_port,
                    args.device_index,
                    warmup,
                    runs,
                    report_dir / f"vivado_{log_stem}.log",
                )
            finally:
                xvc.terminate()
                try:
                    xvc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    xvc.kill()
                time.sleep(0.5)
                bridge = ExlinkJtagBridge(args.port, args.baudrate, args.timeout)
                bridge.set_dma_chunk_bits(chunk)
                bridge.select_engine(args.engine)

        for chunk in chunks:
            assert bridge is not None
            bridge.set_dma_chunk_bits(chunk)
            bridge.select_engine(args.engine)
            for khz in frequencies:
                result = CandidateResult(args.engine, chunk, khz * 1000, 0, cycles)
                try:
                    actual = bridge.set_pio_clock_hz(khz * 1000)
                    result.actual_tck_hz = actual
                    ok, idcodes = idcode_scan(bridge)
                    result.idcode_pass = ok
                    result.idcode_pass_count = len(idcodes) if ok else 0
                    result.idcode = f"0x{idcodes[0]:08X}" if idcodes else ""
                    if not ok:
                        result.failure_reason = "IDCODE mismatch"
                        results.append(result)
                        continue
                    if args.skip_vivado:
                        result.failure_reason = "Vivado skipped by --skip-vivado"
                        results.append(result)
                        continue
                    if not args.vivado or not args.bitstream:
                        raise RuntimeError("--vivado and --bitstream are required unless --skip-vivado is used")
                    vivado_runs = run_with_xvc(chunk, khz, args.warmup, args.runs, f"{chunk}_{khz}")
                    result.vivado_pass = all_test_runs_pass(vivado_runs)
                    result.download_times_s = [run.elapsed_ms / 1000.0 for run in vivado_runs if run.type == "test" and run.status == "PASS"]
                    result.warmup_time_s = next((run.elapsed_ms / 1000.0 for run in vivado_runs if run.type == "warmup"), None)
                except Exception as exc:
                    result.failure_reason = str(exc)
                    try:
                        if bridge is None:
                            time.sleep(0.5)
                            bridge = ExlinkJtagBridge(args.port, args.baudrate, args.timeout)
                        bridge.recover_link()
                        bridge.set_pio_clock_hz(args.minimum_khz * 1000)
                        bridge.reset_tap()
                    except Exception:
                        pass
                results.append(result)

        if not args.skip_vivado:
            downloadable = [r for r in results if r.vivado_pass and r.times()]
            for candidate in sorted(downloadable, key=lambda r: summarize_times(r.times())["median"]):
                khz = max(1, candidate.requested_tck_hz // 1000)
                try:
                    confirm_runs = run_with_xvc(
                        candidate.dma_chunk_bits,
                        khz,
                        args.warmup,
                        args.confirm_runs,
                        f"confirm_{candidate.dma_chunk_bits}_{khz}",
                    )
                    test_runs = [run for run in confirm_runs if run.type == "test"]
                    candidate.confirmed = len(test_runs) == args.confirm_runs and all(run.status == "PASS" for run in test_runs)
                    if candidate.confirmed:
                        candidate.download_times_s = [run.elapsed_ms / 1000.0 for run in test_runs]
                        candidate.warmup_time_s = next((run.elapsed_ms / 1000.0 for run in confirm_runs if run.type == "warmup"), None)
                        break
                    candidate.failure_reason = "10-run confirmation failed"
                except Exception as exc:
                    candidate.failure_reason = f"confirmation failed: {exc}"

        environment = {
            "git_commit": subprocess.run(["git", "-C", "sigrok-pico", "rev-parse", "HEAD"], text=True, stdout=subprocess.PIPE).stdout.strip(),
            "git_dirty": bool(subprocess.run(["git", "-C", "sigrok-pico", "status", "--short"], text=True, stdout=subprocess.PIPE).stdout.strip()),
            "firmware_info": info,
            "com_port": args.port,
            "vid_pid": "not queried",
            "clk_sys": clk_sys_hz,
            "maximum_theoretical_tck_hz": theoretical_hz,
            "vivado_path": args.vivado or "",
            "vivado_version": "not queried",
            "bitstream_path": args.bitstream or "",
            "bitstream_sha256": sha256_file(Path(args.bitstream)) if args.bitstream and Path(args.bitstream).exists() else "",
            "test_start_time": start_time,
            "os": platform.platform(),
            "python_version": sys.version,
            "hardware_vivado_executed": not args.skip_vivado,
        }
        write_report(report_dir, results, environment, args.baseline_seconds, args.target_seconds)
        print(f"Report: {report_dir}")
        return 0
    finally:
        if bridge is not None:
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
