// main/config.h
#pragma once

// --- Input ---
#define PIN_NUM_BOOT_BUTTON 0

// --- SD Card (SDMMC 4-bit, LilyGO T-Dongle-S3) ---
#define PIN_SD_CLK   12
#define PIN_SD_CMD   16
#define PIN_SD_D0    14
#define PIN_SD_D1    17
#define PIN_SD_D2    21
#define PIN_SD_D3    18
#define SD_MOUNT_POINT "/sdcard"

// --- USB Protocol ---
#define MAX_PACKET_LEN 1024