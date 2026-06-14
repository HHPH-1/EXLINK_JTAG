#!/usr/bin/env python3
"""Xilinx Virtual Cable server for the Exlink RP2040 JTAG bridge."""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, field
import json
import socket
import struct
import sys
import threading
import time
import traceback
from typing import Callable

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
DEFAULT_MAX_PIO_TCK_HZ = 100_000_000
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


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


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
class XvcServerConfig:
    serial_port: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 2542
    tck_khz: int = 12500
    engine: str = "pio_safe"
    dma_chunk_bits: int = 8192
    max_shift_bits: int = 131072
    max_logical_shift_bits: int = DEFAULT_LOGICAL_SHIFT_LIMIT_BITS
    baudrate: int = 115200
    timeout: float = 2.0
    socket_timeout: float = 1.0
    profile: bool = False
    log_shifts: bool = False
    verbose_bits: bool = False


@dataclass
class XvcStatus:
    running: bool = False
    listening: bool = False
    client_connected: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 2542
    serial_port: str = ""
    shift_request_count: int = 0
    total_shifted_bits: int = 0
    serial_timeout_count: int = 0
    protocol_error_count: int = 0
    total_accepted_clients: int = 0
    last_error: str = ""


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
    requested_tck_hz: int = 0
    actual_tck_hz: int = 0
    engine: str = ""
    pio_cycles_per_bit: int = 0
    dma_chunk_bits: int = 0
    dma_timeouts: int = 0
    pio_recoveries: int = 0
    usb_disconnects: int = 0

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

    def to_summary_dict(self) -> dict[str, object]:
        return {
            "client": self.client_address,
            "session_index": self.session_index,
            "requested_tck_hz": self.requested_tck_hz,
            "actual_tck_hz": self.actual_tck_hz,
            "engine": self.engine,
            "pio_cycles_per_bit": self.pio_cycles_per_bit,
            "dma_chunk_bits": self.dma_chunk_bits,
            "shift_requests": self.shift_request_count,
            "total_shifted_bits": self.total_shifted_bits,
            "effective_rate_bit_s": self.effective_rate(),
            "serial_timeouts": self.serial_timeout_count,
            "protocol_errors": self.protocol_error_count,
            "dma_timeouts": self.dma_timeouts,
            "pio_recoveries": self.pio_recoveries,
            "usb_disconnects": self.usb_disconnects,
        }


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


def period_ns_to_supported_hz(period_ns: int, maximum_hz: int) -> int:
    requested_hz = period_ns_to_hz(period_ns)
    return min(maximum_hz, max(MIN_PIO_TCK_HZ, requested_hz))


