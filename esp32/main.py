# main.py - ESP32 Wake-on-LAN (MicroPython, asyncio)
#
# Cara wake:
#   1. Web lokal  : http://<ip-esp32>/  atau  http://<HOSTNAME>.local/
#   2. Tombol BOOT di board
#   3. Server online (kalau SERVER_URL + DEVICE_TOKEN diisi di config.py):
#      ESP32 long-poll ke server -> tidak perlu port forwarding.
import network, socket, time, machine, ubinascii, gc, json, asyncio
import config

VERSION = "2.0"
SETTINGS_FILE = "settings.json"  # override dari server (MAC/IP target)


def cfg(name, default=None):
    return getattr(config, name, default)


# ---------------------------------------------------------------- settings
S = {"pc_mac": config.PC_MAC, "pc_ip": cfg("PC_IP"), "broadcast": cfg("BROADCAST")}
try:
    with open(SETTINGS_FILE) as f:
        S.update(json.load(f))
except Exception:
    pass


def apply_settings(d):
    for k in ("pc_mac", "pc_ip", "broadcast"):
        if k in d:
            if k == "pc_mac":
                mac_bytes(d[k])  # validasi
            S[k] = d[k] or None
    with open(SETTINGS_FILE, "w") as f:
        json.dump(S, f)
    print("Settings baru:", S)


# ---------------------------------------------------------------- hardware
def _pin_out(n):
    if n is None:
        return None
    try:
        p = machine.Pin(n, machine.Pin.OUT)
        p.value(0)
        return p
    except Exception:
        return None


led = _pin_out(cfg("LED_PIN"))
btn_flag = False


def _btn_irq(_):
    global btn_flag
    btn_flag = True


if cfg("BUTTON_PIN") is not None:
    btn = machine.Pin(cfg("BUTTON_PIN"), machine.Pin.IN, machine.Pin.PULL_UP)
    btn.irq(trigger=machine.Pin.IRQ_FALLING, handler=_btn_irq)


def blink(n=1, ms=80):
    if not led:
        return
    for _ in range(n):
        led.value(1)
        time.sleep_ms(ms)
        led.value(0)
        time.sleep_ms(ms)


# ---------------------------------------------------------------- magic packet
def mac_bytes(mac):
    h = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(h) != 12:
        raise ValueError("MAC salah: " + mac)
    return ubinascii.unhexlify(h)


wlan = network.WLAN(network.STA_IF)
wake_count = 0
last_wake = None
pc_state = None  # True / False / None


def broadcast_targets():
    t = ["255.255.255.255"]
    b = S.get("broadcast")
    if b and b not in t:
        t.append(b)
    try:
        ip, mask = wlan.ifconfig()[:2]
        ipn = [int(x) for x in ip.split(".")]
        mk = [int(x) for x in mask.split(".")]
        bc = ".".join(str(ipn[i] | (~mk[i] & 255)) for i in range(4))
        if bc not in t:
            t.append(bc)
    except Exception:
        pass
    return t


def send_wol(mac=None):
    global wake_count, last_wake
    mac = mac or S["pc_mac"]
    packet = b"\xff" * 6 + mac_bytes(mac) * 16  # 102 byte
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    try:
        try:
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BROADCAST", 0x20), 1)
        except Exception:
            pass
        for _ in range(3):
            for addr in broadcast_targets():
                for port in (9, 7):
                    try:
                        s.sendto(packet, (addr, port))
                        sent += 1
                    except Exception as e:
                        print("Gagal kirim ke", addr, port, e)
            time.sleep_ms(50)
    finally:
        s.close()
    wake_count += 1
    last_wake = time.ticks_ms()
    print("Magic packet -> %s (%d paket)" % (mac, sent))
    blink(3)
    return sent


def check_pc():
    """TCP connect ke PC. Refused/reset = hidup, timeout = mati."""
    global pc_state
    ip = S.get("pc_ip")
    if not ip or not wlan.isconnected():
        pc_state = None
        return pc_state
    result = False
    for port in (445, 3389, 135):
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect((ip, port))
            result = True
        except OSError as e:
            if e.args and e.args[0] in (104, 111):
                result = True
        finally:
            s.close()
        if result:
            break
    pc_state = result
    return result


def status():
    try:
        rssi = wlan.status("rssi")
    except Exception:
        rssi = None
    return {
        "version": VERSION, "hostname": config.HOSTNAME,
        "ip": wlan.ifconfig()[0] if wlan.isconnected() else None, "rssi": rssi,
        "uptime_s": time.ticks_ms() // 1000, "free_mem": gc.mem_free(),
        "pc_online": pc_state, "pc_mac": S["pc_mac"], "pc_ip": S.get("pc_ip"),
        "broadcast": S.get("broadcast"), "wake_count": wake_count,
        "last_wake_s_ago": None if last_wake is None else time.ticks_diff(time.ticks_ms(), last_wake) // 1000,
        "cloud": cloud_ok,
    }


