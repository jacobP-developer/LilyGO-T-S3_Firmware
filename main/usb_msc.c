// main/usb_msc.c
#include "usb_msc.h"
#include "tusb.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "usb_msc";

#define MSC_SECTOR_SIZE    512
#define MSC_DATA_CLUSTERS  64
#define MSC_RESERVED       1
#define MSC_NUM_FATS       1
#define MSC_FAT_SECTORS    1
#define MSC_ROOT_ENTRIES   16
#define MSC_ROOT_SECTORS   1

#define MSC_TOTAL_SECTORS  (MSC_RESERVED + MSC_NUM_FATS*MSC_FAT_SECTORS + MSC_ROOT_SECTORS + MSC_DATA_CLUSTERS)
#define MSC_TOTAL_BYTES    (MSC_TOTAL_SECTORS * MSC_SECTOR_SIZE)
#define MSC_DATA_OFFSET    ((MSC_RESERVED + MSC_NUM_FATS*MSC_FAT_SECTORS + MSC_ROOT_SECTORS) * MSC_SECTOR_SIZE)
#define MSC_MAX_PAYLOAD    (MSC_DATA_CLUSTERS * MSC_SECTOR_SIZE)

static uint8_t s_fat_img[MSC_TOTAL_BYTES] __attribute__((aligned(4)));
static size_t  s_image_size = 0;

extern const uint8_t payload_ps1_start[] asm("_binary_payload_ps1_start");
extern const uint8_t payload_ps1_end[]   asm("_binary_payload_ps1_end");

static void build_fat_image(void)
{
    memset(s_fat_img, 0, sizeof(s_fat_img));
    size_t payload_len = (size_t)(payload_ps1_end - payload_ps1_start);
    if (payload_len > MSC_MAX_PAYLOAD) {
        ESP_LOGE(TAG, "payload.ps1 %u > %u max", (unsigned)payload_len, MSC_MAX_PAYLOAD);
        payload_len = MSC_MAX_PAYLOAD;
    }

    // --- Boot sector ---
    uint8_t *bs = s_fat_img;
    bs[0] = 0xEB; bs[1] = 0x3C; bs[2] = 0x90;
    memcpy(bs + 3, "M4NT1S  ", 8);
    *(uint16_t *)(bs + 11) = MSC_SECTOR_SIZE;
    bs[13] = 1;
    *(uint16_t *)(bs + 14) = MSC_RESERVED;
    bs[16] = MSC_NUM_FATS;
    *(uint16_t *)(bs + 17) = MSC_ROOT_ENTRIES;
    *(uint16_t *)(bs + 19) = (MSC_TOTAL_SECTORS < 4085) ? MSC_TOTAL_SECTORS : 0;
    bs[21] = 0xF8;
    *(uint16_t *)(bs + 22) = MSC_FAT_SECTORS;
    *(uint16_t *)(bs + 24) = 32;
    *(uint16_t *)(bs + 26) = 64;
    *(uint32_t *)(bs + 28) = 0;
    *(uint32_t *)(bs + 32) = 0;
    bs[36] = 0x80;
    bs[38] = 0x29;
    *(uint32_t *)(bs + 39) = 0x4D344E54;
    memcpy(bs + 43, "M4NT1S     ", 11);
    memcpy(bs + 54, "FAT12   ", 8);
    bs[510] = 0x55; bs[511] = 0xAA;

    // --- FAT table ---
    uint8_t *fat = s_fat_img + MSC_RESERVED * MSC_SECTOR_SIZE;
    fat[0] = 0xF8; fat[1] = 0xFF;
    fat[2] = 0xFF; fat[3] = 0xFF;

    size_t clusters = (payload_len + MSC_SECTOR_SIZE - 1) / MSC_SECTOR_SIZE;
    if (clusters == 0) clusters = 1;
    for (size_t i = 0; i < clusters; i++) {
        uint16_t cnum = (uint16_t)(2 + i);
        uint16_t val  = (i == clusters - 1) ? 0xFFF : (uint16_t)(cnum + 1);
        size_t off = cnum + cnum / 2;
        if (cnum & 1) {
            fat[off]   = (fat[off] & 0x0F) | ((val << 4) & 0xF0);
            fat[off+1] = (val >> 4) & 0xFF;
        } else {
            fat[off]   = val & 0xFF;
            fat[off+1] = (fat[off+1] & 0xF0) | ((val >> 8) & 0x0F);
        }
    }

    // --- Root directory ---
    uint8_t *root = s_fat_img + (MSC_RESERVED + MSC_NUM_FATS*MSC_FAT_SECTORS) * MSC_SECTOR_SIZE;

    // Entry 0: volume label (attribute 0x08). This is what Windows reads
    // for FileSystemLabel / Win32_LogicalDisk.VolumeName. Putting it here
    // makes the label visible; the BPB copy alone is not always enough.
    memcpy(root + 0, "M4NT1S     ", 11);
    root[11] = 0x08;

    // Entry 1: PAYLOAD.PS1
    uint8_t *file = root + 32;
    memcpy(file + 0, "PAYLOAD ", 8);
    memcpy(file + 8, "PS1", 3);
    file[11] = 0x20;
    *(uint16_t *)(file + 26) = 2;
    *(uint32_t *)(file + 28) = (uint32_t)payload_len;

    // --- Payload data ---
    memcpy(s_fat_img + MSC_DATA_OFFSET, payload_ps1_start, payload_len);
    s_image_size = sizeof(s_fat_img);

    ESP_LOGI(TAG, "FAT image ready: %u bytes, payload %u bytes, cluster count %u",
             (unsigned)s_image_size, (unsigned)payload_len, (unsigned)clusters);
}

esp_err_t usb_msc_init(void)
{
    build_fat_image();
    return ESP_OK;
}

int32_t tud_msc_read10_cb(uint8_t lun, uint32_t lba, uint32_t offset, void *buffer, uint32_t bufsize)
{
    (void)lun;
    size_t start = (size_t)lba * MSC_SECTOR_SIZE + offset;
    if (start + bufsize > s_image_size) return -1;
    memcpy(buffer, s_fat_img + start, bufsize);
    return (int32_t)bufsize;
}

int32_t tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset, uint8_t *buffer, uint32_t bufsize)
{
    (void)lun; (void)lba; (void)offset; (void)buffer; (void)bufsize;
    return -1;
}

void tud_msc_capacity_cb(uint8_t lun, uint32_t *block_count, uint16_t *block_size)
{
    (void)lun;
    *block_count = s_image_size / MSC_SECTOR_SIZE;
    *block_size  = MSC_SECTOR_SIZE;
}

bool tud_msc_test_unit_ready_cb(uint8_t lun) { (void)lun; return true; }

void tud_msc_inquiry_cb(uint8_t lun, uint8_t vendor_id[8], uint8_t product_id[16], uint8_t product_rev[4])
{
    (void)lun;
    memcpy(vendor_id,  "M4NT1S  ", 8);
    memcpy(product_id, "PAYLOAD STICK   ", 16);
    memcpy(product_rev, "1.0 ", 4);
}

bool tud_msc_start_stop_cb(uint8_t lun, uint8_t power_condition, bool start, bool load_eject)
{
    (void)lun; (void)power_condition; (void)start; (void)load_eject;
    return true;
}

bool tud_msc_is_writable_cb(uint8_t lun) { (void)lun; return false; }