# Exlink RP2040 JTAG Bridge Stage 2

This stage adds a standalone `exlink_jtag_bridge` firmware target and two PC
tools for low-level CDC testing and Vivado XVC access. The existing
`pico_sdk_sigrok` logic-analyzer firmware remains a separate build target.

## GPIO Mapping

| Exlink channel | RP2040 GPIO | JTAG signal |
| --- | ---: | --- |
| CHAN0 | GPIO2 | TCK |
| CHAN1 | GPIO3 | TMS |
| CHAN2 | GPIO4 | TDI |
| CHAN3 | GPIO5 | TDO |
| GND | GND | GND |

Do not use GPIO0/GPIO1 or the RP2040 SWD pins for target JTAG.

## Voltage Warning

The Exlink RP2040 JTAG outputs are fixed 3.3 V. The target JTAG bank must be
3.3 V compatible. Do not directly connect a 1.8 V JTAG bank. By default, only
connect TCK, TMS, TDI, TDO, and GND. Do not assume Exlink 3V3 should be tied to
the target VREF or 3V3 rail unless the target is explicitly intended to be
powered that way.

## USB CDC Protocol

All multi-byte integers are little-endian.

- `I` returns `i`, `uint16 text_length`, and the ASCII text
  `EXLINK-RP2040-JTAG-BRIDGE v0.1`.
- `T` runs TAP reset and returns `t`, `uint8 status`.
- `S`, `uint32 bit_count`, TMS bytes, TDI bytes shifts up to 4096 bits and
  returns `s`, `uint8 status`, `uint32 bit_count`, and TDO bytes on success.
- `K`, `uint32 half_period_us` sets the JTAG half-period. Valid firmware range
  is 1 us through 100 us and returns `k`, `uint8 status`,
  `uint32 applied_half_period_us`.
- Invalid commands return `e`, `uint8 error_code`.

TMS, TDI, and TDO bitstreams are packed LSB-first in each byte.

## Build

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

## Low-Level Tests

Install the PC dependency:

```powershell
py -m pip install pyserial
```

Commands:

```powershell
python tools/exlink_jtag_test.py --port COMx info
python tools/exlink_jtag_test.py --port COMx reset
python tools/exlink_jtag_test.py --port COMx clock --half-period-us 5
python tools/exlink_jtag_test.py --port COMx loopback
python tools/exlink_jtag_test.py --port COMx scan --bits 128
```

For loopback, temporarily connect CHAN2/TDI to CHAN3/TDO with no target
connected. Remove the jumper after the test.

For Zynq scan, first confirm the target JTAG bank is 3.3 V compatible and wire
only CHAN0/TCK, CHAN1/TMS, CHAN2/TDI, CHAN3/TDO, and GND. The scan command
prints raw TDO bytes, the LSB-first bitstream, 32-bit words, and basic IDCODE
candidates. A candidate is not proof of a complete Vivado connection.

## XVC Server

Start the server:

```powershell
python tools/exlink_xvc_server.py --port COMx --xvc-host 127.0.0.1 --xvc-port 2542
```

Vivado Tcl:

```tcl
open_hw
connect_hw_server
open_hw_target -xvc_url localhost:2542
```

Implemented XVC commands:

- `getinfo:` returns `xvcServer_v1.0:4096\n`.
- `settck:` clamps Vivado's requested period to the firmware range and returns
  the applied period in ns.
- `shift:` forwards one or more 4096-bit CDC shift chunks and returns the
  concatenated TDO bitstream.

Hardware validation still must be performed on a real Exlink and target board.
Do not claim IDCODE readout or Vivado enumeration success until those tests have
actually passed.
