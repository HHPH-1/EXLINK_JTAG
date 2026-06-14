# Exlink RP2040 JTAG Bridge Stage 5.5 Performance Notes

## Stable Baseline

Stage 5 stable baseline remains the reference point. This change adds profiling only and does not claim new hardware validation.

- Firmware identity: `EXLINK-RP2040-JTAG-BRIDGE v0.3`
- Firmware maximum shift: 32768 bits
- DMA chunk size: 2048 bits
- Verified PIO TCK ceiling from prior testing: 5 MHz
- Prior large loopback rate: about 770.5 kbit/s
- Prior real Vivado session rate: about 594.7 kbit/s
- Prior Vivado program time: about 41-42 s

## Current Code Path Analysis

One XVC `shift:` request follows this path:

1. TCP client sends `shift:` command, 32-bit bit count, packed TMS bytes, then packed TDI bytes to `tools/exlink_xvc_server.py`.
2. The server splits requests larger than the firmware 32768-bit limit in `shift_via_bridge_profiled()`.
3. Each firmware subrequest is sent over CDC serial as `S + bit_count + TMS + TDI`.
4. Firmware `jtag_protocol.c` receives the frame into static packed buffers.
5. `jtag_engine_shift_bits()` dispatches to PIO or bitbang.
6. PIO mode splits a 32768-bit logical firmware shift into 2048-bit DMA chunks.
7. Each PIO chunk expands packed TMS/TDI into 32-bit TX DMA words, starts RX/TX DMA, waits for completion, then repacks RX words into packed TDO.
8. Firmware returns `s + status + returned_bits + packed TDO`.
9. The server assembles subrequest TDO bytes and returns one packed TDO buffer to the XVC TCP client.

A 32768-bit firmware shift currently executes 16 DMA chunks at 2048 bits each.

Each chunk currently redoes the conservative setup sequence: abort-if-busy, disable SM, clear FIFOs, restart SM, clear PIO IRQs, drive idle, configure RX DMA, configure TX DMA, enable SM, wait, disable SM, and repack TDO.

Current staging formats:

- XVC/TCP and CDC protocol buffers: packed LSB-first bytes.
- PIO TX DMA buffer: one 32-bit count word, then one 32-bit word per four JTAG bits. Each JTAG bit occupies two 4-bit pin nibbles, so one JTAG bit costs 1 byte in the TX DMA data area.
- PIO RX DMA buffer: 32-bit words from PIO autopush, one TDO sample bit per JTAG bit.

Most likely bottlenecks before measurement:

- USB CDC round-trip and host-side serial write/read latency.
- Python buffer copies for TMS/TDI slicing and TDO assembly, especially for non-byte-aligned logical splits.
- Per-chunk PIO/DMA reset and reconfiguration plus bit-by-bit TX expansion and TDO repacking.

## Implemented Measurement

### PC XVC Server

`tools/exlink_xvc_server.py` now records per-session timing with `time.perf_counter_ns()`:

- `tcp_receive_ns`: XVC shift length, TMS, and TDI receive time.
- `pc_prepare_ns`: split calculations, TMS/TDI extraction, and serial frame preparation.
- `serial_write_ns`: CDC request write completion time.
- `serial_first_byte_wait_ns`: time from serial write completion to first firmware response byte.
- `serial_response_read_ns`: time from first response byte to complete firmware response.
- `tdo_assemble_ns`: TDO subrequest merge and final masking.
- `tcp_send_ns`: socket `sendall()` for returned TDO.
- `shift_total_ns`: complete logical XVC shift handling time.

The server also records the required shift-size histogram and serial subrequest split statistics. It prints the summary when the XVC client disconnects.

### Firmware

Firmware profiling is implemented in `jtag_profile.c` and is off by default. It records integer microsecond counters only:

- `request_parse_us`
- `tx_prepare_us`
- `dma_pio_us`
- `tdo_pack_us`
- `response_queue_us`
- `shift_total_us`

It also records logical shifts, total bits, DMA chunks, max/average chunks, DMA timeouts, PIO recoveries, USB RX wait-loop iterations, and USB TX wait-loop iterations.

When profiling is off, the hot path does not take per-stage timestamps. The remaining overhead is a small enabled check around optional counters.

## Firmware Profile Commands

Use the test tool:

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile on
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile show
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile clear
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile off
```

The underlying firmware command is `R + action`, with actions `show=0`, `clear=1`, `on=2`, and `off=3`. This does not alter the existing shift response format.

## Build

Build command used:

```powershell
cmake --build build\exlink-jtag --target exlink_jtag_bridge
```

This local build completed successfully on 2026-06-14. No new compiler warnings were shown in the build output.

Output artifacts include:

- `build/exlink-jtag/exlink_jtag_bridge.uf2`
- `build/exlink-jtag/exlink_jtag_bridge.elf`
- `build/exlink-jtag/exlink_jtag_bridge.bin`

## Hardware Test Commands

These still need to be run on the real board and target:

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine bitbang
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 5000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 boundary-test
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
```

Vivado profile run:

```tcl
set dev [current_hw_device]
set bit_file {F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit}

for {set i 1} {$i <= 5} {incr i} {
    puts "===== Profile Program $i / 5 ====="
    set_property PROGRAM.FILE $bit_file $dev
    program_hw_devices $dev
}
```

## Results Not Yet Available

No new hardware benchmark, boundary-test, stress-test, Zynq scan, or Vivado download results have been run in this coding session.

The following sections must be filled after real measurements:

- PC-side time distribution
- Firmware-side time distribution
- Shift length histogram from a real Vivado session
- Optimization before/after comparisons
- DMA chunk size comparison for 2048/4096/8192 bits
- Static RAM delta for alternate chunk sizes
- Vivado single-program time
- Reverted optimizations and reasons

## Next Step

Run profiling with the current code first. Only after the PC and firmware summaries identify the dominant cost should Stage 5.5 proceed to the low-risk optimization phases.
