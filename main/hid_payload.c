// main/hid_payload.c
#include "hid_payload.h"
#include "tusb.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char *TAG = "hid_payload";
static volatile bool s_enabled = false;
static hid_layout_t s_layout = HID_LAYOUT_UK;

const uint8_t s_hid_report_descriptor[HID_REPORT_DESC_LEN] = {
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0,
    0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08,
    0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x06,
    0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00,
    0x29, 0x65, 0x81, 0x00, 0xC0
};

uint8_t const *tud_hid_descriptor_report_cb(uint8_t instance) {
    (void)instance;
    return s_hid_report_descriptor;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id,
                               hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen) {
    (void)instance; (void)report_id; (void)report_type;
    (void)buffer; (void)reqlen;
    return 0;
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id,
                           hid_report_type_t report_type,
                           uint8_t const *buffer, uint16_t bufsize) {
    (void)instance; (void)report_id; (void)report_type;
    (void)buffer; (void)bufsize;
}

// Longer host timeout. 5 seconds is enough to survive Defender scan
// stalls, power management blips, and busy USB hubs. The original 1s
// caused silent truncation at ~550 chars on a loaded host.
static bool wait_ready(void) {
    int timeout = 1000;  // 1000 * 5ms = 5s
    while (!tud_hid_ready() && timeout > 0) {
        vTaskDelay(pdMS_TO_TICKS(5));
        timeout--;
    }
    if (timeout == 0) {
        ESP_LOGE(TAG, "HID timeout: host not ready after 5s");
        return false;
    }
    return true;
}

static bool send_report(uint8_t modifier, const uint8_t keys[6]) {
    if (!wait_ready()) return false;
    uint8_t report[8] = { modifier, 0, keys[0], keys[1], keys[2], keys[3], keys[4], keys[5] };
    return tud_hid_report(0, report, sizeof(report));
}

static void release_keys(void) {
    uint8_t none[6] = { 0 };
    send_report(0, none);
    vTaskDelay(pdMS_TO_TICKS(15));
}

static void release_keys_slow(void) {
    uint8_t none[6] = { 0 };
    send_report(0, none);
    vTaskDelay(pdMS_TO_TICKS(50));
}

// Retries a single character up to 3 times. Slower hold (30ms) and
// release (15ms) than before, which materially reduces transient drops
// on busy hosts. The cost is ~45ms/char instead of ~30ms, i.e. a 400
// char payload types in ~18s instead of ~12s. Worth it for reliability.
static bool type_char(char c) {
    uint8_t modifier = 0;
    uint8_t key = 0;

    if (c >= 'a' && c <= 'z') { key = 0x04 + (c - 'a'); }
    else if (c >= 'A' && c <= 'Z') { key = 0x04 + (c - 'A'); modifier = 0x02; }
    else if (c >= '1' && c <= '9') { key = 0x1E + (c - '1'); }
    else if (c == '0') { key = 0x27; }
    else if (c == ' ') { key = 0x2C; }
    else if (c == '-') { key = 0x2D; }
    else if (c == '=') { key = 0x2E; }
    else if (c == '[') { key = 0x2F; }
    else if (c == ']') { key = 0x30; }
    else if (c == ';') { key = 0x33; }
    else if (c == '\'') { key = 0x34; }
    else if (c == '`') { key = 0x35; }
    else if (c == ',') { key = 0x36; }
    else if (c == '.') { key = 0x37; }
    else if (c == '/') { key = 0x38; }
    else if (c == '!') { key = 0x1E; modifier = 0x02; }
    else if (c == '$') { key = 0x21; modifier = 0x02; }
    else if (c == '%') { key = 0x22; modifier = 0x02; }
    else if (c == '^') { key = 0x23; modifier = 0x02; }
    else if (c == '&') { key = 0x24; modifier = 0x02; }
    else if (c == '*') { key = 0x25; modifier = 0x02; }
    else if (c == '(') { key = 0x26; modifier = 0x02; }
    else if (c == ')') { key = 0x27; modifier = 0x02; }
    else if (c == '_') { key = 0x2D; modifier = 0x02; }
    else if (c == '+') { key = 0x2E; modifier = 0x02; }
    else if (c == '{') { key = 0x2F; modifier = 0x02; }
    else if (c == '}') { key = 0x30; modifier = 0x02; }
    else if (c == ':') { key = 0x33; modifier = 0x02; }
    else if (c == '<') { key = 0x36; modifier = 0x02; }
    else if (c == '>') { key = 0x37; modifier = 0x02; }
    else if (c == '?') { key = 0x38; modifier = 0x02; }
    else if (c == '\\') { key = 0x64; }
    else if (c == '|') { key = 0x64; modifier = 0x02; }
    else if (c == '@') { key = 0x34; modifier = 0x02; }
    else if (c == '"') { key = 0x1F; modifier = 0x02; }
    else if (c == '#') { key = 0x31; }
    else if (c == '~') { key = 0x31; modifier = 0x02; }

    if (key == 0) return true;

    for (int attempt = 0; attempt < 3; attempt++) {
        uint8_t keys[6] = { key, 0, 0, 0, 0, 0 };
        if (send_report(modifier, keys)) {
            vTaskDelay(pdMS_TO_TICKS(30));
            release_keys();
            return true;
        }
        ESP_LOGW(TAG, "retry char '%c' (attempt %d)", c, attempt + 1);
        vTaskDelay(pdMS_TO_TICKS(300));
    }
    ESP_LOGE(TAG, "GAVE UP on char '%c' (0x%02x)", c, (uint8_t)c);
    return false;
}

