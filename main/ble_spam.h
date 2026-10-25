// main/ble_spam.h
#pragma once
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    BLE_SPAM_APPLEJUICE = 0,
    BLE_SPAM_SOURAPPLE,
    BLE_SPAM_SAMSUNG,
    BLE_SPAM_WINDOWS,
    BLE_SPAM_FASTPAIR,
    BLE_SPAM_COUNT
} ble_spam_type_t;

esp_err_t ble_spam_init(void);
esp_err_t ble_spam_start(ble_spam_type_t type);
esp_err_t ble_spam_stop(void);
bool      ble_spam_is_running(void);

/* Diagnostic:
 *   >0  = esp_err_t returned by nimble_port_init() / controller init
 *         (257 = ESP_ERR_NO_MEM = largest block below threshold)
 *   <0  = BLE_HS_* rc from ble_gap_adv_set_data / adv_start / ensure_addr,
 *         or -2 if the host sync never completed.
 *    0  = no error recorded. */
int       ble_spam_last_error(void);
void      ble_spam_clear_error(void);

const char *ble_spam_type_name(ble_spam_type_t type);
int         ble_spam_type_count(void);

#ifdef __cplusplus
}
#endif