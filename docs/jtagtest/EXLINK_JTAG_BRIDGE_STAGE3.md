# Exlink RP2040 JTAG Bridge Stage 3

Stage 3 adds a selectable JTAG engine layer and the first PIO-based JTAG
engine. The existing GPIO bitbang engine remains the power-on default so that
the Stage 2 Vivado/XVC flow keeps working unless the host explicitly switches
to PIO mode.

This stage does not add DMA, USB composite support, larger CDC shift packets,
or XVC `settck` changes.

## GPIO Mapping

The Stage 3 firmware uses the same Exlink JTAG v0.2 pin map as the validated
Stage 2 bridge:

| Exlink channel | RP2040 GPIO | JTAG signal | Direction |
| --- | ---: | --- | --- |
| CHAN0 | GPIO2 | TMS | RP2040 output |
| CHAN1 | GPIO3 | TCK | RP2040 output |
| CHAN2 | GPIO4 | TDO | RP2040 input |
| CHAN3 | GPIO5 | TDI | RP2040 output |
| GND | GND | GND | Ground |

GPIO4/TDO must remain an input. The PIO engine uses GPIO2 through GPIO5 as a
4-bit output window, but GPIO4 is configured as input-only at the pin direction
level and is not physically driven.

## Voltage Warning

The Exlink RP2040 JTAG outputs are fixed 3.3 V. The target JTAG bank must be
3.3 V compatible. Do not directly connect a 1.8 V JTAG bank. Normally connect
only TCK, TMS, TDI, TDO, and GND. Do not tie Exlink 3V3 to target VREF or 3V3
unless the target is explicitly intended to be powered that way.

## Firmware Structure

The bridge now has three JTAG layers:

- `jtag_gpio.c`: the existing Stage 2 bitbang implementation.
- `jtag_pio.c`: the new no-DMA PIO implementation.
- `jtag_engine.c`: the common dispatch layer used by the CDC protocol.

`jtag_protocol.c` should call only the `jtag_engine_*` API for reset and shift
operations. It should not directly select GPIO or PIO low-level functions.

Power-on default engine:

```text
bitbang
```

Supported engines:

```text
0 = bitbang
1 = pio
```

## USB CDC Protocol

All multi-byte integers are little-endian. Stage 2 commands are preserved:

- `I` returns `i`, `uint16 text_length`, and the ASCII text
  `EXLINK-RP2040-JTAG-BRIDGE v0.2`.
- `T` runs TAP reset on the active engine and returns `t`, `uint8 status`.
- `S`, `uint32 bit_count`, TMS bytes, TDI bytes shifts up to 4096 bits on the
  active engine and returns `s`, `uint8 status`, `uint32 bit_count`, and TDO
  bytes on success.
- `K`, `uint32 half_period_us` keeps the Stage 2 bitbang clock control command.
  It does not change the fixed PIO clock.
- Invalid commands return `e`, `uint8 error_code`.

Stage 3 adds engine selection:

```text
M
uint8 engine
```

Response:

```text
m
uint8 status
uint8 active_engine
```

Status values:

```text
0 = success
1 = invalid parameter
2 = engine initialization failed
3 = engine switch failed
```

Stage 3 also adds capability query:

```text
Q
```

Response:

```text
q
uint8 status
uint8 active_engine
uint8 supported_engine_flags
uint32 max_shift_bits
```

Engine flags:

```text
bit0 = bitbang
bit1 = pio
```

`max_shift_bits` remains 4096.

TMS, TDI, and TDO bitstreams are packed LSB-first in each byte for both engines.

## PIO Engine

The PIO engine is intentionally simple in this stage:

- no DMA;
- CPU pumps both TX and RX FIFOs;
- fixed requested JTAG rate of 1 MHz;
- one shift transaction at a time;
- static TX/RX buffers sized for 4096 bits;
- 100 ms timeout per shift transaction.

PIO output nibble mapping:

```text
bit0 -> GPIO2 / TMS
bit1 -> GPIO3 / TCK
bit2 -> GPIO4 / TDO window bit, physical pin remains input
bit3 -> GPIO5 / TDI
```

Each JTAG bit is encoded as two nibbles:

```text
low_nibble  = TMS | (TDI << 3)
high_nibble = low_nibble | (1 << 1)
```

So one JTAG bit consumes 8 PIO TX bits, and one 32-bit TX FIFO word carries 4
JTAG bits.

The PIO program flow is:

```text
pull bit_count_minus_1
mov x, osr
pull first TX word
loop:
    out pins, 4
    nop
    out pins, 4
    in pins, 1
    jmp x-- loop
    set pins, 0
    push
```

At the end of each shift the state machine is disabled and TCK is forced low.
On timeout, the firmware disables the state machine, clears FIFOs, restarts the
state machine, forces TCK low, and returns shift failure.

