#!/usr/bin/env bash
# ESP32 Wake-on-LAN - installer buat VM Linux (Ubuntu/Debian/RHEL/Rocky/Alma/Fedora)
#
#   sudo bash deploy/install.sh
#
# Opsional (env):
#   HTTP_PORT=8080 HTTPS_PORT=8443 APP_PORT=3000
#   ADMIN_PASSWORD=...  DEVICE_TOKEN=...   (kosong = pakai yang lama / bikin acak)
#   PUBLIC_IP=1.2.3.4                      (buat sertifikat & info; default auto-detect)
#   RENEW_CERT=1                           (bikin ulang sertifikat, misal IP VM berubah)
# Aman dijalankan ulang (update kode tanpa hapus data/password).
set -euo pipefail

HTTP_PORT="${HTTP_PORT:-8080}"
HTTPS_PORT="${HTTPS_PORT:-8443}"
APP_PORT="${APP_PORT:-3780}"   # port internal Node (cuma 127.0.0.1)
APP_DIR=/opt/esp32-wol
ETC_DIR=/etc/esp32-wol
DATA_DIR=/var/lib/esp32-wol
ENV_FILE="$ETC_DIR/wol.env"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[X]\033[0m %s\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Jalankan pakai sudo: sudo bash $0"
[ -f "$SRC/server/server.js" ] || die "server/server.js tidak ketemu di $SRC"

HAS_SYSTEMD=0
[ -d /run/systemd/system ] && HAS_SYSTEMD=1

# ------------------------------------------------------------ cek port bentrok
# port_owner 8080 -> nama proses yang listen (kosong = bebas)
port_owner() {
  ss -Htlnp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {print $NF; exit}' | sed -n 's/.*(("\([^"]*\)".*/\1/p'
}
if [ "$HAS_SYSTEMD" -eq 1 ] && systemctl is-active -q esp32-wol 2>/dev/null; then
  systemctl stop esp32-wol   # instalasi ulang: lepas port lama dulu
fi
own="$(port_owner "$APP_PORT")"
if [ -n "$own" ]; then
  for p in $(seq 3781 3799); do
    if [ -z "$(port_owner "$p")" ]; then
      warn "Port $APP_PORT dipakai '$own', server WoL pakai port internal $p"
      APP_PORT="$p"; break
    fi
  done
fi
for p in "$HTTP_PORT" "$HTTPS_PORT"; do
  own="$(port_owner "$p")"
  if [ -n "$own" ] && [ "$own" != "nginx" ]; then
    die "Port $p sudah dipakai '$own'. Pakai port lain: HTTP_PORT=.. HTTPS_PORT=.. (tool.py: --http-port/--https-port)"
  fi
done
PORT80_OWNER="$(port_owner 80)"

# ------------------------------------------------------------ paket
node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  [ "$major" -ge 18 ]
}

say "Install paket (nginx, openssl, node) ..."
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  if ! command -v nginx >/dev/null 2>&1 && [ -n "$PORT80_OWNER" ]; then
    # port 80 dipakai '$PORT80_OWNER' -> cegah nginx auto-start (bakal gagal & bikin apt error)
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
    chmod +x /usr/sbin/policy-rc.d
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
  fi
  apt-get install -y -qq nginx openssl curl ca-certificates >/dev/null
  rm -f /usr/sbin/policy-rc.d
  if ! node_ok; then
    apt-get install -y -qq nodejs >/dev/null 2>&1 || true
    if ! node_ok; then
      say "Node dari repo distro terlalu lama, pasang Node 20 dari NodeSource ..."
      curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
      apt-get install -y -qq nodejs >/dev/null
    fi
  fi
elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
  PM="$(command -v dnf || command -v yum)"
  "$PM" install -y -q nginx openssl curl >/dev/null
  if ! node_ok; then
    "$PM" module enable -y -q nodejs:20 >/dev/null 2>&1 || true
    "$PM" install -y -q nodejs >/dev/null 2>&1 || true
    if ! node_ok; then
      curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - >/dev/null
      "$PM" install -y -q nodejs >/dev/null
    fi
  fi
else
  die "Package manager tidak dikenal. Install manual: nginx, openssl, node >= 18"
