#include "jtag_engine.h"
#include "jtag_protocol.h"

#include "exlink_rp2040.h"
#include "pico/stdlib.h"
#include "tusb.h"

#ifdef EXLINK_MULTIFUNCTION
#include "multifunction/mode_workspace.h"
#endif

#ifdef SIGROK_BOARD_EXLINK
_Static_assert(PICO_FLASH_SIZE_BYTES == EXLINK_FLASH_SIZE_BYTES,
               "Exlink RP2040 requires a 2 MB flash configuration");
#endif

int exlink_jtag_mode_main(void)
{
#ifdef EXLINK_MULTIFUNCTION
    exlink_mode_workspace_reset();
#endif
    stdio_init_all();
    (void)jtag_engine_init();
    jtag_protocol_init();

    while (true) {
        tud_task();
        jtag_protocol_task();
        tight_loop_contents();
    }
}

#ifndef EXLINK_MULTIFUNCTION
int main(void)
{
    return exlink_jtag_mode_main();
}
#endif
