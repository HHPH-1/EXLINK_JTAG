#ifndef EXLINK_JTAG_BRIDGE_JTAG_ENGINE_H
#define EXLINK_JTAG_BRIDGE_JTAG_ENGINE_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    JTAG_ENGINE_BITBANG = 0,
    JTAG_ENGINE_PIO = 1
} JtagEngineType_t;

#define JTAG_ENGINE_FLAG_BITBANG (1u << 0)
#define JTAG_ENGINE_FLAG_PIO     (1u << 1)

#define JTAG_ENGINE_MIN_HALF_PERIOD_US 1u
#define JTAG_ENGINE_MAX_HALF_PERIOD_US 100u

bool jtag_engine_init(void);

bool jtag_engine_select(JtagEngineType_t type);

JtagEngineType_t jtag_engine_get_active(void);

uint8_t jtag_engine_get_supported_flags(void);

void jtag_engine_set_half_period_us(uint32_t half_period_us);
uint32_t jtag_engine_get_half_period_us(void);

bool jtag_engine_shift_bits(uint32_t bit_count,
                            const uint8_t *tms_bits,
                            const uint8_t *tdi_bits,
                            uint8_t *tdo_bits);

void jtag_engine_tap_reset(void);

#endif
