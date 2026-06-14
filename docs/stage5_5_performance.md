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

The following real-board loopback tests were run on 2026-06-14 with:

- Port: `COM10`
- Firmware identity: `EXLINK-RP2040-JTAG-BRIDGE v0.3`
- Loopback wiring: `CHAN3 / GPIO5 / TDI -> CHAN2 / GPIO4 / TDO`
- PIO TCK: 5 MHz
- Maximum shift: 32768 bits

Commands used:

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

## Hardware Loopback Results

Initial identity and capabilities:

- `info`: `EXLINK-RP2040-JTAG-BRIDGE v0.3`
- Active engine before testing: `pio`
- Supported engines: `bitbang`, `pio`, `dma`
- Maximum shift: 32768 bits

Loopback validation:

- `engine bitbang`: PASS
- Bitbang `loopback --bits 128`: PASS
- `engine pio`: PASS
- `clock-pio --khz 5000`: PASS, actual PIO TCK 5000000 Hz
- PIO `loopback --bits 128`: PASS
- PIO `loopback --bits 32768`: PASS
- PIO `boundary-test`: PASS for all listed boundary sizes from 1 to 32768 bits, with zero/one/random payloads

Firmware profile was cleared after switching to PIO and setting 5 MHz. Profiling was enabled for boundary-test plus one benchmark run:

- logical shifts: 722
- successful shifts: 722
- total bits: 6732350
- effective firmware-profile rate: 757857 bit/s
- DMA chunks: 3538
- average chunks per shift: 4
- max chunks per shift: 16
- DMA timeouts: 0
- PIO recoveries: 0
- USB RX incomplete waits: 509908
- USB TX space waits: 260634

Firmware timing summary with profiling enabled:

| Stage | Count | Total us | Avg us | Min us | Max us |
| --- | ---: | ---: | ---: | ---: | ---: |
| request_parse_us | 722 | 2050744 | 2840 | 21 | 10597 |
| tx_prepare_us | 722 | 2454421 | 3399 | 1 | 11950 |
| dma_pio_us | 722 | 1373049 | 1901 | 12 | 6665 |
| tdo_pack_us | 722 | 1617374 | 2240 | 1 | 9707 |
| response_queue_us | 722 | 1367949 | 1894 | 26 | 7689 |
| shift_total_us | 722 | 8883401 | 12303 | 71 | 46573 |

Benchmark with profiling enabled:

| Shift bits | Runs | Effective bit/s | Elapsed s |
| ---: | ---: | ---: | ---: |
| 128 | 100 | 318670 | 0.040 |
| 512 | 100 | 514759 | 0.099 |
| 4096 | 100 | 712031 | 0.575 |
| 8192 | 100 | 721353 | 1.136 |
| 16384 | 100 | 725169 | 2.259 |
| 32768 | 100 | 731314 | 4.481 |

Benchmark with profiling disabled:

| Shift bits | Runs | Effective bit/s | Elapsed s |
| ---: | ---: | ---: | ---: |
| 128 | 100 | 325712 | 0.039 |
| 512 | 100 | 514646 | 0.099 |
| 4096 | 100 | 712333 | 0.575 |
| 8192 | 100 | 713934 | 1.147 |
| 16384 | 100 | 721601 | 2.271 |
| 32768 | 100 | 728445 | 4.498 |

Stress test with profiling disabled:

- Command: `stress --bits 32768 --count 1000`
- Result: PASS, 1000/1000 loopback shifts matched
- Engine: `pio`
- DMA: yes
- Total bits: 32768000
- Total time: 44.925 s
- Effective bit/s: 729395
- Average request latency: 44.907 ms

## Zynq Scan Results

Zynq scan testing was run after removing the CHAN3/TDI to CHAN2/TDO loopback jumper and connecting the target JTAG chain.

An initial scan attempt while the Zynq target was powered off returned all-zero TDO at 500 kHz, 1 MHz, 2 MHz, and 5 MHz. This was an invalid target-power condition and is not counted as a bridge failure.

