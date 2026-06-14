#include "jtag_profile.h"

#include "pico/time.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    uint32_t count;
    uint64_t total_us;
    uint64_t min_us;
    uint64_t max_us;
} ProfileTimeStat_t;

typedef struct {
    bool enabled;
    bool shift_active;
    uint32_t active_bit_count;
    uint32_t active_chunk_count;
    uint64_t active_tx_prepare_us;
    uint64_t active_dma_pio_us;
    uint64_t active_tdo_pack_us;

    uint32_t logical_shifts;
    uint32_t successful_shifts;
    uint64_t total_bits;
    uint32_t dma_chunks;
    uint32_t max_chunks_per_shift;
    uint32_t dma_timeouts;
    uint32_t pio_recoveries;
    uint32_t usb_rx_waits;
    uint32_t usb_tx_waits;
    uint32_t usb_rx_read_calls;
    uint64_t usb_rx_bytes;
    uint32_t usb_rx_max_batch;
    uint32_t usb_tx_write_calls;
    uint64_t usb_tx_bytes;
    uint32_t usb_tx_max_batch;
    uint32_t usb_tx_flush_calls;

    ProfileTimeStat_t request_parse;
    ProfileTimeStat_t tx_prepare;
    ProfileTimeStat_t dma_pio;
    ProfileTimeStat_t tdo_pack;
    ProfileTimeStat_t response_queue;
    ProfileTimeStat_t shift_total;
} ProfileState_t;

static ProfileState_t profile;

uint64_t jtag_profile_now_us(void)
{
    return to_us_since_boot(get_absolute_time());
}

static void stat_add(ProfileTimeStat_t *stat, uint64_t value)
{
    stat->count++;
    stat->total_us += value;
    if (stat->count == 1u || value < stat->min_us) {
        stat->min_us = value;
    }
    if (value > stat->max_us) {
        stat->max_us = value;
    }
}

bool jtag_profile_is_enabled(void)
{
    return profile.enabled;
}

void jtag_profile_set_enabled(bool enabled)
{
    profile.enabled = enabled;
    profile.shift_active = false;
}

void jtag_profile_clear(void)
{
    bool enabled = profile.enabled;
    memset(&profile, 0, sizeof(profile));
    profile.enabled = enabled;
}

void jtag_profile_begin_shift(uint32_t bit_count)
{
    if (!profile.enabled) {
        return;
    }

    profile.shift_active = true;
    profile.active_bit_count = bit_count;
    profile.active_chunk_count = 0u;
    profile.active_tx_prepare_us = 0u;
    profile.active_dma_pio_us = 0u;
    profile.active_tdo_pack_us = 0u;
}

void jtag_profile_add_tx_prepare_us(uint64_t value)
{
    if (profile.enabled && profile.shift_active) {
        profile.active_tx_prepare_us += value;
    }
}

void jtag_profile_add_dma_pio_us(uint64_t value)
{
    if (profile.enabled && profile.shift_active) {
        profile.active_dma_pio_us += value;
    }
}

void jtag_profile_add_tdo_pack_us(uint64_t value)
{
    if (profile.enabled && profile.shift_active) {
        profile.active_tdo_pack_us += value;
    }
}

void jtag_profile_add_dma_chunk(void)
{
    if (profile.enabled && profile.shift_active) {
        profile.active_chunk_count++;
    }
}

void jtag_profile_add_dma_timeout(void)
{
    if (profile.enabled) {
        profile.dma_timeouts++;
    }
}

void jtag_profile_add_pio_recovery(void)
{
    if (profile.enabled) {
        profile.pio_recoveries++;
    }
}

void jtag_profile_add_usb_rx_wait(void)
{
    if (profile.enabled) {
        profile.usb_rx_waits++;
    }
}

void jtag_profile_add_usb_tx_wait(void)
{
    if (profile.enabled) {
        profile.usb_tx_waits++;
    }
}

void jtag_profile_add_usb_rx_batch(uint32_t bytes)
{
    if (profile.enabled) {
        profile.usb_rx_read_calls++;
        profile.usb_rx_bytes += bytes;
        if (bytes > profile.usb_rx_max_batch) {
            profile.usb_rx_max_batch = bytes;
        }
    }
}

void jtag_profile_add_usb_tx_batch(uint32_t bytes)
{
    if (profile.enabled) {
        profile.usb_tx_write_calls++;
        profile.usb_tx_bytes += bytes;
        if (bytes > profile.usb_tx_max_batch) {
            profile.usb_tx_max_batch = bytes;
        }
    }
}

void jtag_profile_add_usb_tx_flush(void)
{
    if (profile.enabled) {
        profile.usb_tx_flush_calls++;
    }
}

void jtag_profile_finish_shift(bool success,
                               uint64_t request_parse_us,
                               uint64_t response_queue_us,
                               uint64_t shift_total_us)
{
    if (!profile.enabled || !profile.shift_active) {
        return;
    }

    profile.logical_shifts++;
    if (success) {
        profile.successful_shifts++;
    }
    profile.total_bits += profile.active_bit_count;
    profile.dma_chunks += profile.active_chunk_count;
    if (profile.active_chunk_count > profile.max_chunks_per_shift) {
        profile.max_chunks_per_shift = profile.active_chunk_count;
    }

    stat_add(&profile.request_parse, request_parse_us);
    stat_add(&profile.tx_prepare, profile.active_tx_prepare_us);
    stat_add(&profile.dma_pio, profile.active_dma_pio_us);
    stat_add(&profile.tdo_pack, profile.active_tdo_pack_us);
    stat_add(&profile.response_queue, response_queue_us);
    stat_add(&profile.shift_total, shift_total_us);
    profile.shift_active = false;
}

