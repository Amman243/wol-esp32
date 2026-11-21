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
H:\python\python.exe tool.py config --server https://wol.domainmu.com --token <DEVICE_TOKEN> -y
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

### Hosting
Butuh server yang **jalan terus** (long-poll), contoh: VPS, Railway, Render, Fly.io, Koyeb.
**Tidak bisa** di Vercel / Netlify / Cloudflare Workers (serverless).

- **VPS**: `node server.js` pakai pm2/systemd + nginx/Caddy buat HTTPS.
  Nginx: set `proxy_read_timeout 60s;` (long-poll 20 dtk).
- **Docker**: `docker build -t wol server && docker run -d -p 3000:3000 --env-file server/.env -v wol:/data wol`
- **Railway/Render**: root directory `server`, start command `node server.js`, isi env di dashboard.

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
curl -X POST https://wol.domainmu.com/api/command -H "Authorization: Bearer PASSWORD" -H "Content-Type: application/json" -d "{\"type\":\"wake\"}"
```

## Supaya PC bisa dibangunin
1. BIOS: aktifkan **Wake on LAN / Power On By PCI-E**, ErP = Disabled.
2. Windows: adapter Ethernet -> Wake on Magic Packet = Enabled (sudah).
3. Matikan **Fast Startup** (CMD admin): `powercfg /h off`
4. PC pakai kabel LAN, satu jaringan dengan ESP32.

## Catatan keamanan
- `esp32/config.py` & `server/.env` berisi password -> sudah di `.gitignore`, jangan di-commit.
- ESP32 tidak memverifikasi sertifikat HTTPS server (hemat RAM). Token tetap terenkripsi TLS.
