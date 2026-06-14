#include "jtag_gpio.h"
#include "jtag_protocol.h"

#include "exlink_rp2040.h"
#include "pico/stdlib.h"
#include "tusb.h"

#ifdef SIGROK_BOARD_EXLINK
_Static_assert(PICO_FLASH_SIZE_BYTES == EXLINK_FLASH_SIZE_BYTES,
               "Exlink RP2040 requires a 2 MB flash configuration");
#endif

int main(void)
{
    stdio_init_all();
    jtag_gpio_init();
    jtag_protocol_init();

    while (true) {
        tud_task();
        jtag_protocol_task();
        tight_loop_contents();
    }
}
