// main/main.cpp
#include "ui.h"
#include "input.h"
#include "serial_input.h"
#include "wifi_sniffer.h"
#include "ble_spam.h"
#include "beacon.h"
#include "portal.h"
#include "deauth.h"
#include "handshake.h"
#include "usb_protocol.h"
#include "sd_card.h"
#include "status_led.h"
#include "locked_ui.h"
#include "hid_payload.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cstring>
#include <cstdio>
#include "t-dongle-s3.hpp"
#include "lvgl.h"

LV_FONT_DECLARE(lv_font_montserrat_28);

static const char *TAG = "main";

typedef enum {
    MODE_IDLE, MODE_WIFI_SNIFF, MODE_BLE_SPAM,
    MODE_BEACON_AP, MODE_BEACON_FLOOD, MODE_DEAUTH
} radio_mode_t;

static radio_mode_t active_mode = MODE_IDLE;
static bool deauth_scan_pending = false;

static int      last_ap_count = -1;
static int      last_cl_count = -1;
static uint32_t last_stats_ms = 0;

static uint8_t  s_spoofed_mac[6] = {0};

// Tracks which BSSID the current/last DEAUTH attempt targeted, so the
// main loop can auto-clear MODE_DEAUTH and report CAPTURED vs DONE.
static uint8_t  s_deauth_target_bssid[6] = {0};
static bool     s_deauth_target_valid = false;

static volatile bool s_radios_ready = false;

static void randomize_base_mac(void)
{
    uint8_t mac[6];
    for (int i = 0; i < 6; i++) mac[i] = (uint8_t)(esp_random() & 0xFF);
    mac[0] = (mac[0] & 0xFC) | 0x02;
    memcpy(s_spoofed_mac, mac, 6);
    esp_base_mac_addr_set(mac);
}

/* ---------------------------------------------------------------------------
 * Radio lifecycle
 * ------------------------------------------------------------------------- */
static void stop_all_radios(void)
{
    if (active_mode == MODE_WIFI_SNIFF)        wifi_sniffer_stop();
    else if (active_mode == MODE_BLE_SPAM)     ble_spam_stop();
    else if (active_mode == MODE_BEACON_AP)    beacon_stop();
    else if (active_mode == MODE_BEACON_FLOOD) beacon_flood_stop();
    else if (active_mode == MODE_DEAUTH)       deauth_stop();
    active_mode = MODE_IDLE;
    s_deauth_target_valid = false;
    last_ap_count = -1;
    last_cl_count = -1;
}

static void ensure_wifi_ready_for_mode(radio_mode_t mode)
{
    if (!s_radios_ready) {
        ui_set_status("INIT...");
        return;
    }

    bool needs_wifi =
        (mode == MODE_WIFI_SNIFF    ||
         mode == MODE_BEACON_AP     ||
         mode == MODE_BEACON_FLOOD  ||
         mode == MODE_DEAUTH);

    if (needs_wifi && !wifi_sniffer_is_initialized()) {
        if (wifi_sniffer_init() != ESP_OK) {
            ui_set_status("WIFI INIT FAIL");
            status_led_flash_error();
        }
    }
}

static void dispatch_module(const char *name)
{
    if (!s_radios_ready) {
        ui_set_status("INIT...");
        return;
    }

    bool already_active =
        (strcmp(name, "SCAN")       == 0 && active_mode == MODE_WIFI_SNIFF) ||
        (strcmp(name, "BLE SPAM")   == 0 && active_mode == MODE_BLE_SPAM)  ||
        (strcmp(name, "WIFI CLONE") == 0 && active_mode == MODE_BEACON_AP);

    stop_all_radios();

    if (already_active) { ui_set_status("IDLE"); return; }

    if (strcmp(name, "SCAN") == 0) {
        ensure_wifi_ready_for_mode(MODE_WIFI_SNIFF);
        wifi_sniffer_start();
        active_mode = MODE_WIFI_SNIFF;
        ui_set_status("SCANNING");
    } else if (strcmp(name, "BLE SPAM") == 0) {
        ui_set_status("STUB");
    } else if (strcmp(name, "WIFI CLONE") == 0) {
        ensure_wifi_ready_for_mode(MODE_BEACON_AP);
        beacon_start();
        active_mode = MODE_BEACON_AP;
        ui_set_status("AP ON");
    } else if (strcmp(name, "DEAUTH") == 0) {
        ui_set_status("STUB");
    }
}

