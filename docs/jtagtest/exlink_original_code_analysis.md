# Exlink Original Code Analysis

This analysis was made before changing the sigrok-pico firmware for the
Exlink RP2040 board.

## Build And Board Defaults

- `pico_sdk_sigrok/CMakeLists.txt` sets `PICO_BOARD` to `pico` by default.
- The Pico SDK build should therefore use the Raspberry Pi Pico board
  definition, including the normal 2 MB flash setting, when the SDK is
  available.
- After installing the local RP2040 toolchain, the baseline-compatible build
  completed successfully and generated `build/baseline/pico_sdk_sigrok.uf2`.

## Digital Channels

- The original `sr_device.h` sets `PICO_MODE` to `2`.
- In `PICO_MODE == 2`, the firmware reports `NUM_D_CHAN = 32`.
- The original digital GPIO mask in that mode is `GPIO_D_MASK = 0xFFFFFFFF`.
- In baseline mode (`PICO_MODE == 0`), the firmware reports 21 digital
  channels and maps D0-D20 to GPIO2-GPIO22 with `GPIO_D_MASK = 0x7FFFFC`.
- In digital-26 mode (`PICO_MODE == 1`), the firmware maps D0-D22 to
  GPIO0-GPIO22 and D23-D25 to GPIO26-GPIO28.

## PIO Input Base

- PIO capture is configured in `pico_sdk_sigrok.c` with
  `sm_config_set_in_pins`.
- Original digital-26 and digital-32 modes start PIO input at GPIO0.
- Original baseline mode starts PIO input at GPIO2.
- Therefore the upstream default build does not already match the Exlink
  digital channel requirement. Exlink needs the baseline-style PIO base GPIO2,
  but only 8 channels.

## Analog Channels

- Original baseline mode initializes GPIO26, GPIO27, and GPIO28 as ADC inputs.
- The host-visible analog names are generated as ADC0/1/2 on GPIO26/27/28.
- The original ADC start code uses `adc_select_input(0)` and
  `adc_set_round_robin(dev.a_mask & 0x7)`.
- Original digital-26 and digital-32 modes report no analog channels.
- Exlink cannot use the original baseline ADC mapping because the only exposed
  analog input is ADC3 on GPIO29.

## USB CDC

- USB CDC is initialized in `pico_sdk_sigrok.c` with `stdio_usb_init()`.
- `pico_sdk_sigrok/CMakeLists.txt` enables USB stdio with
  `pico_enable_stdio_usb(pico_sdk_sigrok 1)` and disables UART stdio.
- The code uses TinyUSB CDC through the Pico SDK stdio USB layer and direct
  TinyUSB calls in `my_stdio_usb_out_chars`.

## Files Involved In Exlink Mapping

- `pico_sdk_sigrok/CMakeLists.txt`: add the Exlink build option and compile
  definition.
- `pico_sdk_sigrok/boards/exlink_rp2040.h`: centralized Exlink board mapping.
- `pico_sdk_sigrok/sr_device.h`: report Exlink channel counts and masks.
- `pico_sdk_sigrok/sr_device.c`: expose Exlink channel names and reject
  out-of-range host channels.
- `pico_sdk_sigrok/pico_sdk_sigrok.c`: initialize ADC3/GPIO29, set PIO base to
  GPIO2, and avoid Pico-only GPIO23/LED behavior in the Exlink build.