void hid_payload_init(void) { ESP_LOGI(TAG, "HID payload ready"); }
void hid_payload_enable(bool en) { s_enabled = en; }
bool hid_payload_is_enabled(void) { return s_enabled; }
void hid_payload_set_layout(hid_layout_t layout) { s_layout = layout; }

void hid_payload_send_string(const char *str) {
    if (!s_enabled || !str) return;
    size_t len = strlen(str);
    for (size_t i = 0; i < len; i++) {
        if (!type_char(str[i])) {
            ESP_LOGE(TAG, "string aborted at char %u/%u", (unsigned)i, (unsigned)len);
            return;
        }
    }
    ESP_LOGI(TAG, "string typed OK (%u chars)", (unsigned)len);
}

void hid_payload_send_win_r(void) {
    if (!s_enabled) return;
    release_keys();
    uint8_t none[6] = { 0 };
    send_report(0x08, none);
    vTaskDelay(pdMS_TO_TICKS(60));
    uint8_t r_key[6] = { 0x15, 0, 0, 0, 0, 0 };
    send_report(0x08, r_key);
    vTaskDelay(pdMS_TO_TICKS(60));
    release_keys();
    vTaskDelay(pdMS_TO_TICKS(1200));
}

void hid_payload_send_enter(void) {
    if (!s_enabled) return;
    uint8_t enter_key[6] = { 0x28, 0, 0, 0, 0, 0 };
    send_report(0, enter_key);
    vTaskDelay(pdMS_TO_TICKS(30));
    release_keys();
}

void hid_payload_send_ctrl_shift_enter(void) {
    if (!s_enabled) return;
    release_keys();
    uint8_t none[6] = { 0 };
    send_report(0x03, none);
    vTaskDelay(pdMS_TO_TICKS(50));
    uint8_t enter_key[6] = { 0x28, 0, 0, 0, 0, 0 };
    send_report(0x03, enter_key);
    vTaskDelay(pdMS_TO_TICKS(50));
    release_keys_slow();
}

void hid_payload_send_alt_y(void) {
    if (!s_enabled) return;
    release_keys();
    uint8_t none[6] = { 0 };
    send_report(0x04, none);
    vTaskDelay(pdMS_TO_TICKS(50));
    uint8_t y_key[6] = { 0x1C, 0, 0, 0, 0, 0 };
    send_report(0x04, y_key);
    vTaskDelay(pdMS_TO_TICKS(50));
    release_keys_slow();
}