"""
Backtester simulation tests — avg-return + confidence model.

Verifies the exact behavioural contract:

Entry / exit model
  Signal fires at D0 close.  Entry = D1 OPEN.
  d=1  -> D1 CLOSE only (entered at D1 open; no open-exit on same day)
  d>=2  -> Dd open and Dd close both available
  MAX  -> d=10 (ten-day maximum hold)

Stoploss propagation (no survivorship bias)
  SL hit at day offset k from D1 -> sl_exit_d = k+1.
  All days d >= sl_exit_d carry the SL return — position already closed.
  Days d < sl_exit_d carry normal exit returns — trade still open.

SL offset -> sl_exit_d mapping
  offset=0 (D1 SL) -> sl_exit_d=1   (d=1..10 all get SL return)
  offset=1 (D2 SL) -> sl_exit_d=2   (d=1 normal, d=2..10 SL)
  offset=2 (D3 SL) -> sl_exit_d=3   (d=1..2 normal, d=3..10 SL)

Short direction
  Intraday only (Indian market rule): exit at D1 close or SL during D1.
  d=2..10 remain empty (open_n=close_n=0).

Confidence level
  Bayesian win rate = (wins + PRIOR_N×PRIOR_WR) / (n + PRIOR_N)
  Shrinks toward 50 % for small samples; honest measure of signal reliability.

Direction split
  _process_one_stock returns (partial_long, partial_short, n_long, n_short,
                               n_long_sl, n_short_sl).
  Buy / neutral signals -> partial_long, n_long, n_long_sl
  Sell signals          -> partial_short, n_short, n_short_sl
"""

from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Trading Setups"))

import pandas as pd

from backtester import (
    _process_one_stock, _summarise, _wilson_lower_bound,
    MAX_HOLD_DAYS, PRIOR_N, PRIOR_WR,
)
from core.base_setup import BaseSetup, SignalResult

# ── Counters ──────────────────────────────────────────────────────────────────

PASSES = 0
FAILS  = 0

def check(label: str, actual, expected, tol: float = 1e-7) -> None:
    global PASSES, FAILS
    ok = abs(float(actual) - float(expected)) <= tol
    if ok:
        PASSES += 1
        print(f"  PASS  {label}")
    else:
        FAILS += 1
        print(f"  FAIL  {label}  ->  got {actual!r}, expected {expected!r}")

def checkeq(label: str, actual, expected) -> None:
    global PASSES, FAILS
    ok = (actual == expected)
    if ok:
        PASSES += 1
        print(f"  PASS  {label}")
    else:
        FAILS += 1
        print(f"  FAIL  {label}  ->  got {actual!r}, expected {expected!r}")

def checkgt(label: str, actual, threshold) -> None:
    global PASSES, FAILS
    ok = float(actual) > float(threshold)
    if ok:
        PASSES += 1
        print(f"  PASS  {label}  [{actual!r}]")
    else:
        FAILS += 1
        print(f"  FAIL  {label}  ->  {actual!r} not > {threshold!r}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_df(rows: list[tuple]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    return pd.DataFrame(
        [{"open": o, "high": h, "low": l, "close": c, "volume": 1_000}
         for o, h, l, c in rows],
        index=dates,
    )

WARMUP = [
    (100, 105,  98, 102),
    (102, 107, 100, 105),
    (105, 108, 102, 104),
]

# 10 generic filler bars — used as d=2..d=10 exit days when specific values don't matter
_FILL_ROWS = [(210 + i*5, 215 + i*5, 208 + i*5, 213 + i*5) for i in range(10)]

def _build_rows(d0_row, d1_row, exit_rows, tail_n: int = 2) -> list[tuple]:
    """WARMUP + D0 + D1 + exit_rows + tail. exit_rows covers D2..D(len+1)."""
    tail_rows = [(300 + i*10, 305 + i*10, 298 + i*10, 303 + i*10) for i in range(tail_n)]
    return WARMUP + [d0_row, d1_row] + exit_rows + tail_rows

# ── Mock setup ────────────────────────────────────────────────────────────────

class MockSetup(BaseSetup):
    name = "MOCK"; description = "Simulation mock"; min_periods = 3

    def __init__(self, fire_at_lens: set[int], signal_type: str,
                 sl_override: float | None = None):
        self._fire  = fire_at_lens
        self._stype = signal_type
        self._sl    = sl_override

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        return SignalResult(
            signal=len(df) in self._fire, symbol=symbol,
            setup_name=self.name, date=df.index[-1].strftime("%Y-%m-%d"),
            metadata={"signal_type": self._stype},
        )

    def get_stoploss(self, result: SignalResult, df: pd.DataFrame) -> float | None:
        return self._sl if self._sl is not None else super().get_stoploss(result, df)


# ═════════════════════════════════════════════════════════════════════════════
# T1 — d=1 exit = D1 CLOSE (entry at D1 open, same-day close)
# ═════════════════════════════════════════════════════════════════════════════

