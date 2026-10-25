// main/hid_payload.h
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    HID_LAYOUT_US = 0,
    HID_LAYOUT_UK,
} hid_layout_t;

#define HID_REPORT_DESC_LEN 45

extern const uint8_t s_hid_report_descriptor[HID_REPORT_DESC_LEN];

void hid_payload_init(void);
void hid_payload_enable(bool en);
bool hid_payload_is_enabled(void);
void hid_payload_set_layout(hid_layout_t layout);
void hid_payload_send_string(const char *str);
void hid_payload_send_win_r(void);
void hid_payload_send_enter(void);

/* UAC Bypass Functions */
void hid_payload_send_ctrl_shift_enter(void);
void hid_payload_send_alt_y(void);

#ifdef __cplusplus
}
#endif