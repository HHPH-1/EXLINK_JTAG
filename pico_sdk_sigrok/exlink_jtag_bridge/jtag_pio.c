#include "jtag_pio.h"

#include "jtag_protocol.h"
#include "jtag_engine.h"
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
#define EXLINK_JTAG_PIO_TIMEOUT_US 100000u

/*
 * Stage 3 timing is preserved:
 *   out pins, 4  -> TMS/TDI valid while TCK is low
 *   nop
 *   out pins, 4  -> TCK high
 *   in pins, 1   -> sample TDO while TCK is high
 *   jmp x-- loop -> next bit
 *
 * The loop consumes 5 PIO cycles per JTAG bit, so:
 *   TCK_Hz = clk_sys / (clkdiv * 5)
 */
#define EXLINK_JTAG_PIO_CYCLES_PER_BIT 5u
#define EXLINK_JTAG_PIO_DEFAULT_TCK_HZ 500000u

/*
 * One 32-bit TX FIFO word carries four JTAG bits because each JTAG bit is
 * encoded as two 4-bit pin nibbles. RX autopush emits one 32-bit word per
 * 32 samples, plus the explicit final push for the last partial word. For
 * a 2048-bit chunk the static DMA staging RAM is:
 *   TX: (1 count word + 512 data words) * 4 = 2052 bytes
 *   RX: (2048 / 32 + 1) words * 4          = 260 bytes
 */
#define EXLINK_JTAG_DMA_CHUNK_BITS 2048u
#define EXLINK_JTAG_PIO_TX_WORDS_PER_CHUNK ((EXLINK_JTAG_DMA_CHUNK_BITS + 3u) / 4u)
#define EXLINK_JTAG_PIO_TX_DMA_WORDS (1u + EXLINK_JTAG_PIO_TX_WORDS_PER_CHUNK)
#define EXLINK_JTAG_PIO_RX_WORDS_PER_CHUNK ((EXLINK_JTAG_DMA_CHUNK_BITS / 32u) + 1u)

#define EXLINK_JTAG_PIO_PROGRAM_LENGTH 11u
#define EXLINK_JTAG_PIO_WRAP_TARGET 0u
#define EXLINK_JTAG_PIO_LOOP_TARGET 4u
#define EXLINK_JTAG_PIO_WRAP 10u

static PIO pio = EXLINK_JTAG_PIO_INSTANCE;
static int sm = -1;
static int tx_dma_channel = -1;
static int rx_dma_channel = -1;
static uint offset = 0u;
static uint32_t requested_frequency_hz = EXLINK_JTAG_PIO_DEFAULT_TCK_HZ;
static uint32_t actual_frequency_hz = 0u;
static uint32_t tx_dma_words[EXLINK_JTAG_PIO_TX_DMA_WORDS];
static uint32_t rx_dma_words[EXLINK_JTAG_PIO_RX_WORDS_PER_CHUNK];
static uint16_t exlink_jtag_instructions[EXLINK_JTAG_PIO_PROGRAM_LENGTH];
static pio_program_t exlink_jtag_program = {
    .instructions = exlink_jtag_instructions,
    .length = EXLINK_JTAG_PIO_PROGRAM_LENGTH,
    .origin = -1,
    .pio_version = 0,
};
static bool instructions_initialized = false;

