#!/usr/bin/env python3
"""Xilinx Virtual Cable server for the Exlink RP2040 JTAG bridge."""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

from exlink_jtag_test import BridgeError, ExlinkJtagBridge, MAX_SHIFT_BITS, mask_unused_high_bits


def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise ConnectionError("TCP client disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_command(sock: socket.socket) -> bytes:
    first = recv_exact(sock, 1)
    if first == b"g":
        command = first + recv_exact(sock, len(b"getinfo:") - 1)
    elif first == b"s":
        second = recv_exact(sock, 1)
        if second == b"h":
            command = first + second + recv_exact(sock, len(b"shift:") - 2)
        elif second == b"e":
            command = first + second + recv_exact(sock, len(b"settck:") - 2)
        else:
            raise ConnectionError(f"unknown XVC command prefix {(first + second)!r}")
    else:
        raise ConnectionError(f"unknown XVC command prefix {first!r}")

    if command not in (b"getinfo:", b"settck:", b"shift:"):
        raise ConnectionError(f"unknown XVC command {command!r}")
    return command


def copy_bits(src: bytes, src_offset: int, bit_count: int) -> bytes:
    byte_count = (bit_count + 7) // 8
    if (src_offset & 7) == 0:
        start = src_offset >> 3
        return mask_unused_high_bits(src[start:start + byte_count], bit_count)

    value = int.from_bytes(src, "little") >> src_offset
    mask = (1 << bit_count) - 1
    return (value & mask).to_bytes(byte_count, "little")


def paste_bits(dst: bytearray, dst_offset: int, src: bytes, bit_count: int) -> None:
    byte_count = (bit_count + 7) // 8
    src = mask_unused_high_bits(src, bit_count)
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
    if period_ns == 0:
        return 5_000_000
    requested_hz = int(round(1_000_000_000 / period_ns))
    return min(5_000_000, max(50_000, requested_hz))


def handle_client(sock: socket.socket, bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    peer = sock.getpeername()
    print(f"Vivado client connected: {peer}")

    while True:
        command = recv_command(sock)

        if command == b"getinfo:":
            sock.sendall(args.xvc_info)
        elif command == b"settck:":
            requested_ns = struct.unpack("<I", recv_exact(sock, 4))[0]
            requested_hz = period_ns_to_supported_hz(requested_ns)
            actual_hz = bridge.set_pio_clock_hz(requested_hz)
            actual_ns = int(round(1_000_000_000 / actual_hz)) if actual_hz else requested_ns
            print(f"settck requested={requested_ns} ns actual={actual_ns} ns")
            sock.sendall(struct.pack("<I", actual_ns))
        elif command == b"shift:":
            bit_count = struct.unpack("<I", recv_exact(sock, 4))[0]
            byte_count = (bit_count + 7) // 8
            tms = recv_exact(sock, byte_count)
            tdi = recv_exact(sock, byte_count)
            tdo = bytearray(byte_count)

            offset = 0
            chunks = 0
            while offset < bit_count:
                chunk_bits = min(args.max_shift_bits, bit_count - offset)
                chunk_tms = copy_bits(tms, offset, chunk_bits)
                chunk_tdi = copy_bits(tdi, offset, chunk_bits)
                chunk_tdo = bridge.shift(chunk_bits, chunk_tms, chunk_tdi)
                paste_bits(tdo, offset, chunk_tdo, chunk_bits)
                offset += chunk_bits
                chunks += 1

            if args.verbose_bits:
                print(f"shift bits={bit_count} chunks={chunks} tms={tms.hex()} tdi={tdi.hex()} tdo={bytes(tdo).hex()}")
            else:
                print(f"shift bits={bit_count} chunks={chunks}")
            sock.sendall(bytes(tdo))


def serve(args: argparse.Namespace) -> None:
    bridge = ExlinkJtagBridge(args.port, args.baudrate, args.timeout)
    try:
        print(f"CDC serial open: {args.port}")
        print(f"Bridge info: {bridge.info()}")
        active = bridge.select_engine("pio")
        print(f"Active engine: {active} (pio)")
        _active, _supported, max_shift_bits = bridge.capabilities()
        args.max_shift_bits = min(MAX_SHIFT_BITS, max_shift_bits)
        args.xvc_info = f"xvcServer_v1.0:{args.max_shift_bits}\n".encode("ascii")
        if args.max_shift_bits != MAX_SHIFT_BITS:
            print(f"warning: firmware max shift is {max_shift_bits}, local client uses {args.max_shift_bits}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.xvc_host, args.xvc_port))
            server.listen(1)
            print(f"XVC listening on {args.xvc_host}:{args.xvc_port}")

            while True:
                client, _addr = server.accept()
                client.settimeout(args.socket_timeout)
                with client:
                    try:
                        handle_client(client, bridge, args)
                    except (BridgeError, ConnectionError, OSError) as exc:
                        print(f"client closed: {exc}")
                        time.sleep(0.1)
    finally:
        bridge.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="CDC serial port, for example COM8")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--xvc-host", default="127.0.0.1")
    parser.add_argument("--xvc-port", type=int, default=2542)
    parser.add_argument("--socket-timeout", type=float, default=10.0)
    parser.add_argument("--verbose-bits", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
