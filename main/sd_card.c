// main/sd_card.c
#include "sd_card.h"
#include "config.h"
#include "esp_vfs_fat.h"
#include "driver/sdmmc_host.h"
#include "sdmmc_cmd.h"
#include "esp_log.h"
#include <stdio.h>

static const char *TAG = "sd_card";
static sdmmc_card_t *card = NULL;
static bool s_mounted = false;

esp_err_t sd_card_init(void) {
    if (s_mounted) return ESP_OK;

    esp_vfs_fat_sdmmc_mount_config_t mount_config = {
        .format_if_mount_failed = false,
        .max_files = 5,
        /* FATFS sizes its work buffer from this (x2, 32 KB floor), so a
         * 64 KB value asked for a ~128 KB contiguous internal allocation on
         * a no-PSRAM S3 that already holds the LVGL draw buffer, the Wi-Fi
         * heaps and TinyUSB -> mount failed and handshake dumps reported
         * "SD FAIL". One small cluster is plenty for handshake PCAPs. */
        .allocation_unit_size = 4 * 1024,
    };

    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.max_freq_khz = 20000;

    sdmmc_slot_config_t slot_config = SDMMC_SLOT_CONFIG_DEFAULT();
    slot_config.width = 4;
    slot_config.clk = PIN_SD_CLK;
    slot_config.cmd = PIN_SD_CMD;
    slot_config.d0  = PIN_SD_D0;
    slot_config.d1  = PIN_SD_D1;
    slot_config.d2  = PIN_SD_D2;
    slot_config.d3  = PIN_SD_D3;
    slot_config.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;

    esp_err_t ret = esp_vfs_fat_sdmmc_mount(SD_MOUNT_POINT, &host,
                                            &slot_config, &mount_config,
                                            &card);
    if (ret != ESP_OK) {
        if (ret == ESP_FAIL) {
            ESP_LOGE(TAG, "Mount failed. Card must be FAT32/MBR, not exFAT.");
        } else {
            ESP_LOGE(TAG, "Card init failed: %s", esp_err_to_name(ret));
        }
        return ret;
    }

    s_mounted = true;
    ESP_LOGI(TAG, "SD card mounted at %s", SD_MOUNT_POINT);
    sdmmc_card_print_info(stdout, card);
    return ESP_OK;
}

bool sd_card_is_mounted(void) { return s_mounted; }