def test_d1_is_d1_close():
    """
    Entry = D1 open = 200.  D1 close = 205.
    d=1 close_ret = (205-200)/200 = +2.5%.
    d=1 open_n must be 0 (no open-exit on the entry day).
    Proves that the first exit is D1 close, not D2 open.
    """
    print("\n[ T1  d=1 exit = D1 close only ]")
    d0 = (104, 110, 100, 103)
    d1 = (200, 205, 198, 205)    # entry=200, close=205
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "buy", sl_override=0.0)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1",  ns,  1)
    checkeq("n_sl_hits=0",  nsl, 0)
    check("d=1 close_ret_sum = +2.5%", p[1]["close_ret_sum"], (205-200)/200)
    checkeq("d=1 close_n=1",  p[1]["close_n"],  1)
    checkeq("d=1 open_n=0",   p[1]["open_n"],   0)   # no open exit on entry day
    checkeq("d=1 close_wins=1", p[1]["close_wins"], 1)


def test_entry_is_d1_open_not_d0_close():
    """
    D0 close=103 vs D1 open=200.  D1 close=195.
    ret = (195-200)/200 = -2.5%  -> NEGATIVE proves entry=200 (D1 open).
    If entry were 103: ret=(195-103)/103=+89% -> positive.
    """
    print("\n[ T1b Entry = D1 open, not D0 close ]")
    d0 = (104, 110, 100, 103)
    d1 = (200, 202, 193, 195)    # entry=200, close=195
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "buy", sl_override=0.0)
    p, _, ns, _, _, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns, 1)
    check("d=1 close_ret_sum = -2.5%  [entry=D1_open proven]",
          p[1]["close_ret_sum"], (195-200)/200)
    checkeq("d=1 close_wins=0 (loss)", p[1]["close_wins"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T2 — SL on D1 (offset=0) -> sl_exit_d=1 -> all 10 days carry SL return
# ═════════════════════════════════════════════════════════════════════════════

def test_sl_on_d1_propagates_all_days():
    """
    Entry=110. SL=106. D1 low=104 < 106 -> intraday SL at 106.
    sl_exit_d = 0+1 = 1.
    d=1: SL return in CLOSE bucket only (d=1 is close-only).
    d=2..10: SL return in BOTH open and close.
    """
    print("\n[ T2  SL on D1 -> sl_exit_d=1 -> all 10 days carry SL return ]")
    d0 = (104, 110, 100, 103)
    d1 = (110, 115, 104, 112)    # entry=110, low=104 < SL=106 -> hit
    rows = _build_rows(d0, d1, _FILL_ROWS)

    sl_ret = (106 - 110) / 110
    setup = MockSetup({4}, "buy", sl_override=106.0)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns,  1)
    checkeq("n_sl_hits=1", nsl, 1)

    # d=1: close only (SL triggered intraday on D1)
    check("d=1 close_ret_sum = SL_ret", p[1]["close_ret_sum"], sl_ret)
    checkeq("d=1 close_n=1",  p[1]["close_n"], 1)
    checkeq("d=1 open_n=0",   p[1]["open_n"],  0)   # never populated for d=1
    checkeq("d=1 close_wins=0 (SL is a loss)", p[1]["close_wins"], 0)

    # d=2..10: both open and close carry SL return
    for d in range(2, MAX_HOLD_DAYS + 1):
        checkeq(f"d={d} open_n=1",  p[d]["open_n"],  1)
        checkeq(f"d={d} close_n=1", p[d]["close_n"], 1)
        check(f"d={d} open_ret_sum = SL_ret",  p[d]["open_ret_sum"],  sl_ret)
        check(f"d={d} close_ret_sum = SL_ret", p[d]["close_ret_sum"], sl_ret)


# ═════════════════════════════════════════════════════════════════════════════
# T3 — Gap-down at D1 open <= SL -> sl_exit_price = D1 open = entry -> ret=0
# ═════════════════════════════════════════════════════════════════════════════

def test_gap_down_sl_breakeven():
    """
    SL=106. D1 opens at 103 < SL -> gap-down fill; sl_exit_price = 103.
    entry_price = 103.  ret = (103-103)/103 = 0.0.
    All 10 days carry ret=0 (break-even).
    """
    print("\n[ T3  Gap-down at D1 open -> ret=0 on all 10 days ]")
    d0 = (104, 110, 100, 103)
    d1 = (103, 110,  99, 107)    # open=103 < SL=106 -> gap-down fill at 103
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "buy", sl_override=106.0)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns,  1)
    checkeq("n_sl_hits=1", nsl, 1)
    check("d=1 close_ret_sum=0.0 (break-even)", p[1]["close_ret_sum"], 0.0)
    checkeq("d=1 close_n=1",  p[1]["close_n"], 1)
    for d in range(2, MAX_HOLD_DAYS + 1):
        check(f"d={d} open_ret_sum=0.0", p[d]["open_ret_sum"], 0.0)
        checkeq(f"d={d} open_n=1", p[d]["open_n"], 1)


