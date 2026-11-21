"""
ESP32 Wake-on-LAN tool - flash MicroPython + upload kode ke ESP32 tanpa Arduino IDE.

  python tool.py            -> menu
  python tool.py setup      -> flash firmware + bikin config + upload kode (pertama kali)
  python tool.py upload     -> upload ulang esp32/*.py saja (setelah edit kode)
  python tool.py config     -> ganti WiFi / MAC PC
  python tool.py monitor    -> lihat log serial ESP32 (Ctrl+C keluar)
  python tool.py wake       -> suruh ESP32 kirim magic packet (tes dari PC)
  python tool.py ports      -> lihat daftar COM port
  python tool.py deploy --host user@IP_VM   -> install server+nginx di VM, arahkan ESP32
  python tool.py bundle     -> bikin esp32-wol-vm.sh (1 file) buat deploy manual ke VM

Opsi: --port COM3  --ssid X  --password Y  --mac AA:BB:..  --ip 192.168.0.108
"""
import argparse, ipaddress, os, re, secrets, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
ESP_DIR = os.path.join(ROOT, "esp32")
FW_DIR = os.path.join(ROOT, "firmware")
CFG = os.path.join(ESP_DIR, "config.py")
PY = sys.executable

# CHIP_NAME esptool -> (board MicroPython, offset flash, LED_PIN, BUTTON_PIN)
CHIPS = {
    "ESP32":    ("ESP32_GENERIC",    "0x1000", 2,    0),
    "ESP32-S2": ("ESP32_GENERIC_S2", "0x1000", None, 0),
    "ESP32-S3": ("ESP32_GENERIC_S3", "0x0",    None, 0),
    "ESP32-C3": ("ESP32_GENERIC_C3", "0x0",    None, 9),
    "ESP32-C6": ("ESP32_GENERIC_C6", "0x0",    None, 9),
}
FALLBACK_FW = "20241129-v1.24.1"
# CP210x, CH340/CH9102, FTDI, Espressif native USB, Prolific
USB_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A, 0x067B}


def say(msg=""):
    print(msg, flush=True)


def die(msg):
    say("\n[X] " + msg)
    sys.exit(1)


