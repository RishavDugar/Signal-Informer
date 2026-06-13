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
 *   POST /send          -> { phone, message } | { group, message }
 *                        -> { ok, id } | { ok:false, error }
 *   GET  /messages       -> ?group=<name>&since=<epoch ms>
 *                        -> { ok, messages: [{ts, chatName, isGroup, from, body}, ...] }
 *   POST /reconnect     -> {}  ->  { ok, state }
 *
 * Session is persisted by LocalAuth (./.wwebjs_auth), so you scan the QR code
 * exactly once; subsequent (even headless / detached) starts re-use the session.
 *
 * Reconnection strategy (layered):
 *   1. disconnected event  — fires on clean WA logout / network drop
 *   2. page close / crash  — fires when Chromium's page is killed
 *   3. /send error guard   — catches "detached Frame" / "context destroyed"
 *      that WA Web triggers silently when it hot-swaps its JS bundle
 *   4. health-check ping   — getState() probe every 60 s; catches any case
 *      the above three miss (e.g. WA Web reload with no thrown error)
 *
 * All four paths funnel through a single reconnect() function that is
 * guarded against concurrent calls.
 *
 * Env:
 *   BRIDGE_PORT          (default 8765)   — localhost port to listen on
 *   BRIDGE_TOKEN         (optional)       — if set, callers must send header X-Token
 *   HEADLESS             (default "true") — set "false" once to debug with visible browser
 *   HEALTH_CHECK_INTERVAL_MS (default 60000) — how often to probe the connection
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");
const path = require("path");

const PORT                  = parseInt(process.env.BRIDGE_PORT || "8765", 10);
const TOKEN                 = process.env.BRIDGE_TOKEN || "";
const HEADLESS              = (process.env.HEADLESS || "true").toLowerCase() !== "false";
const HEALTH_INTERVAL_MS    = parseInt(process.env.HEALTH_CHECK_INTERVAL_MS || "60000", 10);

// ── State ─────────────────────────────────────────────────────────────────────

let ready        = false;
let lastState    = "starting";
let reconnecting = false;   // guard: only one reconnect cycle at a time

// ── Inbound message log ──────────────────────────────────────────────────────
// In-memory ring buffer so callers (e.g. a Python automation poller) can ask
// "any new messages in group X since timestamp Y?" via GET /messages.
const MAX_LOG = 500;
const messageLog = [];

function findChatByName(name) {
  const target = String(name || "").trim().toLowerCase();
  return client.getChats().then(chats =>
    chats.find(c => c.isGroup && (c.name || "").trim().toLowerCase() === target));
}

// ── Reconnect helper ──────────────────────────────────────────────────────────

async function reconnect(reason) {
  if (reconnecting) {
    console.log(`[bridge] reconnect() called (${reason}) but already in progress — skipping`);
    return;
  }
  reconnecting = true;
  ready        = false;
  lastState    = "reconnecting";
  console.warn(`[bridge] reconnecting — reason: ${reason}`);

  // Destroy the old browser cleanly before launching a new one so we don't
  // accumulate orphaned Chromium processes over time.
  try { await client.destroy(); } catch (_) {}

  client.initialize().catch((e) => {
    console.error("[bridge] reinit error:", e);
    reconnecting = false;   // allow a future retry
  });
  // reconnecting is cleared by the "ready" event handler below.
}

// ── WhatsApp client ───────────────────────────────────────────────────────────

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

client.on("authenticated", () => {
  lastState = "authenticated";
  console.log("[bridge] authenticated");
});

client.on("auth_failure", (m) => {
  ready     = false;
  lastState = "auth_failure";
  console.error("[bridge] auth failure:", m);
});

// Layer 0: log inbound messages (group chats only matter for the automation trigger,
// but we keep DMs too in case a future caller wants them).
client.on("message", async (msg) => {
  try {
    const chat = await msg.getChat();
    messageLog.push({
      ts: Date.now(), chatId: chat.id._serialized, chatName: chat.name || "",
      isGroup: !!chat.isGroup, from: msg.author || msg.from, body: msg.body || "",
    });
    if (messageLog.length > MAX_LOG) messageLog.splice(0, messageLog.length - MAX_LOG);
  } catch (e) {
    console.warn("[bridge] message log error:", e && e.message || e);
  }
});

