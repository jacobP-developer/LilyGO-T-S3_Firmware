idf_component_register(SRCS "main.cpp"
                            "ui.c"
                            "input.c"
                            "serial_input.c"
                            "wifi_sniffer.c"
                            "ble_spam.c"
                            "beacon.c"
                            "portal.c"
                            "deauth.c"
                            "handshake.c"
                            "usb_protocol.c"
                            "sd_card.c"
                            "status_led.c"
                            "locked_ui.c"
                            "hid_payload.c"
                    INCLUDE_DIRS "."
                    EMBED_TXTFILES "../webpage.html"
                    REQUIRES esp_wifi bt driver fatfs sdmmc esp_netif esp_event
                             nvs_flash esp_driver_ledc esp_http_server lwip
                             t-dongle-s3
                             esp_tinyusb)