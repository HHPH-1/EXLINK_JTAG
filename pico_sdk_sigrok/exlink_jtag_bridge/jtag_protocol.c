#include "jtag_protocol.h"

#include "jtag_engine.h"
#include "jtag_profile.h"
#include "usb_cdc_transport.h"
#include "hardware/clocks.h"
#include "tusb.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define USB_IO_TIMEOUT_MS 1000u

#define RESP_STATUS_OK        0u
#define RESP_STATUS_BAD_LEN   1u
#define RESP_STATUS_TIMEOUT   2u
#define RESP_STATUS_EXEC_FAIL 3u

#define ENGINE_STATUS_OK          0u
#define ENGINE_STATUS_BAD_PARAM   1u
#define ENGINE_STATUS_INIT_FAIL   2u
#define ENGINE_STATUS_SWITCH_FAIL 3u

#define ERROR_INVALID_COMMAND 1u
#define ERROR_TIMEOUT         2u
#define PROFILE_TEXT_BUFFER_BYTES 4096u
#define INFO_TEXT_BUFFER_BYTES 1024u

static uint8_t tms_buffer[EXLINK_JTAG_MAX_SHIFT_BYTES];
static uint8_t tdi_buffer[EXLINK_JTAG_MAX_SHIFT_BYTES];
static uint8_t tdo_buffer[EXLINK_JTAG_MAX_SHIFT_BYTES];
static char profile_text_buffer[PROFILE_TEXT_BUFFER_BYTES];
static char info_text_buffer[INFO_TEXT_BUFFER_BYTES];

static void put_u16_le(uint8_t *buffer, uint16_t value)
{
    buffer[0] = (uint8_t)(value & 0xffu);
    buffer[1] = (uint8_t)((value >> 8) & 0xffu);
}

static void put_u32_le(uint8_t *buffer, uint32_t value)
{
    buffer[0] = (uint8_t)(value & 0xffu);
    buffer[1] = (uint8_t)((value >> 8) & 0xffu);
    buffer[2] = (uint8_t)((value >> 16) & 0xffu);
    buffer[3] = (uint8_t)((value >> 24) & 0xffu);
}

static uint32_t get_u32_le(const uint8_t *buffer)
{
    return (uint32_t)buffer[0] |
           ((uint32_t)buffer[1] << 8) |
           ((uint32_t)buffer[2] << 16) |
           ((uint32_t)buffer[3] << 24);
}

static bool write_error(uint8_t error_code)
{
    uint8_t response[2] = {'e', error_code};
    return usb_cdc_write_all(response, sizeof(response), USB_IO_TIMEOUT_MS);
}

static bool write_shift_status(uint8_t status, uint32_t bit_count, bool include_tdo)
{
    uint8_t header[6];
    header[0] = 's';
    header[1] = status;
    put_u32_le(&header[2], bit_count);

    if (!usb_cdc_write_all(header, sizeof(header), USB_IO_TIMEOUT_MS)) {
        return false;
    }

    if (include_tdo) {
        return usb_cdc_write_all(tdo_buffer, (bit_count + 7u) / 8u, USB_IO_TIMEOUT_MS);
    }

    return true;
}

static bool write_engine_status(uint8_t status)
{
    uint8_t response[3];
    response[0] = 'm';
    response[1] = status;
    response[2] = (uint8_t)jtag_engine_get_active();
    return usb_cdc_write_all(response, sizeof(response), USB_IO_TIMEOUT_MS);
}

static bool write_profile_response(uint8_t status, const char *text, uint16_t length)
{
    uint8_t header[5];
    header[0] = 'r';
    header[1] = status;
    header[2] = jtag_profile_is_enabled() ? 1u : 0u;
    put_u16_le(&header[3], length);

    if (!usb_cdc_write_all(header, sizeof(header), USB_IO_TIMEOUT_MS)) {
        return false;
    }

    if (length > 0u) {
        return usb_cdc_write_all((const uint8_t *)text, length, USB_IO_TIMEOUT_MS);
    }

    return true;
}

