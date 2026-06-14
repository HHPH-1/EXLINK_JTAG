#!/usr/bin/env python3
"""Low-level test client for the Exlink RP2040 JTAG bridge."""

from __future__ import annotations

import argparse
import random
import struct
import sys
import time
from typing import Iterable, List, Tuple

try:
    import serial
except ImportError as exc:  # pragma: no cover - host dependency hint
    raise SystemExit("pyserial is required: py -m pip install pyserial") from exc


MAX_SHIFT_BITS = 32768
ENGINE_NAMES = {
    0: "bitbang",
    1: "pio",
}
ENGINE_VALUES = {value: key for key, value in ENGINE_NAMES.items()}
JTAG_ENGINE_FLAG_BITBANG = 1 << 0
JTAG_ENGINE_FLAG_PIO = 1 << 1
JTAG_ENGINE_FLAG_DMA = 1 << 2
PROFILE_ACTIONS = {
    "show": 0,
    "clear": 1,
    "on": 2,
    "off": 3,
}


class BridgeError(RuntimeError):
    pass


class SerialTimeoutError(BridgeError):
    pass


def serial_read_exact(ser, length: int) -> bytes:
    if length < 0:
        raise ValueError("length must be non-negative")

    chunks = bytearray()
    while len(chunks) < length:
        chunk = ser.read(length - len(chunks))
        if not chunk:
            raise SerialTimeoutError(f"CDC timeout: expected {length} bytes, got {len(chunks)}")
        chunks.extend(chunk)
    return bytes(chunks)


def serial_write_all(ser, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        try:
            written = ser.write(view[offset:])
        except serial.SerialTimeoutException as exc:
            raise SerialTimeoutError(f"CDC write timeout after {offset} of {len(view)} bytes") from exc
        if written is None:
            written = len(view) - offset
        if written <= 0:
            raise SerialTimeoutError(f"CDC write timeout after {offset} of {len(view)} bytes")
        offset += written


class ExlinkJtagBridge:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0):
        self.serial = serial.Serial(port=port, baudrate=baudrate, timeout=timeout, write_timeout=timeout)

    def close(self) -> None:
        self.serial.close()

    def read_exact(self, length: int) -> bytes:
        return serial_read_exact(self.serial, length)

    def write_all(self, data: bytes) -> None:
        serial_write_all(self.serial, data)

    def recover_link(self) -> None:
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

    def expect_error_or(self, expected_tag: bytes) -> bytes:
        tag = self.read_exact(1)
        if tag == b"e":
            code = self.read_exact(1)[0]
            raise BridgeError(f"bridge returned error {code}")
        if tag != expected_tag:
            raise BridgeError(f"unexpected response tag {tag!r}, expected {expected_tag!r}")
        return tag

    def info(self) -> str:
        self.write_all(b"I")
        self.expect_error_or(b"i")
        length = struct.unpack("<H", self.read_exact(2))[0]
        return self.read_exact(length).decode("ascii", errors="replace")

    def reset_tap(self) -> None:
        self.write_all(b"T")
        self.expect_error_or(b"t")
        status = self.read_exact(1)[0]
        if status != 0:
            raise BridgeError(f"TAP reset failed with status {status}")

    def set_clock(self, half_period_us: int) -> int:
        self.write_all(b"K" + struct.pack("<I", half_period_us))
        self.expect_error_or(b"k")
        status = self.read_exact(1)[0]
        applied = struct.unpack("<I", self.read_exact(4))[0]
        if status != 0:
            raise BridgeError(f"clock request rejected; current half-period is {applied} us")
        return applied

    def set_pio_clock_hz(self, requested_hz: int) -> int:
        self.write_all(b"P" + struct.pack("<I", requested_hz))
        self.expect_error_or(b"p")
        status = self.read_exact(1)[0]
        actual = struct.unpack("<I", self.read_exact(4))[0]
        if status != 0:
            raise BridgeError(f"PIO clock request rejected; current PIO TCK is {actual} Hz")
        return actual

    def capabilities(self) -> Tuple[int, int, int]:
        self.write_all(b"Q")
        self.expect_error_or(b"q")
        status = self.read_exact(1)[0]
        active = self.read_exact(1)[0]
        supported = self.read_exact(1)[0]
        max_shift_bits = struct.unpack("<I", self.read_exact(4))[0]
        if status != 0:
            raise BridgeError(f"capabilities query failed with status {status}")
        return active, supported, max_shift_bits

    def profile(self, action: str) -> Tuple[bool, str]:
        if action not in PROFILE_ACTIONS:
            raise ValueError(f"unknown profile action {action!r}")

        self.write_all(b"R" + bytes([PROFILE_ACTIONS[action]]))
        self.expect_error_or(b"r")
        status = self.read_exact(1)[0]
        enabled = self.read_exact(1)[0] != 0
        length = struct.unpack("<H", self.read_exact(2))[0]
        text = self.read_exact(length).decode("ascii", errors="replace")
        if status != 0:
            raise BridgeError(f"profile {action} failed with status {status}")
        return enabled, text

    def select_engine(self, engine: str) -> int:
        if engine not in ENGINE_VALUES:
            raise ValueError(f"unknown engine {engine!r}")

        self.write_all(b"M" + bytes([ENGINE_VALUES[engine]]))
        self.expect_error_or(b"m")
        status = self.read_exact(1)[0]
        active = self.read_exact(1)[0]
        if status != 0:
            raise BridgeError(f"engine switch to {engine} failed with status {status}; active={engine_name(active)}")
        return active

    def shift(self, bit_count: int, tms: bytes, tdi: bytes) -> bytes:
        if bit_count <= 0 or bit_count > MAX_SHIFT_BITS:
            raise ValueError(f"bit_count must be 1..{MAX_SHIFT_BITS}")
        byte_count = (bit_count + 7) // 8
        if len(tms) != byte_count or len(tdi) != byte_count:
            raise ValueError("TMS/TDI payload lengths do not match bit_count")

        frame = b"S" + struct.pack("<I", bit_count) + tms + tdi
        self.write_all(frame)
        self.expect_error_or(b"s")
        status = self.read_exact(1)[0]
        returned_bits = struct.unpack("<I", self.read_exact(4))[0]
        if returned_bits != bit_count:
            raise BridgeError(f"bridge returned bit_count {returned_bits}, expected {bit_count}")
        if status != 0:
            raise BridgeError(f"shift failed with status {status}")
        return self.read_exact(byte_count)


