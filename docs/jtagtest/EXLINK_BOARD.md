# Exlink RP2040 Board Support

This build adapts the existing sigrok-pico RP2040 firmware to the Exlink
RP2040 hardware while keeping the existing sigrok/PulseView CDC protocol.

## Hardware Mapping

| Exlink name | RP2040 GPIO | Function |
| --- | ---: | --- |
| CHAN0 | GPIO2 | Digital channel 0 |
| CHAN1 | GPIO3 | Digital channel 1 |
| CHAN2 | GPIO4 | Digital channel 2 |
| CHAN3 | GPIO5 | Digital channel 3 |
| CHAN4 | GPIO6 | Digital channel 4 |
| CHAN5 | GPIO7 | Digital channel 5 |
| CHAN6 | GPIO8 | Digital channel 6 |
| CHAN7 | GPIO9 | Digital channel 7 |
| PICO_DP | USB_DP | USB Device |
| PICO_DN | USB_DM | USB Device |

The firmware reports eight digital channels to the host as `D0` through `D7`.
These display names map onto the physical Exlink inputs CHAN0 through CHAN7 on
GPIO2 through GPIO9. No analog channel is reported to the host, so PulseView
will not show `A0`.

## Board Notes

- Flash is W25Q16JV, 16 Mbit / 2 MB.
- The RP2040 uses a 12 MHz crystal.
- USB is the RP2040 native USB device path through the board CH334F hub.
- The firmware does not use ESP32 forwarding or USB host mode.
- The RP2040 RUN pin is not controlled by the ESP32 in this firmware.
- Future Vivado JTAG mode is reserved to reuse CHAN0-CHAN3.
- Logic-analyzer mode and a future JTAG mode cannot use CHAN0-CHAN3 at the same
  time.

## Build

The Exlink build uses the normal Pico board definition because the flash and
crystal match a Raspberry Pi Pico.

```powershell
cmake -S pico_sdk_sigrok -B build/exlink -G Ninja -DPICO_BOARD=pico -DSIGROK_BOARD_EXLINK=ON -Dpicotool_DIR=C:\Users\HHPH\Desktop\exlink_JTAG\rp2040\tools\picotool-2.1.0\picotool
cmake --build build/exlink
```

Expected outputs:

- `build/exlink/pico_sdk_sigrok.elf`
- `build/exlink/pico_sdk_sigrok.bin`
- `build/exlink/pico_sdk_sigrok.hex`
- `build/exlink/pico_sdk_sigrok.uf2`

This build was verified locally after installing CMake, Ninja, Arm GNU
Toolchain, Pico SDK 2.1.0, and picotool 2.1.0.

## UF2 Flashing

1. Hold BOOTSEL while connecting or resetting the Exlink RP2040.
2. Wait for the `RPI-RP2` mass-storage drive to appear.
3. Copy `pico_sdk_sigrok.uf2` to `RPI-RP2`.
4. After reboot, check Windows Device Manager for the USB CDC device.
5. Open a sigrok-pico compatible PulseView build.
6. Apply test square waves to CHAN0 through CHAN7 and verify D0 through D7
   order.

## Verification Status

Passed in this environment:

- Source mapping inspection.
- Exlink channel and GPIO static checks in source.
- Baseline firmware build.
- Exlink firmware build.

Not passed in this environment:

- Real UF2 flashing.
- USB enumeration on Exlink hardware.
- PulseView digital sampling tests.