After the Zynq target was powered normally, `scan --bits 128` was repeated 10 times at each PIO TCK frequency:

| PIO TCK | Repeats | Result | Word 0 | Word 1 | Word 2 | Word 3 |
| ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 500 kHz | 10 | PASS | `0x23727093` | `0x4BA00477` | `0x00000000` | `0x00000000` |
| 1 MHz | 10 | PASS | `0x23727093` | `0x4BA00477` | `0x00000000` | `0x00000000` |
| 2 MHz | 10 | PASS | `0x23727093` | `0x4BA00477` | `0x00000000` | `0x00000000` |
| 5 MHz | 10 | PASS | `0x23727093` | `0x4BA00477` | `0x00000000` | `0x00000000` |

The observed chain was stable across 40/40 scans after target power was restored. Both non-zero words were marked as candidate IDCODE values by the test tool.

## Vivado Profile Run

```tcl
set dev [current_hw_device]
set bit_file {F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit}

for {set i 1} {$i <= 5} {incr i} {
    puts "===== Profile Program $i / 5 ====="
    set_property PROGRAM.FILE $bit_file $dev
    program_hw_devices $dev
}
```

Vivado target and bitstream:

- Device: `xc7z020_1`
- Bitstream: `F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit`
- XVC server command: `python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --default-tck-khz 5000`
- Firmware: `EXLINK-RP2040-JTAG-BRIDGE v0.3`
- Engine: `pio`
- DMA: enabled
- PIO TCK: Vivado requested 10 MHz, server applied 5 MHz (`settck` actual 200 ns)

Vivado reported five successful `program_hw_devices` runs:

| Run | CPU time | Elapsed time | Startup status | Peak memory MB |
| ---: | ---: | ---: | --- | ---: |
| 1 | 42 s | 43 s | HIGH | 2657.863 |
| 2 | 42 s | 42 s | HIGH | 2657.863 |
| 3 | 43 s | 42 s | HIGH | 2657.863 |
| 4 | 42 s | 42 s | HIGH | 2657.863 |
| 5 | 42 s | 43 s | HIGH | 2657.863 |

Vivado program-time summary:

- Elapsed times: 43, 42, 42, 42, 43 s
- Average elapsed time: 42.4 s
- Minimum elapsed time: 42 s
- Maximum elapsed time: 43 s
- Average CPU time: 42.2 s
- Each run ended with `INFO: [Labtools 27-3164] End of startup status: HIGH`
- After refresh, Vivado reported no supported debug cores in the programmed design.

### XVC Session Results

The XVC server accepted two clients during this run. Session 1 appears to be the initial Vivado hardware interaction before the five program operations; session 2 contains the large programming traffic.

| Metric | Session 1 | Session 2 |
| --- | ---: | ---: |
| Shift requests | 2624 | 24641 |
| Total bits | 1217300 | 168885369 |
| Minimum shift | 8 | 5 |
| Maximum shift | 1070 | 131019 |
| Average shift | 464 | 6854 |
| Duration | 99.5 s | 268.2 s |
| Effective rate | 12232 bit/s | 629767 bit/s |
| Serial timeouts | 0 | 0 |
| Protocol errors | 0 | 0 |
| Client reconnects | 0 | 1 |
| Errors | 0 | 0 |

Combined XVC totals:

- Logical shift requests: 27265
- Total shifted bits: 170102669
- Serial shift subrequests: 30985
- Serial subrequests equal the final firmware logical shift count, as expected after splitting large XVC shifts.

Session 2 PC-side timing distribution:

| Stage | Total | Average | Minimum | Maximum | Share of shift total |
| --- | ---: | ---: | ---: | ---: | ---: |
| TCP receive | 233.145 ms | 9.461 us | 6.300 us | 172.400 us | 0.1% |
| PC prepare | 76.479 ms | 3.103 us | 1.100 us | 60.500 us | 0.0% |
| Serial write | 54.251821 s | 2.202 ms | 55.400 us | 41.589 ms | 24.1% |
| Serial first-byte wait | 131.053955 s | 5.319 ms | 24.300 us | 99.502 ms | 58.2% |
| Serial response read | 38.079259 s | 1.545 ms | 28.400 us | 32.571 ms | 16.9% |
| TDO assemble | 101.004 ms | 4.099 us | 1.400 us | 216.600 us | 0.0% |
| TCP send | 1.042624 s | 42.312 us | 24.500 us | 355.700 us | 0.5% |
| Shift total | 225.032590 s | 9.132 ms | 244.200 us | 172.864 ms | 100.0% |

Session 2 shift-size histogram:

| Shift size | Requests | Request ratio | Bits | Bit ratio | Average bits | Effective rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1-32 | 43 | 0.2% | 474 | 0.0% | 11 | 32099 bit/s |
| 33-128 | 7875 | 32.0% | 759411 | 0.4% | 96 | 214277 bit/s |
| 129-512 | 14107 | 57.3% | 3977680 | 2.4% | 282 | 442550 bit/s |
| 513-2048 | 1358 | 5.5% | 1756254 | 1.0% | 1293 | 621908 bit/s |
| 2049-4096 | 1 | 0.0% | 2207 | 0.0% | 2207 | 585660 bit/s |
| 4097-8192 | 14 | 0.1% | 84756 | 0.1% | 6054 | 702871 bit/s |
| 8193-32768 | 3 | 0.0% | 47842 | 0.0% | 15947 | 691337 bit/s |
| 32769+ | 1240 | 5.0% | 162256745 | 96.1% | 130852 | 774613 bit/s |

Session 2 serial split summary:

- XVC logical shifts: 24641
- Serial shift subrequests: 28361
- Average subrequests per shift: 1.15
- Maximum subrequests per shift: 4
- Unaligned subrequests: 325

### Firmware Profile From XVC Run

Firmware profiling was cleared, enabled, and then read after both XVC sessions:

- enabled: 1
- logical shifts: 30985
- successful shifts: 30985
- total bits: 170102669
- effective firmware-profile rate: 792051 bit/s
- DMA chunks: 105350
- average chunks per shift: 3
- max chunks per shift: 16
- DMA timeouts: 0
- PIO recoveries: 0
- USB RX incomplete waits: 12580112
- USB TX space waits: 6470148

Firmware timing summary from the XVC run:

| Stage | Count | Total us | Avg us | Min us | Max us |
| --- | ---: | ---: | ---: | ---: | ---: |
| request_parse_us | 30985 | 51063668 | 1648 | 21 | 10498 |
| tx_prepare_us | 30985 | 62044929 | 2002 | 3 | 11950 |
| dma_pio_us | 30985 | 34866153 | 1125 | 13 | 6662 |
| tdo_pack_us | 30985 | 31712467 | 1023 | 1 | 9707 |
| response_queue_us | 30985 | 34492982 | 1113 | 25 | 7945 |
| shift_total_us | 30985 | 214762164 | 6931 | 74 | 46181 |

Primary observations from this run:

- The five-program Vivado elapsed time is stable at 42-43 s.
- The dominant PC-side cost in the programming session is serial first-byte wait at 58.2% of XVC shift processing time, followed by serial write at 24.1% and serial response read at 16.9%.
- PC-side TMS/TDI preparation and TDO assembly are negligible in this measurement.
- Most programming bits are in large XVC shifts: the `32769+` bucket carries 96.1% of all bits.
- Firmware reported zero DMA timeouts and zero PIO recoveries across the XVC run.

## XVC Reconnect Smoke Test

