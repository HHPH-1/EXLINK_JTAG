#ifndef EXLINK_MULTIFUNCTION_MODE_BOOT_H
#define EXLINK_MULTIFUNCTION_MODE_BOOT_H

typedef enum {
    EXLINK_MODE_SCOPE = 0,
    EXLINK_MODE_JTAG = 1
} ExlinkMode;

ExlinkMode exlink_mode_select_on_boot(void);
void exlink_mode_request_next_boot(ExlinkMode mode);

#endif
