#include "jtag_pio.h"

#include "jtag_protocol.h"
#include "jtag_engine.h"
#include "jtag_profile.h"
#include "exlink_rp2040.h"

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/pio.h"
#include "hardware/pio_instructions.h"
#include "pico/stdlib.h"
#include "pico/time.h"

#include <string.h>

#define EXLINK_JTAG_PIO_INSTANCE pio0

/*
 * Safe timing is preserved:
 *   out pins, 4  -> TMS/TDI valid while TCK is low
 *   nop
 *   out pins, 4  -> TCK high
 *   in pins, 1   -> sample TDO while TCK is high
 *   jmp x-- loop -> next bit
 *
 * The safe loop consumes 5 PIO cycles per JTAG bit. The experimental fast
 * loop removes the low-phase nop and consumes 4 cycles per bit. Both use:
 *   TCK_Hz = clk_sys / (clkdiv * cycles_per_bit)
 */
#define EXLINK_JTAG_PIO_SAFE_CYCLES_PER_BIT 5u
#define EXLINK_JTAG_PIO_FAST_CYCLES_PER_BIT 4u
#define EXLINK_JTAG_PIO_DEFAULT_TCK_HZ 500000u

/*
 * One 32-bit TX FIFO word carries four JTAG bits because each JTAG bit is
 * encoded as two 4-bit pin nibbles. RX autopush emits one 32-bit word per
 * 32 samples, plus the explicit final push for the last partial word.
 * Buffers are statically sized for the largest runtime-selectable chunk.
 */
#ifdef EXLINK_JTAG_MAX_DMA_CHUNK_BITS
#define EXLINK_JTAG_PIO_MAX_DMA_CHUNK_BITS EXLINK_JTAG_MAX_DMA_CHUNK_BITS
#else
#define EXLINK_JTAG_PIO_MAX_DMA_CHUNK_BITS JTAG_ENGINE_DMA_CHUNK_32768_BITS
#endif
#define EXLINK_JTAG_PIO_TX_WORDS_PER_CHUNK ((EXLINK_JTAG_PIO_MAX_DMA_CHUNK_BITS + 3u) / 4u)
#define EXLINK_JTAG_PIO_TX_DMA_WORDS (1u + EXLINK_JTAG_PIO_TX_WORDS_PER_CHUNK)
#define EXLINK_JTAG_PIO_RX_WORDS_PER_CHUNK ((EXLINK_JTAG_PIO_MAX_DMA_CHUNK_BITS / 32u) + 1u)

#define EXLINK_JTAG_PIO_SAFE_PROGRAM_LENGTH 11u
#define EXLINK_JTAG_PIO_FAST_PROGRAM_LENGTH 10u
#define EXLINK_JTAG_PIO_SAFE_OFFSET 0u
#define EXLINK_JTAG_PIO_FAST_OFFSET 11u
#define EXLINK_JTAG_PIO_WRAP_TARGET 0u
#define EXLINK_JTAG_PIO_SAFE_LOOP_TARGET 4u
#define EXLINK_JTAG_PIO_FAST_LOOP_TARGET 15u
#define EXLINK_JTAG_PIO_SAFE_WRAP 10u
#define EXLINK_JTAG_PIO_FAST_WRAP 9u

static PIO pio = EXLINK_JTAG_PIO_INSTANCE;
static int sm = -1;
static int tx_dma_channel = -1;
static int rx_dma_channel = -1;
static uint safe_offset = 0u;
static uint fast_offset = 0u;
static bool fast_program_loaded = false;
static bool active_fast_engine = false;
static uint32_t requested_frequency_hz = EXLINK_JTAG_PIO_DEFAULT_TCK_HZ;
static uint32_t actual_frequency_hz = 0u;
static uint32_t dma_chunk_bits =
#if EXLINK_JTAG_USE_LARGE_DMA_CHUNK
    JTAG_ENGINE_DMA_CHUNK_8192_BITS;
#else
    JTAG_ENGINE_DMA_CHUNK_2048_BITS;
