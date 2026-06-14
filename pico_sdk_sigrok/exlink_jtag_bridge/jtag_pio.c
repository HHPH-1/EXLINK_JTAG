#include "jtag_pio.h"

#include "jtag_protocol.h"
#include "exlink_rp2040.h"

#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "hardware/pio.h"
#include "hardware/pio_instructions.h"
#include "pico/stdlib.h"
#include "pico/time.h"

#include <string.h>

#define EXLINK_JTAG_PIO_INSTANCE pio0
#define EXLINK_JTAG_PIO_TIMEOUT_US 100000u
#define EXLINK_JTAG_PIO_CYCLES_PER_BIT 5u

#define EXLINK_JTAG_PIO_TX_WORDS ((EXLINK_JTAG_MAX_SHIFT_BITS + 3u) / 4u)
#define EXLINK_JTAG_PIO_RX_WORDS ((EXLINK_JTAG_MAX_SHIFT_BITS / 32u) + 1u)
#define EXLINK_JTAG_PIO_PROGRAM_LENGTH 11u
#define EXLINK_JTAG_PIO_WRAP_TARGET 0u
#define EXLINK_JTAG_PIO_LOOP_TARGET 4u
#define EXLINK_JTAG_PIO_WRAP 10u

static PIO pio = EXLINK_JTAG_PIO_INSTANCE;
static int sm = -1;
static uint offset = 0u;
static uint32_t actual_frequency_hz = 0u;
static uint32_t tx_words[EXLINK_JTAG_PIO_TX_WORDS];
static uint32_t rx_words[EXLINK_JTAG_PIO_RX_WORDS];
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

static uint32_t jtag_pio_pack_tx(uint32_t bit_count,
                                 const uint8_t *tms_bits,
                                 const uint8_t *tdi_bits,
                                 uint32_t *words,
                                 uint32_t word_capacity)
{
    const uint32_t word_count = (bit_count + 3u) / 4u;
    if (word_count > word_capacity) {
        return 0u;
    }

    memset(words, 0, word_count * sizeof(words[0]));

    for (uint32_t bit = 0; bit < bit_count; ++bit) {
        const uint32_t tms = get_packed_bit(tms_bits, bit);
        const uint32_t tdi = get_packed_bit(tdi_bits, bit);
        const uint32_t low_nibble = (tms << 0) | (tdi << 3);
        const uint32_t high_nibble = low_nibble | (1u << 1);
        const uint32_t shift = (bit & 3u) * 8u;

        words[bit >> 2] |= low_nibble << shift;
        words[bit >> 2] |= high_nibble << (shift + 4u);
    }

    return word_count;
}

static void jtag_pio_unpack_rx(uint32_t bit_count, uint8_t *tdo_bits)
{
    memset(tdo_bits, 0, (bit_count + 7u) / 8u);

    for (uint32_t bit = 0; bit < bit_count; ++bit) {
        const uint32_t word_index = bit / 32u;
        const uint32_t bit_in_word = bit & 31u;
        const uint32_t bits_left = bit_count - (word_index * 32u);
        const uint32_t valid_bits = bits_left > 32u ? 32u : bits_left;
        const uint32_t rx_bit = (valid_bits == 32u) ?
                                bit_in_word :
                                (32u - valid_bits + bit_in_word);

        set_packed_bit(tdo_bits, bit, (uint8_t)((rx_words[word_index] >> rx_bit) & 1u));
    }
}

static void jtag_pio_flush_rx_fifo(void)
{
    while (!pio_sm_is_rx_fifo_empty(pio, (uint)sm)) {
        (void)pio_sm_get(pio, (uint)sm);
    }
}