# ═════════════════════════════════════════════════════════════════════════════
# T4 — SL on D2 (offset=1) -> sl_exit_d=2 -> d=1 normal, d=2..10 SL
# ═════════════════════════════════════════════════════════════════════════════

def test_sl_on_d2_maps_to_exit_d2():
    """
    Entry=110. SL=106.
    D1: low=107 > 106 -> no SL -> d=1 normal exit (D1 close=112)
    D2: low=104 < 106 -> intraday SL at 106. offset=1, sl_exit_d=2.
    d=1: D1 close normal exit -> (112-110)/110 = +1.8%
    d=2..10: SL return = (106-110)/110 = -3.6%
    """
    print("\n[ T4  SL on D2 -> sl_exit_d=2 -> d=1 normal, d=2..10 SL ]")
    d0 = (104, 110, 100, 103)
    d1 = (110, 115, 107, 112)    # low=107 > SL=106 -> no SL; close=112
    d2 = (115, 118, 104, 116)    # low=104 < SL=106 -> intraday hit
    rows = _build_rows(d0, d1, [d2] + _FILL_ROWS[:8])

    sl_ret  = (106 - 110) / 110
    d1_cret = (112 - 110) / 110
    setup = MockSetup({4}, "buy", sl_override=106.0)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns, 1)
    checkeq("n_sl_hits=1", nsl, 1)

    # d=1: normal D1 close exit (SL was NOT triggered on D1)
    check("d=1 close_ret_sum = D1_close_ret", p[1]["close_ret_sum"], d1_cret)
    checkeq("d=1 close_n=1",  p[1]["close_n"], 1)
    checkeq("d=1 close_wins=1 (positive)", p[1]["close_wins"], 1)

    # d=2..10: SL return propagated
    for d in range(2, MAX_HOLD_DAYS + 1):
        checkeq(f"d={d} open_n=1",  p[d]["open_n"],  1)
        check(f"d={d} open_ret_sum = SL_ret",  p[d]["open_ret_sum"],  sl_ret)
        checkeq(f"d={d} open_wins=0 (SL is loss)", p[d]["open_wins"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T5 — SL on D3 (offset=2) -> sl_exit_d=3 -> d=1,2 normal, d=3..10 SL
# ═════════════════════════════════════════════════════════════════════════════

def test_sl_on_d3_maps_to_exit_d3():
    """
    Entry=110. SL=106.
    D1: low=107 -> no SL; d=1 = D1 close = 112 -> +1.8%
    D2: low=108 -> no SL; d=2 = D2 open=115 -> +4.5%
    D3: low=104 -> SL at 106; offset=2, sl_exit_d=3
    d=3..10: SL return = -3.6%
    """
    print("\n[ T5  SL on D3 -> sl_exit_d=3 -> d=1,2 normal, d=3..10 SL ]")
    d0 = (104, 110, 100, 103)
    d1 = (110, 115, 107, 112)    # close=112, no SL
    d2 = (115, 122, 108, 120)    # open=115, low=108, no SL
    d3 = (112, 118, 104, 116)    # low=104 < SL=106 -> hit
    rows = _build_rows(d0, d1, [d2, d3] + _FILL_ROWS[:7])

    sl_ret  = (106 - 110) / 110
    d1_cret = (112 - 110) / 110
    d2_oret = (115 - 110) / 110
    setup = MockSetup({4}, "buy", sl_override=106.0)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns, 1)
    checkeq("n_sl_hits=1", nsl, 1)

    check("d=1 close_ret = D1 close +1.8%",  p[1]["close_ret_sum"], d1_cret)
    checkeq("d=1 close_wins=1", p[1]["close_wins"], 1)

    check("d=2 open_ret = D2 open +4.5%",   p[2]["open_ret_sum"],  d2_oret)
    checkeq("d=2 open_wins=1",  p[2]["open_wins"],  1)

    for d in range(3, MAX_HOLD_DAYS + 1):
        check(f"d={d} open_ret = SL_ret -3.6%", p[d]["open_ret_sum"], sl_ret)
        checkeq(f"d={d} open_wins=0",  p[d]["open_wins"], 0)
        checkeq(f"d={d} close_wins=0", p[d]["close_wins"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T6 — All 10 exit days, no SL — verify returns per day
# ═════════════════════════════════════════════════════════════════════════════

def test_all_ten_days_no_sl():
    """
    Entry=200. SL=0 (never triggers).
    d=1: D1 close=205 -> +2.5%
    d=2: D2 open=208, close=210
    d=3: D3 open=195 (loss), close=198
    """
    print("\n[ T6  All 10 exit days — exact returns, no SL ]")
    d0 = (104, 110, 100, 103)
    d1 = (200, 207, 198, 205)    # close=205
    d2 = (208, 215, 205, 210)    # open=208, close=210
    d3 = (195, 202, 192, 198)    # open=195 (loss), close=198 (loss)
    rows = _build_rows(d0, d1, [d2, d3] + _FILL_ROWS[:7])

    setup = MockSetup({4}, "buy", sl_override=0.0)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns,  1)
    checkeq("n_sl_hits=0", nsl, 0)

    check("d=1 close_ret=+2.5%",  p[1]["close_ret_sum"], (205-200)/200)
    checkeq("d=1 open_n=0",       p[1]["open_n"], 0)
    checkeq("d=1 close_wins=1",   p[1]["close_wins"], 1)

    check("d=2 open_ret=+4.0%",   p[2]["open_ret_sum"],  (208-200)/200)
    check("d=2 close_ret=+5.0%",  p[2]["close_ret_sum"], (210-200)/200)
    checkeq("d=2 open_wins=1",    p[2]["open_wins"],  1)
    checkeq("d=2 close_wins=1",   p[2]["close_wins"], 1)

    check("d=3 open_ret=-2.5%",   p[3]["open_ret_sum"],  (195-200)/200)
    check("d=3 close_ret=-1.0%",  p[3]["close_ret_sum"], (198-200)/200)
    checkeq("d=3 open_wins=0",    p[3]["open_wins"],  0)
    checkeq("d=3 close_wins=0",   p[3]["close_wins"], 0)

    # All populated
    for d in range(1, MAX_HOLD_DAYS + 1):
        if d == 1:
            checkeq(f"d={d} close_n=1", p[d]["close_n"], 1)
        else:
            checkeq(f"d={d} open_n=1",  p[d]["open_n"], 1)
            checkeq(f"d={d} close_n=1", p[d]["close_n"], 1)


# ═════════════════════════════════════════════════════════════════════════════
# T7 — Short: intraday exit at D1 close; d=2..10 empty
# ═════════════════════════════════════════════════════════════════════════════

def test_short_normal_intraday():
    """
    direction=sell. SL=115.  D1: open=110 < 115 (no skip), high=113 < 115.
    Exit at D1 close=105.  ret=(110-105)/110 = +4.5%.
    d=2..10 must have open_n=close_n=0 (squared off — Indian market rule).
    """
    print("\n[ T7  Short — intraday exit at D1 close; d=2..10 empty ]")
    d0 = (104, 110, 100, 103)
    d1 = (110, 113, 104, 105)    # close=105
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "sell", sl_override=115.0)
    _, p, _, ns, _, nsl = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns,  1)
    checkeq("n_sl_hits=0", nsl, 0)
    check("d=1 close_ret = +4.5%", p[1]["close_ret_sum"], (110-105)/110)
    checkeq("d=1 close_n=1",  p[1]["close_n"], 1)
    checkeq("d=1 open_n=0",   p[1]["open_n"],  0)
    checkeq("d=1 close_wins=1", p[1]["close_wins"], 1)
    for d in range(2, MAX_HOLD_DAYS + 1):
        checkeq(f"d={d} open_n=0",  p[d]["open_n"],  0)
        checkeq(f"d={d} close_n=0", p[d]["close_n"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T8 — Short: SL during D1 (high >= SL)
# ═════════════════════════════════════════════════════════════════════════════

def test_short_sl_during_d1():
    """
    direction=sell. SL=115.  D1: open=110, high=116 >= SL -> exit at 115.
    ret = (110-115)/110 = -4.5%  (loss — price rose through SL).
    """
    print("\n[ T8  Short — SL triggered during D1 ]")
    d0 = (104, 110, 100, 103)
    d1 = (110, 116, 104, 108)    # high=116 >= SL=115
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "sell", sl_override=115.0)
    _, p, _, ns, _, nsl = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns,  1)
    checkeq("n_sl_hits=1", nsl, 1)
    check("d=1 close_ret = -4.5%", p[1]["close_ret_sum"], (110-115)/110)
    checkeq("d=1 close_wins=0", p[1]["close_wins"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T9 — Short skip: D1 opens at/above SL -> trade not entered
# ═════════════════════════════════════════════════════════════════════════════

def test_short_skip_adverse_open():
    print("\n[ T9  Short — D1 opens at/above SL -> n_signals=0 ]")
    d0 = (104, 110, 100, 103)
    d1 = (116, 120, 112, 118)    # open=116 >= SL=115 -> skip
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "sell", sl_override=115.0)
    _, p, _, ns, _, nsl = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=0 (D1 open >= SL)", ns, 0)
    checkeq("n_sl_hits=0", nsl, 0)
    total_n = sum(p[d]["open_n"] + p[d]["close_n"] for d in range(1, MAX_HOLD_DAYS + 1))
    checkeq("all buckets empty", total_n, 0)


# ═════════════════════════════════════════════════════════════════════════════
# T10 — Short: D1 opens exactly at SL -> also skipped
# ═════════════════════════════════════════════════════════════════════════════

def test_short_skip_open_equals_sl():
    print("\n[ T10 Short — D1 open == SL -> skip ]")
    d0 = (104, 110, 100, 103)
    d1 = (115, 120, 112, 118)    # open=115 == SL=115
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "sell", sl_override=115.0)
    _, _, _, ns, _, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)
    checkeq("n_signals=0 (open==SL)", ns, 0)