client.on("ready", () => {
  ready        = true;
  lastState    = "ready";
  reconnecting = false;
  console.log("[bridge] READY — sending enabled");

  // Layer 2: hook page-level events now that pupPage is available.
  const page = client.pupPage;
  if (page) {
    page.once("close",  () => reconnect("page close"));
    page.once("crash",  () => reconnect("page crash"));
    // WhatsApp Web sometimes navigates away (bundle update / session refresh).
    // A navigation means all existing frame handles are about to go stale.
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame()) {
        reconnect("main frame navigated");
      }
    });
  }
});

// Layer 1: clean disconnect / network drop.
client.on("disconnected", (reason) => {
  console.warn("[bridge] disconnected:", reason);
  reconnect("disconnected event: " + reason);
});

client.initialize().catch((e) => {
  lastState = "init_error";
  console.error("[bridge] init error:", e);
});

// ── Health-check (Layer 4) ────────────────────────────────────────────────────
// Probe the live WhatsApp Web page via getState() on every tick.
// getState() runs JS inside the page, so a detached frame or dead context
// throws before we ever try to send — giving us an early-warning trigger.

setInterval(async () => {
  if (!ready || reconnecting) return;
  try {
    const state = await client.getState();
    if (!state) throw new Error("getState returned null");
  } catch (e) {
    reconnect("health-check failed: " + (e && e.message || e));
  }
}, HEALTH_INTERVAL_MS);

// ── HTTP API ──────────────────────────────────────────────────────────────────

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

// Explicit reconnect — Python calls this after detecting stale-frame errors.
app.post("/reconnect", (req, res) => {
  reconnect("/reconnect endpoint called");
  res.json({ ok: true, state: lastState });
});

app.post("/send", async (req, res) => {
  const { phone, group, message } = req.body || {};
  if ((!phone && !group) || !message) {
    return res.status(400).json({ ok: false, error: "message and (phone or group) are required" });
  }
  if (!ready) {
    return res.status(503).json({ ok: false, error: `client not ready (state=${lastState})` });
  }
  try {
    let chatId;
    if (group) {
      const chat = await findChatByName(group);
      if (!chat) return res.status(404).json({ ok: false, error: `no group chat named: ${group}` });
      chatId = chat.id._serialized;
    } else {
      const digits   = String(phone).replace(/[^\d]/g, "");
      const numberId = await client.getNumberId(digits);
      if (!numberId) {
        return res.status(404).json({ ok: false, error: `not a WhatsApp number: ${phone}` });
      }
      chatId = numberId._serialized;
    }
    const sent = await client.sendMessage(chatId, message);
    return res.json({ ok: true, id: sent.id._serialized });
  } catch (e) {
    const msg = String((e && e.message) || e);
    // Layer 3: "detached Frame" / "Execution context was destroyed" / "Target closed"
    // are thrown when WhatsApp Web hot-swaps its JS bundle mid-session.
    if (/detached|context.*destroyed|target.*closed/i.test(msg)) {
      reconnect("stale Puppeteer frame on /send: " + msg);
    }
    return res.status(500).json({ ok: false, error: msg });
  }
});

// GET /messages?group=<name>&since=<epoch ms> — inbound messages from a group chat,
// for the Python side to poll for an automation trigger phrase.
app.get("/messages", (req, res) => {
  const group = String(req.query.group || "").trim().toLowerCase();
  const since = parseInt(req.query.since, 10) || 0;
  const messages = messageLog.filter(m =>
    m.ts > since && (!group || (m.isGroup && m.chatName.trim().toLowerCase() === group)));
  res.json({ ok: true, messages, now: Date.now() });
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[bridge] HTTP listening on http://127.0.0.1:${PORT}  (headless=${HEADLESS})`);
});

// ── Graceful shutdown ─────────────────────────────────────────────────────────

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    console.log(`[bridge] ${sig} — shutting down`);
    try { await client.destroy(); } catch (_) {}
    process.exit(0);
  });
}