bool jtag_pio_set_frequency_hz(uint32_t requested_hz, uint32_t *actual_hz)
{
    if (requested_hz == 0u || sm < 0) {
        return false;
    }

    const uint32_t sys_hz = clock_get_hz(clk_sys);
    const float divider = (float)sys_hz /
                          ((float)requested_hz * (float)EXLINK_JTAG_PIO_CYCLES_PER_BIT);

    if (divider < 1.0f || divider > 65535.0f) {
        return false;
    }

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

    actual_frequency_hz = (uint32_t)((float)sys_hz /
                                     (divider * (float)EXLINK_JTAG_PIO_CYCLES_PER_BIT));
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

    uint32_t measured_hz = 0u;
    if (!jtag_pio_set_frequency_hz(1000000u, &measured_hz)) {
        return false;
    }

    pio_drive_idle(1u);
    pio_sm_clear_fifos(pio, (uint)sm);
    pio_sm_restart(pio, (uint)sm);
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

    return jtag_pio_reconfigure();
}

void jtag_pio_deinit(void)
{
    if (sm >= 0) {
        pio_sm_set_enabled(pio, (uint)sm, false);
        pio_sm_clear_fifos(pio, (uint)sm);
        pio_sm_restart(pio, (uint)sm);
        pio_drive_idle(1u);
    }
}

bool jtag_pio_shift_bits(uint32_t bit_count,
                         const uint8_t *tms_bits,
                         const uint8_t *tdi_bits,
                         uint8_t *tdo_bits)
{
    if (sm < 0 || bit_count == 0u || bit_count > EXLINK_JTAG_MAX_SHIFT_BITS ||
        !tms_bits || !tdi_bits || !tdo_bits) {
        return false;
    }

    const uint32_t tx_word_count = jtag_pio_pack_tx(bit_count,
                                                    tms_bits,
                                                    tdi_bits,
                                                    tx_words,
                                                    EXLINK_JTAG_PIO_TX_WORDS);
    if (tx_word_count == 0u) {
        return false;
    }

    memset(rx_words, 0, sizeof(rx_words));
    memset(tdo_bits, 0, (bit_count + 7u) / 8u);

    const uint32_t expected_rx_words = (bit_count / 32u) + 1u;
    uint32_t tx_index = 0u;
    uint32_t rx_index = 0u;
    absolute_time_t start = get_absolute_time();

    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_sm_clear_fifos(pio, (uint)sm);
    pio_sm_restart(pio, (uint)sm);
    pio_drive_idle(1u);

    pio_sm_put(pio, (uint)sm, bit_count - 1u);
    pio_sm_set_enabled(pio, (uint)sm, true);

    while (rx_index < expected_rx_words) {
        if ((tx_index < tx_word_count) && !pio_sm_is_tx_fifo_full(pio, (uint)sm)) {
            pio_sm_put(pio, (uint)sm, tx_words[tx_index]);
            tx_index++;
        }

        if (!pio_sm_is_rx_fifo_empty(pio, (uint)sm)) {
            rx_words[rx_index] = pio_sm_get(pio, (uint)sm);
            rx_index++;
        }

        if (absolute_time_diff_us(start, get_absolute_time()) >
            (int64_t)EXLINK_JTAG_PIO_TIMEOUT_US) {
            pio_sm_set_enabled(pio, (uint)sm, false);
            pio_sm_clear_fifos(pio, (uint)sm);
            pio_sm_restart(pio, (uint)sm);
            pio_drive_idle(0u);
            return false;
        }

        tight_loop_contents();
    }

    pio_sm_set_enabled(pio, (uint)sm, false);
    pio_drive_idle(0u);
    jtag_pio_unpack_rx(bit_count, tdo_bits);
    jtag_pio_flush_rx_fifo();
    return true;
}

void jtag_pio_tap_reset(void)
{
    static const uint8_t tms_reset = 0x3fu;
    static const uint8_t tdi_reset = 0x00u;
    uint8_t tdo_unused = 0u;

    (void)jtag_pio_shift_bits(7u, &tms_reset, &tdi_reset, &tdo_unused);
}