# ---------------------------------------------------------------- WiFi
def wifi_connect(timeout_s=30):
    try:
        network.hostname(config.HOSTNAME)
    except Exception:
        pass
    wlan.active(True)
    if wlan.isconnected():
        return True
    try:
        wlan.disconnect()
    except Exception:
        pass
    print("Konek ke WiFi '%s' ..." % config.WIFI_SSID)
    wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    t0 = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_s * 1000:
            print("Gagal konek WiFi (status %s). Cek SSID/password & harus 2.4GHz." % wlan.status())
            return False
        if led:
            led.value(not led.value())
        time.sleep_ms(250)
    if led:
        led.value(0)
    print("WiFi OK")
    return True


# ---------------------------------------------------------------- web lokal
HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Wake PC</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e8e8e8;font-family:system-ui,sans-serif}
.c{text-align:center;padding:24px}h1{font-size:20px;font-weight:600;margin:0 0 6px}
.m{color:#8b93a1;font-size:13px;margin-bottom:28px}
button{width:220px;height:220px;border-radius:50%;border:0;background:#2563eb;color:#fff;
font-size:26px;font-weight:700;cursor:pointer;box-shadow:0 0 0 10px #2563eb33}
button:active{transform:scale(.96)}button:disabled{background:#374151;box-shadow:none}
#s{margin-top:28px;min-height:22px}.st{color:#8b93a1;font-size:14px;margin-top:10px}
.on{color:#22c55e}.off{color:#f59e0b}
</style></head><body><div class="c">
<h1>Wake on LAN</h1><div class="m" id="mac">MAC %MAC%</div>
<button id="b" onclick="w()">WAKE PC</button>
<div id="s"></div><div class="st" id="p">cek status PC...</div></div>
<script>
async function w(){const b=document.getElementById('b'),s=document.getElementById('s');
b.disabled=true;s.textContent='Mengirim magic packet...';
try{const r=await(await fetch('/wake',{method:'POST'})).json();
s.textContent=r.ok?'Terkirim! ('+r.sent+' paket) Tunggu PC nyala...':'Gagal';}
catch(e){s.textContent='Error: '+e}setTimeout(()=>b.disabled=false,2000);}
async function st(){try{const r=await(await fetch('/status')).json(),p=document.getElementById('p');
document.getElementById('mac').textContent='MAC '+r.pc_mac+(r.cloud?' · server: terhubung':'');
if(r.pc_online===null){p.textContent='';return}
p.innerHTML=r.pc_online?'PC: <span class=on>ONLINE</span>':'PC: <span class=off>tidak merespon (mati / firewall)</span>';}catch(e){}}
st();setInterval(st,5000);
</script></body></html>"""


async def handle_http(reader, writer):
    try:
        line = await asyncio.wait_for(reader.readline(), 5)
        while True:  # buang header
            h = await asyncio.wait_for(reader.readline(), 5)
            if not h or h == b"\r\n":
                break
        parts = line.decode().split(" ")
        path = (parts[1] if len(parts) > 1 else "/").split("?")[0]
        code, ctype = "200 OK", "application/json"
        if path == "/wake":
            body = json.dumps({"ok": True, "sent": send_wol()})
        elif path == "/status":
            body = json.dumps(status())
        elif path == "/":
            ctype, body = "text/html; charset=utf-8", HTML.replace("%MAC%", S["pc_mac"])
        else:
            code, ctype, body = "404 Not Found", "text/plain", "not found"
        body = body.encode()
        writer.write(("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                      "Cache-Control: no-store\r\nConnection: close\r\n\r\n" % (code, ctype, len(body))).encode())
        writer.write(body)
        await writer.drain()
    except Exception as e:
        print("HTTP error:", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------- cloud (long-poll)
cloud_ok = False
_ssl_ctx = None


def parse_url(url):
    proto, rest = url.split("://", 1)
    host, _, base = rest.partition("/")
    tls = proto == "https"
    port = 443 if tls else 80
    if ":" in host:
        host, p = host.split(":")
        port = int(p)
    return tls, host, port, ("/" + base).rstrip("/")


def dechunk(b):
    out = b""
    while b:
        line, _, b = b.partition(b"\r\n")
        n = int(line.split(b";")[0].strip() or b"0", 16)
        if n == 0:
            break
        out += b[:n]
        b = b[n + 2:]
    return out


async def http_post(url, path, payload, timeout_s):
    global _ssl_ctx
    tls, host, port, base = parse_url(url)
    ctx = None
    if tls:
        if _ssl_ctx is None:
            import ssl
            _ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            _ssl_ctx.verify_mode = ssl.CERT_NONE
        ctx = _ssl_ctx
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), 15)
    try:
        data = json.dumps(payload).encode()
        hosthdr = host if port in (80, 443) else "%s:%d" % (host, port)
        writer.write(("POST %s%s HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
                      "Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                      % (base, path, hosthdr, config.DEVICE_TOKEN, len(data))).encode())
        writer.write(data)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout_s)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    head, _, body = raw.partition(b"\r\n\r\n")
    code = int(head.split(b" ", 2)[1])
    if b"transfer-encoding: chunked" in head.lower():
        body = dechunk(body)
    return code, body


def run_command(c):
    t = c.get("type")
    d = c.get("data") or {}
    if t == "wake":
        n = send_wol(d.get("mac"))
        return True, "%d paket ke %s" % (n, d.get("mac") or S["pc_mac"])
    if t == "refresh":
        r = check_pc()
        return True, "PC " + ("nyala" if r else "mati" if r is False else "tidak dicek")
    if t == "config":
        apply_settings(d)
        check_pc()
        return True, "tersimpan"
    if t == "reboot":
        return True, "restart..."
    return False, "command tidak dikenal: %s" % t


async def cloud_task():
    global cloud_ok
    url = cfg("SERVER_URL")
    if not url or not cfg("DEVICE_TOKEN"):
        print("Mode server: OFF (SERVER_URL / DEVICE_TOKEN kosong)")
        return
    print("Mode server: ON ->", url)
    acks, reboot, backoff = [], False, 2
    while True:
        if not wlan.isconnected():
            cloud_ok = False
            await asyncio.sleep(2)
            continue
        try:
            body = {"status": status(), "acks": acks}
            if reboot:
                body["hold"] = 0
            code, resp = await http_post(url, "/api/device/poll", body, 45)
            if code == 401:
                cloud_ok = False
                print("Server menolak DEVICE_TOKEN (401). Cek token di config.py & server.")
                await asyncio.sleep(30)
                continue
            if code != 200:
                raise OSError("HTTP %d" % code)
            acks = []
            if reboot:
                print("Restart atas perintah server")
                await asyncio.sleep_ms(300)
                machine.reset()
            if not cloud_ok:
                print("Terhubung ke server")
            cloud_ok, backoff = True, 2
            for c in json.loads(resp).get("commands", []):
                print("Command dari server:", c.get("type"))
                try:
                    ok, msg = run_command(c)
                except Exception as e:
                    ok, msg = False, str(e)
                if c.get("type") == "reboot" and ok:
                    reboot = True
                acks.append({"id": c.get("id"), "ok": ok, "msg": msg})
        except Exception as e:
            if cloud_ok:
                print("Koneksi server putus:", e)
            elif backoff == 2:
                print("Server tidak bisa dihubungi:", repr(e))
            cloud_ok = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        gc.collect()


# ---------------------------------------------------------------- tasks lain
async def button_task():
    global btn_flag
    last = 0
    while True:
        if btn_flag:
            btn_flag = False
            if time.ticks_diff(time.ticks_ms(), last) > 1000:
                last = time.ticks_ms()
                print("Tombol ditekan")
                send_wol()
        await asyncio.sleep_ms(50)


async def watchdog_task():
    n = 0
    while True:
        await asyncio.sleep(5)
        if not wlan.isconnected():
            print("WiFi putus, reconnect...")
            wifi_connect()
        n += 1
        if n % 3 == 0:  # cek PC tiap 15 detik
            check_pc()
        gc.collect()


async def main():
    print("\n=== ESP32 Wake-on-LAN v%s ===  target MAC: %s" % (VERSION, S["pc_mac"]))
    while not wifi_connect():
        time.sleep(5)
    ip = wlan.ifconfig()[0]
    print("IP ESP32:", ip)
    print("Buka http://%s/  atau  http://%s.local/" % (ip, config.HOSTNAME))
    check_pc()
    await asyncio.start_server(handle_http, "0.0.0.0", 80, backlog=3)
    print("Web lokal jalan di port 80. Tombol BOOT juga bisa dipakai buat wake.")
    blink(2, 150)
    asyncio.create_task(button_task())
    asyncio.create_task(watchdog_task())
    asyncio.create_task(cloud_task())
    while True:
        await asyncio.sleep(3600)


try:
    asyncio.run(main())
except Exception as e:  # Ctrl+C (KeyboardInterrupt) tidak ditangkap -> aman buat upload
    print("Fatal:", e, "- restart 5 detik lagi")
    time.sleep(5)
    machine.reset()
