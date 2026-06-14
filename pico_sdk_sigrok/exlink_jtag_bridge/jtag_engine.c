#include "jtag_engine.h"

#include "jtag_gpio.h"
#include "jtag_pio.h"

static JtagEngineType_t active_engine = JTAG_ENGINE_BITBANG;
static bool pio_initialized = false;

bool jtag_engine_init(void)
{
    jtag_gpio_init();
    active_engine = JTAG_ENGINE_BITBANG;
    pio_initialized = false;
    return true;
}

bool jtag_engine_select(JtagEngineType_t type)
{
    switch (type) {
    case JTAG_ENGINE_BITBANG:
        if (active_engine == JTAG_ENGINE_PIO) {
            jtag_pio_deinit();
        }
        jtag_gpio_init();
        active_engine = JTAG_ENGINE_BITBANG;
        return true;

    case JTAG_ENGINE_PIO:
        if (!pio_initialized) {
            pio_initialized = jtag_pio_init();
        } else {
            pio_initialized = jtag_pio_reconfigure();
        }

        if (!pio_initialized) {
            jtag_gpio_init();
            active_engine = JTAG_ENGINE_BITBANG;
            return false;
        }

        active_engine = JTAG_ENGINE_PIO;
        return true;

    default:
        return false;
    }
}

JtagEngineType_t jtag_engine_get_active(void)
{
    return active_engine;
}

uint8_t jtag_engine_get_supported_flags(void)
{
    return JTAG_ENGINE_FLAG_BITBANG | JTAG_ENGINE_FLAG_PIO;
}

void jtag_engine_set_half_period_us(uint32_t half_period_us)
{
    jtag_set_half_period_us(half_period_us);
}

uint32_t jtag_engine_get_half_period_us(void)
{
    return jtag_get_half_period_us();
}

bool jtag_engine_shift_bits(uint32_t bit_count,
                            const uint8_t *tms_bits,
                            const uint8_t *tdi_bits,
                            uint8_t *tdo_bits)
{
    if (active_engine == JTAG_ENGINE_PIO) {
        return jtag_pio_shift_bits(bit_count, tms_bits, tdi_bits, tdo_bits);
    }

    return jtag_shift_bits(bit_count, tms_bits, tdi_bits, tdo_bits);
}

void jtag_engine_tap_reset(void)
{
    if (active_engine == JTAG_ENGINE_PIO) {
        jtag_pio_tap_reset();
        return;
    }

    jtag_tap_reset();
}
