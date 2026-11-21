// ESP32 Wake-on-LAN server - Node.js tanpa dependency.
// ESP32 konek KELUAR ke server ini (long-poll), jadi tidak perlu port forwarding di router.
//
//   DEVICE_TOKEN=xxx ADMIN_PASSWORD=yyy node server.js
//
// Env:  HOST (0.0.0.0)  PORT (3000)  DEVICE_TOKEN  ADMIN_PASSWORD  HOLD_MS (20000)  DATA_FILE (./data.json)
"use strict";
const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// ---------------------------------------------------------------- .env sederhana
try {
  for (const line of fs.readFileSync(path.join(__dirname, ".env"), "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
} catch {}

const PORT = Number(process.env.PORT) || 3000;
const HOST = process.env.HOST || "0.0.0.0"; // 127.0.0.1 kalau di belakang nginx
const DEVICE_TOKEN = process.env.DEVICE_TOKEN || "";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "";
const HOLD_MS = Number(process.env.HOLD_MS) || 20000;
const OFFLINE_MS = HOLD_MS + 20000;
const CMD_EXPIRE_MS = 60000; // command yang tidak diambil ESP32 dalam 60 detik dibatalkan
const DATA_FILE = process.env.DATA_FILE || path.join(__dirname, "data.json");
const SECRET = crypto.createHash("sha256").update("wol:" + ADMIN_PASSWORD + ":" + DEVICE_TOKEN).digest();

if (DEVICE_TOKEN.length < 16 || ADMIN_PASSWORD.length < 6) {
  console.error("Set env DEVICE_TOKEN (min 16 karakter) dan ADMIN_PASSWORD (min 6). Lihat .env.example");
  process.exit(1);
}

// ---------------------------------------------------------------- state
const state = { device: { last_seen: 0, status: null, remote: null }, history: [] };
const queue = []; // command menunggu diambil ESP32
let waiter = null; // long-poll ESP32 yang sedang menunggu

try { Object.assign(state, JSON.parse(fs.readFileSync(DATA_FILE, "utf8"))); } catch {}
let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.writeFile(DATA_FILE, JSON.stringify(state), () => {});
  }, 500);
}

function log(...a) { console.log(new Date().toISOString(), ...a); }

// ---------------------------------------------------------------- helpers
function json(res, code, obj, headers = {}) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
    ...headers,
  });
  res.end(body);
}

function readBody(req, limit = 16384) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > limit) { reject(new Error("body too large")); req.destroy(); }
      else chunks.push(c);
    });
    req.on("end", () => {
      const s = Buffer.concat(chunks).toString("utf8");
      if (!s) return resolve({});
      try { resolve(JSON.parse(s)); } catch { reject(new Error("invalid json")); }
    });
    req.on("error", reject);
  });
}

