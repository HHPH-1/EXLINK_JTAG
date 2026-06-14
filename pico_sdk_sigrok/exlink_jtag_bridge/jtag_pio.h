#ifndef EXLINK_JTAG_BRIDGE_JTAG_PIO_H
#define EXLINK_JTAG_BRIDGE_JTAG_PIO_H

#include <stdbool.h>
#include <stdint.h>

bool jtag_pio_init(void);
bool jtag_pio_reconfigure(void);
void jtag_pio_deinit(void);

bool jtag_pio_select_fast_engine(bool fast);
bool jtag_pio_fast_engine_available(void);

bool jtag_pio_set_frequency_hz(uint32_t requested_hz, uint32_t *actual_hz);
uint32_t jtag_pio_get_frequency_hz(void);
uint32_t jtag_pio_get_requested_frequency_hz(void);
uint32_t jtag_pio_get_cycles_per_bit(void);
uint32_t jtag_pio_get_maximum_frequency_hz(void);

bool jtag_pio_set_dma_chunk_bits(uint32_t chunk_bits);
uint32_t jtag_pio_get_dma_chunk_bits(void);
uint32_t jtag_pio_get_max_dma_chunk_bits(void);

bool jtag_pio_shift_bits(uint32_t bit_count,
                         const uint8_t *tms_bits,
                         const uint8_t *tdi_bits,
                         uint8_t *tdo_bits);

void jtag_pio_tap_reset(void);

#endif
