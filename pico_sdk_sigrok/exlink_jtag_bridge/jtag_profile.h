#ifndef EXLINK_JTAG_BRIDGE_JTAG_PROFILE_H
#define EXLINK_JTAG_BRIDGE_JTAG_PROFILE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    JTAG_PROFILE_ACTION_SHOW = 0,
    JTAG_PROFILE_ACTION_CLEAR = 1,
    JTAG_PROFILE_ACTION_ON = 2,
    JTAG_PROFILE_ACTION_OFF = 3
} JtagProfileAction_t;

bool jtag_profile_is_enabled(void);
void jtag_profile_set_enabled(bool enabled);
void jtag_profile_clear(void);

uint64_t jtag_profile_now_us(void);
void jtag_profile_begin_shift(uint32_t bit_count);
void jtag_profile_add_tx_prepare_us(uint64_t value);
void jtag_profile_add_dma_pio_us(uint64_t value);
void jtag_profile_add_tdo_pack_us(uint64_t value);
void jtag_profile_add_dma_chunk(void);
void jtag_profile_add_dma_timeout(void);
void jtag_profile_add_pio_recovery(void);
void jtag_profile_add_usb_rx_wait(void);
void jtag_profile_add_usb_tx_wait(void);
void jtag_profile_add_usb_rx_batch(uint32_t bytes);
void jtag_profile_add_usb_tx_batch(uint32_t bytes);
void jtag_profile_add_usb_tx_flush(void);
void jtag_profile_finish_shift(bool success,
                               uint64_t request_parse_us,
                               uint64_t response_queue_us,
                               uint64_t shift_total_us);

size_t jtag_profile_format(char *buffer, size_t buffer_size);

#endif
