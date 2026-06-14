#ifndef EXLINK_JTAG_BRIDGE_JTAG_PROTOCOL_H
#define EXLINK_JTAG_BRIDGE_JTAG_PROTOCOL_H

#include <stdint.h>

#define EXLINK_JTAG_MAX_SHIFT_BITS  4096u
#define EXLINK_JTAG_MAX_SHIFT_BYTES ((EXLINK_JTAG_MAX_SHIFT_BITS + 7u) / 8u)

void jtag_protocol_init(void);
void jtag_protocol_task(void);

#endif