def bits_to_bytes(bits: Iterable[int]) -> bytes:
    out = bytearray()
    for index, bit in enumerate(bits):
        if (index & 7) == 0:
            out.append(0)
        if bit:
            out[index >> 3] |= 1 << (index & 7)
    return bytes(out)


def get_bit(data: bytes, index: int) -> int:
    return (data[index >> 3] >> (index & 7)) & 1


def mask_unused_high_bits(data: bytes | bytearray, bit_count: int) -> bytes:
    out = bytearray(data)
    used = bit_count & 7
    if used and out:
        out[-1] &= (1 << used) - 1
    return bytes(out)


def random_payload(bit_count: int, rng: random.Random) -> bytes:
    byte_count = (bit_count + 7) // 8
    if hasattr(rng, "randbytes"):
        data = rng.randbytes(byte_count)
    else:  # pragma: no cover - for older Python builds
        data = bytes(rng.getrandbits(8) for _ in range(byte_count))
    return mask_unused_high_bits(data, bit_count)


def bytes_to_bit_string(data: bytes, bit_count: int) -> str:
    return "".join(str(get_bit(data, bit)) for bit in range(bit_count))


def words32_lsb_first(data: bytes, bit_count: int) -> List[int]:
    words: List[int] = []
    for base in range(0, bit_count, 32):
        value = 0
        for bit in range(min(32, bit_count - base)):
            value |= get_bit(data, base + bit) << bit
        words.append(value)
    return words


def candidate_idcode(value: int) -> bool:
    return (value & 1) == 1 and value not in (0x00000000, 0xFFFFFFFF)


def engine_name(value: int) -> str:
    return ENGINE_NAMES.get(value, f"unknown({value})")


def supported_engine_names(flags: int) -> List[str]:
    names: List[str] = []
    if flags & JTAG_ENGINE_FLAG_BITBANG:
        names.append("bitbang")
    if flags & JTAG_ENGINE_FLAG_PIO:
        names.append("pio")
    if flags & JTAG_ENGINE_FLAG_DMA:
        names.append("dma")
    return names


def make_pattern(bit_count: int, pattern: str, seed: int) -> bytes:
    byte_count = (bit_count + 7) // 8
    if pattern == "zero":
        return bytes(byte_count)
    if pattern == "one":
        data = bytearray([0xFF] * byte_count)
        return mask_unused_high_bits(data, bit_count)
    if pattern == "random":
        rng = random.Random(seed)
        return random_payload(bit_count, rng)
    raise ValueError(f"unknown pattern {pattern!r}")