/* ---------------------------------------------------------------------------
 * Submenu population
 * ------------------------------------------------------------------------- */
static void populate_deauth_ui(void)
{
    int n = deauth_network_count();
    const char *names[16];
    int cap = n > 16 ? 16 : n;
    for (int i = 0; i < cap; i++) names[i] = deauth_network_at(i)->ssid;
    ui_set_deauth_list(names, cap);
    ui_set_status("IDLE");
}

static void populate_ble_ui(void)
{
    int n = ble_spam_type_count();
    const char *names[BLE_SPAM_COUNT];
    for (int i = 0; i < n; i++) names[i] = ble_spam_type_name((ble_spam_type_t)i);
    ui_set_ble_list(names, n);
    ui_set_status("IDLE");
}

static void populate_flood_ui(void)
{
    int n = beacon_flood_type_count();
    const char *names[BEACON_FLOOD_COUNT];
    for (int i = 0; i < n; i++) names[i] = beacon_flood_type_name((beacon_flood_type_t)i);
    ui_set_flood_list(names, n);
    ui_set_status("IDLE");
}

static void populate_stats_ui(void)
{
    static char host_buf[48];
    static char mac_buf[48];
    static char heap_buf[48];
    static char up_buf[48];
    static char reset_buf[48];
    static char sd_buf[48];
    static char scan_buf[48];

    const char *host_name = status_led_is_connected()
                                ? status_led_get_host_name()
                                : "NONE";
    snprintf(host_buf, sizeof(host_buf), "%s", host_name);

    snprintf(mac_buf, sizeof(mac_buf), "%02X:%02X:%02X:%02X:%02X:%02X",
             s_spoofed_mac[0], s_spoofed_mac[1], s_spoofed_mac[2],
             s_spoofed_mac[3], s_spoofed_mac[4], s_spoofed_mac[5]);

    size_t free_h = esp_get_free_heap_size();
    size_t large  = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    snprintf(heap_buf, sizeof(heap_buf), "%uK free %uK large",
             (unsigned)(free_h / 1024), (unsigned)(large / 1024));

    uint32_t up_s = (uint32_t)(esp_timer_get_time() / 1000000);
    snprintf(up_buf, sizeof(up_buf), "%02u:%02u:%02u",
             (unsigned)(up_s / 3600),
             (unsigned)((up_s / 60) % 60),
             (unsigned)(up_s % 60));

    const char *rr;
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   rr = "POWERON";   break;
    case ESP_RST_SW:        rr = "SW";        break;
    case ESP_RST_PANIC:     rr = "PANIC";     break;
    case ESP_RST_INT_WDT:   rr = "INT_WDT";   break;
    case ESP_RST_TASK_WDT:  rr = "TASK_WDT";  break;
    case ESP_RST_WDT:       rr = "WDT";       break;
    case ESP_RST_BROWNOUT:  rr = "BROWNOUT";  break;
    case ESP_RST_DEEPSLEEP: rr = "DEEPSLEEP"; break;
    case ESP_RST_EXT:       rr = "EXT";       break;
    default:                rr = "UNKNOWN";   break;
    }
    snprintf(reset_buf, sizeof(reset_buf), "%s", rr);

    snprintf(sd_buf, sizeof(sd_buf), "%s",
             sd_card_is_mounted() ? "MOUNTED" : "NONE");

    int aps = 0, cls = 0;
    wifi_sniffer_get_stats(&aps, &cls);
    snprintf(scan_buf, sizeof(scan_buf), "%d APs %d CLs", aps, cls);

    static const char *names_arr[] = {
        "HOST",
        "SPOOFED MAC",
        "FREE HEAP",
        "UPTIME",
        "RESET REASON",
        "SD CARD",
        "SCAN TOTALS",
    };
    const char *values_arr[] = {
        host_buf, mac_buf, heap_buf, up_buf, reset_buf, sd_buf, scan_buf,
    };
    ui_set_stats_list(names_arr, values_arr, 7);
}

/* ---------------------------------------------------------------------------
 * Deferred radio init
 * ------------------------------------------------------------------------- */
static void deferred_radios_init(void *arg)
{
    (void)arg;

    if (wifi_sniffer_init() != ESP_OK) ESP_LOGE(TAG, "wifi_sniffer_init failed");
    if (beacon_init()       != ESP_OK) ESP_LOGE(TAG, "beacon_init failed");
    if (portal_init()       != ESP_OK) ESP_LOGE(TAG, "portal_init failed");
    if (deauth_init()       != ESP_OK) ESP_LOGE(TAG, "deauth_init failed");

    s_radios_ready = true;
    vTaskDelete(NULL);
}

