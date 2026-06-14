#ifndef EXLINK_JTAG_BRIDGE_JTAG_PIO_H
#define EXLINK_JTAG_BRIDGE_JTAG_PIO_H

#include <stdbool.h>
#include <stdint.h>

bool jtag_pio_init(void);
bool jtag_pio_reconfigure(void);
void jtag_pio_deinit(void);

bool jtag_pio_set_frequency_hz(uint32_t requested_hz, uint32_t *actual_hz);
uint32_t jtag_pio_get_frequency_hz(void);

bool jtag_pio_shift_bits(uint32_t bit_count,
                         const uint8_t *tms_bits,
                         const uint8_t *tdi_bits,
                         uint8_t *tdo_bits);

void jtag_pio_tap_reset(void);

#endif
