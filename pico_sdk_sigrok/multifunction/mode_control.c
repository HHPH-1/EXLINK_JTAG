#include "mode_control.h"

#include "hardware/watchdog.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"
#include "tusb.h"

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CONTROL_PREFIX "@EXLINK:"
#define CONTROL_BUFFER_BYTES 48u
#define CONTROL_WRITE_TIMEOUT_US 250000u

static char control_buffer[CONTROL_BUFFER_BYTES];
static size_t control_length;
static bool control_active;

static const char *mode_name(ExlinkMode mode)
{
    return mode == EXLINK_MODE_JTAG ? "JTAG" : "SCOPE";
}

static void control_write_all(const char *text)
{
    const uint8_t *buffer = (const uint8_t *)text;
    size_t remaining = strlen(text);
    absolute_time_t deadline = make_timeout_time_us(CONTROL_WRITE_TIMEOUT_US);

    while (remaining > 0u && !time_reached(deadline)) {
        tud_task();
        if (!tud_ready()) {
            continue;
        }

        uint32_t available = tud_cdc_write_available();
        if (available == 0u) {
            tud_cdc_write_flush();
            continue;
        }

        uint32_t chunk = remaining < available ? (uint32_t)remaining : available;
        uint32_t wrote = tud_cdc_write(buffer, chunk);
        if (wrote > 0u) {
            buffer += wrote;
            remaining -= wrote;
        }
        tud_cdc_write_flush();
    }

    tud_cdc_write_flush();
    for (uint32_t i = 0; i < 20u; ++i) {
        tud_task();
        sleep_ms(1);
    }
}

static void switch_mode(ExlinkMode mode)
{
    exlink_mode_request_next_boot(mode);
    sleep_ms(50);
    watchdog_reboot(0, 0, 0);
    while (true) {
        tight_loop_contents();
    }
}

static void handle_control_line(ExlinkMode current_mode, bool busy)
{
    if (strcmp(control_buffer, "@EXLINK:INFO") == 0) {
        char response[96];
        snprintf(response, sizeof(response), "@EXLINK:OK:INFO:%s:%s\n",
                 EXLINK_MULTIFUNCTION_FW_VERSION, mode_name(current_mode));
        control_write_all(response);
        return;
    }

    if (strcmp(control_buffer, "@EXLINK:MODE?") == 0) {
        char response[32];
        snprintf(response, sizeof(response), "@EXLINK:OK:MODE:%s\n", mode_name(current_mode));
        control_write_all(response);
        return;
    }

    if (strcmp(control_buffer, "@EXLINK:CAPS?") == 0) {
        control_write_all("@EXLINK:OK:CAPS:SCOPE,JTAG,XVC,PIO,DMA\n");
        return;
    }

    if (strcmp(control_buffer, "@EXLINK:MODE:JTAG") == 0) {
        if (current_mode == EXLINK_MODE_JTAG) {
            control_write_all("@EXLINK:OK:MODE:JTAG\n");
        } else if (busy) {
            control_write_all("@EXLINK:ERR:BUSY\n");
        } else {
            control_write_all("@EXLINK:OK:SWITCHING:JTAG\n");
            switch_mode(EXLINK_MODE_JTAG);
        }
        return;
    }

    if (strcmp(control_buffer, "@EXLINK:MODE:SCOPE") == 0) {
        if (current_mode == EXLINK_MODE_SCOPE) {
            control_write_all("@EXLINK:OK:MODE:SCOPE\n");
        } else if (busy) {
            control_write_all("@EXLINK:ERR:BUSY\n");
        } else {
            control_write_all("@EXLINK:OK:SWITCHING:SCOPE\n");
            switch_mode(EXLINK_MODE_SCOPE);
        }
        return;
    }

    if (strcmp(control_buffer, "@EXLINK:BOOTSEL:CONFIRM") == 0) {
        control_write_all("@EXLINK:OK:SWITCHING:BOOTSEL\n");
        sleep_ms(50);
        rom_reset_usb_boot(0, 0);
        while (true) {
            tight_loop_contents();
        }
    }

    control_write_all("@EXLINK:ERR:BAD_COMMAND\n");
}

void exlink_mode_control_reset_parser(void)
{
    control_active = false;
    control_length = 0u;
    control_buffer[0] = '\0';
}

bool exlink_mode_control_feed_char(uint8_t ch,
                                   ExlinkMode current_mode,
                                   bool busy,
                                   bool command_boundary)
{
    if (!control_active) {
        if (!command_boundary || ch != (uint8_t)'@') {
            return false;
        }
        control_active = true;
        control_length = 0u;
    }

    if (ch == (uint8_t)'\r') {
        return true;
    }

    if (ch == (uint8_t)'\n') {
        control_buffer[control_length] = '\0';
        control_active = false;
        if (strncmp(control_buffer, CONTROL_PREFIX, strlen(CONTROL_PREFIX)) == 0) {
            handle_control_line(current_mode, busy);
        } else {
            control_write_all("@EXLINK:ERR:BAD_COMMAND\n");
        }
        control_length = 0u;
        return true;
    }

    if (control_length + 1u >= sizeof(control_buffer)) {
        exlink_mode_control_reset_parser();
        control_write_all("@EXLINK:ERR:BAD_COMMAND\n");
        return true;
    }

    control_buffer[control_length++] = (char)ch;
    return true;
}