static void handle_info(void)
{
    const char *active_engine = "bitbang";
    switch (jtag_engine_get_active()) {
    case JTAG_ENGINE_PIO:
        active_engine = "pio_safe";
        break;
    case JTAG_ENGINE_PIO_FAST:
        active_engine = "pio_fast";
        break;
    case JTAG_ENGINE_BITBANG:
    default:
        active_engine = "bitbang";
        break;
    }

    int wrote = snprintf(info_text_buffer,
                         sizeof(info_text_buffer),
                         "Firmware version: EXLINK-RP2040-JTAG-MAXSPEED v0.4\r\n"
                         "Protocol version: 4\r\n"
                         "xosc_hz: %lu\r\n"
                         "clk_sys_hz: %lu\r\n"
                         "clk_usb_hz: %lu\r\n"
                         "Active engine: %s\r\n"
                         "PIO cycles per bit: %lu\r\n"
                         "Minimum TCK: %lu\r\n"
                         "Maximum theoretical TCK: %lu\r\n"
                         "Maximum allowed TCK: %lu\r\n"
                         "Requested TCK: %lu\r\n"
                         "Actual TCK: %lu\r\n"
                         "DMA chunk bits: %lu\r\n"
                         "Maximum DMA chunk bits: %lu\r\n"
                         "Maximum Shift bits: %lu\r\n",
                         (unsigned long)clock_get_hz(clk_ref),
                         (unsigned long)clock_get_hz(clk_sys),
                         (unsigned long)clock_get_hz(clk_usb),
                         active_engine,
                         (unsigned long)jtag_engine_get_pio_cycles_per_bit(),
                         (unsigned long)JTAG_ENGINE_MIN_PIO_TCK_HZ,
                         (unsigned long)jtag_engine_get_maximum_pio_frequency_hz(),
                         (unsigned long)jtag_engine_get_maximum_pio_frequency_hz(),
                         (unsigned long)jtag_engine_get_requested_pio_frequency_hz(),
                         (unsigned long)jtag_engine_get_pio_frequency_hz(),
                         (unsigned long)jtag_engine_get_dma_chunk_bits(),
                         (unsigned long)jtag_engine_get_max_dma_chunk_bits(),
                         (unsigned long)EXLINK_JTAG_MAX_SHIFT_BITS);
    if (wrote < 0) {
        wrote = 0;
    }
    if ((size_t)wrote >= sizeof(info_text_buffer)) {
        wrote = (int)sizeof(info_text_buffer) - 1;
    }

    uint8_t header[3];
    header[0] = 'i';
    put_u16_le(&header[1], (uint16_t)wrote);

    (void)usb_cdc_write_all(header, sizeof(header), USB_IO_TIMEOUT_MS);
    (void)usb_cdc_write_all((const uint8_t *)info_text_buffer, (size_t)wrote, USB_IO_TIMEOUT_MS);
}

static void handle_reset(void)
{
    uint8_t response[2] = {'t', RESP_STATUS_OK};
    jtag_engine_tap_reset();
    (void)usb_cdc_write_all(response, sizeof(response), USB_IO_TIMEOUT_MS);
}

