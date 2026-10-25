// main/serial_input.c
#include "serial_input.h"
#include "input.h"
#include "ui.h"
#include "usb_protocol.h"
#include "status_led.h"
#include "locked_ui.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

#define CMD_SYNC          0xAA
#define CMD_TYPE          0x02
#define CMD_SET_ROTATION  0x01
#define CMD_HEARTBEAT     0x02
#define CMD_HOST_NAME     0x03
#define CMD_UNLOCK        0x04
#define CMD_PAYLOAD_MAX   64
#define CMD_HEADER_LEN    5

typedef enum {
    ST_IDLE,
    ST_ESC,
    ST_CSI,
    ST_CMD_HDR,
    ST_CMD_PAYLOAD
} parser_state_t;

static void dispatch_command(const uint8_t *payload, size_t len)
{
    if (len < 1) return;
    switch (payload[0]) {
    case CMD_SET_ROTATION:
        if (len >= 2) {
            ui_set_rotation(payload[1] != 0);
        }
        break;
    case CMD_HEARTBEAT:
        status_led_notify_heartbeat();
        break;
    case CMD_HOST_NAME: {
        if (len > 1) {
            char name[33];
            size_t n = len - 1;
            if (n > 32) n = 32;
            memcpy(name, payload + 1, n);
            name[n] = '\0';
            status_led_set_host_name(name);
        }
        break;
    }
    case CMD_UNLOCK:
        locked_ui_exit();
        break;
    default:
        break;
    }
}

static void serial_input_task(void *arg)
{
    (void)arg;
    parser_state_t state = ST_IDLE;
    uint8_t  byte;
    uint8_t  header[CMD_HEADER_LEN];
    size_t   hdr_pos = 0;
    uint16_t cmd_len = 0;
    uint16_t cmd_crc = 0;
    uint8_t  payload[CMD_PAYLOAD_MAX];
    size_t   pay_pos = 0;

    while (1) {
        int n = usb_protocol_read(&byte, 1, pdMS_TO_TICKS(100));

        if (n < 0) {
            vTaskDelay(pdMS_TO_TICKS(50));
            if (state == ST_CMD_HDR || state == ST_CMD_PAYLOAD) {
                state = ST_IDLE;
                hdr_pos = 0;
                pay_pos = 0;
            }
            continue;
        }

        if (n == 0) {
            if (state == ST_CMD_HDR || state == ST_CMD_PAYLOAD) {
                state = ST_IDLE;
                hdr_pos = 0;
                pay_pos = 0;
            }
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        switch (state) {
        case ST_IDLE:
            if (byte == 0x1B) {
                state = ST_ESC;
            } else if (byte == CMD_SYNC) {
                hdr_pos = 0;
                state = ST_CMD_HDR;
            } else if (byte == '\r' || byte == '\n') {
                input_post_event(INPUT_ENTER);
            }
            break;

        case ST_ESC:
            if (byte == '[' || byte == 'O') {
                state = ST_CSI;
            } else if (byte == CMD_SYNC) {
                hdr_pos = 0;
                state = ST_CMD_HDR;
            } else {
                input_post_event(INPUT_BACK);
                state = ST_IDLE;
            }
            break;

        case ST_CSI:
            switch (byte) {
            case 'A': input_post_event(INPUT_UP);   break;
            case 'B': input_post_event(INPUT_DOWN); break;
            case 'C': /* right — reserved */        break;
            case 'D': input_post_event(INPUT_BACK); break;
            default:  break;
            }
            state = ST_IDLE;
            break;

        case ST_CMD_HDR:
            header[hdr_pos++] = byte;
            if (hdr_pos < CMD_HEADER_LEN) break;

            if (header[0] != CMD_TYPE) {
                state = ST_IDLE;
                hdr_pos = 0;
                break;
            }
            cmd_len = ((uint16_t)header[1] << 8) | header[2];
            cmd_crc = ((uint16_t)header[3] << 8) | header[4];

            if (cmd_len == 0 || cmd_len > CMD_PAYLOAD_MAX) {
                state = ST_IDLE;
                hdr_pos = 0;
                break;
            }
            pay_pos = 0;
            state = ST_CMD_PAYLOAD;
            break;

        case ST_CMD_PAYLOAD:
            payload[pay_pos++] = byte;
            if (pay_pos == cmd_len) {
                if (usb_protocol_crc16(payload, cmd_len) == cmd_crc) {
                    dispatch_command(payload, cmd_len);
                }
                state = ST_IDLE;
                hdr_pos = 0;
                pay_pos = 0;
            }
            break;
        }
    }
}

esp_err_t serial_input_init(void)
{
    BaseType_t ret = xTaskCreate(serial_input_task, "serial_input",
                                 4096, NULL, 5, NULL);
    return (ret == pdPASS) ? ESP_OK : ESP_FAIL;
}