# Contoh config. tool.py akan membuat esp32/config.py otomatis.
WIFI_SSID = "NamaWiFi"          # harus WiFi 2.4GHz
WIFI_PASSWORD = "passwordwifi"
PC_MAC = "EC:D6:8A:ED:A5:3E"    # MAC Ethernet PC yang mau dibangunin
PC_IP = "192.168.0.108"         # opsional, buat cek status online
BROADCAST = "192.168.0.255"
HOSTNAME = "wol-esp32"          # -> http://wol-esp32.local/
LED_PIN = 2                     # None kalau board tidak punya LED
BUTTON_PIN = 0                  # tombol BOOT (ESP32/S2/S3 = 0, C3/C6 = 9)
SERVER_URL = "https://wol.domainmu.com"  # kosongkan "" kalau cuma mau lokal
DEVICE_TOKEN = "token-acak-min-16-karakter"  # sama persis dengan env DEVICE_TOKEN di server