fi
node_ok || die "Node.js >= 18 gagal dipasang"
NODE_BIN="$(command -v node)"
say "Node $(node -v) di $NODE_BIN, $(nginx -v 2>&1)"

# ------------------------------------------------------------ user & file
id wol >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin wol
mkdir -p "$APP_DIR" "$ETC_DIR/ssl" "$DATA_DIR"
rm -rf "$APP_DIR/public"
cp "$SRC/server/server.js" "$SRC/server/package.json" "$APP_DIR/"
cp -r "$SRC/server/public" "$APP_DIR/"
chown -R root:root "$APP_DIR"
chown wol:wol "$DATA_DIR"
chmod 750 "$DATA_DIR"

# ------------------------------------------------------------ env (password & token)
rand() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
get_env() {
  if [ -f "$ENV_FILE" ]; then sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1; fi
}

DEVICE_TOKEN="${DEVICE_TOKEN:-$(get_env DEVICE_TOKEN)}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(get_env ADMIN_PASSWORD)}"
NEW_PW=0
[ -n "$DEVICE_TOKEN" ] || DEVICE_TOKEN="$(rand 32)"
if [ -z "$ADMIN_PASSWORD" ]; then ADMIN_PASSWORD="$(rand 12)"; NEW_PW=1; fi
[ "${#DEVICE_TOKEN}" -ge 16 ] || die "DEVICE_TOKEN minimal 16 karakter"
[ "${#ADMIN_PASSWORD}" -ge 6 ] || die "ADMIN_PASSWORD minimal 6 karakter"
if printf '%s%s' "$DEVICE_TOKEN" "$ADMIN_PASSWORD" | grep -q "[[:space:]\"'\\\\]"; then
  die "Token/password tidak boleh ada spasi, kutip, atau backslash"
fi

umask 027
cat > "$ENV_FILE" <<ENV
HOST=127.0.0.1
PORT=$APP_PORT
DATA_FILE=$DATA_DIR/data.json
HOLD_MS=20000
DEVICE_TOKEN=$DEVICE_TOKEN
ADMIN_PASSWORD=$ADMIN_PASSWORD
ENV
chown root:wol "$ENV_FILE"
chmod 640 "$ENV_FILE"
umask 022

# ------------------------------------------------------------ sertifikat self-signed
if [ -z "${PUBLIC_IP:-}" ]; then
  PUBLIC_IP="$(curl -fsS -4 --max-time 5 https://api.ipify.org 2>/dev/null || true)"
fi
LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
PUBLIC_IP="${PUBLIC_IP:-${LOCAL_IP:-127.0.0.1}}"
if [ ! -f "$ETC_DIR/ssl/wol.crt" ] || [ -n "${RENEW_CERT:-}" ]; then
  say "Bikin sertifikat self-signed (IP $PUBLIC_IP) ..."
  SAN="IP:127.0.0.1"
  for ip in $PUBLIC_IP $LOCAL_IP; do
    case ",$SAN," in *",IP:$ip,"*) ;; *) SAN="$SAN,IP:$ip" ;; esac
  done
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=esp32-wol" \
    -addext "subjectAltName=$SAN" \
    -keyout "$ETC_DIR/ssl/wol.key" -out "$ETC_DIR/ssl/wol.crt" 2>/dev/null
  chmod 600 "$ETC_DIR/ssl/wol.key"
fi

# ------------------------------------------------------------ nginx
say "Pasang config nginx (port $HTTP_PORT & $HTTPS_PORT) ..."
sed -e "s/__HTTP_PORT__/$HTTP_PORT/g" -e "s/__HTTPS_PORT__/$HTTPS_PORT/g" \
  "$SRC/deploy/nginx-esp32-wol.conf" > /etc/nginx/conf.d/esp32-wol.conf
if [ ! -f /proc/net/if_inet6 ]; then
  sed -i '/listen \[::\]/d' /etc/nginx/conf.d/esp32-wol.conf   # IPv6 mati di VM
