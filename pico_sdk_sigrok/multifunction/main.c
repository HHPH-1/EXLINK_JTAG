#include "mode_boot.h"

int exlink_scope_mode_main(void);
int exlink_jtag_mode_main(void);

int main(void)
{
    if (exlink_mode_select_on_boot() == EXLINK_MODE_JTAG) {
        return exlink_jtag_mode_main();
    }

    return exlink_scope_mode_main();
}
