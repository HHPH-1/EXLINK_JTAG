#include "jtag_gpio.h"

#include "exlink_rp2040.h"
#include "hardware/gpio.h"
#include "pico/stdlib.h"

#include <string.h>

static uint32_t half_period_us = JTAG_DEFAULT_HALF_PERIOD_US;

static uint8_t get_bit(const uint8_t *buffer, uint32_t bit_index)
{
    return (uint8_t)((buffer[bit_index >> 3] >> (bit_index & 7u)) & 1u);
}

static void set_bit(uint8_t *buffer, uint32_t bit_index, uint8_t value)
{
    if (value) {
        buffer[bit_index >> 3] |= (uint8_t)(1u << (bit_index & 7u));
    }
}

void jtag_gpio_init(void)
{
    gpio_init(EXLINK_JTAG_TCK_GPIO);
    gpio_init(EXLINK_JTAG_TMS_GPIO);
    gpio_init(EXLINK_JTAG_TDI_GPIO);
    gpio_init(EXLINK_JTAG_TDO_GPIO);

    gpio_put(EXLINK_JTAG_TCK_GPIO, 0);
    gpio_put(EXLINK_JTAG_TMS_GPIO, 1);
    gpio_put(EXLINK_JTAG_TDI_GPIO, 0);

    gpio_set_dir(EXLINK_JTAG_TCK_GPIO, GPIO_OUT);
    gpio_set_dir(EXLINK_JTAG_TMS_GPIO, GPIO_OUT);
    gpio_set_dir(EXLINK_JTAG_TDI_GPIO, GPIO_OUT);
    gpio_set_dir(EXLINK_JTAG_TDO_GPIO, GPIO_IN);

    gpio_disable_pulls(EXLINK_JTAG_TDO_GPIO);

    gpio_set_drive_strength(EXLINK_JTAG_TCK_GPIO, GPIO_DRIVE_STRENGTH_4MA);
    gpio_set_drive_strength(EXLINK_JTAG_TMS_GPIO, GPIO_DRIVE_STRENGTH_4MA);
    gpio_set_drive_strength(EXLINK_JTAG_TDI_GPIO, GPIO_DRIVE_STRENGTH_4MA);

    gpio_set_slew_rate(EXLINK_JTAG_TCK_GPIO, GPIO_SLEW_RATE_SLOW);
    gpio_set_slew_rate(EXLINK_JTAG_TMS_GPIO, GPIO_SLEW_RATE_SLOW);
    gpio_set_slew_rate(EXLINK_JTAG_TDI_GPIO, GPIO_SLEW_RATE_SLOW);

    half_period_us = JTAG_DEFAULT_HALF_PERIOD_US;
}

void jtag_gpio_deinit(void)
{
    gpio_put(EXLINK_JTAG_TCK_GPIO, 0);
    gpio_put(EXLINK_JTAG_TMS_GPIO, 1);
    gpio_put(EXLINK_JTAG_TDI_GPIO, 0);

    gpio_deinit(EXLINK_JTAG_TCK_GPIO);
    gpio_deinit(EXLINK_JTAG_TMS_GPIO);
    gpio_deinit(EXLINK_JTAG_TDI_GPIO);
    gpio_deinit(EXLINK_JTAG_TDO_GPIO);
}

void jtag_set_half_period_us(uint32_t value)
{
    if (value < JTAG_MIN_HALF_PERIOD_US) {
        value = JTAG_MIN_HALF_PERIOD_US;
    } else if (value > JTAG_MAX_HALF_PERIOD_US) {
        value = JTAG_MAX_HALF_PERIOD_US;
    }

    half_period_us = value;
}

uint32_t jtag_get_half_period_us(void)
{
    return half_period_us;
}

uint8_t jtag_clock_bit(uint8_t tms, uint8_t tdi)
{
    gpio_put(EXLINK_JTAG_TCK_GPIO, 0);
    gpio_put(EXLINK_JTAG_TMS_GPIO, tms ? 1 : 0);
    gpio_put(EXLINK_JTAG_TDI_GPIO, tdi ? 1 : 0);

    busy_wait_us(half_period_us);
    gpio_put(EXLINK_JTAG_TCK_GPIO, 1);
    busy_wait_us(half_period_us);

    uint8_t tdo = gpio_get(EXLINK_JTAG_TDO_GPIO) ? 1u : 0u;
    gpio_put(EXLINK_JTAG_TCK_GPIO, 0);
    return tdo;
}

void jtag_tap_reset(void)
{
    for (uint32_t i = 0; i < 6u; ++i) {
        (void)jtag_clock_bit(1u, 0u);
    }

    (void)jtag_clock_bit(0u, 0u);
}

bool jtag_shift_bits(uint32_t bit_count,
                     const uint8_t *tms_bits,
                     const uint8_t *tdi_bits,
                     uint8_t *tdo_bits)
{
    if (!tms_bits || !tdi_bits || !tdo_bits) {
        return false;
    }

    memset(tdo_bits, 0, (bit_count + 7u) / 8u);

    for (uint32_t bit = 0; bit < bit_count; ++bit) {
        uint8_t tdo = jtag_clock_bit(get_bit(tms_bits, bit), get_bit(tdi_bits, bit));
        set_bit(tdo_bits, bit, tdo);
    }

    return true;
}