fi
sed -e "s/__APP_PORT__/$APP_PORT/g" "$SRC/deploy/nginx-esp32-wol-proxy.conf" > /etc/nginx/esp32-wol-proxy.conf
if [ -n "$PORT80_OWNER" ] && [ "$PORT80_OWNER" != "nginx" ] && [ -e /etc/nginx/sites-enabled/default ]; then
  warn "Port 80 dipakai '$PORT80_OWNER' -> matikan site default nginx (biar nginx tidak rebutan port 80)"
  rm -f /etc/nginx/sites-enabled/default
fi

if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
  say "SELinux: izinkan nginx proxy & port ..."
  setsebool -P httpd_can_network_connect 1 || true
  if command -v semanage >/dev/null 2>&1; then
    for p in "$HTTP_PORT" "$HTTPS_PORT"; do
      semanage port -a -t http_port_t -p tcp "$p" 2>/dev/null \
        || semanage port -m -t http_port_t -p tcp "$p" 2>/dev/null || true
    done
  fi
fi
nginx -t

# ------------------------------------------------------------ service
if [ "$HAS_SYSTEMD" -eq 1 ]; then
  say "Pasang & start service ..."
  sed "s#__NODE__#$NODE_BIN#" "$SRC/deploy/esp32-wol.service" > /etc/systemd/system/esp32-wol.service
  systemctl daemon-reload
  systemctl enable -q esp32-wol nginx
  systemctl restart esp32-wol
  if systemctl is-active -q nginx; then systemctl reload nginx; else systemctl start nginx; fi
else
  warn "systemd tidak ada (container?). Start manual:"
  warn "  set -a; . $ENV_FILE; set +a; runuser -u wol -- $NODE_BIN $APP_DIR/server.js &  nginx"
fi

# ------------------------------------------------------------ firewall
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  say "ufw: buka port $HTTP_PORT, $HTTPS_PORT"
  ufw allow "$HTTP_PORT/tcp" >/dev/null
  ufw allow "$HTTPS_PORT/tcp" >/dev/null
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  say "firewalld: buka port $HTTP_PORT, $HTTPS_PORT"
  firewall-cmd -q --permanent --add-port="$HTTP_PORT/tcp" --add-port="$HTTPS_PORT/tcp"
  firewall-cmd -q --reload
elif command -v iptables >/dev/null 2>&1 && iptables -S INPUT 2>/dev/null | grep -qE -- '-j (REJECT|DROP)|^-P INPUT DROP'; then
  # contoh: image Ubuntu di Oracle Cloud -> iptables REJECT semua kecuali 22
  say "iptables: buka port $HTTP_PORT, $HTTPS_PORT"
  for p in "$HTTP_PORT" "$HTTPS_PORT"; do
    iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$p" -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1 || true
  elif [ -d /etc/iptables ]; then
    iptables-save > /etc/iptables/rules.v4
  elif [ -f /etc/sysconfig/iptables ]; then
    iptables-save > /etc/sysconfig/iptables
  fi
fi

# ------------------------------------------------------------ cek
if [ "$HAS_SYSTEMD" -eq 1 ]; then
  sleep 1
  if curl -fsS --max-time 5 "http://127.0.0.1:$HTTP_PORT/healthz" >/dev/null; then
    say "Health check OK"
  else
    warn "Health check gagal. Cek: journalctl -u esp32-wol -n 50"
  fi
fi

PW_NOTE=""
[ "$NEW_PW" -eq 1 ] && PW_NOTE="   (baru dibuat, simpan!)"
cat <<INFO

================= ESP32 WoL server terpasang =================
 Dashboard : http://$PUBLIC_IP:$HTTP_PORT
             https://$PUBLIC_IP:$HTTPS_PORT   (self-signed: klik Advanced -> Proceed)
 Password  : $ADMIN_PASSWORD$PW_NOTE
 DEVICE_TOKEN : $DEVICE_TOKEN

 Arahkan ESP32 (jalankan di PC Windows):
   H:\\python\\python.exe tool.py config --server https://$PUBLIC_IP:$HTTPS_PORT --token $DEVICE_TOKEN -y

 VM di cloud (AWS/GCP/Azure/dll)? Buka juga port $HTTP_PORT & $HTTPS_PORT
 di Security Group / firewall panel provider.

 Log    : journalctl -u esp32-wol -f
 Config : $ENV_FILE  (ubah lalu: systemctl restart esp32-wol)
==============================================================
INFO
