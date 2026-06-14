#!/usr/bin/env python3
"""Xilinx Virtual Cable server for the Exlink RP2040 JTAG bridge."""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time

from exlink_jtag_test import BridgeError, ExlinkJtagBridge, MAX_SHIFT_BITS, get_bit


XVC_INFO = f"xvcServer_v1.0:{MAX_SHIFT_BITS}\n".encode("ascii")


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
    out = bytearray((bit_count + 7) // 8)
    for bit in range(bit_count):
        if get_bit(src, src_offset + bit):
            out[bit >> 3] |= 1 << (bit & 7)
    return bytes(out)


def paste_bits(dst: bytearray, dst_offset: int, src: bytes, bit_count: int) -> None:
    for bit in range(bit_count):
        if get_bit(src, bit):
            dst[(dst_offset + bit) >> 3] |= 1 << ((dst_offset + bit) & 7)


def clamp_half_period(period_ns: int) -> int:
    half_period_us = int(math.ceil(period_ns / 2000.0))
    return min(100, max(1, half_period_us))


def handle_client(sock: socket.socket, bridge: ExlinkJtagBridge, args: argparse.Namespace) -> None:
    peer = sock.getpeername()
    print(f"Vivado client connected: {peer}")

    while True:
        command = recv_command(sock)

        if command == b"getinfo:":
            sock.sendall(XVC_INFO)
        elif command == b"settck:":
            requested_ns = struct.unpack("<I", recv_exact(sock, 4))[0]
            half_period_us = clamp_half_period(requested_ns)
            applied = bridge.set_clock(half_period_us)
            actual_ns = applied * 2000
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
                chunk_bits = min(MAX_SHIFT_BITS, bit_count - offset)
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
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.xvc_host, args.xvc_port))
            server.listen(1)
            print(f"XVC listening on {args.xvc_host}:{args.xvc_port}")

            while True:
                client, _addr = server.accept()
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
    parser.add_argument("--verbose-bits", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