static void handle_shift(void)
{
    bool profiling = jtag_profile_is_enabled();
    uint64_t shift_start_us = 0u;
    uint64_t request_parse_us = 0u;

    if (profiling) {
        shift_start_us = jtag_profile_now_us();
    }

    uint8_t length_buffer[4];
    if (!usb_cdc_read_exact(length_buffer, sizeof(length_buffer), USB_IO_TIMEOUT_MS)) {
        (void)write_error(ERROR_TIMEOUT);
        return;
    }

    uint32_t bit_count = get_u32_le(length_buffer);
    if ((bit_count == 0u) || (bit_count > EXLINK_JTAG_MAX_SHIFT_BITS)) {
        (void)write_shift_status(RESP_STATUS_BAD_LEN, bit_count, false);
        return;
    }

    uint32_t byte_count = (bit_count + 7u) / 8u;
    if (!usb_cdc_read_exact(tms_buffer, byte_count, USB_IO_TIMEOUT_MS) ||
        !usb_cdc_read_exact(tdi_buffer, byte_count, USB_IO_TIMEOUT_MS)) {
        (void)write_shift_status(RESP_STATUS_TIMEOUT, bit_count, false);
        return;
    }

    if (profiling) {
        request_parse_us = jtag_profile_now_us() - shift_start_us;
        jtag_profile_begin_shift(bit_count);
    }

    if (!jtag_engine_shift_bits(bit_count, tms_buffer, tdi_buffer, tdo_buffer)) {
        uint64_t response_queue_us = 0u;
        if (profiling) {
            uint64_t response_start_us = jtag_profile_now_us();
            (void)write_shift_status(RESP_STATUS_EXEC_FAIL, bit_count, false);
            response_queue_us = jtag_profile_now_us() - response_start_us;
            jtag_profile_finish_shift(false,
                                      request_parse_us,
                                      response_queue_us,
                                      jtag_profile_now_us() - shift_start_us);
        } else {
            (void)write_shift_status(RESP_STATUS_EXEC_FAIL, bit_count, false);
        }
        return;
    }

    if (profiling) {
        uint64_t response_start_us = jtag_profile_now_us();
        (void)write_shift_status(RESP_STATUS_OK, bit_count, true);
        uint64_t response_queue_us = jtag_profile_now_us() - response_start_us;
        jtag_profile_finish_shift(true,
                                  request_parse_us,
                                  response_queue_us,
                                  jtag_profile_now_us() - shift_start_us);
    } else {
        (void)write_shift_status(RESP_STATUS_OK, bit_count, true);
    }
}

static void handle_clock(void)
{
    uint8_t value_buffer[4];
    uint8_t response[6];

    if (!usb_cdc_read_exact(value_buffer, sizeof(value_buffer), USB_IO_TIMEOUT_MS)) {
        (void)write_error(ERROR_TIMEOUT);
        return;
    }

    uint32_t requested = get_u32_le(value_buffer);
    uint8_t status = RESP_STATUS_OK;

    if ((requested < JTAG_ENGINE_MIN_HALF_PERIOD_US) ||
        (requested > JTAG_ENGINE_MAX_HALF_PERIOD_US)) {
        status = RESP_STATUS_BAD_LEN;
    } else {
        jtag_engine_set_half_period_us(requested);
    }

    response[0] = 'k';
    response[1] = status;
    put_u32_le(&response[2], jtag_engine_get_half_period_us());
    (void)usb_cdc_write_all(response, sizeof(response), USB_IO_TIMEOUT_MS);
}

static void handle_pio_clock(void)
{
    uint8_t value_buffer[4];
    uint8_t response[6];

    if (!usb_cdc_read_exact(value_buffer, sizeof(value_buffer), USB_IO_TIMEOUT_MS)) {
        (void)write_error(ERROR_TIMEOUT);
        return;
    }

    uint32_t requested = get_u32_le(value_buffer);
    uint32_t actual = 0u;
    uint8_t status = RESP_STATUS_OK;

    if (!jtag_engine_set_pio_frequency_hz(requested, &actual)) {
        status = RESP_STATUS_BAD_LEN;
        actual = jtag_engine_get_pio_frequency_hz();
    }

    response[0] = 'p';
    response[1] = status;
    put_u32_le(&response[2], actual);
    (void)usb_cdc_write_all(response, sizeof(response), USB_IO_TIMEOUT_MS);
}

