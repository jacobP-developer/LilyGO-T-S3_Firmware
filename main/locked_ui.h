// main/locked_ui.h
#pragma once
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Enter locked mode: landscape, shows M4NT1S + SD state, enables HID. */
void locked_ui_enter(void);

/* Leave locked mode: return to the normal menu, disable HID. */
void locked_ui_exit(void);

/* True while the device is in locked mode. */
bool locked_ui_is_locked(void);

/* Called from the BOOT button handler when locked: run the HID payload. */
void locked_ui_trigger_payload(void);

/* Called from the SD card module after a successful mount, to refresh
 * the displayed free-space figure. */
void locked_ui_refresh_sd_state(void);

#ifdef __cplusplus
}
#endif