function safeEqual(a, b) {
  const x = Buffer.from(String(a)), y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

function bearer(req) {
  const m = (req.headers.authorization || "").match(/^Bearer\s+(.+)$/i);
  return m ? m[1].trim() : "";
}

function sign(exp) { return crypto.createHmac("sha256", SECRET).update(String(exp)).digest("base64url"); }

function cookies(req) {
  const out = {};
  for (const p of (req.headers.cookie || "").split(";")) {
    const i = p.indexOf("=");
    if (i > 0) out[p.slice(0, i).trim()] = decodeURIComponent(p.slice(i + 1).trim());
  }
  return out;
}

function isAdmin(req) {
  if (safeEqual(bearer(req), ADMIN_PASSWORD)) return true; // buat shortcut/API
  const [exp, sig] = (cookies(req).wol || "").split(".");
  return !!exp && !!sig && Number(exp) > Date.now() && safeEqual(sig, sign(exp));
}

function clientIp(req) {
  return (req.headers["x-forwarded-for"] || "").split(",")[0].trim() || req.socket.remoteAddress;
}

const loginFails = new Map(); // ip -> {n, until}
function loginBlocked(ip) {
  const f = loginFails.get(ip);
  return f && f.n >= 5 && f.until > Date.now();
}
function loginFailed(ip) {
  const f = loginFails.get(ip) || { n: 0, until: 0 };
  if (f.until < Date.now()) f.n = 0;
  f.n++;
  f.until = Date.now() + 5 * 60000;
  loginFails.set(ip, f);
}

// ---------------------------------------------------------------- commands
const MAC_RE = /^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$/;
const IP_RE = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;

function validateCommand(type, data) {
  data = data && typeof data === "object" ? data : {};
  if (type === "wake") {
    if (data.mac && !MAC_RE.test(data.mac)) return "MAC tidak valid";
    return data.mac ? { mac: data.mac.toUpperCase().replace(/-/g, ":") } : {};
  }
  if (type === "reboot" || type === "refresh") return {};
  if (type === "config") {
    const out = {};
    if (data.pc_mac !== undefined) {
      if (!MAC_RE.test(data.pc_mac)) return "MAC tidak valid";
      out.pc_mac = data.pc_mac.toUpperCase().replace(/-/g, ":");
    }
    for (const k of ["pc_ip", "broadcast"]) {
      if (data[k] !== undefined && data[k] !== "") {
        if (!IP_RE.test(data[k])) return k + " tidak valid";
        out[k] = data[k];
      } else if (data[k] === "") out[k] = null;
    }
    if (!Object.keys(out).length) return "tidak ada yang diubah";
    return out;
  }
  return "command tidak dikenal";
}

function addCommand(type, data, by) {
  const cmd = { id: crypto.randomBytes(6).toString("hex"), type, data, by,
    created: Date.now(), sent: null, done: null, ok: null, msg: "" };
  queue.push(cmd);
  state.history.unshift(cmd);
  state.history.length = Math.min(state.history.length, 50);
  save();
  log("command", type, JSON.stringify(data), "dari", by);
  flushWaiter();
  return cmd;
}

function takeCommands() {
  const now = Date.now();
  const out = [];
  while (queue.length) {
    const c = queue.shift();
    if (now - c.created > CMD_EXPIRE_MS) {
      c.done = now; c.ok = false; c.msg = "kedaluwarsa (ESP32 offline)";
      continue;
    }
    c.sent = now;
    out.push({ id: c.id, type: c.type, data: c.data });
  }
  if (out.length) save();
  return out;
}

function expireQueue() {
  const now = Date.now();
  for (let i = queue.length - 1; i >= 0; i--) {
    const c = queue[i];
    if (now - c.created > CMD_EXPIRE_MS) {
      c.done = now; c.ok = false; c.msg = "kedaluwarsa (ESP32 offline)";
      queue.splice(i, 1);
      save();
    }
  }
}
setInterval(expireQueue, 5000).unref();

function flushWaiter() {
  if (!waiter) return;
  const w = waiter;
  waiter = null;
  clearTimeout(w.timer);
  if (w.res.destroyed || w.res.writableEnded) return;
  json(w.res, 200, { commands: takeCommands(), hold_ms: HOLD_MS });
}

// ---------------------------------------------------------------- device API
async function devicePoll(req, res) {
  if (!safeEqual(bearer(req), DEVICE_TOKEN)) return json(res, 401, { error: "bad token" });
  const body = await readBody(req);
  const d = state.device;
  const wasOffline = Date.now() - d.last_seen > OFFLINE_MS;
  d.last_seen = Date.now();
  d.remote = clientIp(req);
  if (body.status && typeof body.status === "object") d.status = body.status;
  if (wasOffline) log("ESP32 online dari", d.remote);

  for (const a of Array.isArray(body.acks) ? body.acks : []) {
    const c = state.history.find((h) => h.id === a.id);
    if (c && !c.done) {
      c.done = Date.now();
      c.ok = !!a.ok;
      c.msg = String(a.msg || "").slice(0, 200);
      log("ack", c.type, c.ok ? "OK" : "GAGAL", c.msg);
    }
  }
  save();

  // satu device = satu long-poll aktif
  if (waiter) {
    const old = waiter;
    waiter = null;
    clearTimeout(old.timer);
    if (!old.res.destroyed) json(old.res, 200, { commands: [], hold_ms: HOLD_MS });
  }
  if (queue.length || body.hold === 0) {
    return json(res, 200, { commands: takeCommands(), hold_ms: HOLD_MS });
  }
  const w = { res, timer: setTimeout(() => { if (waiter === w) flushWaiter(); }, HOLD_MS) };
  waiter = w;
  res.on("close", () => {
    if (waiter === w) { clearTimeout(w.timer); waiter = null; }
  });
}

// ---------------------------------------------------------------- user API
function publicState() {
  const d = state.device;
  const online = !!waiter || (!!d.last_seen && Date.now() - d.last_seen < OFFLINE_MS);
  return {
    device: { online, last_seen: d.last_seen || null, remote: d.remote, status: d.status },
    history: state.history.slice(0, 20),
    server_time: Date.now(),
  };
}

async function route(req, res) {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;

  if (p === "/api/device/poll" && req.method === "POST") return devicePoll(req, res);

  if (p === "/healthz") return json(res, 200, { ok: true });

  if (p === "/api/login" && req.method === "POST") {
    const ip = clientIp(req);
    if (loginBlocked(ip)) return json(res, 429, { error: "Terlalu banyak percobaan, tunggu 5 menit" });
    const body = await readBody(req);
    if (!safeEqual(body.password || "", ADMIN_PASSWORD)) {
      loginFailed(ip);
      return json(res, 401, { error: "Password salah" });
    }
    loginFails.delete(ip);
    const exp = Date.now() + 30 * 24 * 3600 * 1000;
    const secure = req.headers["x-forwarded-proto"] === "https" || req.socket.encrypted ? "; Secure" : "";
    return json(res, 200, { ok: true }, {
      "Set-Cookie": `wol=${exp}.${sign(exp)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000${secure}`,
    });
  }

  if (p === "/api/logout" && req.method === "POST") {
    return json(res, 200, { ok: true }, { "Set-Cookie": "wol=; Path=/; HttpOnly; Max-Age=0" });
  }

  if (p.startsWith("/api/")) {
    if (!isAdmin(req)) return json(res, 401, { error: "login dulu" });
    if (p === "/api/state" && req.method === "GET") return json(res, 200, publicState());
    if (p === "/api/command" && req.method === "POST") {
      const body = await readBody(req);
      const data = validateCommand(body.type, body.data);
      if (typeof data === "string") return json(res, 400, { error: data });
      const cmd = addCommand(body.type, data, clientIp(req));
      return json(res, 200, { ok: true, id: cmd.id, device_online: publicState().device.online });
    }
    return json(res, 404, { error: "not found" });
  }

  if ((p === "/" || p === "/index.html") && req.method === "GET") {
    const html = fs.readFileSync(path.join(__dirname, "public", "index.html"));
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    return res.end(html);
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("not found");
}

const server = http.createServer((req, res) => {
  route(req, res).catch((e) => {
    log("error", e.message);
    if (!res.headersSent) json(res, 400, { error: e.message });
  });
});
server.requestTimeout = 0; // long-poll
server.headersTimeout = 15000;
server.keepAliveTimeout = 5000;
server.listen(PORT, HOST, () => log(`WoL server jalan di http://${HOST}:${PORT}  (hold ${HOLD_MS}ms)`));