# ═════════════════════════════════════════════════════════════════════════════
# T11 — Neutral direction: actual return (no abs threshold)
# ═════════════════════════════════════════════════════════════════════════════

def test_neutral_direction():
    """
    Entry=200, no SL.  signal_type="" -> neutral -> long path.
    d=1: D1 close=200 -> ret=0.0 (break-even, recorded faithfully)
    d=2: D2 open=201  -> ret=+0.5%
    d=3: D3 open=195  -> ret=-2.5%
    """
    print("\n[ T11 Neutral — actual return, no abs() threshold ]")
    d0 = (104, 110, 100, 103)
    d1 = (200, 205, 198, 200)    # close=200 -> ret=0
    d2 = (201, 204, 199, 202)    # open=201
    d3 = (195, 198, 193, 197)    # open=195
    rows = _build_rows(d0, d1, [d2, d3] + _FILL_ROWS[:7])

    setup = MockSetup({4}, "", sl_override=0.0)
    p, _, ns, _, _, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns, 1)
    check("d=1 close_ret=0.0",   p[1]["close_ret_sum"], (200-200)/200)
    check("d=2 open_ret=+0.5%",  p[2]["open_ret_sum"],  (201-200)/200)
    check("d=3 open_ret=-2.5%",  p[3]["open_ret_sum"],  (195-200)/200)
    checkeq("d=1 close_wins=0 (0 not > 0)", p[1]["close_wins"], 0)
    checkeq("d=2 open_wins=1",  p[2]["open_wins"],  1)
    checkeq("d=3 open_wins=0",  p[3]["open_wins"],  0)


