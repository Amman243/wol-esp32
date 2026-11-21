# ESP32 Wake-on-LAN (MicroPython + server web)

```
 HP / laptop di mana saja                 Rumah (satu jaringan)
 ┌───────────────┐   HTTPS   ┌────────┐  long-poll  ┌───────┐  magic packet  ┌────┐
 │ dashboard web │ ────────▶ │ server │ ◀────────── │ ESP32 │ ─────────────▶ │ PC │
 └───────────────┘           └────────┘  (keluar)   └───────┘   UDP 9/7      └────┘
```
ESP32 yang **konek keluar** ke server, jadi tidak perlu port forwarding / IP publik di rumah.
Command dari dashboard sampai ke ESP32 < 1 detik.

Cara wake: dashboard server, web lokal ESP32 (`http://wol-esp32.local/`), atau tombol BOOT di board.

## 1. ESP32 (tool.py, tanpa Arduino IDE)
Double-click `ESP32-WOL.bat`, atau `H:\python\python.exe tool.py <perintah>`:

| Perintah | Fungsi |
|---|---|
| `setup`   | flash firmware MicroPython + config + upload (pertama kali / board baru) |
| `upload`  | upload ulang `esp32/main.py` + `config.py` setelah diedit |
| `config`  | ganti WiFi / MAC PC / URL server / token |
| `monitor` | lihat log serial (Ctrl+C keluar) |
| `wake`    | tes wake lewat web lokal ESP32 |

Contoh pindah ke server hosting:
```
H:\python\python.exe tool.py config --server https://IP_VM:8443 --token <DEVICE_TOKEN> -y
```

## 2. Server (`server/`)
Node.js >= 18, **tanpa dependency** (tidak perlu `npm install`).

```
cd server
copy .env.example .env      # isi DEVICE_TOKEN & ADMIN_PASSWORD
node server.js              # http://localhost:3000
```

Env: `DEVICE_TOKEN` (sama dengan di `esp32/config.py`), `ADMIN_PASSWORD` (login dashboard),
`PORT` (3000), `HOLD_MS` (20000), `DATA_FILE` (data.json).

### Hosting di VM (nginx, akses langsung IP:port)
Paling gampang, dari PC Windows (butuh SSH ke VM):
```
H:\python\python.exe tool.py deploy --host ubuntu@IP_VM
```
Ini upload `server/` + `deploy/` ke VM, jalankan `deploy/install.sh` (pasang nginx, Node,
service systemd, sertifikat self-signed, buka firewall), lalu arahkan ESP32 ke VM.

Atau manual di VM: copy folder `server/` + `deploy/`, lalu `sudo bash deploy/install.sh`.

Hasilnya:
| Akses | Keterangan |
|---|---|
| `http://IP_VM:8080`  | dashboard, tanpa enkripsi |
| `https://IP_VM:8443` | dashboard, self-signed (browser kasih peringatan sekali). ESP32 pakai ini |

Ganti port: `--http-port 9000 --https-port 9443` (atau env `HTTP_PORT`/`HTTPS_PORT` di install.sh).
VM di cloud (AWS/GCP/Azure/dll): buka port tsb di Security Group panel provider.

File di VM:
- `/opt/esp32-wol/` kode server, `/etc/esp32-wol/wol.env` password & token
- `/etc/nginx/conf.d/esp32-wol.conf` config nginx (template: `deploy/nginx-esp32-wol.conf`)
- service `esp32-wol` -> `journalctl -u esp32-wol -f`, `systemctl restart esp32-wol`

Update kode server: jalankan `tool.py deploy` lagi (password & data tetap).
Tidak bisa di hosting serverless (Vercel/Netlify), karena butuh proses yang jalan terus.

### API
| Endpoint | Auth | Fungsi |
|---|---|---|
| `POST /api/login` `{password}` | - | login dashboard (cookie 30 hari) |
| `GET /api/state` | admin | status ESP32, PC, riwayat |
| `POST /api/command` `{type, data}` | admin | `wake` `{mac?}`, `refresh`, `reboot`, `config` `{pc_mac, pc_ip, broadcast}` |
| `POST /api/device/poll` | device | dipakai ESP32 |

Admin bisa pakai cookie atau header `Authorization: Bearer <ADMIN_PASSWORD>`, jadi bisa dibikin
shortcut di HP:
```
curl -k -X POST https://IP_VM:8443/api/command -H "Authorization: Bearer PASSWORD" -H "Content-Type: application/json" -d "{\"type\":\"wake\"}"
```

## Supaya PC bisa dibangunin
1. BIOS: aktifkan **Wake on LAN / Power On By PCI-E**, ErP = Disabled.
2. Windows: adapter Ethernet -> Wake on Magic Packet = Enabled (sudah).
3. Matikan **Fast Startup** (CMD admin): `powercfg /h off`
4. PC pakai kabel LAN, satu jaringan dengan ESP32.

## Catatan keamanan
- `esp32/config.py` & `server/.env` berisi password -> sudah di `.gitignore`, jangan di-commit.
- ESP32 tidak memverifikasi sertifikat HTTPS server (hemat RAM). Token tetap terenkripsi TLS.