static void handle_engine_select(void)
{
    uint8_t engine = 0u;
    uint8_t status = ENGINE_STATUS_OK;

    if (!usb_cdc_read_exact(&engine, sizeof(engine), USB_IO_TIMEOUT_MS)) {
        (void)write_error(ERROR_TIMEOUT);
        return;
    }

    if (engine > (uint8_t)JTAG_ENGINE_PIO_FAST) {
        (void)write_engine_status(ENGINE_STATUS_BAD_PARAM);
        return;
    }

    if (!jtag_engine_select((JtagEngineType_t)engine)) {
        status = (engine == (uint8_t)JTAG_ENGINE_PIO) ?
                 ENGINE_STATUS_INIT_FAIL :
                 ENGINE_STATUS_SWITCH_FAIL;
    }

    (void)write_engine_status(status);
}

static void handle_dma_chunk(void)
{
    uint8_t value_buffer[4];
    uint8_t response[6];

    if (!usb_cdc_read_exact(value_buffer, sizeof(value_buffer), USB_IO_TIMEOUT_MS)) {
        (void)write_error(ERROR_TIMEOUT);
        return;
    }

    uint32_t requested = get_u32_le(value_buffer);
    uint8_t status = RESP_STATUS_OK;
    if (!jtag_engine_set_dma_chunk_bits(requested)) {
        status = RESP_STATUS_BAD_LEN;
    }

    response[0] = 'd';
    response[1] = status;
    put_u32_le(&response[2], jtag_engine_get_dma_chunk_bits());
    (void)usb_cdc_write_all(response, sizeof(response), USB_IO_TIMEOUT_MS);
}

static void handle_capabilities(void)
{
    uint8_t response[8];
    response[0] = 'q';
    response[1] = RESP_STATUS_OK;
    response[2] = (uint8_t)jtag_engine_get_active();
    response[3] = jtag_engine_get_supported_flags();
    put_u32_le(&response[4], EXLINK_JTAG_MAX_SHIFT_BITS);

    (void)usb_cdc_write_all(response, sizeof(response), USB_IO_TIMEOUT_MS);
}

static void handle_profile(void)
{
    uint8_t action = 0u;
    if (!usb_cdc_read_exact(&action, sizeof(action), USB_IO_TIMEOUT_MS)) {
        (void)write_error(ERROR_TIMEOUT);
        return;
    }

    uint8_t status = RESP_STATUS_OK;
    switch ((JtagProfileAction_t)action) {
    case JTAG_PROFILE_ACTION_SHOW:
        break;
    case JTAG_PROFILE_ACTION_CLEAR:
        jtag_profile_clear();
        break;
    case JTAG_PROFILE_ACTION_ON:
        jtag_profile_set_enabled(true);
        break;
    case JTAG_PROFILE_ACTION_OFF:
        jtag_profile_set_enabled(false);
        break;
    default:
        status = RESP_STATUS_BAD_LEN;
        break;
    }

    size_t length = jtag_profile_format(profile_text_buffer, sizeof(profile_text_buffer));
    if (length > 0xffffu) {
        length = 0xffffu;
    }
    (void)write_profile_response(status, profile_text_buffer, (uint16_t)length);
}

void jtag_protocol_init(void)
{
    memset(tms_buffer, 0, sizeof(tms_buffer));
    memset(tdi_buffer, 0, sizeof(tdi_buffer));
    memset(tdo_buffer, 0, sizeof(tdo_buffer));
}

void jtag_protocol_task(void)
{
    if (!tud_ready() || tud_cdc_available() == 0u) {
        return;
    }

    uint8_t command = 0;
    if (tud_cdc_read(&command, 1u) != 1u) {
        return;
    }

    switch (command) {
    case 'I':
        handle_info();
        break;
    case 'T':
        handle_reset();
        break;
    case 'S':
        handle_shift();
        break;
    case 'K':
        handle_clock();
        break;
    case 'P':
        handle_pio_clock();
        break;
    case 'M':
        handle_engine_select();
        break;
    case 'Q':
        handle_capabilities();
        break;
    case 'R':
        handle_profile();
        break;
    case 'D':
        handle_dma_chunk();
        break;
    default:
        (void)write_error(ERROR_INVALID_COMMAND);
        break;
    }
}