/* ---------------------------------------------------------------------------
 * app_main
 * ------------------------------------------------------------------------- */
extern "C" void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_NONE);

    randomize_base_mac();
    status_led_init();

    esp_reset_reason_t rr = esp_reset_reason();
    size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    char bootbuf[48];
    snprintf(bootbuf, sizeof(bootbuf), "R=%d L=%uK", (int)rr, (unsigned)(largest / 1024));
    ui_set_status(bootbuf);

    esp_err_t nvs_ret = nvs_flash_init();
    if (nvs_ret == ESP_ERR_NVS_NO_FREE_PAGES || nvs_ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* TinyUSB device stack: CDC + HID composite. */
    ESP_ERROR_CHECK(usb_protocol_init());
    hid_payload_init();

    espp::TDongleS3 &board = espp::TDongleS3::get();
    if (!board.initialize_lcd())     { status_led_flash_error(); return; }
    static constexpr size_t pixel_buffer_size = 160 * 80;
    if (!board.initialize_display(pixel_buffer_size)) { status_led_flash_error(); return; }

    ui_capture_boot_rotation();

    /* Start in locked mode. */
    ui_init();
    locked_ui_enter();

    ESP_ERROR_CHECK(input_init());
    ESP_ERROR_CHECK(serial_input_init());

    xTaskCreatePinnedToCore(deferred_radios_init, "radios_init", 8192, NULL, 1,
                            NULL, 0);

    input_event_t event;
    while (1) {
        lv_timer_handler();

        if (!locked_ui_is_locked()) {
            status_led_set_mode(active_mode == MODE_IDLE
                                    ? LED_MODE_IDLE : LED_MODE_ACTIVE);

            if (active_mode == MODE_WIFI_SNIFF) {
                uint32_t t = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
                if (t - last_stats_ms >= 1000) {
                    last_stats_ms = t;
                    int ap_c = 0, cl_c = 0;
                    wifi_sniffer_get_stats(&ap_c, &cl_c);
                    if (ap_c != last_ap_count || cl_c != last_cl_count) {
                        last_ap_count = ap_c;
                        last_cl_count = cl_c;
                        char b[32];
                        snprintf(b, sizeof(b), "APs:%d CL:%d", ap_c, cl_c);
                        ui_set_status(b);
                    }
                }
            }

            if (deauth_scan_pending && deauth_scan_done()) {
                deauth_scan_pending = false;
                populate_deauth_ui();
            }

            if (active_mode == MODE_BLE_SPAM && !ble_spam_is_running()) {
                int last = ble_spam_last_error();
                char eb[48];
                snprintf(eb, sizeof(eb), "BLE ERR %d", last ? last : -1);
                ui_set_status(eb);
                status_led_flash_error();
                ui_draw_menu();
                active_mode = MODE_IDLE;
            }

            // Auto-clear MODE_DEAUTH when the deauth task exits on its own
            // (successful capture, SD failure, or early abort). Without
            // this, the UI stays stuck on "ATTACKING" and the LED stays blue.
            if (active_mode == MODE_DEAUTH && !deauth_is_running()) {
                bool captured = s_deauth_target_valid &&
                                handshake_is_usable(s_deauth_target_bssid);
                ui_set_status(captured ? "CAPTURED" : "DONE");
                s_deauth_target_valid = false;
                active_mode = MODE_IDLE;
                ui_draw_menu();
            }
        }

        if (input_get_event(&event, pdMS_TO_TICKS(10)) == pdTRUE) {
            if (locked_ui_is_locked()) {
                if (event == INPUT_ENTER || event == INPUT_DOWN) {
                    status_led_flash_error();
                    locked_ui_trigger_payload();
                }
                continue;
            }

            switch (event) {
                case INPUT_UP:   ui_move_up();   break;
                case INPUT_DOWN: ui_move_down(); break;

                case INPUT_ENTER: {
                    if (ui_in_stats_submenu()) {
                        populate_stats_ui();
                        break;
                    }

                    if (!s_radios_ready) {
                        ui_set_status("INIT...");
                        ui_draw_menu();
                        break;
                    }

                    if (ui_in_flood_submenu()) {
                        if (beacon_flood_is_running()) {
                            beacon_flood_stop();
                            active_mode = MODE_IDLE;
                            ui_set_status("IDLE");
                        } else if (ui_flood_count() > 0) {
                            int idx = ui_flood_cursor();
                            ensure_wifi_ready_for_mode(MODE_BEACON_FLOOD);
                            if (beacon_flood_start((beacon_flood_type_t)idx) == ESP_OK) {
                                active_mode = MODE_BEACON_FLOOD;
                                ui_set_status("FLOOD");
                            } else {
                                status_led_flash_error();
                            }
                        }
                        ui_draw_menu();
                        break;
                    }

                    if (ui_in_ble_submenu()) {
                        if (ble_spam_is_running()) {
                            ble_spam_stop();
                            active_mode = MODE_IDLE;
                            ui_set_status("IDLE");
                        } else if (ui_ble_count() > 0) {
                            int idx = ui_ble_cursor();
                            esp_err_t r = ble_spam_start((ble_spam_type_t)idx);
                            if (r == ESP_OK) {
                                active_mode = MODE_BLE_SPAM;
                                ui_set_status("SPAMMING");
                            } else {
                                char eb[48];
                                int last = ble_spam_last_error();
                                snprintf(eb, sizeof(eb), "BLE ERR %d",
                                         last ? last : (int)r);
                                ui_set_status(eb);
                                status_led_flash_error();
                            }
                        }
                        ui_draw_menu();
                        break;
                    }

                    if (ui_in_deauth_submenu()) {
                        if (deauth_is_running()) {
                            deauth_stop();
                            active_mode = MODE_IDLE;
                            s_deauth_target_valid = false;
                            ui_set_status("IDLE");
                        } else if (ui_deauth_count() > 0) {
                            int idx = ui_deauth_cursor();
                            ensure_wifi_ready_for_mode(MODE_DEAUTH);
                            esp_err_t r = deauth_start(idx);
                            if (r == ESP_OK) {
                                const deauth_network_t *n = deauth_network_at(idx);
                                if (n) {
                                    memcpy(s_deauth_target_bssid, n->bssid, 6);
                                    s_deauth_target_valid = true;
                                }
                                active_mode = MODE_DEAUTH;
                                ui_set_status("ATTACKING");
                            } else if (r == ESP_ERR_NOT_SUPPORTED) {
                                ui_set_status("PMF REQ");
                                status_led_flash_error();
                            } else {
                                status_led_flash_error();
                            }
                        }
                        ui_draw_menu();
                        break;
                    }

                    const char *name = ui_get_selected_name();

                    if (name && strcmp(name, "BEACON") == 0 && !ui_in_submenu()) {
                        ui_enter_beacon_submenu(); ui_draw_menu(); break;
                    }

                    if (name && strcmp(name, "DEAUTH") == 0 && !ui_in_submenu()) {
                        ensure_wifi_ready_for_mode(MODE_DEAUTH);
                        stop_all_radios();
                        ui_enter_deauth_submenu();
                        ui_draw_menu();
                        esp_err_t e = deauth_scan_start();
                        if (e == ESP_OK) deauth_scan_pending = true;
                        else {
                            ui_set_status("SCAN FAIL");
                            status_led_flash_error();
                            ui_draw_menu();
                        }
                        break;
                    }

                    if (name && strcmp(name, "BLE SPAM") == 0 && !ui_in_submenu()) {
                        stop_all_radios();
                        ui_enter_ble_submenu();
                        populate_ble_ui();
                        break;
                    }

                    if (name && strcmp(name, "WIFI FLOOD") == 0) {
                        ensure_wifi_ready_for_mode(MODE_BEACON_FLOOD);
                        stop_all_radios();
                        ui_enter_flood_submenu();
                        populate_flood_ui();
                        break;
                    }

                    if (name && strcmp(name, "STATS") == 0 && !ui_in_submenu()) {
                        stop_all_radios();
                        ui_enter_stats_submenu();
                        populate_stats_ui();
                        break;
                    }

                    if (name) dispatch_module(name);
                    ui_draw_menu();
                    break;
                }

                case INPUT_BACK:
                    stop_all_radios();
                    deauth_scan_pending = false;
                    ui_set_status("IDLE");
                    if (ui_in_submenu()) ui_exit_to_main();
                    ui_draw_menu();
                    break;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(5));
    }
}