#endif
static uint32_t tx_dma_words[EXLINK_JTAG_PIO_TX_DMA_WORDS];
static uint32_t rx_dma_words[EXLINK_JTAG_PIO_RX_WORDS_PER_CHUNK];
static dma_channel_config tx_dma_config;
static dma_channel_config rx_dma_config;
static bool dma_configs_initialized = false;
static uint16_t exlink_jtag_safe_instructions[EXLINK_JTAG_PIO_SAFE_PROGRAM_LENGTH];
static uint16_t exlink_jtag_fast_instructions[EXLINK_JTAG_PIO_FAST_PROGRAM_LENGTH];
static pio_program_t exlink_jtag_safe_program = {
    .instructions = exlink_jtag_safe_instructions,
    .length = EXLINK_JTAG_PIO_SAFE_PROGRAM_LENGTH,
    .origin = -1,
    .pio_version = 0,
};
static pio_program_t exlink_jtag_fast_program = {
    .instructions = exlink_jtag_fast_instructions,
    .length = EXLINK_JTAG_PIO_FAST_PROGRAM_LENGTH,
    .origin = -1,
    .pio_version = 0,
};
static bool instructions_initialized = false;

static void jtag_pio_configure_dma_channels(void);

static const uint32_t jtag_tx_tms_lut[16] = {
    0x00000000u, 0x00000011u, 0x00001100u, 0x00001111u,
    0x00110000u, 0x00110011u, 0x00111100u, 0x00111111u,
    0x11000000u, 0x11000011u, 0x11001100u, 0x11001111u,
    0x11110000u, 0x11110011u, 0x11111100u, 0x11111111u,
};

static const uint32_t jtag_tx_tdi_lut[16] = {
    0x00000000u, 0x00000088u, 0x00008800u, 0x00008888u,
    0x00880000u, 0x00880088u, 0x00888800u, 0x00888888u,
    0x88000000u, 0x88000088u, 0x88008800u, 0x88008888u,
    0x88880000u, 0x88880088u, 0x88888800u, 0x88888888u,
};

static void jtag_pio_init_program_instructions(void)
{
    if (instructions_initialized) {
        return;
    }

    exlink_jtag_safe_instructions[0] = (uint16_t)pio_encode_pull(false, true);
    exlink_jtag_safe_instructions[1] = (uint16_t)pio_encode_mov(pio_x, pio_osr);
    exlink_jtag_safe_instructions[2] = (uint16_t)pio_encode_out(pio_null, 32);
    exlink_jtag_safe_instructions[3] = (uint16_t)pio_encode_pull(false, true);
    exlink_jtag_safe_instructions[4] = (uint16_t)pio_encode_out(pio_pins, 4);
    exlink_jtag_safe_instructions[5] = (uint16_t)pio_encode_nop();
    exlink_jtag_safe_instructions[6] = (uint16_t)pio_encode_out(pio_pins, 4);
    exlink_jtag_safe_instructions[7] = (uint16_t)pio_encode_in(pio_pins, 1);
    exlink_jtag_safe_instructions[8] = (uint16_t)pio_encode_jmp_x_dec(EXLINK_JTAG_PIO_SAFE_LOOP_TARGET);
    exlink_jtag_safe_instructions[9] = (uint16_t)pio_encode_set(pio_pins, 0);
    exlink_jtag_safe_instructions[10] = (uint16_t)pio_encode_push(false, true);

    exlink_jtag_fast_instructions[0] = (uint16_t)pio_encode_pull(false, true);
    exlink_jtag_fast_instructions[1] = (uint16_t)pio_encode_mov(pio_x, pio_osr);
    exlink_jtag_fast_instructions[2] = (uint16_t)pio_encode_out(pio_null, 32);
    exlink_jtag_fast_instructions[3] = (uint16_t)pio_encode_pull(false, true);
    exlink_jtag_fast_instructions[4] = (uint16_t)pio_encode_out(pio_pins, 4);
    exlink_jtag_fast_instructions[5] = (uint16_t)pio_encode_out(pio_pins, 4);
    exlink_jtag_fast_instructions[6] = (uint16_t)pio_encode_in(pio_pins, 1);
    exlink_jtag_fast_instructions[7] = (uint16_t)pio_encode_jmp_x_dec(EXLINK_JTAG_PIO_FAST_LOOP_TARGET);
    exlink_jtag_fast_instructions[8] = (uint16_t)pio_encode_set(pio_pins, 0);
    exlink_jtag_fast_instructions[9] = (uint16_t)pio_encode_push(false, true);
    instructions_initialized = true;
}