# ------------------------------------------------------------------ deps
def ensure_deps():
    missing = []
    for mod, pkg in (("esptool", "esptool"), ("serial", "pyserial"), ("mpremote", "mpremote")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        say("Install " + ", ".join(missing) + " ...")
        subprocess.check_call([PY, "-m", "pip", "install", "--no-cache-dir",
                               "--disable-pip-version-check", "-q"] + missing)


# ------------------------------------------------------------------ port
def list_ports():
    from serial.tools import list_ports
    return sorted(list_ports.comports(), key=lambda p: p.device)


def find_port(wanted=None, quiet=False, wait_s=0):
    if wanted:
        return wanted
    t0 = time.time()
    while True:
        esp = [p for p in list_ports() if p.vid in USB_VIDS]
        if esp or time.time() - t0 >= wait_s:
            break
        time.sleep(0.5)
    if len(esp) == 1:
        if not quiet:
            say("Port ESP32: %s (%s)" % (esp[0].device, esp[0].description))
        return esp[0].device
    if len(esp) > 1:
        for i, p in enumerate(esp, 1):
            say("  %d. %s  %s" % (i, p.device, p.description))
        n = input("Pilih nomor port: ").strip()
        return esp[int(n) - 1].device
    say("ESP32 tidak terdeteksi. Port yang ada:")
    for p in list_ports():
        say("  %s  %s" % (p.device, p.description))
    die("Colok ESP32 pakai kabel USB DATA (bukan kabel charge doang), cek driver CP210x/CH340, "
        "atau kasih --port COMx")


# ------------------------------------------------------------------ chip & firmware
def detect_chip(port):
    say("Deteksi chip di %s ..." % port)
    r = subprocess.run([PY, "-m", "esptool", "--port", port, "flash-id"],
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    m = re.search(r"Connected to (ESP32(?:-[A-Z0-9]+)?)", out) or re.search(r"Chip is (ESP32(?:-[A-Z0-9]+)?)", out)
    if not m:
        say(out[-1500:])
        die("Gagal konek ke ESP32. Coba tahan tombol BOOT pas proses connect, lalu ulangi.")
    chip = m.group(1)
    if chip not in CHIPS:
        die("Chip %s belum didukung tool ini." % chip)
    say("Chip: " + chip)
    return chip


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "esp32-wol-tool"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def get_firmware(board):
    os.makedirs(FW_DIR, exist_ok=True)
    name = None
    try:
        page = http_get("https://micropython.org/download/%s/" % board).decode("utf-8", "replace")
        found = set(re.findall(r"(%s-\d{8}-v(\d+)\.(\d+)\.(\d+)\.bin)" % re.escape(board), page))
        if found:
            name = max(found, key=lambda f: tuple(int(x) for x in f[1:]))[0]
    except Exception as e:
        say("Gagal cek versi terbaru (%s), pakai %s" % (e, FALLBACK_FW))
    name = name or "%s-%s.bin" % (board, FALLBACK_FW)
    path = os.path.join(FW_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 100_000:
        say("Firmware sudah ada: firmware/" + name)
        return path
    say("Download firmware " + name + " ...")
    data = http_get("https://micropython.org/resources/firmware/" + name)
    with open(path, "wb") as f:
        f.write(data)
    say("OK (%d KB)" % (len(data) // 1024))
    return path


def flash_firmware(port, chip):
    board, offset, _, _ = CHIPS[chip]
    fw = get_firmware(board)
    say("\nHapus flash (erase) ...")
    if subprocess.call([PY, "-m", "esptool", "--port", port, "erase-flash"]):
        die("Erase gagal.")
    say("\nTulis firmware MicroPython ...")
    if subprocess.call([PY, "-m", "esptool", "--port", port, "--baud", "460800",
                        "write-flash", "-z", offset, fw]):
        say("Baud 460800 gagal, coba 115200 ...")
        if subprocess.call([PY, "-m", "esptool", "--port", port, "--baud", "115200",
                            "write-flash", "-z", offset, fw]):
            die("Flash firmware gagal.")
    say("Firmware terpasang. Tunggu ESP32 boot pertama (bikin filesystem) ...")
    time.sleep(6)


# ------------------------------------------------------------------ config
def detect_pc_net():
    """Cari adapter fisik yang Up & punya IPv4 (prioritas Ethernet)."""
    ps = ("Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | ForEach-Object { "
          "$ip = Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue "
          "| Select-Object -First 1; '{0}|{1}|{2}|{3}|{4}' -f $_.Name,$_.MacAddress,$ip.IPAddress,"
          "$ip.PrefixLength,$_.MediaType }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    best = None
    for line in out.splitlines():
        p = line.strip().split("|")
        if len(p) < 5 or not p[2] or p[2].startswith("169.254"):
            continue
        cand = {"name": p[0], "mac": p[1].replace("-", ":").upper(), "ip": p[2],
                "bcast": str(ipaddress.ip_network("%s/%s" % (p[2], p[3]), strict=False).broadcast_address),
                "eth": "802.3" in p[4]}
        if best is None or (cand["eth"] and not best["eth"]):
            best = cand
    return best


def read_config():
    cfg = {}
    if os.path.exists(CFG):
        try:
            exec(open(CFG, encoding="utf-8").read(), {}, cfg)
        except Exception:
            pass
    return cfg


def ask(label, default=None):
    v = input("%s%s: " % (label, " [%s]" % default if default not in (None, "") else "")).strip()
    return v or default


def make_config(args, chip=None, interactive=True):
    old = read_config()
    net = detect_pc_net() or {}
    if net and not old.get("PC_MAC"):
        say("PC ini terdeteksi: %s  MAC %s  IP %s" % (net["name"], net["mac"], net["ip"]))
    _, _, led, btn = CHIPS.get(chip or "", (None, None, old.get("LED_PIN", 2), old.get("BUTTON_PIN", 0)))
    c = {
        "WIFI_SSID": args.ssid or old.get("WIFI_SSID"),
        "WIFI_PASSWORD": args.password if args.password is not None else old.get("WIFI_PASSWORD"),
        "PC_MAC": args.mac or old.get("PC_MAC") or net.get("mac"),
        "PC_IP": args.ip or old.get("PC_IP") or net.get("ip"),
        "BROADCAST": old.get("BROADCAST") or net.get("bcast") or "255.255.255.255",
        "HOSTNAME": old.get("HOSTNAME", "wol-esp32"),
        "LED_PIN": led,
        "BUTTON_PIN": btn,
        "SERVER_URL": (args.server if args.server is not None else old.get("SERVER_URL", "")).rstrip("/"),
        "DEVICE_TOKEN": args.token or old.get("DEVICE_TOKEN") or secrets.token_urlsafe(24),
    }
    if interactive:
        say("\n--- Config (Enter = pakai nilai di [kurung], '-' = kosongkan) ---")
        c["WIFI_SSID"] = ask("Nama WiFi (2.4GHz)", c["WIFI_SSID"])
        c["WIFI_PASSWORD"] = ask("Password WiFi", c["WIFI_PASSWORD"])
        c["PC_MAC"] = ask("MAC address PC yang mau dibangunin", c["PC_MAC"])
        c["PC_IP"] = ask("IP PC (buat cek status, boleh kosong)", c["PC_IP"])
        c["SERVER_URL"] = ask("URL server (https://domain-kamu, kosong = lokal saja)", c["SERVER_URL"])
        c["DEVICE_TOKEN"] = ask("DEVICE_TOKEN (harus sama dengan di server)", c["DEVICE_TOKEN"])
        for k in ("PC_IP", "SERVER_URL"):
            if c[k] == "-":
                c[k] = ""
    c["SERVER_URL"] = (c["SERVER_URL"] or "").rstrip("/")
    if c["SERVER_URL"] and not re.match(r"https?://[^/\s]+", c["SERVER_URL"]):
        die("URL server harus diawali http:// atau https://")
    if not c["WIFI_SSID"]:
        die("Nama WiFi wajib diisi (--ssid).")
    if not re.fullmatch(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", c["PC_MAC"] or ""):
        die("MAC tidak valid: %s" % c["PC_MAC"])
    c["PC_MAC"] = c["PC_MAC"].replace("-", ":").upper()
    if c["PC_IP"]:
        try:
            if c["PC_IP"] == net.get("ip"):
                c["BROADCAST"] = net["bcast"]
            else:  # IP diisi manual -> asumsi subnet /24
                c["BROADCAST"] = str(ipaddress.ip_network(c["PC_IP"] + "/24", strict=False).broadcast_address)
        except ValueError:
            die("IP tidak valid: %s" % c["PC_IP"])
    with open(CFG, "w", encoding="utf-8") as f:
        f.write("# dibuat otomatis oleh tool.py\n")
        for k, v in c.items():
            f.write("%s = %r\n" % (k, v))
    say("Config disimpan: esp32/config.py")
    if c["SERVER_URL"]:
        say("Server: %s\nSet di server -> DEVICE_TOKEN=%s" % (c["SERVER_URL"], c["DEVICE_TOKEN"]))
    return c


# ------------------------------------------------------------------ upload & monitor
def open_serial(port, tries=20):
    import serial
    for _ in range(tries):
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = port, 115200, 0.2
            s.dtr = False
            s.rts = False
            s.open()
            return s
        except Exception:
            time.sleep(0.5)
    die("Tidak bisa buka %s (dipakai program lain? tutup serial monitor lain)." % port)


def upload(port):
    if not os.path.exists(CFG):
        die("esp32/config.py belum ada. Jalankan: python tool.py config")
    say("\nUpload config.py + main.py ke ESP32 ...")
    cmd = [PY, "-m", "mpremote", "connect", port,
           "fs", "cp", os.path.join(ESP_DIR, "config.py"), ":config.py", "+",
           "fs", "cp", os.path.join(ESP_DIR, "main.py"), ":main.py", "+",
           # hapus override dari server supaya config.py yang baru dipakai
           "exec", "import os\ntry:\n os.remove('settings.json')\nexcept OSError:\n pass"]
    if subprocess.call(cmd):
        die("Upload gagal. Cabut-colok ESP32 lalu coba lagi.")
    say("Upload OK. Restart ESP32 & tunggu IP ...\n")
    watch(port, restart=True, until_ip=True)


def watch(port, restart=False, until_ip=False, timeout_s=45):
    s = open_serial(port)
    if restart:  # Ctrl+C lalu Ctrl+D = soft reset -> main.py jalan ulang
        s.write(b"\r\x03\x03")
        time.sleep(0.3)
        s.write(b"\x04")
    t0 = time.time()
    buf = b""
    try:
        while True:
            data = s.read(256)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
                buf = (buf + data)[-4000:]
                if until_ip:
                    m = re.search(rb"Buka (http://\S+/)\s+atau\s+(http://\S+/)", buf)
                    if m:
                        say("\n\n=== BERES! Buka di browser HP/PC (satu WiFi): ===")
                        say("   " + m.group(1).decode())
                        say("   " + m.group(2).decode())
                        return
            if until_ip and time.time() - t0 > timeout_s:
                say("\n\nBelum dapat IP. Cek nama/password WiFi (harus 2.4GHz). "
                    "Ganti pakai: python tool.py config, lalu python tool.py upload")
                return
    except KeyboardInterrupt:
        say("\n(keluar monitor)")
    finally:
        s.close()


def deploy(args):
    """Upload server ke VM via ssh/scp, install nginx+service, lalu arahkan ESP32 ke VM."""
    import shutil, tempfile
    target = args.host or ask("SSH ke VM (user@ip)")
    if not target:
        die("Isi --host user@ip")
    if not shutil.which("ssh") or not shutil.which("scp"):
        die("ssh/scp tidak ada. Aktifkan OpenSSH Client di Windows Optional Features.")
    cfg = read_config()
    token = args.token or cfg.get("DEVICE_TOKEN") or secrets.token_urlsafe(24)
    vm_ip = args.vm_ip or target.split("@")[-1]

    stage = tempfile.mkdtemp(prefix="wol-deploy-")
    try:
        os.makedirs(os.path.join(stage, "server"))
        for f in ("server.js", "package.json"):
            shutil.copy(os.path.join(ROOT, "server", f), os.path.join(stage, "server", f))
        shutil.copytree(os.path.join(ROOT, "server", "public"), os.path.join(stage, "server", "public"))
        shutil.copytree(os.path.join(ROOT, "deploy"), os.path.join(stage, "deploy"))
        remote = "/tmp/esp32-wol-deploy"
        say("Upload file ke %s ..." % target)
        if subprocess.call(["ssh", target, "rm -rf " + remote]):
            die("SSH gagal. Cek user@ip, port 22, dan key/password.")
        if subprocess.call(["scp", "-q", "-r", stage, "%s:%s" % (target, remote)]):
            die("scp gagal.")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    env = ["DEVICE_TOKEN=" + token, "HTTP_PORT=%d" % args.http_port,
           "HTTPS_PORT=%d" % args.https_port, "PUBLIC_IP=" + vm_ip]
    if args.admin_password:
        env.append("ADMIN_PASSWORD=" + args.admin_password)
    cmd = "env %s bash %s/deploy/install.sh" % (" ".join(env), remote)
    remote_cmd = 'if [ "$(id -u)" -eq 0 ]; then %s; else sudo %s; fi; rm -rf %s' % (cmd, cmd, remote)
    say("Install di VM (mungkin diminta password sudo) ...\n")
    if subprocess.call(["ssh", "-t", target, remote_cmd]):
        die("Install di VM gagal, lihat pesan di atas.")

    url = "https://%s:%d" % (vm_ip, args.https_port)
    if input("\nArahkan ESP32 ke %s sekarang? [Y/n]: " % url).strip().lower() in ("", "y"):
        args.server, args.token = url, token
        make_config(args, interactive=False)
        upload(find_port(args.port))


BUNDLE = os.path.join(ROOT, "esp32-wol-vm.sh")


def make_bundle(args):
    """Bikin 1 file installer .sh (server + nginx + service) buat dicopy manual ke VM."""
    import base64, io, tarfile
    token = args.token or read_config().get("DEVICE_TOKEN") or secrets.token_urlsafe(24)

    def norm(ti):
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        ti.mode = 0o755 if ti.isdir() or ti.name.endswith(".sh") else 0o644
        return ti

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in ("server/server.js", "server/package.json", "server/public", "deploy"):
            tar.add(os.path.join(ROOT, rel), arcname=rel, filter=norm)
    b64 = base64.b64encode(buf.getvalue()).decode()
    b64 = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))

    script = """#!/usr/bin/env bash
# ESP32 Wake-on-LAN - installer 1 file (server + dashboard + nginx + systemd)
#
# Di VM:  sudo bash esp32-wol-vm.sh
#
# Opsional: HTTP_PORT=8080 HTTPS_PORT=8443 ADMIN_PASSWORD=xxx sudo -E bash esp32-wol-vm.sh
# Aman dijalankan ulang buat update (password & data tidak hilang).
set -euo pipefail
export DEVICE_TOKEN="${DEVICE_TOKEN:-%s}"
[ "$(id -u)" -eq 0 ] || { echo "Jalankan pakai sudo/root: sudo bash $0"; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
base64 -d > "$TMP/bundle.tgz" <<'__BUNDLE__'
%s
__BUNDLE__
tar -xzf "$TMP/bundle.tgz" -C "$TMP"
bash "$TMP/deploy/install.sh"
""" % (token, b64)
    with open(BUNDLE, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)
    say("Installer dibuat: %s (%d KB)" % (BUNDLE, os.path.getsize(BUNDLE) // 1024))
    say("DEVICE_TOKEN di dalamnya: " + token)
    return token


def wake_via_http():
    cfg = read_config()
    host = ask("IP / host ESP32", "%s.local" % cfg.get("HOSTNAME", "wol-esp32"))
    req = urllib.request.Request("http://%s/wake" % host, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            say(r.read().decode())
    except Exception as e:
        die("Gagal hubungi ESP32 di %s: %s" % (host, e))


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="ESP32 Wake-on-LAN tool")
    ap.add_argument("cmd", nargs="?", choices=["setup", "upload", "config", "monitor", "wake", "ports", "deploy", "bundle"])
    ap.add_argument("--port")
    ap.add_argument("--ssid")
    ap.add_argument("--password")
    ap.add_argument("--mac")
    ap.add_argument("--ip")
    ap.add_argument("--server", help="URL server, contoh https://wol.domainmu.com")
    ap.add_argument("--token", help="DEVICE_TOKEN (sama dengan di server)")
    ap.add_argument("--host", help="deploy: SSH target VM, contoh ubuntu@203.0.113.10")
    ap.add_argument("--vm-ip", help="deploy: IP publik VM (default dari --host)")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--https-port", type=int, default=8443)
    ap.add_argument("--admin-password", help="deploy: password dashboard (default acak)")
    ap.add_argument("-y", "--yes", action="store_true", help="jangan tanya-tanya (pakai argumen/config)")
    args = ap.parse_args()

    cmd = args.cmd
    if not cmd:
        say("=== ESP32 Wake-on-LAN Tool ===")
        say(" 1. Setup lengkap (flash firmware + config + upload)  <- pertama kali")
        say(" 2. Upload ulang kode")
        say(" 3. Ganti config (WiFi / MAC)")
        say(" 4. Serial monitor")
        say(" 5. Tes wake dari PC ini")
        say(" 6. Lihat COM port")
        say(" 7. Deploy server ke VM (ssh) + arahkan ESP32")
        say(" 8. Bikin installer .sh buat deploy manual ke VM")
        cmd = {"1": "setup", "2": "upload", "3": "config", "4": "monitor",
               "5": "wake", "6": "ports", "7": "deploy", "8": "bundle"}.get(input("Pilih: ").strip())
        if not cmd:
            die("Pilihan tidak valid.")

    ensure_deps()
    interactive = not args.yes

    if cmd == "ports":
        for p in list_ports():
            say("  %-6s %s%s" % (p.device, p.description, "   <- ESP32?" if p.vid in USB_VIDS else ""))
    elif cmd == "config":
        make_config(args, interactive=interactive)
        if input("Upload ke ESP32 sekarang? [Y/n]: ").strip().lower() in ("", "y"):
            upload(find_port(args.port))
    elif cmd == "setup":
        port = find_port(args.port)
        chip = detect_chip(port)
        make_config(args, chip=chip, interactive=interactive)
        flash_firmware(port, chip)
        upload(find_port(args.port, quiet=True, wait_s=15))
    elif cmd == "upload":
        upload(find_port(args.port))
    elif cmd == "monitor":
        port = find_port(args.port)
        say("Serial monitor %s - Ctrl+C buat keluar\n" % port)
        watch(port)
    elif cmd == "wake":
        wake_via_http()
    elif cmd == "deploy":
        deploy(args)
    elif cmd == "bundle":
        make_bundle(args)


if __name__ == "__main__":
    main()
