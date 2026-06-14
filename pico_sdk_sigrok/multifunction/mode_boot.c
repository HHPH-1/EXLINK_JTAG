#include "mode_boot.h"

#include "hardware/structs/watchdog.h"
#include "hardware/watchdog.h"

#include <stdint.h>

#define EXLINK_MODE_SCRATCH_MAGIC 0x45584c4du
#define EXLINK_MODE_SCRATCH_INDEX_MAGIC 0u
#define EXLINK_MODE_SCRATCH_INDEX_MAGIC_INV 1u
#define EXLINK_MODE_SCRATCH_INDEX_MODE 2u

static void clear_mode_scratch(void)
{
    watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MAGIC] = 0u;
    watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MAGIC_INV] = 0u;
    watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MODE] = 0u;
}

ExlinkMode exlink_mode_select_on_boot(void)
{
    ExlinkMode mode = EXLINK_MODE_SCOPE;

    if (watchdog_caused_reboot() &&
        watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MAGIC] == EXLINK_MODE_SCRATCH_MAGIC &&
        watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MAGIC_INV] == ~EXLINK_MODE_SCRATCH_MAGIC) {
        if (watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MODE] == (uint32_t)EXLINK_MODE_JTAG) {
            mode = EXLINK_MODE_JTAG;
        }
    }

    clear_mode_scratch();
    return mode;
}

void exlink_mode_request_next_boot(ExlinkMode mode)
{
    watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MAGIC] = EXLINK_MODE_SCRATCH_MAGIC;
    watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MAGIC_INV] = ~EXLINK_MODE_SCRATCH_MAGIC;
    watchdog_hw->scratch[EXLINK_MODE_SCRATCH_INDEX_MODE] = (uint32_t)mode;
}
