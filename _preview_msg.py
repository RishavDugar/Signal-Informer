"""Preview WhatsApp message format without sending."""
import sys
sys.path.insert(0, ".")
from data.db import get_active_stocks, get_ohlcv, get_stock_id
from setup_loader import load_setups
from notifications.whatsapp import (
    send_batch_signal_alert, _get_prev_ohlc, _render_meta, _SETUP_DESC,
    _DIRECTION_EMOJI, _p
)
from datetime import date

setups  = load_setups()
stocks  = get_active_stocks()
symbols = [s["symbol"] for s in stocks]

fired: list[dict] = []
for symbol in symbols:
    df = get_ohlcv(symbol, days=200)
    if df.empty:
        continue
    for setup in setups:
        r = setup.signal(df, symbol)
        if r.signal:
            fired.append(r.to_dict())

print(f"Total fired signals: {len(fired)}")

# Build the message locally (without sending)
by_symbol: dict[str, list[dict]] = {}
for sig in fired:
    by_symbol.setdefault(sig["symbol"], []).append(sig)

today = date.today().strftime("%Y-%m-%d")
lines = [
    f"*Signal Alert -- {today}*",
    f"_{len(fired)} signal(s) on {len(by_symbol)} stock(s)_",
    ""
]

for symbol, sigs in list(by_symbol.items())[:5]:  # preview first 5 stocks
    display  = symbol.replace(".NS","").replace(".BO","")
    exchange = "BSE" if symbol.endswith(".BO") else "NSE"
    ohlc     = _get_prev_ohlc(symbol)

    lines += [
        "--------------------",
        f"*{display}* ({exchange})",
    ]
    if ohlc:
        lines.append(ohlc)
    lines.append("")

    for idx, sig in enumerate(sigs, 1):
        name   = sig["setup_name"]
        meta   = sig.get("metadata", {})
        stype  = meta.get("signal_type","")
        dlabel = _DIRECTION_EMOJI.get(stype, stype.upper()) if stype else ""
        dstr   = f" [{dlabel}]" if dlabel else ""
        desc   = _SETUP_DESC.get(name, name)

        lines.append(f"*{idx}. {name}*{dstr}")
        lines.append(f"  _{desc}_")
        lines.extend(_render_meta(name, meta))
        lines.append("")

msg = "\n".join(lines)
print("\n" + "="*60)
print("MESSAGE PREVIEW")
print("="*60)
print(msg)
print("="*60)
print(f"Total chars: {len(msg)}")