static uint8_t get_packed_bit(const uint8_t *buffer, uint32_t bit_index)
{
    return (uint8_t)((buffer[bit_index >> 3] >> (bit_index & 7u)) & 1u);
}

static void set_packed_bit(uint8_t *buffer, uint32_t bit_index, uint8_t value)
{
    if (value) {
        buffer[bit_index >> 3] |= (uint8_t)(1u << (bit_index & 7u));
    }
}

static void put_u32_le(uint8_t *buffer, uint32_t value)
{
    buffer[0] = (uint8_t)(value & 0xffu);
    buffer[1] = (uint8_t)((value >> 8) & 0xffu);
    buffer[2] = (uint8_t)((value >> 16) & 0xffu);
    buffer[3] = (uint8_t)((value >> 24) & 0xffu);
}

static void pio_drive_idle(uint32_t tms)
{
    const uint32_t output_mask = (1u << EXLINK_JTAG_TMS_GPIO) |
                                 (1u << EXLINK_JTAG_TCK_GPIO) |
                                 (1u << EXLINK_JTAG_TDI_GPIO);
    const uint32_t output_value = tms ? (1u << EXLINK_JTAG_TMS_GPIO) : 0u;
    pio_sm_set_pins_with_mask(pio, (uint)sm, output_value, output_mask);
}

static uint32_t jtag_pio_pack_tx_chunk_reference(const uint8_t *tms_bits,
                                                 const uint8_t *tdi_bits,
                                                 uint32_t source_bit_offset,
                                                 uint32_t bit_count)
{
    const uint32_t tx_word_count = (bit_count + 3u) / 4u;

    if (tx_word_count > EXLINK_JTAG_PIO_TX_WORDS_PER_CHUNK) {
        return 0u;
    }

    memset(tx_dma_words, 0, (tx_word_count + 1u) * sizeof(tx_dma_words[0]));
    tx_dma_words[0] = bit_count - 1u;

    for (uint32_t bit = 0; bit < bit_count; ++bit) {
        const uint32_t source_bit = source_bit_offset + bit;
        const uint32_t tms = get_packed_bit(tms_bits, source_bit);
        const uint32_t tdi = get_packed_bit(tdi_bits, source_bit);
        const uint32_t low_nibble = (tms << 0) | (tdi << 3);
        const uint32_t high_nibble = low_nibble | (1u << 1);
        const uint32_t shift = (bit & 3u) * 8u;

        tx_dma_words[1u + (bit >> 2)] |= low_nibble << shift;
        tx_dma_words[1u + (bit >> 2)] |= high_nibble << (shift + 4u);
    }

    return tx_word_count + 1u;
}

static uint32_t jtag_pio_pack_tx_chunk_fast(const uint8_t *tms_bits,
                                            const uint8_t *tdi_bits,
                                            uint32_t source_bit_offset,
                                            uint32_t bit_count)
{
    if ((source_bit_offset & 7u) != 0u) {
        return jtag_pio_pack_tx_chunk_reference(tms_bits, tdi_bits, source_bit_offset, bit_count);
    }

    const uint32_t tx_word_count = (bit_count + 3u) / 4u;
    if (tx_word_count > EXLINK_JTAG_PIO_TX_WORDS_PER_CHUNK) {
        return 0u;
    }

    memset(tx_dma_words, 0, (tx_word_count + 1u) * sizeof(tx_dma_words[0]));
    tx_dma_words[0] = bit_count - 1u;

    const uint32_t source_byte_offset = source_bit_offset >> 3;
    const uint32_t full_groups = bit_count >> 2;
    for (uint32_t group = 0; group < full_groups; ++group) {
        const uint32_t bit_in_byte = (group & 1u) ? 4u : 0u;
        const uint32_t byte_index = source_byte_offset + (group >> 1);
        const uint32_t tms_nibble = (tms_bits[byte_index] >> bit_in_byte) & 0x0fu;
        const uint32_t tdi_nibble = (tdi_bits[byte_index] >> bit_in_byte) & 0x0fu;

        tx_dma_words[1u + group] = 0x20202020u |
                                   jtag_tx_tms_lut[tms_nibble] |
                                   jtag_tx_tdi_lut[tdi_nibble];
    }

    const uint32_t tail_bits = bit_count & 3u;
    if (tail_bits != 0u) {
        uint32_t tail_word = 0u;
        const uint32_t tail_base = full_groups << 2;
        for (uint32_t bit = 0; bit < tail_bits; ++bit) {
            const uint32_t source_bit = source_bit_offset + tail_base + bit;
            const uint32_t tms = get_packed_bit(tms_bits, source_bit);
            const uint32_t tdi = get_packed_bit(tdi_bits, source_bit);
            const uint32_t low_nibble = (tms << 0) | (tdi << 3);
            const uint32_t high_nibble = low_nibble | (1u << 1);
            const uint32_t shift = bit * 8u;

            tail_word |= low_nibble << shift;
            tail_word |= high_nibble << (shift + 4u);
        }
        tx_dma_words[1u + full_groups] = tail_word;
    }

    return tx_word_count + 1u;
}

