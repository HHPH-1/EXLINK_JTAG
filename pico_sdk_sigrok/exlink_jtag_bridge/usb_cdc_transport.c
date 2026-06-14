#include "usb_cdc_transport.h"

#include "jtag_profile.h"
#include "jtag_protocol.h"

#include "pico/time.h"
#include "tusb.h"

bool usb_cdc_read_exact(uint8_t *buffer, size_t length, uint32_t timeout_ms)
{
    size_t offset = 0;
    uint32_t start_ms = to_ms_since_boot(get_absolute_time());

    while (offset < length) {
        tud_task();

        if (!tud_ready()) {
            return false;
        }

        uint32_t available = tud_cdc_available();
        if (available > 0u) {
            uint32_t remaining = (uint32_t)(length - offset);
            uint32_t to_read = available < remaining ? available : remaining;
            uint32_t read = tud_cdc_read(buffer + offset, to_read);
            if (read > 0u) {
                jtag_profile_add_usb_rx_batch(read);
            }
            offset += read;
            start_ms = to_ms_since_boot(get_absolute_time());
            continue;
        }

        uint32_t now_ms = to_ms_since_boot(get_absolute_time());
        if ((uint32_t)(now_ms - start_ms) >= timeout_ms) {
            return false;
        }

        jtag_profile_add_usb_rx_wait();
        tight_loop_contents();
    }

    return true;
}

bool usb_cdc_write_all(const uint8_t *buffer, size_t length, uint32_t timeout_ms)
{
    size_t offset = 0;
    uint32_t start_ms = to_ms_since_boot(get_absolute_time());

    while (offset < length) {
        tud_task();

        if (!tud_ready()) {
            return false;
        }

        uint32_t available = tud_cdc_write_available();
        if (available > 0u) {
            uint32_t remaining = (uint32_t)(length - offset);
            uint32_t to_write = available < remaining ? available : remaining;
            uint32_t wrote = tud_cdc_write(buffer + offset, to_write);
#if EXLINK_JTAG_USE_BATCHED_CDC
            if (wrote > 0u) {
                jtag_profile_add_usb_tx_batch(wrote);
                offset += wrote;
                start_ms = to_ms_since_boot(get_absolute_time());
                continue;
            }
#else
            if (wrote > 0u) {
                jtag_profile_add_usb_tx_batch(wrote);
            }
            tud_cdc_write_flush();
            jtag_profile_add_usb_tx_flush();
            offset += wrote;
            start_ms = to_ms_since_boot(get_absolute_time());
            if (wrote > 0u) {
                continue;
            }
#endif
        }

        tud_cdc_write_flush();
        jtag_profile_add_usb_tx_flush();
        uint32_t now_ms = to_ms_since_boot(get_absolute_time());
        if ((uint32_t)(now_ms - start_ms) >= timeout_ms) {
            return false;
        }

        jtag_profile_add_usb_tx_wait();
        tight_loop_contents();
    }

    tud_cdc_write_flush();
    jtag_profile_add_usb_tx_flush();
    return true;
}