# ═════════════════════════════════════════════════════════════════════════════
# T12 — Multiple signals: sl_hits cumulate
# ═════════════════════════════════════════════════════════════════════════════

def test_multiple_signals_sl_cumulate():
    """
    Signal fires at len=4 and len=5.  Both get stopped out on their D2.
    n_signals=2, n_sl_hits=2.
    """
    print("\n[ T12 Multiple signals — sl_hits cumulate ]")
    rows = WARMUP + [
        (104, 110, 100, 103),   # bar3 D0  signal-1 (len=4)
        (200, 206, 198, 204),   # bar4 D1-sig1 / D0-sig2 (len=5 fires sig2)
        (210, 216, 193, 214),   # bar5 D2-sig1 low=193<SL=195, D1-sig2 entry=210
        (220, 226, 191, 224),   # bar6 D2-sig2 low=191<SL=195
    ] + _FILL_ROWS[:7] + [(300, 305, 298, 303), (310, 315, 308, 313)]

    setup = MockSetup({4, 5}, "buy", sl_override=195.0)
    _, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)
    checkeq("n_signals=2", ns,  2)
    checkeq("n_sl_hits=2", nsl, 2)


# ═════════════════════════════════════════════════════════════════════════════
# T13 — _summarise: confidence = Bayesian win rate
# ═════════════════════════════════════════════════════════════════════════════

def test_summarise_confidence():
    """
    d=1: close-only; 10 wins out of 20.  wr=0.50 -> Bayesian conf=(10+5)/(20+10)=0.50.
    d=2..10: open has higher avg (2.0/20=0.10 vs 1.0/20=0.05) and more wins (14 vs 10).
             -> best_exit=open, wr=14/20=0.70, Bayesian conf=(14+5)/(20+10)=0.633.
    """
    print("\n[ T13 _summarise: confidence = Bayesian win rate ]")
    by_day = {}
    for d in range(1, MAX_HOLD_DAYS + 1):
        by_day[d] = {
            "open_ret_sum": 2.0, "open_n": 20, "open_wins": 14,   # avg=0.10, wr=0.70
            "close_ret_sum": 1.0, "close_n": 20, "close_wins": 10, # avg=0.05, wr=0.50
        }
    # d=1: open_n=0 (entered at D1 open; no same-day open exit)
    by_day[1] = {
        "open_ret_sum": 0.0, "open_n": 0, "open_wins": 0,
        "close_ret_sum": 1.0, "close_n": 20, "close_wins": 10,
    }

    stats = _summarise(by_day, total_signals=20, total_sl_hits=4)

    # d=1: close-only, wr=10/20=0.50
    d1 = stats["by_day"]["1"]
    check("d=1 win_rate=0.50",      d1["win_rate"], 10/20)
    expected_d1 = (10/20 * 20 + PRIOR_WR * PRIOR_N) / (20 + PRIOR_N)
    check("d=1 confidence=Bayesian", d1["confidence"], expected_d1)
    checkeq("d=1 best_exit=close",  d1["best_exit"], "close")

    # d=2: open avg (0.10) > close avg (0.05) -> best_exit=open, wr=14/20=0.70
    d2 = stats["by_day"]["2"]
    check("d=2 win_rate=0.70",      d2["win_rate"], 14/20)
    expected_d2 = (14/20 * 20 + PRIOR_WR * PRIOR_N) / (20 + PRIOR_N)
    check("d=2 confidence=Bayesian", d2["confidence"], round(expected_d2, 4))
    checkeq("d=2 best_exit=open",   d2["best_exit"], "open")

    checkeq("sl_hits=4",   stats["sl_hits"],  4)
    check("sl_rate=0.2",   stats["sl_rate"],  0.2)
    # Best day picks a day with open (wr=0.70); confidence should be well above 0.5
    checkgt("best_confidence > 0.5", stats["best_confidence"], 0.5)
    checkgt("best_win_rate > 0.5",   stats["best_win_rate"],   0.5)


