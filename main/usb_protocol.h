// main/usb_protocol.h
#pragma once
#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>
#include "freertos/FreeRTOS.h"

#ifdef __cplusplus
extern "C" {
#endif

#define USB_TYPE_FRAME       0x01
#define USB_TYPE_COMMAND     0x02
#define USB_TYPE_CREDENTIAL  0x03
#define USB_TYPE_HANDSHAKE   0x04
#define USB_TYPE_STATUS      0x05
#define USB_TYPE_SCAN_AP     0x06
#define USB_TYPE_SCAN_CLIENT 0x07

esp_err_t usb_protocol_init(void);

int  usb_protocol_read(uint8_t *buf, size_t len, TickType_t timeout);
int  usb_protocol_write(const uint8_t *buf, size_t len, TickType_t timeout);

void usb_send_packet(const uint8_t *data, size_t len);
esp_err_t usb_send_typed_packet(uint8_t type, const uint8_t *data, size_t len);
void usb_send_credential(const char *email, const char *password);
void usb_send_handshake(const char *bssid, const char *ssid, const char *path);
void usb_send_status(const char *text);

uint16_t usb_protocol_crc16(const uint8_t *data, size_t len);

#ifdef __cplusplus
}
#endif