static uint64_t stat_average(const ProfileTimeStat_t *stat)
{
    if (stat->count == 0u) {
        return 0u;
    }
    return stat->total_us / stat->count;
}

static void append_text(char *buffer, size_t buffer_size, size_t *offset, const char *format, ...)
{
    if (*offset >= buffer_size) {
        return;
    }

    va_list args;
    va_start(args, format);
    int wrote = vsnprintf(buffer + *offset, buffer_size - *offset, format, args);
    va_end(args);

    if (wrote < 0) {
        return;
    }

    size_t available = buffer_size - *offset;
    if ((size_t)wrote >= available) {
        *offset = buffer_size;
    } else {
        *offset += (size_t)wrote;
    }
}

static void append_stat(char *buffer,
                        size_t buffer_size,
                        size_t *offset,
                        const char *name,
                        const ProfileTimeStat_t *stat)
{
    append_text(buffer, buffer_size, offset,
                "  %s: count=%lu total_us=%llu avg_us=%llu min_us=%llu max_us=%llu\r\n",
                name,
                (unsigned long)stat->count,
                (unsigned long long)stat->total_us,
                (unsigned long long)stat_average(stat),
                (unsigned long long)stat->min_us,
                (unsigned long long)stat->max_us);
}

size_t jtag_profile_format(char *buffer, size_t buffer_size)
{
    size_t offset = 0u;
    uint32_t average_chunks = 0u;
    uint64_t effective_rate = 0u;

    if (buffer_size == 0u) {
        return 0u;
    }

    if (profile.logical_shifts > 0u) {
        average_chunks = profile.dma_chunks / profile.logical_shifts;
    }
    if (profile.shift_total.total_us > 0u) {
        effective_rate = (profile.total_bits * 1000000ull) / profile.shift_total.total_us;
    }

    append_text(buffer, buffer_size, &offset, "Firmware performance profile\r\n");
    append_text(buffer, buffer_size, &offset, "  enabled=%u\r\n", profile.enabled ? 1u : 0u);
    append_text(buffer, buffer_size, &offset, "  logical_shifts=%lu\r\n", (unsigned long)profile.logical_shifts);
    append_text(buffer, buffer_size, &offset, "  successful_shifts=%lu\r\n", (unsigned long)profile.successful_shifts);
    append_text(buffer, buffer_size, &offset, "  total_bits=%llu\r\n", (unsigned long long)profile.total_bits);
    append_text(buffer, buffer_size, &offset, "  effective_rate_bit_s=%llu\r\n", (unsigned long long)effective_rate);
    append_text(buffer, buffer_size, &offset, "  dma_chunks=%lu\r\n", (unsigned long)profile.dma_chunks);
    append_text(buffer, buffer_size, &offset, "  average_chunks_per_shift=%lu\r\n", (unsigned long)average_chunks);
    append_text(buffer, buffer_size, &offset, "  max_chunks_per_shift=%lu\r\n", (unsigned long)profile.max_chunks_per_shift);
    append_text(buffer, buffer_size, &offset, "  dma_timeouts=%lu\r\n", (unsigned long)profile.dma_timeouts);
    append_text(buffer, buffer_size, &offset, "  pio_recoveries=%lu\r\n", (unsigned long)profile.pio_recoveries);
    append_text(buffer, buffer_size, &offset, "  usb_rx_incomplete_waits=%lu\r\n", (unsigned long)profile.usb_rx_waits);
    append_text(buffer, buffer_size, &offset, "  usb_tx_space_waits=%lu\r\n", (unsigned long)profile.usb_tx_waits);
    append_text(buffer, buffer_size, &offset, "  usb_rx_read_calls=%lu\r\n", (unsigned long)profile.usb_rx_read_calls);
    append_text(buffer, buffer_size, &offset, "  usb_rx_bytes=%llu\r\n", (unsigned long long)profile.usb_rx_bytes);
    append_text(buffer, buffer_size, &offset, "  usb_rx_max_batch=%lu\r\n", (unsigned long)profile.usb_rx_max_batch);
    append_text(buffer, buffer_size, &offset, "  usb_tx_write_calls=%lu\r\n", (unsigned long)profile.usb_tx_write_calls);
    append_text(buffer, buffer_size, &offset, "  usb_tx_bytes=%llu\r\n", (unsigned long long)profile.usb_tx_bytes);
    append_text(buffer, buffer_size, &offset, "  usb_tx_max_batch=%lu\r\n", (unsigned long)profile.usb_tx_max_batch);
    append_text(buffer, buffer_size, &offset, "  usb_tx_flush_calls=%lu\r\n", (unsigned long)profile.usb_tx_flush_calls);
    append_text(buffer, buffer_size, &offset, "Timing stats are integer microseconds; profiling off skips per-stage time reads.\r\n");
    append_stat(buffer, buffer_size, &offset, "request_parse_us", &profile.request_parse);
    append_stat(buffer, buffer_size, &offset, "tx_prepare_us", &profile.tx_prepare);
    append_stat(buffer, buffer_size, &offset, "dma_pio_us", &profile.dma_pio);
    append_stat(buffer, buffer_size, &offset, "tdo_pack_us", &profile.tdo_pack);
    append_stat(buffer, buffer_size, &offset, "response_queue_us", &profile.response_queue);
    append_stat(buffer, buffer_size, &offset, "shift_total_us", &profile.shift_total);

    if (offset >= buffer_size) {
        buffer[buffer_size - 1u] = '\0';
        return buffer_size - 1u;
    }

    buffer[offset] = '\0';
    return offset;
}