static uint32_t jtag_pio_pack_tx_chunk(const uint8_t *tms_bits,
                                       const uint8_t *tdi_bits,
                                       uint32_t source_bit_offset,
                                       uint32_t bit_count)
{
#if EXLINK_JTAG_USE_FAST_TX_PACK
    return jtag_pio_pack_tx_chunk_fast(tms_bits, tdi_bits, source_bit_offset, bit_count);
#else
    return jtag_pio_pack_tx_chunk_reference(tms_bits, tdi_bits, source_bit_offset, bit_count);
#endif
}

static void jtag_pio_unpack_rx_chunk_reference(uint32_t bit_count,
                                               uint8_t *tdo_bits,
                                               uint32_t target_bit_offset)
{
    for (uint32_t bit = 0; bit < bit_count; ++bit) {
        const uint32_t word_index = bit / 32u;
        const uint32_t bit_in_word = bit & 31u;
        const uint32_t bits_left = bit_count - (word_index * 32u);
        const uint32_t valid_bits = bits_left > 32u ? 32u : bits_left;
        const uint32_t rx_bit = (valid_bits == 32u) ?
                                bit_in_word :
                                (32u - valid_bits + bit_in_word);

        set_packed_bit(tdo_bits,
                       target_bit_offset + bit,
                       (uint8_t)((rx_dma_words[word_index] >> rx_bit) & 1u));
    }
}

static void jtag_pio_unpack_rx_chunk_fast(uint32_t bit_count,
                                          uint8_t *tdo_bits,
                                          uint32_t target_bit_offset)
{
    if ((target_bit_offset & 7u) != 0u) {
        jtag_pio_unpack_rx_chunk_reference(bit_count, tdo_bits, target_bit_offset);
        return;
    }

    const uint32_t target_byte_offset = target_bit_offset >> 3;
    const uint32_t full_words = bit_count >> 5;
    for (uint32_t word = 0; word < full_words; ++word) {
        put_u32_le(&tdo_bits[target_byte_offset + (word << 2)], rx_dma_words[word]);
    }

    const uint32_t remaining_bits = bit_count & 31u;
    if (remaining_bits != 0u) {
        const uint32_t rx_word = rx_dma_words[full_words];
        const uint32_t valid_base = 32u - remaining_bits;
        const uint32_t target_base = target_bit_offset + (full_words << 5);
        for (uint32_t bit = 0; bit < remaining_bits; ++bit) {
            set_packed_bit(tdo_bits,
                           target_base + bit,
                           (uint8_t)((rx_word >> (valid_base + bit)) & 1u));
        }
    }
}

static void jtag_pio_unpack_rx_chunk(uint32_t bit_count,
                                     uint8_t *tdo_bits,
                                     uint32_t target_bit_offset)
{
#if EXLINK_JTAG_USE_FAST_RX_PACK
    jtag_pio_unpack_rx_chunk_fast(bit_count, tdo_bits, target_bit_offset);
#else
    jtag_pio_unpack_rx_chunk_reference(bit_count, tdo_bits, target_bit_offset);
#endif
}

