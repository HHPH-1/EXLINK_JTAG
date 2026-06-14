#include "jtag_engine.h"

#include "jtag_gpio.h"
#include "jtag_pio.h"

static JtagEngineType_t active_engine = JTAG_ENGINE_BITBANG;
static bool pio_initialized = false;

bool jtag_engine_init(void)
{
    jtag_gpio_init();
    pio_initialized = jtag_pio_init();
    active_engine = pio_initialized ? JTAG_ENGINE_PIO : JTAG_ENGINE_BITBANG;
    return true;
}

bool jtag_engine_select(JtagEngineType_t type)
{
    switch (type) {
    case JTAG_ENGINE_BITBANG:
        if (active_engine == JTAG_ENGINE_PIO || active_engine == JTAG_ENGINE_PIO_FAST) {
            jtag_pio_deinit();
        }
        jtag_gpio_init();
        active_engine = JTAG_ENGINE_BITBANG;
        return true;

    case JTAG_ENGINE_PIO:
    case JTAG_ENGINE_PIO_FAST:
        if (!pio_initialized) {
            pio_initialized = jtag_pio_init();
        } else {
            pio_initialized = jtag_pio_reconfigure();
        }

        if (pio_initialized && !jtag_pio_select_fast_engine(type == JTAG_ENGINE_PIO_FAST)) {
            pio_initialized = false;
        }

        if (!pio_initialized) {
            jtag_gpio_init();
            active_engine = JTAG_ENGINE_BITBANG;
            return false;
        }

        active_engine = type;
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
    uint8_t flags = JTAG_ENGINE_FLAG_BITBANG | JTAG_ENGINE_FLAG_PIO | JTAG_ENGINE_FLAG_DMA;
    if (jtag_pio_fast_engine_available()) {
        flags |= JTAG_ENGINE_FLAG_PIO_FAST;
    }
    return flags;
}

void jtag_engine_set_half_period_us(uint32_t half_period_us)
{
    jtag_set_half_period_us(half_period_us);
}

uint32_t jtag_engine_get_half_period_us(void)
{
    return jtag_get_half_period_us();
}

bool jtag_engine_set_pio_frequency_hz(uint32_t requested_hz, uint32_t *actual_hz)
{
    return jtag_pio_set_frequency_hz(requested_hz, actual_hz);
}

uint32_t jtag_engine_get_pio_frequency_hz(void)
{
    return jtag_pio_get_frequency_hz();
}

uint32_t jtag_engine_get_requested_pio_frequency_hz(void)
{
    return jtag_pio_get_requested_frequency_hz();
}

uint32_t jtag_engine_get_pio_cycles_per_bit(void)
{
    return jtag_pio_get_cycles_per_bit();
}

uint32_t jtag_engine_get_maximum_pio_frequency_hz(void)
{
    return jtag_pio_get_maximum_frequency_hz();
}

bool jtag_engine_set_dma_chunk_bits(uint32_t chunk_bits)
{
    return jtag_pio_set_dma_chunk_bits(chunk_bits);
}

uint32_t jtag_engine_get_dma_chunk_bits(void)
{
    return jtag_pio_get_dma_chunk_bits();
}

uint32_t jtag_engine_get_max_dma_chunk_bits(void)
{
    return jtag_pio_get_max_dma_chunk_bits();
}

bool jtag_engine_shift_bits(uint32_t bit_count,
                            const uint8_t *tms_bits,
                            const uint8_t *tdi_bits,
                            uint8_t *tdo_bits)
{
    if (active_engine == JTAG_ENGINE_PIO || active_engine == JTAG_ENGINE_PIO_FAST) {
        return jtag_pio_shift_bits(bit_count, tms_bits, tdi_bits, tdo_bits);
    }

    return jtag_shift_bits(bit_count, tms_bits, tdi_bits, tdo_bits);
}

void jtag_engine_tap_reset(void)
{
    if (active_engine == JTAG_ENGINE_PIO || active_engine == JTAG_ENGINE_PIO_FAST) {
        jtag_pio_tap_reset();
        return;
    }

    jtag_tap_reset();
}
