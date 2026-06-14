#!/usr/bin/env python3
"""Software equivalence tests for Exlink JTAG PIO fast pack paths."""

from __future__ import annotations

import random
import sys


LENGTHS = [
    1, 2, 3, 4, 5,
    7, 8, 9,
    15, 16, 17,
    31, 32, 33,
    63, 64, 65,
    127, 128, 129,
    255, 256, 257,
    511, 512, 513,
    1023, 1024, 1025,
    2047, 2048, 2049,
    4095, 4096, 4097,
    8191, 8192,
]
OFFSETS = [0, 1, 2, 3, 7, 8, 9, 15, 16, 31, 32]
PATTERNS = ["zero", "one", "55", "aa", "increment", "random"]
RANDOM_CASES = 10000
RANDOM_SEED = 0xE56A

TMS_LUT = [
    0x00000000, 0x00000011, 0x00001100, 0x00001111,
    0x00110000, 0x00110011, 0x00111100, 0x00111111,
    0x11000000, 0x11000011, 0x11001100, 0x11001111,
    0x11110000, 0x11110011, 0x11111100, 0x11111111,
]
TDI_LUT = [
    0x00000000, 0x00000088, 0x00008800, 0x00008888,
    0x00880000, 0x00880088, 0x00888800, 0x00888888,
    0x88000000, 0x88000088, 0x88008800, 0x88008888,
    0x88880000, 0x88880088, 0x88888800, 0x88888888,
]


def get_bit(data: bytes | bytearray, index: int) -> int:
    return (data[index >> 3] >> (index & 7)) & 1


def set_bit(data: bytearray, index: int, value: int) -> None:
    if value:
        data[index >> 3] |= 1 << (index & 7)


def mask_unused(data: bytes | bytearray, bit_count: int) -> bytes:
    out = bytearray(data)
    used = bit_count & 7
    if used and out:
        out[-1] &= (1 << used) - 1
    return bytes(out)


def make_payload(bit_count: int, pattern: str, rng: random.Random) -> bytes:
    byte_count = (bit_count + 7) // 8
    if pattern == "zero":
        return bytes(byte_count)
    if pattern == "one":
        return mask_unused(bytes([0xFF]) * byte_count, bit_count)
    if pattern == "55":
        return mask_unused(bytes([0x55]) * byte_count, bit_count)
    if pattern == "aa":
        return mask_unused(bytes([0xAA]) * byte_count, bit_count)
    if pattern == "increment":
        return mask_unused(bytes((index & 0xFF) for index in range(byte_count)), bit_count)
    if pattern == "random":
        return mask_unused(bytes(rng.getrandbits(8) for _ in range(byte_count)), bit_count)
    raise ValueError(pattern)


def reference_tx(tms: bytes, tdi: bytes, offset: int, bit_count: int) -> list[int]:
    words = [0] * (1 + ((bit_count + 3) // 4))
    words[0] = bit_count - 1
    for bit in range(bit_count):
        source_bit = offset + bit
        tms_bit = get_bit(tms, source_bit)
        tdi_bit = get_bit(tdi, source_bit)
        low_nibble = tms_bit | (tdi_bit << 3)
        high_nibble = low_nibble | (1 << 1)
        shift = (bit & 3) * 8
        words[1 + (bit >> 2)] |= low_nibble << shift
        words[1 + (bit >> 2)] |= high_nibble << (shift + 4)
    return words


def fast_tx(tms: bytes, tdi: bytes, offset: int, bit_count: int) -> list[int]:
    if offset & 7:
        return reference_tx(tms, tdi, offset, bit_count)

    words = [0] * (1 + ((bit_count + 3) // 4))
    words[0] = bit_count - 1
    source_byte_offset = offset >> 3
    full_groups = bit_count >> 2
    for group in range(full_groups):
        bit_in_byte = 4 if (group & 1) else 0
        byte_index = source_byte_offset + (group >> 1)
        tms_nibble = (tms[byte_index] >> bit_in_byte) & 0x0F
        tdi_nibble = (tdi[byte_index] >> bit_in_byte) & 0x0F
        words[1 + group] = 0x20202020 | TMS_LUT[tms_nibble] | TDI_LUT[tdi_nibble]

    tail_bits = bit_count & 3
    if tail_bits:
        tail_base = full_groups << 2
        for bit in range(tail_bits):
            source_bit = offset + tail_base + bit
            tms_bit = get_bit(tms, source_bit)
            tdi_bit = get_bit(tdi, source_bit)
            low_nibble = tms_bit | (tdi_bit << 3)
            high_nibble = low_nibble | (1 << 1)
            shift = bit * 8
            words[1 + full_groups] |= low_nibble << shift
            words[1 + full_groups] |= high_nibble << (shift + 4)
    return words


def reference_rx(rx_words: list[int], bit_count: int, offset: int) -> bytes:
    out = bytearray((offset + bit_count + 7) // 8)
    for bit in range(bit_count):
        word_index = bit // 32
        bit_in_word = bit & 31
        bits_left = bit_count - (word_index * 32)
        valid_bits = 32 if bits_left > 32 else bits_left
        rx_bit = bit_in_word if valid_bits == 32 else (32 - valid_bits + bit_in_word)
        set_bit(out, offset + bit, (rx_words[word_index] >> rx_bit) & 1)
    return bytes(out)


def fast_rx(rx_words: list[int], bit_count: int, offset: int) -> bytes:
    if offset & 7:
        return reference_rx(rx_words, bit_count, offset)

    out = bytearray((offset + bit_count + 7) // 8)
    target_byte_offset = offset >> 3
    full_words = bit_count >> 5
    for word in range(full_words):
        value = rx_words[word]
        start = target_byte_offset + (word << 2)
        out[start:start + 4] = value.to_bytes(4, "little")

    remaining = bit_count & 31
    if remaining:
        rx_word = rx_words[full_words]
        valid_base = 32 - remaining
        target_base = offset + (full_words << 5)
        for bit in range(remaining):
            set_bit(out, target_base + bit, (rx_word >> (valid_base + bit)) & 1)
    return bytes(out)


def check_case(bit_count: int, offset: int, pattern: str, rng: random.Random) -> None:
    total_bits = offset + bit_count
    tms = make_payload(total_bits, pattern, rng)
    tdi = make_payload(total_bits, pattern, rng)
    ref_tx = reference_tx(tms, tdi, offset, bit_count)
    got_tx = fast_tx(tms, tdi, offset, bit_count)
    if got_tx != ref_tx:
        raise AssertionError(f"TX mismatch bits={bit_count} offset={offset} pattern={pattern}")

    rx_word_count = (bit_count // 32) + 1
    rx_words = [rng.getrandbits(32) for _ in range(rx_word_count)]
    ref_rx = reference_rx(rx_words, bit_count, offset)
    got_rx = fast_rx(rx_words, bit_count, offset)
    if got_rx != ref_rx:
        raise AssertionError(f"RX mismatch bits={bit_count} offset={offset} pattern={pattern}")


def main() -> int:
    rng = random.Random(RANDOM_SEED)
    cases = 0
    for bit_count in LENGTHS:
        for offset in OFFSETS:
            for pattern in PATTERNS:
                check_case(bit_count, offset, pattern, rng)
                cases += 1

    for _ in range(RANDOM_CASES):
        bit_count = rng.randint(1, 8192)
        offset = rng.choice(OFFSETS)
        check_case(bit_count, offset, "random", rng)
        cases += 1

    print(f"PASS: {cases} fast TX/RX pack equivalence cases, seed=0x{RANDOM_SEED:X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