def run_loopback_payload(bridge: ExlinkJtagBridge, bit_count: int, tdi: bytes) -> None:
    tms = bytes((bit_count + 7) // 8)
    tdo = bridge.shift(bit_count, tms, tdi)
    if mask_unused_high_bits(tdo, bit_count) != mask_unused_high_bits(tdi, bit_count):
        mismatches = [bit for bit in range(bit_count) if get_bit(tdo, bit) != get_bit(tdi, bit)]
        preview = ", ".join(str(bit) for bit in mismatches[:16])
        raise BridgeError(f"loopback mismatch at {len(mismatches)} bit(s): {preview}")


def cmd_info(bridge: ExlinkJtagBridge, _args: argparse.Namespace) -> None:
    print(bridge.info())


def cmd_reset(bridge: ExlinkJtagBridge, _args: argparse.Namespace) -> None:
    bridge.reset_tap()
    print("PASS: TAP reset command completed")


def cmd_clock(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    applied = bridge.set_clock(args.half_period_us)
    print(f"PASS: half-period set to {applied} us")


def cmd_clock_pio(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    requested_hz = args.khz * 1000
    actual_hz = bridge.set_pio_clock_hz(requested_hz)
    print(f"PASS: PIO TCK set to {actual_hz} Hz ({actual_hz / 1000:.1f} kHz)")


def cmd_capabilities(bridge: ExlinkJtagBridge, _args: argparse.Namespace) -> None:
    active, supported, max_shift_bits = bridge.capabilities()
    names = supported_engine_names(supported)
    print(f"Active engine: {engine_name(active)}")
    print(f"Supported engines: {', '.join(names) if names else 'none'}")
    print(f"Maximum shift: {max_shift_bits} bits")


def cmd_profile(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    _enabled, text = bridge.profile(args.action)
    print(text, end="" if text.endswith("\n") else "\n")


def cmd_engine(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    active = bridge.select_engine(args.engine)
    print(f"PASS: active engine is {engine_name(active)}")


def cmd_loopback(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    tdi = random_payload(args.bits, rng)
    run_loopback_payload(bridge, args.bits, tdi)

    print("PASS: TDI->TDO loopback matched")
    print("Temporarily connect CHAN3/TDI to CHAN2/TDO.")
    print("Remove the temporary CHAN3/TDI to CHAN2/TDO jumper before connecting a target.")


def cmd_boundary_test(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    lengths = [1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65,
               127, 128, 129, 255, 256, 257, 511, 512, 513,
               1023, 1024, 1025, 2047, 2048, 2049,
               4095, 4096, 4097, 8191, 8192, 8193,
               16383, 16384, 16385, 32767, 32768]
    patterns = ["zero", "one", "random"]

    for bit_count in lengths:
        for pattern in patterns:
            payload = make_pattern(bit_count, pattern, args.seed + bit_count)
            run_loopback_payload(bridge, bit_count, payload)
        print(f"PASS: {bit_count} bits")

    print("PASS: boundary loopback test completed")
    print("Remove the temporary CHAN3/TDI to CHAN2/TDO jumper before connecting a target.")


def cmd_stress(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    active, supported, _max_shift_bits = bridge.capabilities()
    start = time.perf_counter()
    latencies = []

    for iteration in range(args.count):
        payload = random_payload(args.bits, rng)
        request_start = time.perf_counter()
        run_loopback_payload(bridge, args.bits, payload)
        latencies.append(time.perf_counter() - request_start)
        if args.progress and ((iteration + 1) % args.progress) == 0:
            print(f"PASS: {iteration + 1}/{args.count}")

    elapsed = time.perf_counter() - start
    total_bits = args.bits * args.count
    rate = total_bits / elapsed if elapsed > 0 else 0.0
    average_latency = sum(latencies) / len(latencies) if latencies else 0.0
    print("PASS: stress completed")
    print(f"Engine: {engine_name(active)}")
    print(f"DMA: {'yes' if supported & (1 << 2) else 'no'}")
    print(f"Bit count: {args.bits}")
    print(f"Count: {args.count}")
    print(f"Total bits: {total_bits}")
    print(f"Total time: {elapsed:.3f} s")
    print(f"Effective bit/s: {rate:.0f}")
    print(f"Average request latency: {average_latency * 1000:.3f} ms")


def cmd_benchmark(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    active, supported, _max_shift_bits = bridge.capabilities()
    lengths = [128, 512, 4096, 8192, 16384, 32768]
    print(f"Engine: {engine_name(active)}")
    print(f"DMA: {'yes' if supported & (1 << 2) else 'no'}")
    for bit_count in lengths:
        payloads = [random_payload(bit_count, rng) for _ in range(args.count)]
        start = time.perf_counter()
        for payload in payloads:
            run_loopback_payload(bridge, bit_count, payload)
        elapsed = time.perf_counter() - start
        total_bits = bit_count * args.count
        rate = total_bits / elapsed if elapsed > 0 else 0.0
        print(f"{bit_count:5d} bits: {rate:10.0f} bit/s over {args.count} runs ({elapsed:.3f} s)")
    print("PASS: benchmark completed")


def cmd_scan(bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    bridge.reset_tap()
    bridge.shift(3, bits_to_bytes([1, 0, 0]), bits_to_bytes([0, 0, 0]))

    tms_bits = [0 for _ in range(args.bits)]
    tms_bits[-1] = 1
    tdo = bridge.shift(args.bits, bits_to_bytes(tms_bits), bits_to_bytes([0] * args.bits))
    bridge.shift(2, bits_to_bytes([1, 0]), bits_to_bytes([0, 0]))

    words = words32_lsb_first(tdo, args.bits)
    print(f"raw TDO bytes: {tdo.hex(' ')}")
    print(f"LSB-first bits: {bytes_to_bit_string(tdo, args.bits)}")
    print("32-bit LSB-first words:")
    for index, value in enumerate(words):
        marker = "candidate IDCODE" if candidate_idcode(value) else ""
        print(f"  [{index}] 0x{value:08X} {marker}".rstrip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="CDC serial port, for example COM8")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=2.0)

    subparsers = parser.add_subparsers(dest="command", required=True)

    info_parser = subparsers.add_parser("info")
    info_parser.set_defaults(func=cmd_info)

    capabilities_parser = subparsers.add_parser("capabilities")
    capabilities_parser.set_defaults(func=cmd_capabilities)

    profile_parser = subparsers.add_parser("profile")
    profile_parser.add_argument("action", choices=sorted(PROFILE_ACTIONS))
    profile_parser.set_defaults(func=cmd_profile)

    engine_parser = subparsers.add_parser("engine")
    engine_parser.add_argument("engine", choices=sorted(ENGINE_VALUES))
    engine_parser.set_defaults(func=cmd_engine)

    reset_parser = subparsers.add_parser("reset")
    reset_parser.set_defaults(func=cmd_reset)

    loop_parser = subparsers.add_parser("loopback")
    loop_parser.add_argument("--bits", type=int, default=256)
    loop_parser.add_argument("--seed", type=int, default=0xE1)
    loop_parser.set_defaults(func=cmd_loopback)

    boundary_parser = subparsers.add_parser("boundary-test")
    boundary_parser.add_argument("--seed", type=int, default=0xB0)
    boundary_parser.set_defaults(func=cmd_boundary_test)

    stress_parser = subparsers.add_parser("stress")
    stress_parser.add_argument("--bits", type=int, default=32768)
    stress_parser.add_argument("--count", type=int, default=1000)
    stress_parser.add_argument("--seed", type=int, default=0x5103)
    stress_parser.add_argument("--progress", type=int, default=100)
    stress_parser.set_defaults(func=cmd_stress)

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--bits", type=int, default=128)
    scan_parser.set_defaults(func=cmd_scan)

    clock_parser = subparsers.add_parser("clock")
    clock_parser.add_argument("--half-period-us", type=int, required=True)
    clock_parser.set_defaults(func=cmd_clock)

    clock_pio_parser = subparsers.add_parser("clock-pio")
    clock_pio_parser.add_argument("--khz", type=int, required=True)
    clock_pio_parser.set_defaults(func=cmd_clock_pio)

    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_parser.add_argument("--count", type=int, default=100)
    benchmark_parser.add_argument("--seed", type=int, default=0xBEE4)
    benchmark_parser.set_defaults(func=cmd_benchmark)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if hasattr(args, "bits") and (args.bits <= 0 or args.bits > MAX_SHIFT_BITS):
        parser.error(f"--bits must be 1..{MAX_SHIFT_BITS}")
    if hasattr(args, "count") and args.count <= 0:
        parser.error("--count must be greater than zero")
    if hasattr(args, "progress") and args.progress < 0:
        parser.error("--progress must be zero or greater")
    if hasattr(args, "khz") and (args.khz < 50 or args.khz > 5000):
        parser.error("--khz must be 50..5000")

    bridge = ExlinkJtagBridge(args.port, args.baudrate, args.timeout)
    try:
        args.func(bridge, args)
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