static void jtag_pio_clear_irqs(void)
{
    for (uint irq = 0u; irq < 8u; ++irq) {
        pio_interrupt_clear(pio, irq);
    }
}

static void jtag_pio_abort_dma_if_busy(void)
{
    if (tx_dma_channel >= 0 && dma_channel_is_busy((uint)tx_dma_channel)) {
        dma_channel_abort((uint)tx_dma_channel);
    }

    if (rx_dma_channel >= 0 && dma_channel_is_busy((uint)rx_dma_channel)) {
        dma_channel_abort((uint)rx_dma_channel);
    }
}

static void jtag_pio_recover_after_error(void)
{
    jtag_profile_add_pio_recovery();
    jtag_pio_abort_dma_if_busy();
    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_sm_clear_fifos(pio, (uint)sm);
    pio_sm_restart(pio, (uint)sm);
    jtag_pio_clear_irqs();
    pio_drive_idle(0u);
    if (tx_dma_channel >= 0 && rx_dma_channel >= 0) {
        jtag_pio_configure_dma_channels();
    }
}

static void jtag_pio_begin_logical_shift(void)
{
    jtag_pio_abort_dma_if_busy();
    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_sm_clear_fifos(pio, (uint)sm);
    pio_sm_restart(pio, (uint)sm);
    jtag_pio_clear_irqs();
    pio_drive_idle(1u);
}

static void jtag_pio_finish_logical_shift(void)
{
    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_drive_idle(0u);
}

static bool jtag_pio_claim_dma_channels(void)
{
    if (tx_dma_channel < 0) {
        tx_dma_channel = dma_claim_unused_channel(false);
        if (tx_dma_channel < 0) {
            return false;
        }
    }

    if (rx_dma_channel < 0) {
        rx_dma_channel = dma_claim_unused_channel(false);
        if (rx_dma_channel < 0) {
            dma_channel_unclaim((uint)tx_dma_channel);
            tx_dma_channel = -1;
            return false;
        }
    }

    return true;
}

static void jtag_pio_configure_dma_channels(void)
{
    tx_dma_config = dma_channel_get_default_config((uint)tx_dma_channel);
    channel_config_set_transfer_data_size(&tx_dma_config, DMA_SIZE_32);
    channel_config_set_read_increment(&tx_dma_config, true);
    channel_config_set_write_increment(&tx_dma_config, false);
    channel_config_set_dreq(&tx_dma_config, pio_get_dreq(pio, (uint)sm, true));

    rx_dma_config = dma_channel_get_default_config((uint)rx_dma_channel);
    channel_config_set_transfer_data_size(&rx_dma_config, DMA_SIZE_32);
    channel_config_set_read_increment(&rx_dma_config, false);
    channel_config_set_write_increment(&rx_dma_config, true);
    channel_config_set_dreq(&rx_dma_config, pio_get_dreq(pio, (uint)sm, false));

    dma_configs_initialized = true;
}

static void jtag_pio_configure_sm(float divider)
{
    const uint program_offset = active_fast_engine ? fast_offset : safe_offset;
    const uint program_wrap = active_fast_engine ?
                              EXLINK_JTAG_PIO_FAST_WRAP :
                              EXLINK_JTAG_PIO_SAFE_WRAP;
    pio_sm_config config = pio_get_default_sm_config();
    sm_config_set_wrap(&config,
                       program_offset + EXLINK_JTAG_PIO_WRAP_TARGET,
                       program_offset + program_wrap);
    sm_config_set_out_pins(&config, EXLINK_JTAG_TMS_GPIO, 4);
    sm_config_set_set_pins(&config, EXLINK_JTAG_TMS_GPIO, 4);
    sm_config_set_in_pins(&config, EXLINK_JTAG_TDO_GPIO);
    sm_config_set_out_shift(&config, true, true, 32);
    sm_config_set_in_shift(&config, true, true, 32);
    sm_config_set_clkdiv(&config, divider);

    pio_sm_init(pio, (uint)sm, program_offset, &config);
}

