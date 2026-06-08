/*
 * Signal Infomer — headless WhatsApp bridge.
 *
 * Why this exists: pywhatkit sends via GUI automation (it types the message and
 * presses Enter through the OS input layer), which the OS blocks when the screen
 * is off or the device is locked — so messages silently fail to send. This
 * service drives WhatsApp Web through Puppeteer (the Chrome DevTools protocol),
 * which does NOT depend on an interactive/unlocked desktop. It therefore sends
 * reliably with the screen off and the device locked.
 *
 * It exposes a tiny localhost HTTP API the Python side calls:
 *   GET  /status        -> { ready: bool, state: string }
 *   POST /send          -> { phone, message }  ->  { ok, id } | { ok:false, error }
 *
 * Session is persisted by LocalAuth (./.wwebjs_auth), so you scan the QR code
 * exactly once; subsequent (even headless / detached) starts re-use the session.
 *
 * Env:
 *   BRIDGE_PORT   (default 8765)   — localhost port to listen on
 *   BRIDGE_TOKEN  (optional)       — if set, callers must send header X-Token
 *   HEADLESS      (default "true") — set "false" once to debug with a visible browser
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");
const path = require("path");

const PORT = parseInt(process.env.BRIDGE_PORT || "8765", 10);
const TOKEN = process.env.BRIDGE_TOKEN || "";
const HEADLESS = (process.env.HEADLESS || "true").toLowerCase() !== "false";

let ready = false;
let lastState = "starting";

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: path.join(__dirname, ".wwebjs_auth") }),
  puppeteer: {
    headless: HEADLESS,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
    ],
  },
});

client.on("qr", (qr) => {
  lastState = "qr";
  console.log("\n[bridge] Scan this QR with WhatsApp > Linked Devices (one time):\n");
  qrcode.generate(qr, { small: true });
});
client.on("authenticated", () => { lastState = "authenticated"; console.log("[bridge] authenticated"); });
client.on("auth_failure", (m) => { ready = false; lastState = "auth_failure"; console.error("[bridge] auth failure:", m); });
client.on("ready", () => { ready = true; lastState = "ready"; console.log("[bridge] READY — sending enabled"); });
client.on("disconnected", (reason) => {
  ready = false; lastState = "disconnected";
  console.warn("[bridge] disconnected:", reason, "— reinitialising");
  client.initialize().catch((e) => console.error("[bridge] reinit error:", e));
});

client.initialize().catch((e) => { lastState = "init_error"; console.error("[bridge] init error:", e); });

const app = express();
app.use(express.json({ limit: "1mb" }));

// Optional shared-secret auth (localhost-only bind already limits exposure).
app.use((req, res, next) => {
  if (TOKEN && req.get("X-Token") !== TOKEN) {
    return res.status(401).json({ ok: false, error: "bad or missing X-Token" });
  }
  next();
});

app.get("/status", (req, res) => res.json({ ready, state: lastState }));

app.post("/send", async (req, res) => {
  const { phone, message } = req.body || {};
  if (!phone || !message) {
    return res.status(400).json({ ok: false, error: "phone and message are required" });
  }
  if (!ready) {
    return res.status(503).json({ ok: false, error: `client not ready (state=${lastState})` });
  }
  try {
    const digits = String(phone).replace(/[^\d]/g, "");
    // Resolve the canonical chat id; null => the number isn't on WhatsApp.
    const numberId = await client.getNumberId(digits);
    if (!numberId) {
      return res.status(404).json({ ok: false, error: `not a WhatsApp number: ${phone}` });
    }
    const sent = await client.sendMessage(numberId._serialized, message);
    return res.json({ ok: true, id: sent.id._serialized });
  } catch (e) {
    return res.status(500).json({ ok: false, error: String((e && e.message) || e) });
  }
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[bridge] HTTP listening on http://127.0.0.1:${PORT}  (headless=${HEADLESS})`);
});

// Graceful shutdown so the Chromium child doesn't linger.
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    console.log(`[bridge] ${sig} — shutting down`);
    try { await client.destroy(); } catch (_) {}
    process.exit(0);
  });
}