def period_ns_to_hz(period_ns: int) -> int:
    if period_ns == 0:
        return DEFAULT_MAX_PIO_TCK_HZ
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
    print(f"  Requested TCK: {stats.requested_tck_hz} Hz")
    print(f"  Actual TCK: {stats.actual_tck_hz} Hz")
    print(f"  Engine: {stats.engine}")
    print(f"  PIO cycles per bit: {stats.pio_cycles_per_bit}")
    print(f"  DMA chunk: {stats.dma_chunk_bits} bits")
    print(f"  Shift requests: {stats.shift_request_count}")
    print(f"  Total shifted bits: {stats.total_shifted_bits}")
    print(f"  Minimum shift: {stats.minimum_shift_bits or 0}")
    print(f"  Maximum shift: {stats.maximum_shift_bits}")
    print(f"  Average shift: {stats.average_shift_bits():.0f}")
    print(f"  Duration: {elapsed:.1f} s")
    print(f"  Effective rate: {stats.effective_rate():.0f} bit/s")
    print(f"  Serial timeouts: {stats.serial_timeout_count}")
    print(f"  Protocol errors: {stats.protocol_error_count}")
    print(f"  DMA timeouts: {stats.dma_timeouts}")
    print(f"  PIO recoveries: {stats.pio_recoveries}")
    print(f"  USB disconnects: {stats.usb_disconnects}")
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
        requested_tck_hz=getattr(args, "requested_pio_tck_hz", 0),
        actual_tck_hz=getattr(args, "actual_pio_tck_hz", 0),
        engine=getattr(args, "active_engine_name", ""),
        pio_cycles_per_bit=getattr(args, "pio_cycles_per_bit", 0),
        dma_chunk_bits=getattr(args, "actual_dma_chunk_bits", 0),
    )
    recovery_required = False
    stop_event: threading.Event | None = getattr(args, "_stop_event", None)
    status: XvcStatus | None = getattr(args, "_status", None)
    if status is not None:
        status.client_connected = True
        status.total_accepted_clients = state.total_accepted_clients
    print(f"XVC client connected: {client_address}")

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            command = recv_command(sock)

            if command == b"getinfo:":
                stats.getinfo_count += 1
                sock.sendall(args.xvc_info)
                continue

            if command == b"settck:":
                stats.settck_count += 1
                requested_ns = struct.unpack("<I", recv_exact(sock, 4))[0]
                requested_hz = period_ns_to_hz(requested_ns)
                if args.force_tck_khz is not None:
                    supported_hz = args.force_tck_khz * 1000
                    firmware_actual_hz = args.actual_pio_tck_hz
                else:
                    supported_hz = period_ns_to_supported_hz(requested_ns, args.maximum_pio_tck_hz)
                    firmware_actual_hz = bridge.set_pio_clock_hz(supported_hz)
                    args.requested_pio_tck_hz = supported_hz
                    args.actual_pio_tck_hz = firmware_actual_hz
                    stats.requested_tck_hz = supported_hz
                    stats.actual_tck_hz = firmware_actual_hz
                actual_ns = hz_to_period_ns(firmware_actual_hz, requested_ns)
                actual_hz = period_ns_to_hz(actual_ns)
                print(
                    "settck: "
                    f"requested={requested_ns} ns ({requested_hz} Hz), "
                    f"firmware_requested={supported_hz} Hz, "
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
                if status is not None:
                    status.shift_request_count = stats.shift_request_count
                    status.total_shifted_bits = stats.total_shifted_bits
                continue

            raise ProtocolError(f"unhandled XVC command {command!r}")

    except ClientDisconnected as exc:
        print(str(exc))
    except (ProtocolError, socket.timeout) as exc:
        stats.protocol_error_count += 1
        state.protocol_error_count += 1
        recovery_required = True
        if status is not None:
            status.protocol_error_count = state.protocol_error_count
            status.last_error = str(exc)
        print(f"Protocol error: {exc}")
    except SerialTimeoutError as exc:
        stats.serial_timeout_count += 1
        state.serial_timeout_count += 1
        recovery_required = True
        if status is not None:
            status.serial_timeout_count = state.serial_timeout_count
            status.last_error = str(exc)
        print(f"Serial timeout: {exc}")
    except BridgeError as exc:
        stats.protocol_error_count += 1
        state.protocol_error_count += 1
        recovery_required = True
        if status is not None:
            status.protocol_error_count = state.protocol_error_count
            status.last_error = str(exc)
        print(f"Bridge error: {exc}")
    except OSError as exc:
        print(f"Client socket closed: {exc}")
    finally:
        if args.profile:
            try:
                _enabled, profile_text = bridge.profile("show")
                for line in profile_text.splitlines():
                    if line.strip().startswith("dma_timeouts="):
                        stats.dma_timeouts = int(line.split("=", 1)[1])
                    elif line.strip().startswith("pio_recoveries="):
                        stats.pio_recoveries = int(line.split("=", 1)[1])
            except BridgeError as exc:
                print(f"Profile read failed: {exc}")
        if recovery_required:
            recover_bridge(bridge)
        if not args.no_stats:
            print_session_summary(stats)
        if args.json_summary:
            summary_json = json.dumps(stats.to_summary_dict(), sort_keys=True)
            if args.json_summary == "-":
                print(summary_json)
            else:
                with open(args.json_summary, "a", encoding="utf-8") as f:
                    f.write(summary_json + "\n")
        if status is not None:
            status.client_connected = False
            status.shift_request_count = stats.shift_request_count
            status.total_shifted_bits = stats.total_shifted_bits
            status.serial_timeout_count = state.serial_timeout_count
            status.protocol_error_count = state.protocol_error_count
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

        requested_engine = "pio_fast" if args.engine == "pio_fast" else "pio"
        if ENGINE_NAMES.get(active) != requested_engine:
            bridge.select_engine(requested_engine)
            active, supported, firmware_max_shift_bits = bridge.capabilities()

        if args.dma_chunk_bits:
            args.actual_dma_chunk_bits = bridge.set_dma_chunk_bits(args.dma_chunk_bits)

        requested_hz = (args.force_tck_khz or args.default_tck_khz) * 1000
        actual_pio_tck_hz = bridge.set_pio_clock_hz(requested_hz)
        bridge.reset_tap()
        if args.profile:
            bridge.profile("clear")
            bridge.profile("on")
        info_values = bridge.info_dict()
        args.maximum_pio_tck_hz = int(info_values.get("Maximum theoretical TCK", DEFAULT_MAX_PIO_TCK_HZ))
        args.pio_cycles_per_bit = int(info_values.get("PIO cycles per bit", 0))
        args.actual_dma_chunk_bits = int(info_values.get("DMA chunk bits", getattr(args, "actual_dma_chunk_bits", 0)))
        args.active_engine_name = info_values.get("Active engine", requested_engine)
        args.requested_pio_tck_hz = requested_hz
        args.actual_pio_tck_hz = actual_pio_tck_hz
        dma_enabled = bool(supported & JTAG_ENGINE_FLAG_DMA)
        return bridge, firmware_info, firmware_max_shift_bits, dma_enabled, actual_pio_tck_hz
    except Exception:
        bridge.close()
        raise


def serve(args: argparse.Namespace, stop_event: threading.Event | None = None) -> None:
    bridge: ExlinkJtagBridge | None = None
    server: socket.socket | None = None
    state = ServerState()
    if stop_event is None:
        stop_event = getattr(args, "_stop_event", None)
    status: XvcStatus | None = getattr(args, "_status", None)

    try:
        bridge, firmware_info, firmware_max_shift_bits, dma_enabled, actual_pio_tck_hz = initialize_bridge(args)
        args._bridge = bridge
        args.firmware_max_shift_bits = firmware_max_shift_bits
        args.xvc_info = f"xvcServer_v1.0:{firmware_max_shift_bits}\n".encode("ascii")

        print("Exlink XVC Server")
        print(f"Firmware: {firmware_info}")
        print(f"Serial port: {args.port}")
        print(f"Engine: {args.active_engine_name}")
        print(f"DMA: {'enabled' if dma_enabled else 'disabled'}")
        print(f"DMA chunk: {args.actual_dma_chunk_bits} bits")
        print(f"Maximum shift: {firmware_max_shift_bits} bits")
        print(f"Maximum theoretical TCK: {args.maximum_pio_tck_hz / 1000:.0f} kHz")
        if args.force_tck_khz is not None:
            print(f"Force TCK: {args.force_tck_khz} kHz")
        print(f"PIO TCK: {actual_pio_tck_hz / 1000:.0f} kHz")

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        args._server_socket = server
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.xvc_host, args.xvc_port))
        server.listen(1)
        server.settimeout(1.0)
        if status is not None:
            status.running = True
            status.listening = True
            status.listen_host = args.xvc_host
            status.listen_port = args.xvc_port
            status.serial_port = args.port
        print(f"Listening on {args.xvc_host}:{args.xvc_port}")
        print("Waiting for XVC client...")

        while stop_event is None or not stop_event.is_set():
            try:
                client, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop_event is not None and stop_event.is_set():
                    break
                raise

            state.total_accepted_clients += 1
            if status is not None:
                status.total_accepted_clients = state.total_accepted_clients

            client.settimeout(args.socket_timeout)
            with client:
                args._client_socket = client
                try:
                    handle_client(client, bridge, args, state)
                finally:
                    args._client_socket = None

    except KeyboardInterrupt:
        print("Shutting down on Ctrl+C")
    finally:
        if status is not None:
            status.listening = False
            status.client_connected = False
            status.serial_timeout_count = state.serial_timeout_count
            status.protocol_error_count = state.protocol_error_count
        if server is not None:
            with contextlib.suppress(OSError):
                server.close()
        args._server_socket = None
        if bridge is not None:
            with contextlib.suppress(Exception):
                bridge.close()
        args._bridge = None
        if status is not None:
            status.running = False