# ═════════════════════════════════════════════════════════════════════════════
# T14 — Default BaseSetup.get_stoploss
# ═════════════════════════════════════════════════════════════════════════════

def test_default_get_stoploss():
    print("\n[ T14 Default BaseSetup.get_stoploss ]")

    class ConcreteSetup(BaseSetup):
        name="X"; description=""; min_periods=1
        def signal(self, df, sym): return SignalResult(False,sym,self.name,"",{})

    s  = ConcreteSetup()
    df = make_df([(100, 110, 90, 105)])

    for stype in ("buy", "long", "oversold"):
        r = SignalResult(True,"T","X","2024-01-01",{"signal_type": stype})
        check(f"SL for '{stype}' = low (90)", s.get_stoploss(r, df), 90.0)

    for stype in ("sell", "short", "overbought"):
        r = SignalResult(True,"T","X","2024-01-01",{"signal_type": stype})
        check(f"SL for '{stype}' = high (110)", s.get_stoploss(r, df), 110.0)


# ═════════════════════════════════════════════════════════════════════════════
# T15 — EIGHTY_TWENTY.get_stoploss override
# ═════════════════════════════════════════════════════════════════════════════

def test_eighty_twenty_stoploss():
    print("\n[ T15 EIGHTY_TWENTY.get_stoploss ]")
    from eighty_twenty import EightyTwentySetup
    s  = EightyTwentySetup()
    df = make_df([(100, 110, 90, 105)])
    r_b = SignalResult(True,"T","EIGHTY_TWENTY","2024-01-01",{"signal_type":"buy","entry_level":92.5})
    r_s = SignalResult(True,"T","EIGHTY_TWENTY","2024-01-01",{"signal_type":"sell","entry_level":108.5})
    check("buy SL = entry_level 92.5",   s.get_stoploss(r_b, df), 92.5)
    check("sell SL = entry_level 108.5", s.get_stoploss(r_s, df), 108.5)


# ═════════════════════════════════════════════════════════════════════════════
# T16 — BOLLINGER_SQUEEZE.get_stoploss override
# ═════════════════════════════════════════════════════════════════════════════

def test_bollinger_squeeze_stoploss():
    print("\n[ T16 BOLLINGER_SQUEEZE.get_stoploss ]")
    from bollinger_squeeze import BollingerSqueezeSetup
    s  = BollingerSqueezeSetup()
    df = make_df([(100, 120, 80, 105)])
    r_b = SignalResult(True,"T","BOLLINGER_SQUEEZE","2024-01-01",{"signal_type":"buy","upper":118.0,"lower":82.0,"middle":100.0})
    r_s = SignalResult(True,"T","BOLLINGER_SQUEEZE","2024-01-01",{"signal_type":"sell","upper":118.0,"lower":82.0,"middle":100.0})
    check("buy SL = lower 82.0",   s.get_stoploss(r_b, df), 82.0)
    check("sell SL = upper 118.0", s.get_stoploss(r_s, df), 118.0)


# ═════════════════════════════════════════════════════════════════════════════
# T17 — SL exactly at entry -> ret=0 -> close_wins=0 (strict > 0)
# ═════════════════════════════════════════════════════════════════════════════

