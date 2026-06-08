# WhatsApp Bridge (headless sender)

Sends WhatsApp messages over the WhatsApp Web **protocol** via Puppeteer
(headless Chromium driven through the DevTools protocol). Unlike `pywhatkit`,
it does **not** use OS-level keystrokes, so it **sends reliably with the screen
off and the device locked**.

The Python side (`notifications/whatsapp.py`, `WHATSAPP_BACKEND=bridge`) POSTs to
this service on `127.0.0.1`.

## One-time setup

```bash
cd notifications/whatsapp_bridge
npm install            # pulls whatsapp-web.js + a bundled Chromium (~150 MB)
node bridge.js         # boots the bridge and prints a QR code
```

> **Windows / PowerShell:** if `npm start` fails with *"npm.ps1 cannot be loaded
> because running scripts is disabled"*, that's the PowerShell execution policy
> blocking npm's script wrapper. Just run **`node bridge.js`** instead (a plain
> exe, not blocked) — or use `npm.cmd start`, or run once
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. This only affects
> interactive npm; the scheduler autostarts the bridge with `node` directly, so
> it is never affected.

On first run a **QR code** prints in the terminal. On your phone:
**WhatsApp → Settings → Linked Devices → Link a Device**, and scan it.
The session is saved to `./.wwebjs_auth/`, so you only do this once — later
(even headless, even when launched automatically by Python) it reconnects
without a QR.

When you see `[bridge] READY — sending enabled`, it's good to go.

## Keeping it running

The bridge must be running when the pipeline fires. Pick one:

- **Autostart (default):** leave `WHATSAPP_BRIDGE_AUTOSTART=true`. If the bridge
  isn't up when a send fires, Python launches it headless and waits for it to
  reconnect from the saved session. Simplest; relies on the one-time QR scan
  having been done.
- **Always-on (most robust):** run it as a persistent background process so it's
  warm before sends:
  - Quick: `npm start` in a terminal that stays open, or
  - Service: `npx pm2 start bridge.js --name wa-bridge` (pm2), or wrap with
    [nssm](https://nssm.cc/) as a Windows service, or add a **Task Scheduler**
    task "At log on" running `node bridge.js` in this folder.

Headless Chromium runs fine under a locked Windows session, so any of these keep
working with the screen off.

## HTTP API

| Method | Path      | Body                      | Result                          |
|--------|-----------|---------------------------|---------------------------------|
| GET    | `/status` | —                         | `{ ready, state }`              |
| POST   | `/send`   | `{ phone, message }`      | `{ ok, id }` or `{ ok:false, error }` |

`phone` is any format with country code (e.g. `+919876543210`); non-digits are
stripped and the number is verified to be on WhatsApp before sending.

## Config (Node env)

- `BRIDGE_PORT` (default `8765`)
- `BRIDGE_TOKEN` (optional) — if set, callers must send header `X-Token`. Match
  it with `WHATSAPP_BRIDGE_TOKEN` in the project `.env`.
- `HEADLESS` (default `true`) — set `false` once if you want to watch the browser
  while debugging the QR/login.

## Notes & limitations

- This uses WhatsApp Web (unofficial automation), same category as the previous
  `pywhatkit` approach — keep volume reasonable to avoid number bans.
- If you log out the linked device from your phone, delete `.wwebjs_auth/` and
  re-run `npm start` to re-link.