def namespace_from_config(config: XvcServerConfig, stop_event: threading.Event | None = None,
                          status: XvcStatus | None = None) -> argparse.Namespace:
    logical_shift_limit = max(
        config.max_logical_shift_bits,
        config.max_shift_bits,
        DEFAULT_LOGICAL_SHIFT_LIMIT_BITS,
    )
    return argparse.Namespace(
        port=config.serial_port,
        baudrate=config.baudrate,
        timeout=config.timeout,
        xvc_host=config.listen_host,
        xvc_port=config.listen_port,
        socket_timeout=config.socket_timeout,
        default_tck_khz=config.tck_khz,
        force_tck_khz=config.tck_khz,
        engine=config.engine,
        dma_chunk_bits=config.dma_chunk_bits,
        profile=config.profile,
        json_summary=None,
        log_file=None,
        max_logical_shift_bits=logical_shift_limit,
        log_shifts=config.log_shifts,
        verbose_bits=config.verbose_bits,
        no_stats=False,
        _stop_event=stop_event,
        _status=status,
        _server_socket=None,
        _client_socket=None,
        _bridge=None,
    )


class ExlinkXvcServer:
    def __init__(
        self,
        config: XvcServerConfig,
        log_callback: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.log_callback = log_callback
        self._stop_event = threading.Event()
        self._status = XvcStatus(
            listen_host=config.listen_host,
            listen_port=config.listen_port,
            serial_port=config.serial_port,
        )
        self._args = namespace_from_config(config, self._stop_event, self._status)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("XVC server is already running")
            self._stop_event.clear()
            self._status = XvcStatus(
                running=True,
                listen_host=self.config.listen_host,
                listen_port=self.config.listen_port,
                serial_port=self.config.serial_port,
            )
            self._args = namespace_from_config(self.config, self._stop_event, self._status)
            self._thread = threading.Thread(target=self._run, name="ExlinkXvcServer", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            if self.log_callback is not None:
                self.log_callback("XVC server thread starting")
            serve(self._args, self._stop_event)
            if self.log_callback is not None:
                self.log_callback("XVC server thread stopped")
        except Exception as exc:  # pragma: no cover - defensive path for GUI packaging
            self._status.last_error = str(exc)
            self._status.running = False
            if self.log_callback is not None:
                self.log_callback(f"XVC server failed: {exc}")
                self.log_callback(traceback.format_exc())
            else:
                raise
        finally:
            self._status.running = False
            self._status.listening = False
            self._status.client_connected = False

    def stop(self) -> None:
        self._stop_event.set()
        for attr in ("_client_socket", "_server_socket"):
            sock = getattr(self._args, attr, None)
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    sock.close()
        bridge = getattr(self._args, "_bridge", None)
        if bridge is not None:
            with contextlib.suppress(Exception):
                bridge.close()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def get_status(self) -> XvcStatus:
        return XvcStatus(**self._status.__dict__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="CDC serial port, for example COM8")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--xvc-host", "--host", dest="xvc_host", default="127.0.0.1")
    parser.add_argument("--xvc-port", "--tcp-port", dest="xvc_port", type=int, default=2542)
    parser.add_argument("--socket-timeout", type=float, default=10.0)
    parser.add_argument("--default-tck-khz", type=int, default=12500)
    parser.add_argument("--force-tck-khz", type=int, help="hold firmware TCK at this value despite Vivado settck requests")
    parser.add_argument("--engine", choices=["pio", "pio_safe", "pio_fast"], default="pio_safe")
    parser.add_argument("--dma-chunk-bits", type=int, choices=[2048, 4096, 8192, 16384, 32768], default=8192)
    parser.add_argument("--profile", action="store_true", help="enable firmware profile for this XVC session")
    parser.add_argument("--json-summary", nargs="?", const="-", help="write a JSON session summary to this file or stdout")
    parser.add_argument("--log-file", help="tee server stdout/stderr to this file")
    parser.add_argument("--max-logical-shift-bits", type=int, default=DEFAULT_LOGICAL_SHIFT_LIMIT_BITS)
    parser.add_argument("--log-shifts", action="store_true", help="log each XVC shift without payload bytes")
    parser.add_argument("--verbose-bits", action="store_true", help="log each XVC shift with TMS/TDI/TDO hex")
    parser.add_argument("--no-stats", action="store_true", help="disable per-session summary output")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.default_tck_khz < (MIN_PIO_TCK_HZ // 1000):
        parser.error(f"--default-tck-khz must be at least {MIN_PIO_TCK_HZ // 1000}")
    if args.force_tck_khz is not None and args.force_tck_khz < (MIN_PIO_TCK_HZ // 1000):
        parser.error(f"--force-tck-khz must be at least {MIN_PIO_TCK_HZ // 1000}")
    if args.max_logical_shift_bits <= 0:
        parser.error("--max-logical-shift-bits must be greater than zero")

    log_handle = None
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    try:
        if args.log_file:
            log_handle = open(args.log_file, "a", encoding="utf-8")
            sys.stdout = Tee(original_stdout, log_handle)
            sys.stderr = Tee(original_stderr, log_handle)
        serve(args)
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        if log_handle is not None:
            log_handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
