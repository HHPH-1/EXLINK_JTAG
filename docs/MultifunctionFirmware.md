# Exlink Multifunction Firmware

`exlink_multifunction.uf2` contains the existing sigrok scope firmware and the
existing Exlink JTAG bridge firmware in one image. Only one mode is initialized
per boot.

## Boot Mode

Cold boot, brownout, normal reset, first UF2 boot, and USB unplug/replug default
to SCOPE.

Software mode changes write a temporary watchdog scratch marker, flush the CDC
acknowledgement, and reboot through the watchdog. On the next boot the marker is
honored only when the reset was caused by the watchdog, then the scratch marker
is cleared. No mode selection is written to flash.

## Control Frames

Control frames are ASCII CDC lines recognized at protocol command boundaries:

```text
@EXLINK:INFO
@EXLINK:MODE?
@EXLINK:CAPS?
@EXLINK:MODE:JTAG
@EXLINK:MODE:SCOPE
@EXLINK:BOOTSEL:CONFIRM
```

Responses:

```text
@EXLINK:OK:INFO:<version>:<mode>
@EXLINK:OK:MODE:SCOPE
@EXLINK:OK:MODE:JTAG
@EXLINK:OK:CAPS:SCOPE,JTAG,XVC,PIO,DMA
@EXLINK:OK:SWITCHING:JTAG
@EXLINK:OK:SWITCHING:SCOPE
@EXLINK:ERR:BUSY
@EXLINK:ERR:BAD_COMMAND
```

SCOPE refuses mode switches while its capture state is not `IDLE`. JTAG command
parsing checks control frames only when no shift payload is being read.

## RAM Layout

The multifunction target defines one 220000 byte mode workspace. SCOPE uses it
as the existing capture buffer. JTAG divides the same workspace into shift
buffers and PIO DMA staging buffers.

The standalone `pico_sdk_sigrok`, `exlink_jtag_bridge`, and
`exlink_jtag_maxspeed` targets keep their existing allocation behavior.

## Build

```powershell
cmake -S .\sigrok-pico\pico_sdk_sigrok -B .\sigrok-pico\build\exlink-multifunction -G Ninja -DSIGROK_BOARD_EXLINK=ON -DEXLINK_BUILD_JTAG_BRIDGE=ON
cmake --build .\sigrok-pico\build\exlink-multifunction --target pico_sdk_sigrok exlink_jtag_bridge exlink_jtag_maxspeed exlink_multifunction -j 8
```

Output:

```text
sigrok-pico/build/exlink-multifunction/exlink_multifunction.uf2
```

Hardware validation is still required for PulseView sampling, Vivado download,
and repeated mode switching.