static bool jtag_pio_calculate_divider(uint32_t requested_hz,
                                       float *divider,
                                       uint32_t *actual_hz)
{
    const uint32_t max_hz = jtag_pio_get_maximum_frequency_hz();
    if ((requested_hz < JTAG_ENGINE_MIN_PIO_TCK_HZ) ||
        (requested_hz > max_hz)) {
        return false;
    }

    const uint32_t sys_hz = clock_get_hz(clk_sys);
    const float calculated = (float)sys_hz /
                             ((float)requested_hz * (float)jtag_pio_get_cycles_per_bit());

    if (calculated < 1.0f || calculated > 65535.0f) {
        return false;
    }

    if (divider) {
        *divider = calculated;
    }
    if (actual_hz) {
        *actual_hz = (uint32_t)((float)sys_hz /
                                (calculated * (float)jtag_pio_get_cycles_per_bit()));
    }

    return true;
}

bool jtag_pio_set_frequency_hz(uint32_t requested_hz, uint32_t *actual_hz)
{
    float divider = 0.0f;
    uint32_t measured_hz = 0u;

    if (!jtag_pio_calculate_divider(requested_hz, &divider, &measured_hz)) {
        if (actual_hz) {
            *actual_hz = actual_frequency_hz;
        }
        return false;
    }

    requested_frequency_hz = requested_hz;
    actual_frequency_hz = measured_hz;

    if (sm >= 0) {
        pio_sm_set_enabled(pio, (uint)sm, false);
        pio_sm_clear_fifos(pio, (uint)sm);
        pio_sm_restart(pio, (uint)sm);
        jtag_pio_clear_irqs();
        pio_drive_idle(0u);
        jtag_pio_configure_sm(divider);
        pio_sm_clear_fifos(pio, (uint)sm);
        pio_sm_restart(pio, (uint)sm);
        pio_drive_idle(0u);
    }

    if (actual_hz) {
        *actual_hz = actual_frequency_hz;
    }

    return true;
}

uint32_t jtag_pio_get_frequency_hz(void)
{
    return actual_frequency_hz;
}

uint32_t jtag_pio_get_requested_frequency_hz(void)
{
    return requested_frequency_hz;
}

uint32_t jtag_pio_get_cycles_per_bit(void)
{
    return active_fast_engine ?
           EXLINK_JTAG_PIO_FAST_CYCLES_PER_BIT :
           EXLINK_JTAG_PIO_SAFE_CYCLES_PER_BIT;
}

uint32_t jtag_pio_get_maximum_frequency_hz(void)
{
    return clock_get_hz(clk_sys) / jtag_pio_get_cycles_per_bit();
}

bool jtag_pio_set_dma_chunk_bits(uint32_t chunk_bits)
{
    switch (chunk_bits) {
    case JTAG_ENGINE_DMA_CHUNK_2048_BITS:
    case JTAG_ENGINE_DMA_CHUNK_4096_BITS:
    case JTAG_ENGINE_DMA_CHUNK_8192_BITS:
    case JTAG_ENGINE_DMA_CHUNK_16384_BITS:
    case JTAG_ENGINE_DMA_CHUNK_32768_BITS:
        if (chunk_bits > EXLINK_JTAG_PIO_MAX_DMA_CHUNK_BITS) {
            return false;
        }
        dma_chunk_bits = chunk_bits;
        return true;
    default:
        return false;
    }
}

uint32_t jtag_pio_get_dma_chunk_bits(void)
{
    return dma_chunk_bits;
}

uint32_t jtag_pio_get_max_dma_chunk_bits(void)
{
    return EXLINK_JTAG_PIO_MAX_DMA_CHUNK_BITS;
}

bool jtag_pio_fast_engine_available(void)
{
    return fast_program_loaded;
}

bool jtag_pio_select_fast_engine(bool fast)
{
    if (fast && !fast_program_loaded) {
        return false;
    }

    active_fast_engine = fast;
    return jtag_pio_reconfigure();
}