static void jtag_pio_init_program_instructions(void)
{
    if (instructions_initialized) {
        return;
    }

    exlink_jtag_instructions[0] = (uint16_t)pio_encode_pull(false, true);
    exlink_jtag_instructions[1] = (uint16_t)pio_encode_mov(pio_x, pio_osr);
    exlink_jtag_instructions[2] = (uint16_t)pio_encode_out(pio_null, 32);
    exlink_jtag_instructions[3] = (uint16_t)pio_encode_pull(false, true);
    exlink_jtag_instructions[4] = (uint16_t)pio_encode_out(pio_pins, 4);
    exlink_jtag_instructions[5] = (uint16_t)pio_encode_nop();
    exlink_jtag_instructions[6] = (uint16_t)pio_encode_out(pio_pins, 4);
    exlink_jtag_instructions[7] = (uint16_t)pio_encode_in(pio_pins, 1);
    exlink_jtag_instructions[8] = (uint16_t)pio_encode_jmp_x_dec(EXLINK_JTAG_PIO_LOOP_TARGET);
    exlink_jtag_instructions[9] = (uint16_t)pio_encode_set(pio_pins, 0);
    exlink_jtag_instructions[10] = (uint16_t)pio_encode_push(false, true);
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

static void pio_drive_idle(uint32_t tms)
{
    const uint32_t output_mask = (1u << EXLINK_JTAG_TMS_GPIO) |
                                 (1u << EXLINK_JTAG_TCK_GPIO) |
                                 (1u << EXLINK_JTAG_TDI_GPIO);
    const uint32_t output_value = tms ? (1u << EXLINK_JTAG_TMS_GPIO) : 0u;
    pio_sm_set_pins_with_mask(pio, (uint)sm, output_value, output_mask);
}

static uint32_t jtag_pio_pack_tx_chunk(const uint8_t *tms_bits,
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

static void jtag_pio_unpack_rx_chunk(uint32_t bit_count,
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
    jtag_pio_abort_dma_if_busy();
    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_sm_clear_fifos(pio, (uint)sm);
    pio_sm_restart(pio, (uint)sm);
    jtag_pio_clear_irqs();
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

static void jtag_pio_configure_sm(float divider)
{
    pio_sm_config config = pio_get_default_sm_config();
    sm_config_set_wrap(&config,
                       offset + EXLINK_JTAG_PIO_WRAP_TARGET,
                       offset + EXLINK_JTAG_PIO_WRAP);
    sm_config_set_out_pins(&config, EXLINK_JTAG_TMS_GPIO, 4);
    sm_config_set_set_pins(&config, EXLINK_JTAG_TMS_GPIO, 4);
    sm_config_set_in_pins(&config, EXLINK_JTAG_TDO_GPIO);
    sm_config_set_out_shift(&config, true, true, 32);
    sm_config_set_in_shift(&config, true, true, 32);
    sm_config_set_clkdiv(&config, divider);

    pio_sm_init(pio, (uint)sm, offset, &config);
}

static bool jtag_pio_calculate_divider(uint32_t requested_hz,
                                       float *divider,
                                       uint32_t *actual_hz)
{
    if ((requested_hz < JTAG_ENGINE_MIN_PIO_TCK_HZ) ||
        (requested_hz > JTAG_ENGINE_MAX_PIO_TCK_HZ)) {
        return false;
    }

    const uint32_t sys_hz = clock_get_hz(clk_sys);
    const float calculated = (float)sys_hz /
                             ((float)requested_hz * (float)EXLINK_JTAG_PIO_CYCLES_PER_BIT);

    if (calculated < 1.0f || calculated > 65535.0f) {
        return false;
    }

    if (divider) {
        *divider = calculated;
    }
    if (actual_hz) {
        *actual_hz = (uint32_t)((float)sys_hz /
                                (calculated * (float)EXLINK_JTAG_PIO_CYCLES_PER_BIT));
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
        jtag_pio_configure_sm(divider);
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
    return true;
}

bool jtag_pio_init(void)
{
    if (sm < 0) {
        jtag_pio_init_program_instructions();

        if (!pio_can_add_program(pio, &exlink_jtag_program)) {
            return false;
        }

        sm = pio_claim_unused_sm(pio, false);
        if (sm < 0) {
            return false;
        }

        offset = pio_add_program(pio, &exlink_jtag_program);
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
    if ((bit_count == 0u) || (bit_count > EXLINK_JTAG_DMA_CHUNK_BITS)) {
        return false;
    }

    const uint32_t tx_transfer_count = jtag_pio_pack_tx_chunk(tms_bits,
                                                              tdi_bits,
                                                              source_bit_offset,
                                                              bit_count);
    const uint32_t rx_transfer_count = (bit_count / 32u) + 1u;

    if ((tx_transfer_count == 0u) ||
        (rx_transfer_count > EXLINK_JTAG_PIO_RX_WORDS_PER_CHUNK)) {
        return false;
    }

    memset(rx_dma_words, 0, rx_transfer_count * sizeof(rx_dma_words[0]));

    jtag_pio_abort_dma_if_busy();
    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_sm_clear_fifos(pio, (uint)sm);
    pio_sm_restart(pio, (uint)sm);
    jtag_pio_clear_irqs();
    pio_drive_idle(1u);

    dma_channel_config rx_config = dma_channel_get_default_config((uint)rx_dma_channel);
    channel_config_set_transfer_data_size(&rx_config, DMA_SIZE_32);
    channel_config_set_read_increment(&rx_config, false);
    channel_config_set_write_increment(&rx_config, true);
    channel_config_set_dreq(&rx_config, pio_get_dreq(pio, (uint)sm, false));

    dma_channel_configure((uint)rx_dma_channel,
                          &rx_config,
                          rx_dma_words,
                          &pio->rxf[sm],
                          rx_transfer_count,
                          true);

    dma_channel_config tx_config = dma_channel_get_default_config((uint)tx_dma_channel);
    channel_config_set_transfer_data_size(&tx_config, DMA_SIZE_32);
    channel_config_set_read_increment(&tx_config, true);
    channel_config_set_write_increment(&tx_config, false);
    channel_config_set_dreq(&tx_config, pio_get_dreq(pio, (uint)sm, true));

    dma_channel_configure((uint)tx_dma_channel,
                          &tx_config,
                          &pio->txf[sm],
                          tx_dma_words,
                          tx_transfer_count,
                          true);

    pio_sm_set_enabled(pio, (uint)sm, true);

    absolute_time_t start = get_absolute_time();
    while (dma_channel_is_busy((uint)tx_dma_channel) ||
           dma_channel_is_busy((uint)rx_dma_channel)) {
        if (absolute_time_diff_us(start, get_absolute_time()) >
            (int64_t)EXLINK_JTAG_PIO_TIMEOUT_US) {
            jtag_pio_recover_after_error();
            return false;
        }

        tight_loop_contents();
    }

    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_drive_idle(0u);

    if (dma_channel_is_busy((uint)tx_dma_channel) ||
        dma_channel_is_busy((uint)rx_dma_channel) ||
        !pio_sm_is_rx_fifo_empty(pio, (uint)sm)) {
        jtag_pio_recover_after_error();
        return false;
    }

    jtag_pio_unpack_rx_chunk(bit_count, tdo_bits, source_bit_offset);
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

    uint32_t bit_offset = 0u;
    while (bit_offset < bit_count) {
        const uint32_t remaining = bit_count - bit_offset;
        const uint32_t chunk_bits = remaining > EXLINK_JTAG_DMA_CHUNK_BITS ?
                                    EXLINK_JTAG_DMA_CHUNK_BITS :
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

    return true;
}

void jtag_pio_tap_reset(void)
{
    static const uint8_t tms_reset = 0x3fu;
    static const uint8_t tdi_reset = 0x00u;
    uint8_t tdo_unused = 0u;

    (void)jtag_pio_shift_bits(7u, &tms_reset, &tdi_reset, &tdo_unused);
}
