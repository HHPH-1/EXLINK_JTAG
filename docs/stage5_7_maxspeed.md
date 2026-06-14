# Stage 5.7 Exlink JTAG Maxspeed

This stage adds an experimental max-speed firmware target and host-side test flow.

## Firmware

Build:

```powershell
cmake -S .\sigrok-pico\pico_sdk_sigrok -B .\build\exlink-jtag -G Ninja -DSIGROK_BOARD_EXLINK=ON -DEXLINK_BUILD_JTAG_BRIDGE=ON
cmake --build .\build\exlink-jtag --target exlink_jtag_maxspeed
```

UF2:

```text
c:\Users\HHPH\Desktop\exlink_JTAG\rp2040\build\exlink-jtag\exlink_jtag_maxspeed.uf2
```

Safe PIO is the default engine and uses 5 PIO cycles per JTAG bit. The experimental
`pio_fast` engine uses 4 PIO cycles per bit and must be selected explicitly.

Supported runtime DMA chunks:

```text
2048, 4096, 8192, 16384, 32768 bit
```

The firmware reports runtime clock data through `info`, including `clk_sys_hz`,
PIO cycles per bit, theoretical maximum TCK, actual TCK, DMA chunk, and maximum
shift bits.

## Low-Level Commands

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio_safe
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 dma-chunk --bits 8192
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 5000
```

Do not run loopback tests while the Zynq target is connected.

## XVC Server

```powershell
python sigrok-pico\tools\exlink_xvc_server.py `
  --port COM10 `
  --xvc-port 2542 `
  --force-tck-khz 5000 `
  --engine pio_safe `
  --dma-chunk-bits 8192 `
  --profile `
  --json-summary reports\maxspeed\xvc_summary.jsonl `
  --log-file reports\maxspeed\xvc.log
```

When `--force-tck-khz` is set, Vivado `settck:` commands are acknowledged, but
the firmware TCK remains at the forced test value.

## One-Command Max-Speed Search

After flashing `exlink_jtag_maxspeed.uf2`:

```powershell
python sigrok-pico\tools\exlink_maxspeed_test.py `
  --port COM10 `
  --bitstream "F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit" `
  --vivado "F:\apps\Vidado2020_2\Vivado\2020.2\bin\vivado.bat" `
  --xvc-port 2542 `
  --coarse-step-khz 2500 `
  --binary-resolution-khz 250 `
  --warmup 1 `
  --runs 3 `
  --confirm-runs 10 `
  --baseline-seconds 27.245 `
  --target-seconds 3.400
```

Use `--skip-vivado` only for software and IDCODE flow validation. It is reported
as skipped, not as a hardware PASS.