bool jtag_pio_reconfigure(void)
{
    if (sm < 0) {
        return false;
    }

    pio_sm_set_enabled(pio, (uint)sm, false);

    pio_gpio_init(pio, EXLINK_JTAG_TMS_GPIO);
    pio_gpio_init(pio, EXLINK_JTAG_TCK_GPIO);
    pio_gpio_init(pio, EXLINK_JTAG_TDO_GPIO);
    pio_gpio_init(pio, EXLINK_JTAG_TDI_GPIO);

    pio_drive_idle(1u);
    pio_sm_set_consecutive_pindirs(pio, (uint)sm, EXLINK_JTAG_TMS_GPIO, 2, true);
    pio_sm_set_consecutive_pindirs(pio, (uint)sm, EXLINK_JTAG_TDO_GPIO, 1, false);
    pio_sm_set_consecutive_pindirs(pio, (uint)sm, EXLINK_JTAG_TDI_GPIO, 1, true);
    gpio_disable_pulls(EXLINK_JTAG_TDO_GPIO);

    if (!jtag_pio_set_frequency_hz(requested_frequency_hz, NULL)) {
        return false;
    }

    pio_drive_idle(1u);
    pio_sm_clear_fifos(pio, (uint)sm);
    pio_sm_restart(pio, (uint)sm);
    jtag_pio_clear_irqs();
    jtag_pio_configure_dma_channels();
    return true;
}

bool jtag_pio_init(void)
{
    if (sm < 0) {
        jtag_pio_init_program_instructions();

        if (!pio_can_add_program_at_offset(pio, &exlink_jtag_safe_program, EXLINK_JTAG_PIO_SAFE_OFFSET)) {
            return false;
        }

        sm = pio_claim_unused_sm(pio, false);
        if (sm < 0) {
            return false;
        }

        pio_add_program_at_offset(pio, &exlink_jtag_safe_program, EXLINK_JTAG_PIO_SAFE_OFFSET);
        safe_offset = EXLINK_JTAG_PIO_SAFE_OFFSET;
        if (pio_can_add_program_at_offset(pio, &exlink_jtag_fast_program, EXLINK_JTAG_PIO_FAST_OFFSET)) {
            pio_add_program_at_offset(pio, &exlink_jtag_fast_program, EXLINK_JTAG_PIO_FAST_OFFSET);
            fast_offset = EXLINK_JTAG_PIO_FAST_OFFSET;
            fast_program_loaded = true;
        } else {
            fast_program_loaded = false;
        }
    }

    if (!jtag_pio_claim_dma_channels()) {
        return false;
    }

    return jtag_pio_reconfigure();
}

void jtag_pio_deinit(void)
{
    if (sm >= 0) {
        jtag_pio_abort_dma_if_busy();
        pio_sm_set_enabled(pio, (uint)sm, false);
        pio_sm_clear_fifos(pio, (uint)sm);
        pio_sm_restart(pio, (uint)sm);
        jtag_pio_clear_irqs();
        pio_drive_idle(1u);
    }
}

