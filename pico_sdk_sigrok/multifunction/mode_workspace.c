#include "mode_workspace.h"

#include <stdint.h>

static uint8_t g_mode_workspace[EXLINK_MODE_WORKSPACE_BYTES] __attribute__((aligned(4)));
static size_t g_mode_workspace_offset;

void exlink_mode_workspace_reset(void)
{
    g_mode_workspace_offset = 0u;
}

void *exlink_mode_workspace_alloc(size_t bytes, size_t alignment)
{
    if (alignment == 0u) {
        alignment = 1u;
    }

    const size_t mask = alignment - 1u;
    if ((alignment & mask) != 0u) {
        return 0;
    }

    const size_t aligned_offset = (g_mode_workspace_offset + mask) & ~mask;
    if (aligned_offset > EXLINK_MODE_WORKSPACE_BYTES ||
        bytes > EXLINK_MODE_WORKSPACE_BYTES - aligned_offset) {
        return 0;
    }

    g_mode_workspace_offset = aligned_offset + bytes;
    return &g_mode_workspace[aligned_offset];
}

uint8_t *exlink_mode_workspace_base(void)
{
    return g_mode_workspace;
}

size_t exlink_mode_workspace_size(void)
{
    return EXLINK_MODE_WORKSPACE_BYTES;
}

size_t exlink_mode_workspace_used(void)
{
    return g_mode_workspace_offset;
}
