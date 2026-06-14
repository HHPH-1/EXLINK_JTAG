/*
 * TinyUSB configuration for the Exlink RP2040 JTAG bridge build.
 *
 * This mirrors the Pico SDK stdio USB configuration, but leaves CDC FIFO sizes
 * overridable by target compile definitions so Stage 5.6 can use larger CDC
 * buffers without changing descriptors or the COM-port interface.
 */

#ifndef _EXLINK_TUSB_CONFIG_H
#define _EXLINK_TUSB_CONFIG_H

#include "pico/stdio_usb.h"

#if !defined(LIB_TINYUSB_HOST) && !defined(LIB_TINYUSB_DEVICE)
#define CFG_TUSB_RHPORT0_MODE   (OPT_MODE_DEVICE)

#define CFG_TUD_CDC             (1)
#ifndef CFG_TUD_CDC_RX_BUFSIZE
#define CFG_TUD_CDC_RX_BUFSIZE  (256)
#endif
#ifndef CFG_TUD_CDC_TX_BUFSIZE
#define CFG_TUD_CDC_TX_BUFSIZE  (256)
#endif

#if !PICO_STDIO_USB_RESET_INTERFACE_SUPPORT_MS_OS_20_DESCRIPTOR
#define CFG_TUD_VENDOR            (0)
#else
#define CFG_TUD_VENDOR            (1)
#define CFG_TUD_VENDOR_RX_BUFSIZE  (256)
#define CFG_TUD_VENDOR_TX_BUFSIZE  (256)
#endif
#endif

#endif