The checked-in `jtag_pio.pio` file is a readable source reference. The firmware
currently builds the same program from a C instruction table in `jtag_pio.c`.
This avoids depending on host `pioasm` during the build.

## Build

Configure and build:

```powershell
cmake -S pico_sdk_sigrok `
  -B build/exlink-jtag `
  -G Ninja `
  -DPICO_BOARD=pico `
  -DSIGROK_BOARD_EXLINK=ON `
  -DEXLINK_BUILD_JTAG_BRIDGE=ON `
  -Dpicotool_DIR=C:\Users\HHPH\Desktop\exlink_JTAG\rp2040\tools\picotool-2.1.0\picotool

cmake --build build/exlink-jtag --target exlink_jtag_bridge
cmake --build build/exlink-jtag --target pico_sdk_sigrok
```

Expected UF2 files:

- `build/exlink-jtag/exlink_jtag_bridge.uf2`
- `build/exlink-jtag/pico_sdk_sigrok.uf2`

The local implementation build has been checked with:

```powershell
cmake --build build/exlink-jtag --target exlink_jtag_bridge
cmake --build build/exlink-jtag --target pico_sdk_sigrok
python -m py_compile sigrok-pico/tools/exlink_jtag_test.py sigrok-pico/tools/exlink_xvc_server.py
git -C sigrok-pico diff --check
```

Real hardware validation is still required.

## PC Test Tool

Install the PC dependency:

```powershell
py -m pip install pyserial
```

Basic commands:

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine bitbang
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 reset
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 boundary-test
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 4096 --count 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
```

For loopback tests, temporarily connect:

```text
CHAN3 / GPIO5 / TDI -> CHAN2 / GPIO4 / TDO
```

Remove the jumper before connecting a target.

## Recommended Validation Sequence

### 1. Bitbang Regression

After flashing `exlink_jtag_bridge.uf2`:

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COMx info
python sigrok-pico\tools\exlink_jtag_test.py --port COMx capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COMx engine bitbang
python sigrok-pico\tools\exlink_jtag_test.py --port COMx reset
python sigrok-pico\tools\exlink_jtag_test.py --port COMx loopback --bits 4096
```

Expected result: all commands report PASS.

### 2. PIO Loopback

With CHAN3/TDI temporarily connected to CHAN2/TDO:

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COMx engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COMx loopback --bits 128
```

Expected result: PASS.

### 3. Boundary Lengths

Still in PIO mode and with the loopback jumper installed:

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COMx boundary-test
```

The test covers these bit counts:

```text
1, 2, 3, 4, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65,
127, 128, 129, 255, 256, 257, 4095, 4096
```

Each length is tested with all-zero, all-one, and deterministic pseudo-random
TDI data.

### 4. PIO Stress Test

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COMx stress --bits 4096 --count 1000
```

Expected result: all 1000 loopback shifts complete without mismatch.

### 5. Zynq Scan

Remove the loopback jumper. Confirm the target JTAG bank is 3.3 V compatible,
then connect only TMS, TCK, TDI, TDO, and GND.

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 reset
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
```

Run the scan repeatedly. A candidate IDCODE is useful evidence, but do not
claim Zynq validation until the same expected words are observed consistently
on real hardware.

## XVC Server

The XVC server remains Stage 2 compatible. It continues to use the firmware
`S` shift command, so it automatically uses whichever engine is active on the
bridge.

Start the server:

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --xvc-host 127.0.0.1 --xvc-port 2542
```

Vivado Tcl:

```tcl
open_hw
connect_hw_server
open_hw_target -xvc_url localhost:2542
```

Implemented XVC commands remain:

- `getinfo:`
- `settck:`
- `shift:`

`settck:` still controls only the Stage 2 bitbang half-period command. The PIO
engine is fixed at the internal 1 MHz setting in this stage.

## Stage 3 Acceptance Checklist

Use this checklist during hardware validation:

- bitbang remains the power-on default;
- `capabilities` reports bitbang and PIO support;
- `engine bitbang` and `engine pio` switch successfully;
- GPIO2 is TMS, GPIO3 is TCK, GPIO4 is TDO input, GPIO5 is TDI;
- PIO TCK returns low after every shift;
- bitbang 4096-bit loopback passes;
- PIO 128-bit loopback passes;
- all boundary lengths pass in PIO loopback;
- 4096-bit PIO loopback passes 1000 consecutive iterations;
- Zynq scan is stable across repeated runs;
- `exlink_jtag_bridge` builds;
- `pico_sdk_sigrok` still builds.

Do not mark hardware validation complete until the real Exlink board and target
board have actually passed the relevant tests.