static bool jtag_pio_shift_dma_chunk(const uint8_t *tms_bits,
                                     const uint8_t *tdi_bits,
                                     uint8_t *tdo_bits,
                                     uint32_t source_bit_offset,
                                     uint32_t bit_count)
{
    if ((bit_count == 0u) || (bit_count > dma_chunk_bits)) {
        return false;
    }

    bool profiling = jtag_profile_is_enabled();
    uint64_t stage_start_us = 0u;
    if (profiling) {
        stage_start_us = jtag_profile_now_us();
    }

    const uint32_t tx_transfer_count = jtag_pio_pack_tx_chunk(tms_bits,
                                                              tdi_bits,
                                                              source_bit_offset,
                                                              bit_count);
    if (profiling) {
        jtag_profile_add_tx_prepare_us(jtag_profile_now_us() - stage_start_us);
    }

    const uint32_t rx_transfer_count = (bit_count / 32u) + 1u;

    if ((tx_transfer_count == 0u) ||
        (rx_transfer_count > EXLINK_JTAG_PIO_RX_WORDS_PER_CHUNK)) {
        return false;
    }

    memset(rx_dma_words, 0, rx_transfer_count * sizeof(rx_dma_words[0]));

    if (profiling) {
        stage_start_us = jtag_profile_now_us();
        jtag_profile_add_dma_chunk();
    }

#if EXLINK_JTAG_USE_REDUCED_CHUNK_RESET
    if (!dma_configs_initialized) {
        jtag_pio_configure_dma_channels();
    }
#else
    jtag_pio_begin_logical_shift();
    jtag_pio_configure_dma_channels();
#endif

    dma_channel_configure((uint)rx_dma_channel,
                          &rx_dma_config,
                          rx_dma_words,
                          &pio->rxf[sm],
                          rx_transfer_count,
                          true);

    dma_channel_configure((uint)tx_dma_channel,
                          &tx_dma_config,
                          &pio->txf[sm],
                          tx_dma_words,
                          tx_transfer_count,
                          true);

    pio_sm_set_enabled(pio, (uint)sm, true);

    const uint32_t tck_hz = actual_frequency_hz > 0u ? actual_frequency_hz : EXLINK_JTAG_PIO_DEFAULT_TCK_HZ;
    const uint64_t expected_us = ((uint64_t)bit_count * 1000000ull) / tck_hz;
    const uint64_t timeout_us = expected_us * 8ull + 50000ull;
    const uint64_t bounded_timeout_us = timeout_us < 100000ull ? 100000ull : timeout_us;
    absolute_time_t start = get_absolute_time();
    while (dma_channel_is_busy((uint)tx_dma_channel) ||
           dma_channel_is_busy((uint)rx_dma_channel)) {
        if (absolute_time_diff_us(start, get_absolute_time()) >
            (int64_t)bounded_timeout_us) {
            jtag_profile_add_dma_timeout();
            jtag_pio_recover_after_error();
            return false;
        }

        tight_loop_contents();
    }

    pio_sm_set_enabled(pio, (uint)sm, false);
#if !EXLINK_JTAG_USE_REDUCED_CHUNK_RESET
    pio_drive_idle(0u);
#endif

    if (profiling) {
        jtag_profile_add_dma_pio_us(jtag_profile_now_us() - stage_start_us);
    }

    if (dma_channel_is_busy((uint)tx_dma_channel) ||
        dma_channel_is_busy((uint)rx_dma_channel) ||
        !pio_sm_is_rx_fifo_empty(pio, (uint)sm)) {
        jtag_pio_recover_after_error();
        return false;
    }

    if (profiling) {
        stage_start_us = jtag_profile_now_us();
    }
    jtag_pio_unpack_rx_chunk(bit_count, tdo_bits, source_bit_offset);
    if (profiling) {
        jtag_profile_add_tdo_pack_us(jtag_profile_now_us() - stage_start_us);
    }
    return true;
}

bool jtag_pio_shift_bits(uint32_t bit_count,
                         const uint8_t *tms_bits,
                         const uint8_t *tdi_bits,
                         uint8_t *tdo_bits)
{
    if (sm < 0 || tx_dma_channel < 0 || rx_dma_channel < 0 ||
        bit_count == 0u || bit_count > EXLINK_JTAG_MAX_SHIFT_BITS ||
        !tms_bits || !tdi_bits || !tdo_bits) {
        return false;
    }

    memset(tdo_bits, 0, (bit_count + 7u) / 8u);

#if EXLINK_JTAG_USE_REDUCED_CHUNK_RESET
    jtag_pio_begin_logical_shift();
#endif

    uint32_t bit_offset = 0u;
    while (bit_offset < bit_count) {
        const uint32_t remaining = bit_count - bit_offset;
        const uint32_t chunk_bits = remaining > dma_chunk_bits ?
                                    dma_chunk_bits :
                                    remaining;

        if (!jtag_pio_shift_dma_chunk(tms_bits,
                                      tdi_bits,
                                      tdo_bits,
                                      bit_offset,
                                      chunk_bits)) {
            return false;
        }

        bit_offset += chunk_bits;
    }

#if EXLINK_JTAG_USE_REDUCED_CHUNK_RESET
    jtag_pio_finish_logical_shift();
#endif

    return true;
}

void jtag_pio_tap_reset(void)
{
    static const uint8_t tms_reset = 0x3fu;
    static const uint8_t tdi_reset = 0x00u;
    uint8_t tdo_unused = 0u;

    (void)jtag_pio_shift_bits(7u, &tms_reset, &tdi_reset, &tdo_unused);
}
