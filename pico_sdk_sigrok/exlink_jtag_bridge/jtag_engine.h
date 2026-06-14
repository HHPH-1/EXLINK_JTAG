#ifndef EXLINK_JTAG_BRIDGE_JTAG_ENGINE_H
#define EXLINK_JTAG_BRIDGE_JTAG_ENGINE_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    JTAG_ENGINE_BITBANG = 0,
    JTAG_ENGINE_PIO = 1,
    JTAG_ENGINE_PIO_FAST = 2
} JtagEngineType_t;

#define JTAG_ENGINE_FLAG_BITBANG (1u << 0)
#define JTAG_ENGINE_FLAG_PIO     (1u << 1)
#define JTAG_ENGINE_FLAG_DMA     (1u << 2)
#define JTAG_ENGINE_FLAG_PIO_FAST (1u << 3)

#define JTAG_ENGINE_MIN_HALF_PERIOD_US 1u
#define JTAG_ENGINE_MAX_HALF_PERIOD_US 100u

#define JTAG_ENGINE_MIN_PIO_TCK_HZ 50000u
#define JTAG_ENGINE_MAX_PIO_TCK_HZ 0xffffffffu

#define JTAG_ENGINE_DMA_CHUNK_2048_BITS  2048u
#define JTAG_ENGINE_DMA_CHUNK_4096_BITS  4096u
#define JTAG_ENGINE_DMA_CHUNK_8192_BITS  8192u
#define JTAG_ENGINE_DMA_CHUNK_16384_BITS 16384u
#define JTAG_ENGINE_DMA_CHUNK_32768_BITS 32768u

bool jtag_engine_init(void);

bool jtag_engine_select(JtagEngineType_t type);

JtagEngineType_t jtag_engine_get_active(void);

uint8_t jtag_engine_get_supported_flags(void);

void jtag_engine_set_half_period_us(uint32_t half_period_us);
uint32_t jtag_engine_get_half_period_us(void);

bool jtag_engine_set_pio_frequency_hz(uint32_t requested_hz, uint32_t *actual_hz);
uint32_t jtag_engine_get_pio_frequency_hz(void);
uint32_t jtag_engine_get_requested_pio_frequency_hz(void);
uint32_t jtag_engine_get_pio_cycles_per_bit(void);
uint32_t jtag_engine_get_maximum_pio_frequency_hz(void);

bool jtag_engine_set_dma_chunk_bits(uint32_t chunk_bits);
uint32_t jtag_engine_get_dma_chunk_bits(void);
uint32_t jtag_engine_get_max_dma_chunk_bits(void);

bool jtag_engine_shift_bits(uint32_t bit_count,
                            const uint8_t *tms_bits,
                            const uint8_t *tdi_bits,
                            uint8_t *tdo_bits);

void jtag_engine_tap_reset(void);

#endif