A local XVC reconnect smoke test was run against the real bridge on `COM10` with the XVC server at 5 MHz:

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --default-tck-khz 5000
```

Two local TCP clients connected sequentially to `127.0.0.1:2542`. Each client ran `getinfo:`, `settck:`, and one 8-bit `shift:` request, then disconnected normally.

Client-observed results:

| Client | `getinfo:` | Requested TCK | Returned period | Shift bits | TDO |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | `xvcServer_v1.0:32768` | 10 MHz | 200 ns / 5 MHz | 8 | `ff` |
| 2 | `xvcServer_v1.0:32768` | 10 MHz | 200 ns / 5 MHz | 8 | `ff` |

Server-observed results:

- Session 1: `getinfo=1`, `settck=1`, `shift requests=1`, `serial timeouts=0`, `protocol errors=0`, `errors=0`
- Session 2: `getinfo=1`, `settck=1`, `shift requests=1`, `client reconnects=1`, `serial timeouts=0`, `protocol errors=0`, `errors=0`
- COM10 was released after the smoke test and responded to `info` with `EXLINK-RP2040-JTAG-BRIDGE v0.3`.

## Repair And Optimization Plan

Test-process fixes:

- Treat all-zero scan output as inconclusive until target power, GND, TCK/TMS/TDI/TDO wiring, and target JTAG bank voltage are confirmed.
- After switching from loopback or after a target power cycle, run one single `scan --bits 128` sanity check before starting a multi-run scan sweep.
- Keep profile disabled for baseline loopback and scan regressions unless the test is explicitly measuring firmware timing.

Low-risk performance fixes to evaluate next:

1. Serial and firmware turnaround path:
   - The main measured cost is CDC request/response turnaround, especially serial first-byte wait.
   - Inspect TinyUSB CDC receive/send loops, `tud_task()` placement, `tud_cdc_write_available()` waits, and response flush behavior.
   - Goal: reduce first-byte wait and response queue/read time without changing USB descriptors or XVC protocol.

2. Short-shift fast path:
   - Session 2 has many small requests: 32.0% in 33-128 bits and 57.3% in 129-512 bits.
   - Avoid full-size staging work for short requests; process only `(bit_count + 7) / 8` valid bytes and the active DMA range.
   - Preserve the existing TAP sequence and LSB-first bit order.

3. Active-range buffer cleanup:
   - Audit whether firmware clears or repacks more than the active request range.
   - Clear unused high bits in the final byte, but do not clear full 32768-bit buffers unless required.
   - Keep stale-data safety checks in place.

4. DMA chunk size comparison:
   - Build and test 2048, 4096, and 8192-bit chunk configurations as separate runs.
   - Record `.bss` / RAM delta, 32768-bit loopback throughput, stress latency, and one Vivado program time for each candidate.
   - Keep 2048 bits if larger chunks do not clearly improve performance or if RAM cost is not justified.

5. Per-chunk PIO/DMA setup reduction:
   - Audit which reset/reconfigure operations currently happen for every chunk.
   - Move only proven-safe setup/cleanup to logical-shift boundaries.
   - Do not add DMA chaining, ping-pong DMA, new PIO encoding, or a firmware max-shift change in this low-risk phase.

## Results Not Yet Available

No DMA chunk size comparison or optimization before/after comparison has been run in this coding session.

The following sections must be filled after real measurements:

- Optimization before/after comparisons
- DMA chunk size comparison for 2048/4096/8192 bits
- Static RAM delta for alternate chunk sizes
- Reverted optimizations and reasons

## Next Step

## Next Step

Stage 5.5 profiling and hardware validation are complete.

Proceed to Stage 5.6 with a single controlled optimization target:

* Optimize packed TMS/TDI conversion into the PIO TX DMA staging format.
* Optimize PIO RX DMA words back into packed TDO.
* Preserve the existing PIO program, JTAG timing, DMA chunk size, firmware maximum shift, USB CDC transport, XVC protocol, and bitbang fallback.

The real Vivado firmware profile shows that `tx_prepare_us` and `tdo_pack_us` together consume approximately 43.7% of firmware shift processing time, making them the first isolated optimization target.

USB CDC flush behavior, DMA chunk-size experiments, per-chunk PIO/DMA setup reduction, and firmware maximum-shift changes are deferred to later independent stages so that each optimization can be measured and reverted separately.
