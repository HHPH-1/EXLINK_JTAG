#ifndef EXLINK_RP2040_H
#define EXLINK_RP2040_H

#include <stdint.h>

#define EXLINK_BOARD_NAME              "Exlink RP2040"

#define EXLINK_FLASH_SIZE_BYTES        (2u * 1024u * 1024u)

#define EXLINK_LA_GPIO_BASE            2u
#define EXLINK_LA_CHANNEL_COUNT        8u
#define EXLINK_LA_GPIO_MASK            (0xFFu << EXLINK_LA_GPIO_BASE)

#define EXLINK_LA_CH0_GPIO             2u
#define EXLINK_LA_CH1_GPIO             3u
#define EXLINK_LA_CH2_GPIO             4u
#define EXLINK_LA_CH3_GPIO             5u
#define EXLINK_LA_CH4_GPIO             6u
#define EXLINK_LA_CH5_GPIO             7u
#define EXLINK_LA_CH6_GPIO             8u
#define EXLINK_LA_CH7_GPIO             9u

#define EXLINK_ADC_GPIO                29u
#define EXLINK_ADC_INPUT               3u
#define EXLINK_ADC_CHANNEL_COUNT       0u
#define EXLINK_ADC_ROUND_ROBIN_MASK    (1u << EXLINK_ADC_INPUT)

/*
 * Reserved for a future Vivado JTAG mode.
 * This build only implements the existing sigrok logic-analyzer firmware.
 */
#define EXLINK_JTAG_TCK_GPIO           EXLINK_LA_CH0_GPIO
#define EXLINK_JTAG_TMS_GPIO           EXLINK_LA_CH1_GPIO
#define EXLINK_JTAG_TDI_GPIO           EXLINK_LA_CH2_GPIO
#define EXLINK_JTAG_TDO_GPIO           EXLINK_LA_CH3_GPIO

_Static_assert(EXLINK_LA_CH0_GPIO == EXLINK_LA_GPIO_BASE,
               "CHAN0 must be the first PIO input GPIO");

_Static_assert(EXLINK_LA_CH7_GPIO ==
               EXLINK_LA_GPIO_BASE + EXLINK_LA_CHANNEL_COUNT - 1u,
               "Logic analyzer GPIOs must be contiguous");

#endif
