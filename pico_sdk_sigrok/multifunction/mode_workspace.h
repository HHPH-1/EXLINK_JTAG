#ifndef EXLINK_MULTIFUNCTION_MODE_WORKSPACE_H
#define EXLINK_MULTIFUNCTION_MODE_WORKSPACE_H

#include <stddef.h>
#include <stdint.h>

#define EXLINK_MODE_WORKSPACE_BYTES 220000u

void exlink_mode_workspace_reset(void);
void *exlink_mode_workspace_alloc(size_t bytes, size_t alignment);
uint8_t *exlink_mode_workspace_base(void);
size_t exlink_mode_workspace_size(void);
size_t exlink_mode_workspace_used(void);

#endif
