#ifndef EXLINK_JTAG_BRIDGE_USB_CDC_TRANSPORT_H
#define EXLINK_JTAG_BRIDGE_USB_CDC_TRANSPORT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

bool usb_cdc_read_exact(uint8_t *buffer, size_t length, uint32_t timeout_ms);
bool usb_cdc_write_all(const uint8_t *buffer, size_t length, uint32_t timeout_ms);

#endif
