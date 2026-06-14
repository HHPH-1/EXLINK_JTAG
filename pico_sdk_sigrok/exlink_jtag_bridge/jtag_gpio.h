#ifndef EXLINK_JTAG_BRIDGE_JTAG_GPIO_H
#define EXLINK_JTAG_BRIDGE_JTAG_GPIO_H

#include <stdbool.h>
#include <stdint.h>

#define JTAG_DEFAULT_HALF_PERIOD_US 2u
#define JTAG_MIN_HALF_PERIOD_US     1u
#define JTAG_MAX_HALF_PERIOD_US     100u

void jtag_gpio_init(void);
void jtag_gpio_deinit(void);

void jtag_set_half_period_us(uint32_t half_period_us);
uint32_t jtag_get_half_period_us(void);

uint8_t jtag_clock_bit(uint8_t tms, uint8_t tdi);
void jtag_tap_reset(void);

bool jtag_shift_bits(uint32_t bit_count,
                     const uint8_t *tms_bits,
                     const uint8_t *tdi_bits,
                     uint8_t *tdo_bits);

#endif
