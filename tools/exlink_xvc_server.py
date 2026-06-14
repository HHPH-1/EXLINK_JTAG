#!/usr/bin/env python3
"""Xilinx Virtual Cable server for the Exlink RP2040 JTAG bridge."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import socket
import struct
import sys
import time

from exlink_jtag_test import (
    BridgeError,
    ENGINE_NAMES,
    ExlinkJtagBridge,
    JTAG_ENGINE_FLAG_DMA,
    JTAG_ENGINE_FLAG_PIO,
    SerialTimeoutError,
    mask_unused_high_bits,
)


MIN_PIO_TCK_HZ = 50_000
MAX_PIO_TCK_HZ = 5_000_000
DEFAULT_LOGICAL_SHIFT_LIMIT_BITS = 16 * 1024 * 1024
XVC_COMMANDS = (b"getinfo:", b"settck:", b"shift:")
PERF_FIELDS = (
    "tcp_receive_ns",
    "pc_prepare_ns",
    "serial_write_ns",
    "serial_first_byte_wait_ns",
    "serial_response_read_ns",
    "tdo_assemble_ns",
    "tcp_send_ns",
    "shift_total_ns",
)
HISTOGRAM_BUCKETS = (
    ("1-32", 1, 32),
    ("33-128", 33, 128),
    ("129-512", 129, 512),
    ("513-2048", 513, 2048),
    ("2049-4096", 2049, 4096),
    ("4097-8192", 4097, 8192),
    ("8193-32768", 8193, 32768),
    ("32769+", 32769, None),
)


class ClientDisconnected(ConnectionError):
    def __init__(self, message: str, received: int = 0, expected: int = 0, normal: bool = False):
        super().__init__(message)
        self.received = received
        self.expected = expected
        self.normal = normal


class ProtocolError(RuntimeError):
    pass


@dataclass
class TimingStat:
    count: int = 0
    total_ns: int = 0
    min_ns: int | None = None
    max_ns: int = 0

    def add(self, value_ns: int) -> None:
        self.count += 1
        self.total_ns += value_ns
        self.max_ns = max(self.max_ns, value_ns)
        if self.min_ns is None:
            self.min_ns = value_ns
        else:
            self.min_ns = min(self.min_ns, value_ns)

    def average_ns(self) -> int:
        if self.count == 0:
            return 0
        return self.total_ns // self.count


@dataclass
class HistogramBucket:
    name: str
    min_bits: int
    max_bits: int | None
    requests: int = 0
    bits: int = 0
    processing_ns: int = 0

    def contains(self, bit_count: int) -> bool:
        if bit_count < self.min_bits:
            return False
        return self.max_bits is None or bit_count <= self.max_bits

    def record(self, bit_count: int, processing_ns: int) -> None:
        self.requests += 1
        self.bits += bit_count
        self.processing_ns += processing_ns


@dataclass
class ServerState:
    total_accepted_clients: int = 0
    serial_timeout_count: int = 0
    protocol_error_count: int = 0


@dataclass
class SessionStats:
    client_address: str
    session_index: int
    total_accepted_clients: int
    reconnect_count: int
    start_time: float = field(default_factory=time.perf_counter)
    getinfo_count: int = 0
    settck_count: int = 0
    shift_request_count: int = 0
    total_shifted_bits: int = 0
    minimum_shift_bits: int | None = None
    maximum_shift_bits: int = 0
    serial_timeout_count: int = 0
    protocol_error_count: int = 0
    timings: dict[str, TimingStat] = field(
        default_factory=lambda: {name: TimingStat() for name in PERF_FIELDS}
    )
    histogram: list[HistogramBucket] = field(
        default_factory=lambda: [HistogramBucket(*bucket) for bucket in HISTOGRAM_BUCKETS]
    )
    serial_shift_subrequest_count: int = 0
    maximum_subrequests_per_shift: int = 0
    unaligned_subrequest_count: int = 0

    def record_shift(self, bit_count: int) -> None:
        self.shift_request_count += 1
        self.total_shifted_bits += bit_count
        self.maximum_shift_bits = max(self.maximum_shift_bits, bit_count)
        if self.minimum_shift_bits is None:
            self.minimum_shift_bits = bit_count
        else:
            self.minimum_shift_bits = min(self.minimum_shift_bits, bit_count)

    def record_shift_perf(self, bit_count: int, timings: dict[str, int], chunks: int, unaligned_chunks: int) -> None:
        self.record_shift(bit_count)
        for name in PERF_FIELDS:
            self.timings[name].add(timings.get(name, 0))
        for bucket in self.histogram:
            if bucket.contains(bit_count):
                bucket.record(bit_count, timings.get("shift_total_ns", 0))
                break
        self.serial_shift_subrequest_count += chunks
        self.maximum_subrequests_per_shift = max(self.maximum_subrequests_per_shift, chunks)
        self.unaligned_subrequest_count += unaligned_chunks

    def duration(self) -> float:
        return max(0.0, time.perf_counter() - self.start_time)

    def average_shift_bits(self) -> float:
        if self.shift_request_count == 0:
            return 0.0
        return self.total_shifted_bits / self.shift_request_count

    def effective_rate(self) -> float:
        elapsed = self.duration()
        if elapsed <= 0.0:
            return 0.0
        return self.total_shifted_bits / elapsed


def recv_exact(sock: socket.socket, length: int) -> bytes:
    if length < 0:
        raise ValueError("length must be non-negative")

    chunks = bytearray()
    while len(chunks) < length:
        try:
            chunk = sock.recv(length - len(chunks))
        except socket.timeout as exc:
            raise ProtocolError(f"TCP timeout: expected {length} bytes, got {len(chunks)}") from exc
        except OSError as exc:
            raise ClientDisconnected(f"TCP receive failed: {exc}") from exc
        if not chunk:
            raise ClientDisconnected(
                f"Client disconnected during incomplete command: received {len(chunks)} of {length} bytes",
                received=len(chunks),
                expected=length,
            )
        chunks.extend(chunk)
    return bytes(chunks)


def format_duration_ns(value_ns: int) -> str:
    if value_ns >= 1_000_000_000:
        return f"{value_ns / 1_000_000_000:.6f} s"
    if value_ns >= 1_000_000:
        return f"{value_ns / 1_000_000:.3f} ms"
    if value_ns >= 1_000:
        return f"{value_ns / 1_000:.3f} us"
    return f"{value_ns} ns"


def recv_command(sock: socket.socket) -> bytes:
    command = bytearray()

    def read_command_bytes(length: int, total_expected: int) -> bytes:
        try:
            data = recv_exact(sock, length)
        except ClientDisconnected as exc:
            total_received = len(command) + exc.received
            if total_received == 0:
                raise ClientDisconnected("Client disconnected normally", normal=True) from exc
            raise ClientDisconnected(
                f"Client disconnected during incomplete command: received {total_received} of {total_expected} bytes",
                received=total_received,
                expected=total_expected,
            ) from exc
        command.extend(data)
        return data

    first = read_command_bytes(1, 1)
    if first == b"g":
        read_command_bytes(len(b"getinfo:") - 1, len(b"getinfo:"))
    elif first == b"s":
        second = read_command_bytes(1, 2)
        if second == b"h":
            read_command_bytes(len(b"shift:") - 2, len(b"shift:"))
        elif second == b"e":
            read_command_bytes(len(b"settck:") - 2, len(b"settck:"))
        else:
            raise ProtocolError(f"unknown XVC command prefix {(first + second)!r}")
    else:
        raise ProtocolError(f"unknown XVC command prefix {first!r}")

    command = bytes(command)
    if command not in XVC_COMMANDS:
        raise ProtocolError(f"unknown XVC command {command!r}")
    return command


def copy_bits(src: bytes, src_offset: int, bit_count: int) -> bytes:
    byte_count = (bit_count + 7) // 8
    if bit_count == 0:
        return b""
    if (src_offset & 7) == 0:
        start = src_offset >> 3
        return mask_unused_high_bits(src[start:start + byte_count], bit_count)

    value = int.from_bytes(src, "little") >> src_offset
    mask = (1 << bit_count) - 1
    return (value & mask).to_bytes(byte_count, "little")


def paste_bits(dst: bytearray, dst_offset: int, src: bytes, bit_count: int) -> None:
    if bit_count == 0:
        return

    byte_count = (bit_count + 7) // 8
    src = mask_unused_high_bits(src[:byte_count], bit_count)
    if (dst_offset & 7) == 0:
        start = dst_offset >> 3
        dst[start:start + byte_count] = src
        return

    value = int.from_bytes(src, "little") << dst_offset
    current = int.from_bytes(dst, "little")
    mask = ((1 << bit_count) - 1) << dst_offset
    merged = (current & ~mask) | (value & mask)
    dst[:] = merged.to_bytes(len(dst), "little")


def period_ns_to_supported_hz(period_ns: int) -> int:
    requested_hz = period_ns_to_hz(period_ns)
    return min(MAX_PIO_TCK_HZ, max(MIN_PIO_TCK_HZ, requested_hz))


def period_ns_to_hz(period_ns: int) -> int:
    if period_ns == 0:
        return MAX_PIO_TCK_HZ
    return 1_000_000_000 // period_ns


def hz_to_period_ns(hz: int, fallback_ns: int) -> int:
    if hz <= 0:
        return fallback_ns
    return max(1, int(round(1_000_000_000 / hz)))


def print_session_summary(stats: SessionStats) -> None:
    elapsed = stats.duration()
    errors = stats.serial_timeout_count + stats.protocol_error_count
    print("Session summary:")
    print(f"  Client: {stats.client_address}")
    print(f"  Session index: {stats.session_index}")
    print(f"  Total accepted clients: {stats.total_accepted_clients}")
    print(f"  getinfo: {stats.getinfo_count}")
    print(f"  settck: {stats.settck_count}")
    print(f"  Shift requests: {stats.shift_request_count}")
    print(f"  Total bits: {stats.total_shifted_bits}")
    print(f"  Minimum shift: {stats.minimum_shift_bits or 0}")
    print(f"  Maximum shift: {stats.maximum_shift_bits}")
    print(f"  Average shift: {stats.average_shift_bits():.0f}")
    print(f"  Duration: {elapsed:.1f} s")
    print(f"  Effective rate: {stats.effective_rate():.0f} bit/s")
    print(f"  Serial timeouts: {stats.serial_timeout_count}")
    print(f"  Protocol errors: {stats.protocol_error_count}")
    print(f"  Client reconnects: {stats.reconnect_count}")
    print(f"  Errors: {errors}")
    print_performance_summary(stats)


def print_performance_summary(stats: SessionStats) -> None:
    if stats.shift_request_count == 0:
        return

    total = stats.timings["shift_total_ns"].total_ns
    print("Performance summary:")
    print("  Timing definition: TCP receive covers XVC shift length/TMS/TDI only;")
    print("  serial response read starts after the first response byte is observed.")
    print("  Stage percentages use shift_total as the denominator and may not sum to 100%.")
    print(f"  Shift requests: {stats.shift_request_count}")
    print(f"  Total shifted bits: {stats.total_shifted_bits}")
    print(f"  Total shift processing time: {format_duration_ns(total)}")

    labels = {
        "tcp_receive_ns": "TCP receive",
        "pc_prepare_ns": "PC prepare",
        "serial_write_ns": "Serial write",
        "serial_first_byte_wait_ns": "Serial first-byte wait",
        "serial_response_read_ns": "Serial response read",
        "tdo_assemble_ns": "TDO assemble",
        "tcp_send_ns": "TCP send",
        "shift_total_ns": "Shift total",
    }

    for name in PERF_FIELDS:
        stat = stats.timings[name]
        percentage = (stat.total_ns * 100.0 / total) if total else 0.0
        print(f"  {labels[name]}:")
        print(f"    total: {format_duration_ns(stat.total_ns)}")
        print(f"    average: {format_duration_ns(stat.average_ns())}")
        print(f"    minimum: {format_duration_ns(stat.min_ns or 0)}")
        print(f"    maximum: {format_duration_ns(stat.max_ns)}")
        print(f"    percentage: {percentage:.1f}%")

    print("Shift size histogram:")
    for bucket in stats.histogram:
        request_ratio = (bucket.requests * 100.0 / stats.shift_request_count) if stats.shift_request_count else 0.0
        bit_ratio = (bucket.bits * 100.0 / stats.total_shifted_bits) if stats.total_shifted_bits else 0.0
        average_bits = (bucket.bits / bucket.requests) if bucket.requests else 0.0
        effective_rate = (bucket.bits * 1_000_000_000 / bucket.processing_ns) if bucket.processing_ns else 0.0
        print(f"  {bucket.name}:")
        print(f"    requests={bucket.requests}")
        print(f"    request_ratio={request_ratio:.1f}%")
        print(f"    bits={bucket.bits}")
        print(f"    bit_ratio={bit_ratio:.1f}%")
        print(f"    average_bits={average_bits:.0f}")
        print(f"    processing_time={format_duration_ns(bucket.processing_ns)}")
        print(f"    effective_rate={effective_rate:.0f} bit/s")

    average_subrequests = (
        stats.serial_shift_subrequest_count / stats.shift_request_count
        if stats.shift_request_count else 0.0
    )
    print("Serial subrequest summary:")
    print(f"  XVC logical shifts: {stats.shift_request_count}")
    print(f"  Serial shift subrequests: {stats.serial_shift_subrequest_count}")
    print(f"  Average subrequests per shift: {average_subrequests:.2f}")
    print(f"  Maximum subrequests per shift: {stats.maximum_subrequests_per_shift}")
    print(f"  Unaligned subrequests: {stats.unaligned_subrequest_count}")


def recover_bridge(bridge: ExlinkJtagBridge) -> None:
    try:
        bridge.recover_link()
        bridge.capabilities()
        print("Firmware communication recovered")
    except BridgeError as exc:
        print(f"Firmware recovery check failed: {exc}")


def serial_shift_profiled(
    bridge: ExlinkJtagBridge,
    bit_count: int,
    tms: bytes,
    tdi: bytes,
) -> tuple[bytes, dict[str, int]]:
    byte_count = (bit_count + 7) // 8
    frame = b"S" + struct.pack("<I", bit_count) + tms + tdi

    t0 = time.perf_counter_ns()
    bridge.write_all(frame)
    t1 = time.perf_counter_ns()

    tag = bridge.read_exact(1)
    t2 = time.perf_counter_ns()
    if tag == b"e":
        code = bridge.read_exact(1)[0]
        raise BridgeError(f"bridge returned error {code}")
    if tag != b"s":
        raise BridgeError(f"unexpected response tag {tag!r}, expected b's'")

    status = bridge.read_exact(1)[0]
    returned_bits = struct.unpack("<I", bridge.read_exact(4))[0]
    if returned_bits != bit_count:
        raise BridgeError(f"bridge returned bit_count {returned_bits}, expected {bit_count}")
    if status != 0:
        raise BridgeError(f"shift failed with status {status}")
    tdo = bridge.read_exact(byte_count)
    t3 = time.perf_counter_ns()

    return tdo, {
        "serial_write_ns": t1 - t0,
        "serial_first_byte_wait_ns": t2 - t1,
        "serial_response_read_ns": t3 - t2,
    }


def shift_via_bridge_profiled(
    bridge: ExlinkJtagBridge,
    bit_count: int,
    tms: bytes,
    tdi: bytes,
    firmware_max_shift_bits: int,
) -> tuple[bytes, int, int, dict[str, int]]:
    byte_count = (bit_count + 7) // 8
    tdo = bytearray(byte_count)
    offset = 0
    chunks = 0
    unaligned_chunks = 0
    timings = {
        "pc_prepare_ns": 0,
        "serial_write_ns": 0,
        "serial_first_byte_wait_ns": 0,
        "serial_response_read_ns": 0,
        "tdo_assemble_ns": 0,
    }

    while offset < bit_count:
        chunk_bits = min(firmware_max_shift_bits, bit_count - offset)
        prepare_start = time.perf_counter_ns()
        chunk_tms = copy_bits(tms, offset, chunk_bits)
        chunk_tdi = copy_bits(tdi, offset, chunk_bits)
        prepare_end = time.perf_counter_ns()
        chunk_tdo, serial_timings = serial_shift_profiled(bridge, chunk_bits, chunk_tms, chunk_tdi)
        assemble_start = time.perf_counter_ns()
        paste_bits(tdo, offset, chunk_tdo, chunk_bits)
        assemble_end = time.perf_counter_ns()

        timings["pc_prepare_ns"] += prepare_end - prepare_start
        timings["serial_write_ns"] += serial_timings["serial_write_ns"]
        timings["serial_first_byte_wait_ns"] += serial_timings["serial_first_byte_wait_ns"]
        timings["serial_response_read_ns"] += serial_timings["serial_response_read_ns"]
        timings["tdo_assemble_ns"] += assemble_end - assemble_start
        if chunks > 0 and ((offset & 7) != 0 or (chunk_bits & 7) != 0):
            unaligned_chunks += 1
        offset += chunk_bits
        chunks += 1

    assemble_start = time.perf_counter_ns()
    masked = mask_unused_high_bits(tdo, bit_count)
    assemble_end = time.perf_counter_ns()
    timings["tdo_assemble_ns"] += assemble_end - assemble_start
    return masked, chunks, unaligned_chunks, timings


def handle_client(
    sock: socket.socket,
    bridge: ExlinkJtagBridge,
    args: argparse.Namespace,
    state: ServerState,
) -> None:
    peer = sock.getpeername()
    client_address = f"{peer[0]}:{peer[1]}"
    stats = SessionStats(
        client_address=client_address,
        session_index=state.total_accepted_clients,
        total_accepted_clients=state.total_accepted_clients,
        reconnect_count=max(state.total_accepted_clients - 1, 0),
    )
    recovery_required = False
    print(f"XVC client connected: {client_address}")

    try:
        while True:
            command = recv_command(sock)

            if command == b"getinfo:":
                stats.getinfo_count += 1
                sock.sendall(args.xvc_info)
                continue

            if command == b"settck:":
                stats.settck_count += 1
                requested_ns = struct.unpack("<I", recv_exact(sock, 4))[0]
                requested_hz = period_ns_to_hz(requested_ns)
                supported_hz = period_ns_to_supported_hz(requested_ns)
                firmware_actual_hz = bridge.set_pio_clock_hz(supported_hz)
                actual_ns = hz_to_period_ns(firmware_actual_hz, requested_ns)
                actual_hz = period_ns_to_hz(actual_ns)
                print(
                    "settck: "
                    f"requested={requested_ns} ns ({requested_hz} Hz), "
                    f"actual={actual_ns} ns ({actual_hz} Hz)"
                )
                sock.sendall(struct.pack("<I", actual_ns))
                continue

            if command == b"shift:":
                shift_start = time.perf_counter_ns()
                tcp_receive_start = shift_start
                bit_count = struct.unpack("<I", recv_exact(sock, 4))[0]
                if bit_count <= 0:
                    raise ProtocolError(f"invalid shift bit_count {bit_count}")
                if bit_count > args.max_logical_shift_bits:
                    raise ProtocolError(
                        f"shift bit_count {bit_count} exceeds logical limit "
                        f"{args.max_logical_shift_bits}"
                    )

                byte_count = (bit_count + 7) // 8
                tms = recv_exact(sock, byte_count)
                tdi = recv_exact(sock, byte_count)
                tcp_receive_end = time.perf_counter_ns()
                tdo, chunks, unaligned_chunks, timings = shift_via_bridge_profiled(
                    bridge, bit_count, tms, tdi, args.firmware_max_shift_bits
                )

                if args.verbose_bits:
                    print(
                        f"shift bits={bit_count} chunks={chunks} "
                        f"tms={tms.hex()} tdi={tdi.hex()} tdo={tdo.hex()}"
                    )
                elif args.log_shifts:
                    print(f"shift bits={bit_count} chunks={chunks}")
                tcp_send_start = time.perf_counter_ns()
                sock.sendall(tdo)
                tcp_send_end = time.perf_counter_ns()
                timings["tcp_receive_ns"] = tcp_receive_end - tcp_receive_start
                timings["tcp_send_ns"] = tcp_send_end - tcp_send_start
                timings["shift_total_ns"] = tcp_send_end - shift_start
                stats.record_shift_perf(bit_count, timings, chunks, unaligned_chunks)
                continue

            raise ProtocolError(f"unhandled XVC command {command!r}")

    except ClientDisconnected as exc:
        print(str(exc))
    except (ProtocolError, socket.timeout) as exc:
        stats.protocol_error_count += 1
        state.protocol_error_count += 1
        recovery_required = True
        print(f"Protocol error: {exc}")
    except SerialTimeoutError as exc:
        stats.serial_timeout_count += 1
        state.serial_timeout_count += 1
        recovery_required = True
        print(f"Serial timeout: {exc}")
    except BridgeError as exc:
        stats.protocol_error_count += 1
        state.protocol_error_count += 1
        recovery_required = True
        print(f"Bridge error: {exc}")
    except OSError as exc:
        print(f"Client socket closed: {exc}")
    finally:
        if recovery_required:
            recover_bridge(bridge)
        if not args.no_stats:
            print_session_summary(stats)
        print("Waiting for XVC client...")


def initialize_bridge(args: argparse.Namespace) -> tuple[ExlinkJtagBridge, str, int, bool, int]:
    bridge = ExlinkJtagBridge(args.port, args.baudrate, args.timeout)
    try:
        firmware_info = bridge.info()
        active, supported, firmware_max_shift_bits = bridge.capabilities()

        if firmware_max_shift_bits <= 0:
            raise BridgeError(f"firmware reported invalid max shift {firmware_max_shift_bits}")
        if not (supported & JTAG_ENGINE_FLAG_PIO):
            raise BridgeError("firmware does not report PIO engine support")

        if ENGINE_NAMES.get(active) != "pio":
            bridge.select_engine("pio")
            active, supported, firmware_max_shift_bits = bridge.capabilities()

        requested_hz = args.default_tck_khz * 1000
        actual_pio_tck_hz = bridge.set_pio_clock_hz(requested_hz)
        dma_enabled = bool(supported & JTAG_ENGINE_FLAG_DMA)
        return bridge, firmware_info, firmware_max_shift_bits, dma_enabled, actual_pio_tck_hz
    except Exception:
        bridge.close()
        raise


def serve(args: argparse.Namespace) -> None:
    bridge: ExlinkJtagBridge | None = None
    server: socket.socket | None = None
    state = ServerState()

    try:
        bridge, firmware_info, firmware_max_shift_bits, dma_enabled, actual_pio_tck_hz = initialize_bridge(args)
        args.firmware_max_shift_bits = firmware_max_shift_bits
        args.xvc_info = f"xvcServer_v1.0:{firmware_max_shift_bits}\n".encode("ascii")

        print("Exlink XVC Server")
        print(f"Firmware: {firmware_info}")
        print(f"Serial port: {args.port}")
        print("Engine: pio")
        print(f"DMA: {'enabled' if dma_enabled else 'disabled'}")
        print(f"Maximum shift: {firmware_max_shift_bits} bits")
        print(f"PIO TCK: {actual_pio_tck_hz / 1000:.0f} kHz")

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.xvc_host, args.xvc_port))
        server.listen(1)
        server.settimeout(1.0)
        print(f"Listening on {args.xvc_host}:{args.xvc_port}")
        print("Waiting for XVC client...")

        while True:
            try:
                client, _addr = server.accept()
            except socket.timeout:
                continue

            state.total_accepted_clients += 1

            client.settimeout(args.socket_timeout)
            with client:
                handle_client(client, bridge, args, state)

    except KeyboardInterrupt:
        print("Shutting down on Ctrl+C")
    finally:
        if server is not None:
            server.close()
        if bridge is not None:
            bridge.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="CDC serial port, for example COM8")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--xvc-host", "--host", dest="xvc_host", default="127.0.0.1")
    parser.add_argument("--xvc-port", "--tcp-port", dest="xvc_port", type=int, default=2542)
    parser.add_argument("--socket-timeout", type=float, default=10.0)
    parser.add_argument("--default-tck-khz", type=int, default=1000)
    parser.add_argument("--max-logical-shift-bits", type=int, default=DEFAULT_LOGICAL_SHIFT_LIMIT_BITS)
    parser.add_argument("--log-shifts", action="store_true", help="log each XVC shift without payload bytes")
    parser.add_argument("--verbose-bits", action="store_true", help="log each XVC shift with TMS/TDI/TDO hex")
    parser.add_argument("--no-stats", action="store_true", help="disable per-session summary output")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.default_tck_khz < (MIN_PIO_TCK_HZ // 1000) or args.default_tck_khz > (MAX_PIO_TCK_HZ // 1000):
        parser.error(f"--default-tck-khz must be {MIN_PIO_TCK_HZ // 1000}..{MAX_PIO_TCK_HZ // 1000}")
    if args.max_logical_shift_bits <= 0:
        parser.error("--max-logical-shift-bits must be greater than zero")

    serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