def test_sl_at_entry_price():
    """
    Entry=110. SL=110. D1 low=109 < 110 -> SL intraday at 110 = entry.
    ret = (110-110)/110 = 0.0.  close_wins = 0 (0 is not > 0 -> not a win).
    All 10 days carry ret=0.0, no wins.
    """
    print("\n[ T17 SL at entry -> ret=0 -> 0 wins (strict > 0) ]")
    d0 = (104, 110, 100, 103)
    d1 = (110, 115, 109, 112)    # low=109 < SL=110
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "buy", sl_override=110.0)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_sl_hits=1", nsl, 1)
    check("d=1 close_ret=0.0",   p[1]["close_ret_sum"], 0.0)
    checkeq("d=1 close_wins=0",  p[1]["close_wins"], 0)
    for d in range(2, MAX_HOLD_DAYS + 1):
        check(f"d={d} open_ret=0.0",   p[d]["open_ret_sum"], 0.0)
        checkeq(f"d={d} open_wins=0",  p[d]["open_wins"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T18 — Short losing trade (price rises -> loss)
# ═════════════════════════════════════════════════════════════════════════════

def test_short_losing_trade():
    print("\n[ T18 Short losing trade — price rises ]")
    d0 = (104, 110, 100, 103)
    d1 = (110, 119, 108, 118)    # high=119 < SL=120; close=118 > entry -> loss
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "sell", sl_override=120.0)
    _, p, _, ns, _, nsl = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_sl_hits=0", nsl, 0)
    check("d=1 close_ret=-7.3%", p[1]["close_ret_sum"], (110-118)/110)
    checkeq("d=1 close_wins=0",  p[1]["close_wins"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T19 — No SL (parent default = signal bar low=100, never touched): all positive
# ═════════════════════════════════════════════════════════════════════════════

def test_no_sl_all_days_positive():
    """
    SL = bar3 low = 100 (from parent default).
    Entry=200; all exits well above 200; no bar's low touches 100.
    All 10 days: close_n>=1, open_ret_sum > 0 for d>=2.
    """
    print("\n[ T19 No SL hit — all 10 exit days positive returns ]")
    d0 = (104, 110, 100, 103)    # low=100 -> default SL=100
    d1 = (200, 205, 198, 203)    # close=203
    exit_rows = [(210+d*5, 215+d*5, 208+d*5, 213+d*5) for d in range(MAX_HOLD_DAYS - 1)]
    rows = _build_rows(d0, d1, exit_rows)

    setup = MockSetup({4}, "buy", sl_override=None)
    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns,  1)
    checkeq("n_sl_hits=0", nsl, 0)
    checkgt("d=1 close_ret > 0",  p[1]["close_ret_sum"], 0)
    checkeq("d=1 close_n=1",      p[1]["close_n"], 1)
    for d in range(2, MAX_HOLD_DAYS + 1):
        if p[d]["open_n"] > 0:
            checkgt(f"d={d} open_ret > 0", p[d]["open_ret_sum"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T20 — sl_pct override replaces native SL
# ═════════════════════════════════════════════════════════════════════════════

def test_sl_pct_override():
    """
    setup.sl_pct = 5.0 -> SL placed 5% below entry.
    Entry=200 -> sl_price=190.  If D1 low < 190 -> SL triggered.
    D1 low=185 < 190 -> SL hit at open (gap-down) or low.
    """
    print("\n[ T20 sl_pct override — percentage-based stoploss ]")
    d0 = (104, 110, 100, 103)
    d1 = (200, 202, 185, 196)    # low=185 < SL=190 (5% below 200)
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "buy", sl_override=None)  # native SL = bar3 low = 100 (won't trigger)
    setup.sl_pct = 5.0     # override: stop 5% below entry

    p, _, ns, _, nsl, _ = _process_one_stock(setup, "T", make_df(rows), stride=1)

    checkeq("n_signals=1", ns,  1)
    checkeq("n_sl_hits=1  [sl_pct=5% triggered]", nsl, 1)
    # ret = (190 - 200) / 200 = -5.0%
    check("d=1 close_ret = -5.0%", p[1]["close_ret_sum"], (190-200)/200)
    checkeq("d=1 close_wins=0", p[1]["close_wins"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T21 — Transaction cost is netted from every trade return
# ═════════════════════════════════════════════════════════════════════════════

def test_transaction_cost_netting():
    """
    Entry=200, D1 close=205 -> gross +2.5%. With cost=1.0% round trip the booked
    return must be +1.5%. A trade whose gross edge is smaller than the cost must
    flip from a 'win' to a 'loss' — the whole point of costing at the trade level.
    """
    print("\n[ T21 Transaction cost netted from trade return ]")
    d0 = (104, 110, 100, 103)
    d1 = (200, 205, 198, 205)
    rows = _build_rows(d0, d1, _FILL_ROWS)

    setup = MockSetup({4}, "buy", sl_override=0.0)
    p, _, ns, _, _, _ = _process_one_stock(setup, "T", make_df(rows), stride=1, cost=0.01)
    check("d=1 net ret = +1.5% (2.5% - 1.0% cost)", p[1]["close_ret_sum"], (205-200)/200 - 0.01)
    checkeq("d=1 still a win", p[1]["close_wins"], 1)

    # A +0.5% gross trade with 1% cost -> -0.5% net -> loss
    d1b = (200, 202, 199, 201)   # close=201 -> +0.5% gross
    rows_b = _build_rows(d0, d1b, _FILL_ROWS)
    p2, _, _, _, _, _ = _process_one_stock(setup, "T", make_df(rows_b), stride=1, cost=0.01)
    check("d=1 net ret = -0.5%", p2[1]["close_ret_sum"], (201-200)/200 - 0.01)
    checkeq("d=1 flips to a loss (cost > gross edge)", p2[1]["close_wins"], 0)
    checkeq("d=1 loss_n=1", p2[1]["close_loss_n"], 1)


# ═════════════════════════════════════════════════════════════════════════════
# T22 — Wilson lower bound: pessimistic, sample-size aware
# ═════════════════════════════════════════════════════════════════════════════

def test_wilson_lower_bound():
    """
    Wilson lower bound must (a) be <= the point estimate, (b) rise toward the
    point estimate as n grows for the same win rate, and (c) be 0 for n=0.
    """
    print("\n[ T22 Wilson lower bound — honest small-sample reliability ]")
    checkeq("n=0 -> 0.0", _wilson_lower_bound(0, 0), 0.0)
    lo_small = _wilson_lower_bound(7, 10)     # 70% on n=10
    lo_big   = _wilson_lower_bound(700, 1000) # 70% on n=1000
    checkgt("lo(70%,n=10) < point 0.70", 0.70, lo_small)
    checkgt("lo(70%,n=1000) > lo(70%,n=10)", lo_big, lo_small)
    checkgt("lo(70%,n=1000) closer to 0.70", lo_big, 0.65)


# ═════════════════════════════════════════════════════════════════════════════
# T23 — _summarise reports expectancy / profit factor / wr_lower
# ═════════════════════════════════════════════════════════════════════════════

def test_summarise_profit_factor():
    """
    d=1 close: 12 wins summing +3.0 (avg win +0.25), 8 losses summing -1.0
    (avg loss -0.125). profit_factor = 3.0 / 1.0 = 3.0. Magnitude metrics must
    surface so a win rate can't masquerade as the whole story.
    """
    print("\n[ T23 _summarise: expectancy / profit factor / wr_lower ]")
    by_day = {d: {
        "open_ret_sum": 0.0, "open_n": 0, "open_wins": 0,
        "open_win_sum": 0.0, "open_loss_sum": 0.0, "open_loss_n": 0,
        "close_ret_sum": 2.0, "close_n": 20, "close_wins": 12,
        "close_win_sum": 3.0, "close_loss_sum": -1.0, "close_loss_n": 8,
    } for d in range(1, MAX_HOLD_DAYS + 1)}

    stats = _summarise(by_day, total_signals=20, total_sl_hits=0)
    d1 = stats["by_day"]["1"]
    check("d=1 avg_win = +0.25",    d1["avg_win"],  3.0/12)
    check("d=1 avg_loss = -0.125",  d1["avg_loss"], -1.0/8)
    check("d=1 profit_factor = 3.0", d1["profit_factor"], 3.0)
    checkgt("d=1 wr_lower < win_rate 0.60", 0.60, d1["wr_lower"])
    checkgt("top-level profit_factor present", stats["profit_factor"], 1.0)


# ═════════════════════════════════════════════════════════════════════════════
# Run all tests
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("  Backtester Simulation — Entry/Exit + Confidence Model")
    print(f"  MAX_HOLD_DAYS={MAX_HOLD_DAYS}   PRIOR_N={PRIOR_N}   PRIOR_WR={PRIOR_WR}")
    print("  d=1 = D1 CLOSE   d=2..10 = Dd open+close   sl_exit_d = offset+1")
    print("  BUY/neutral -> partial_long   SELL -> partial_short (intraday only)")
    print("=" * 70)

    test_d1_is_d1_close()
    test_entry_is_d1_open_not_d0_close()
    test_sl_on_d1_propagates_all_days()
    test_gap_down_sl_breakeven()
    test_sl_on_d2_maps_to_exit_d2()
    test_sl_on_d3_maps_to_exit_d3()
    test_all_ten_days_no_sl()
    test_short_normal_intraday()
    test_short_sl_during_d1()
    test_short_skip_adverse_open()
    test_short_skip_open_equals_sl()
    test_neutral_direction()
    test_multiple_signals_sl_cumulate()
    test_summarise_confidence()
    test_default_get_stoploss()
    test_eighty_twenty_stoploss()
    test_bollinger_squeeze_stoploss()
    test_sl_at_entry_price()
    test_short_losing_trade()
    test_no_sl_all_days_positive()
    test_sl_pct_override()
    test_transaction_cost_netting()
    test_wilson_lower_bound()
    test_summarise_profit_factor()

    print()
    print("=" * 70)
    print(f"  PASS: {PASSES}   FAIL: {FAILS}")
    print("=" * 70)
    if FAILS:
        sys.exit(1)
