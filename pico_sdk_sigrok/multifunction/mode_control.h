#ifndef EXLINK_MULTIFUNCTION_MODE_CONTROL_H
#define EXLINK_MULTIFUNCTION_MODE_CONTROL_H

#include "mode_boot.h"

#include <stdbool.h>
#include <stdint.h>

#define EXLINK_MULTIFUNCTION_FW_VERSION "EXLINK-MULTIFUNCTION v0.1"
#define EXLINK_MULTIFUNCTION_PROTOCOL_VERSION "1"

void exlink_mode_control_reset_parser(void);
bool exlink_mode_control_feed_char(uint8_t ch,
                                   ExlinkMode current_mode,
                                   bool busy,
                                   bool command_boundary);

#endif
