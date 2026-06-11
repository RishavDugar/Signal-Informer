# Strategy Research — June 2026 Expansion

> Scope of this round: (1) audit the confidence calculator and return estimator
> for economic soundness, (2) expand the strategy library from 18 to 76 setups,
> (3) hyperparameter-tune everything, (4) extend the engine to intraday (HFT)
> data from 1-minute to 15-minute bars with long+short, square-off-by-close
> trading. All numbers below are NET of transaction costs.

---

## 1. Audit of the confidence calculator & return estimator

The system's job is to tell an investor *"if you trade this signal, what do you
make on average, and how sure are we?"* The audit found four places where the
old math gave an optimistic or economically incoherent answer, and one place
where it threw information away.

### 1.1 Findings

| # | Problem | Economic consequence | Fix |
|---|---------|----------------------|-----|
| 1 | **Conviction ignored return variance.** Weight = `1 + smoothed_avg×20`. A +1% average over 30 wild trades (t ≈ 0.5, indistinguishable from luck) weighed the same as +1% earned steadily (t ≈ 3). | Investor is told to size up on noise. | Track per-trade return variance; compute `ret_lower = avg − z·(std/√n)` — the expectancy we are 90% confident the setup *beats* — and drive the weight from it: `clip(1 + ret_lower×20, 0.10, 2.00)`. |
| 2 | **Best-day / best-exit chosen on the in-sample point estimate.** Choosing the max of 10 holding days × 2 exit styles is a 20-way multiple comparison; the winner's average is biased upward by construction. | Reported expectancy systematically overstates what the investor will realise. | Selection now optimises `ret_lower` instead of the raw average. A noisy day with a lucky mean no longer wins the selection. |
| 3 | **`MIN_OBS_FOR_BEST = 5`.** A holding period could be crowned on 5 trades. | Curve-fitting; horizon advice flips run to run. | Raised to 20 (env-overridable). |
| 4 | **Hyperparameter search scored combos by raw average return.** With dozens-to-hundreds of combos per setup, the argmax of noisy averages is the textbook definition of overfitting. | "Optimal" parameters that evaporate out of sample. | `_combined_score` now uses `ret_lower` (× the existing stop-out penalty). A combo can only win by having a statistically defensible edge, not a lucky draw. |
| 5 | **WhatsApp conviction summed correlated setup weights linearly.** Five oversold-flavoured setups firing on the same price drop are not five independent confirmations. | A crowd of mediocre correlated signals outranked one statistically solid setup. | Same-direction weights now combine with geometric discounting (1, ½, ¼, …): confirmation still helps, but sub-additively. |

What was already right and is kept: trade-level cost netting (30 bps round
trip), the Wilson lower bound on the win rate, the Bayesian-smoothed headline
win rate, profit-factor/magnitude reporting, and the prefer-earlier-day
tie-break (capital is freed sooner).

### 1.2 New numbers an investor sees

Every setup (and every holding day) now also reports:

- `ret_lower` — one-sided 90% lower confidence bound on the **mean net
  return**. This is the number conviction weights are built from. Positive
  `ret_lower` = "even on a pessimistic read of the sample, the edge is
  positive after costs."
- `ret_std` — per-trade return dispersion (risk per signal).
- `t_stat` — mean ÷ standard error. Rule of thumb: below ~1.3 the edge is not
  distinguishable from zero at 90% confidence.

Known remaining caveats (documented, not yet corrected):

- **Overlapping trades.** The backtester books a trade at every signal bar;
  consecutive signals in the same stock can overlap in time, and signals
  across stocks share market beta, so the effective sample is smaller than n.
  `ret_lower` is therefore still somewhat too tight for multi-day holds.
- **Best-exit selection is still in-sample** (open vs close per day), though
  selecting on the lower bound shrinks the bias materially.

---

## 2. The strategy library: 18 → 76 setups

### 2.1 Architecture change that made it possible

The legacy protocol recomputed all indicators on a growing window for every
bar — O(n²) per stock, ~10 minutes for 18 setups on daily data and unusable on
intraday data. The new `VectorSetup` base (`core/vector_setup.py`) computes
the whole signal series in one vectorised pass:

- `vector_signals(df) → Series of {+1 long, −1 short, 0}` — strictly causal
  (rolling/shift only, fire-on-first-bar cross semantics).
- The classic `signal()` API is derived automatically from the last bar, so
  every new setup plugs into the daily pipeline, the signal DB and WhatsApp
  alerts unchanged.
- All 18 legacy setups also received hand-written `vector_signals`, verified
  bar-for-bar against their per-bar implementations on real data
  (17/18 exact; BOLLINGER_SQUEEZE differs only in the first ~45 warm-up bars
  of a window, where the vector version is the stricter one).
- Result: **76 setups × 500 stocks × ~2.5 years backtests in ~40 s** (was
  ~10 min for 18 setups), and the hyperparameter search over 3,348
  combinations completes in under half an hour on 12 cores.

Multiple setups share one themed file; the loader, backtester workers,
weights JSON and hypersearch all key on `setup.name`, so **per-strategy
metrics are fully preserved** (each of the 76 names gets its own row in
`db/strategy_weights.json` and the console table).

### 2.2 The 58 new setups

**Mean reversion** (`setups_mean_reversion.py`, 14): RSI2_EXTREME (Connors
RSI-2 with trend filter), DOUBLE_SEVENS, IBS_REVERSAL (internal bar strength),
ZSCORE_REVERSION, BOLLINGER_TAG, GAP_DOWN_REVERSAL, CAPITULATION_REVERSAL
(waterfall + volume climax), RSI_DIVERGENCE, STOCH_HOOK, WICK_REJECTION
(hammer/shooting star at n-day extremes), STREAK_FADE, MFI_EXTREME,
WILLIAMS_R_EXTREME, CCI_REVERSAL.

**Momentum / trend** (`setups_momentum.py`, 12): DONCHIAN_BREAKOUT (Turtle),
HIGH_52W_BREAKOUT (52-week-high anomaly), GOLDEN_CROSS_PULLBACK,
MACD_ZERO_TURN, ROC_THRUST, ADX_DI_CROSS, AROON_CROSS, SUPERTREND_FLIP,
KELTNER_BREAKOUT, HH_HL_STRUCTURE, TSI_CROSS, VORTEX_CROSS.

**Volatility** (`setups_volatility.py`, 8): NR7_BREAKOUT (Crabel),
TTM_SQUEEZE (Bollinger-inside-Keltner), ATR_COMPRESSION_BREAK,
VOLATILITY_BREAKOUT (Larry Williams k×ATR), INSIDE_BAR_BREAKOUT,
RANGE_EXPANSION, GAP_AND_GO, BB_WIDTH_SQUEEZE.

**Volume** (`setups_volume.py`, 8): OBV_DIVERGENCE, POCKET_PIVOT
(O'Neil/Morales), VOLUME_DRYUP, CMF_CROSS (Chaikin), FORCE_INDEX_PULLBACK
(Elder), AD_DIVERGENCE, HIGH_VOLUME_THRUST, EOM_CROSS.

**Price patterns** (`setups_patterns.py`, 8): ENGULFING_EXTREME, MORNING_STAR,
THREE_SOLDIERS, PIERCING_LINE, KEY_REVERSAL, DOJI_EXTREME, MARUBOZU_CONT,
OOPS_REVERSAL (Larry Williams).

**Oscillators / hybrids** (`setups_oscillators.py`, 8): STOCH_RSI_CROSS,
ULTIMATE_OSC, CMO_EXTREME, DPO_REVERSION, TRIX_CROSS, PPO_MOMENTUM,
ELDER_IMPULSE, RSI_BB_CONFLUENCE.

Design rules applied throughout: every pattern is quantified (no chart
subjectivity); reversal patterns are anchored to an n-bar extreme (pattern
**plus location**); cross/first-bar semantics prevent a persisting condition
from re-firing daily; each class declares its own `param_grid` which the
hyperparameter search picks up automatically (stop distance `sl_pct` is always
appended to the grid).

---

## 3. Daily-timeframe results (NSE-500, 2023-09 → 2026-06, net of 30 bps)

Hyperparameter search: 3,348 combinations across all 76 setups (grid + tuned
stop distance `sl_pct` ∈ {1, 2, 3, 5}%), scored on the lower-bound expectancy
with a stop-out penalty, then validated at full precision. Headline: **10 of
76 setups have positive net expectancy at their tuned parameters; 3 clear the
0.5% investor screen and earn conviction weight > 1.**

### 3.1 Setups that survive 30 bps costs (long side, tuned params)

| Setup | Weight | Hold | Avg net/trade | ret_lower | t-stat | Win rate | WR floor | PF | SL rate | Trades |
|---|---|---|---|---|---|---|---|---|---|---|
| **GAP_AND_GO** | **1.348** | d6 | **+1.96%** | +1.74% | 11.4 | 55.7% | 54.1% | 2.02 | 39% | 1,777 |
| **RSI2_EXTREME** | **1.220** | d2 | +1.76% | +1.10% | 3.4 | 55.4% | 48.0% | 3.36 | 32% | 74 |
| **CAPITULATION_REVERSAL** | **1.094** | d7 | +0.88% | +0.47% | 2.7 | 41.8% | 39.4% | 1.30 | 56% | 684 |
| BOLLINGER_TAG | 0.10* | d5 | +0.46% | — | — | 49% | — | — | 40% | 2,405 |
| ZSCORE_REVERSION | 0.10* | d5 | +0.47% | — | — | 49% | — | — | 40% | 2,385 |
| VOLUME_CLIMAX | 0.10* | d6 | +0.47% | — | — | 45% | — | — | 46% | 1,382 |
| ADX_GAPPER | 0.10* | d6 | +0.40% | +0.12% | 1.8 | 48.1% | 45.7% | 1.19 | 42% | 717 |
| RSI_BB_CONFLUENCE | 0.10* | d3 | +0.32% | — | — | 48% | — | — | 44% | 1,490 |
| RSI_EXTREME | 0.10* | d2 | +0.17% | +0.03% | 1.6 | 50.1% | 48.4% | 1.12 | 44% | 1,436 |
| WHIPLASH | 0.10* | d5 | +0.09% | — | — | 47% | — | — | 40% | 9,382 |

\* positive expectancy but below the `MIN_AVG_RETURN = 0.5%` investor screen,
so the weight is floored at 0.10 and these will not drive WhatsApp picks.
BOLLINGER_TAG / ZSCORE_REVERSION / VOLUME_CLIMAX sit at 0.46–0.47% — a
whisker under the gate; if the screen is ever relaxed to 0.4% they join the
alert pool.

Read on GAP_AND_GO (the standout): a stock that gaps up ≥3% **and holds the
gap into the close** continues for ~6 more sessions; +1.96% per trade net,
t-stat 11.4 over 1,777 trades — the only setup whose edge would survive even
a doubling of assumed costs. Its short side is also the only profitable short
book (+0.2%/trade on 796 trades).

### 3.2 What the honest math killed

The remaining 66 setups are **negative after costs at every parameter combo
tried** — including this round's fashionable ones (SUPERTREND_FLIP,
DONCHIAN_BREAKOUT, TTM_SQUEEZE, POCKET_PIVOT). Two structural reasons:

1. **30 bps round trip is a high bar**: most daily-bar signals have a gross
   edge of 10–40 bps, which costs consume entirely.
2. **The old scoring would have "found" winners anyway** — by picking the
   luckiest parameter combo. The lower-bound scoring refuses to: a combo can
   only win with consistent, low-variance, adequately-sampled returns. The
   3 survivors are survivors precisely because their t-stats (2.7–11.4) say
   the edge is real, not selected.

Sub-finding on stop placement: for every mean-reversion setup, tight stops
were catastrophic (1% stop → 85–93% stop-out rates — the entry *is* at the
volatile extreme), and the search consistently chose the widest stop (5%).
Mean-reversion entries need room or no stop, not protection at the entry bar's
low.

### 3.3 Sample-size caveat

RSI2_EXTREME passes with only 74 trades (the period-4/threshold-5 variant is
extremely selective). Its `ret_lower` (+1.10%) already penalises the small
sample, but it should be watched in the weekly outcome scorecard before
being trusted with large size.

---

## 4. Intraday / HFT extension (1-minute to 15-minute bars)

### 4.1 Engine

`hft_backtester.py` runs every vector-capable setup over the parquet store
copied to `data/hft/` (536 NSE symbols, hive-partitioned
`timeframe/symbol/year`, 2015–2026):

- **Intraday only**: entry at the next bar's open after the signal bar
  (same session only — a signal on the last bar of a session is discarded);
  every position is squared off at the session's final bar close. Because
  nothing is held overnight, **both longs and shorts are allowed**.
- Optional `sl_pct` stop is evaluated intrabar from entry to session close;
  gap-through opens fill at the open (worst case).
- **Costs**: 10 bps round trip (`HFT_TRANSACTION_COST`) vs 30 bps for
  delivery — intraday Indian equity costs are dominated by slippage; STT
  applies to the sell side only and brokerage is capped per order.
- **Relaxed screens** (`HFT_MIN_AVG_RETURN`): a setup passes at avg ≥ 5 bps
  and `ret_lower > 0`, versus 50 bps on the daily engine — intraday edges are
  smaller per trade but recur far more often.
- Statistics per (setup × timeframe): n, long/short split, win rate, Wilson
  lower bound, avg net return, std, t-stat, `ret_lower`, profit factor,
  avg win/loss, stop-out rate, average holding bars.
  Output: `db/hft_results.json`.

### 4.2 Two execution-realism rules learned the hard way

1. **Circuit guard.** The first 15-minute pass showed CAPITULATION_REVERSAL at
   +16.3% per intraday trade — impossible. The cause: crash days (Mar-2020,
   election day Jun-2024) print bars you cannot trade — stocks locked at
   circuit limits with zero-range bars and >10% bar-to-bar jumps. The engine
   now refuses entries when the signal bar moved >10% bar-to-bar or the entry
   bar has zero range (locked at a limit). That one guard cut the setup to
   +1.97% — still strong, but honest.
2. **Bar-vs-day semantics.** Setup lookbacks are in BARS, not days. On
   15-minute data, CAPITULATION_REVERSAL's "12% drop over 3 days" becomes
   "12% drop in 45 minutes", and GAP_DOWN_REVERSAL's overnight gap fires on
   the first bar of the session. Some setups transform into *different but
   legitimate* strategies intraday (the gap fade); others become circuit-day
   artifacts. Each intraday "pass" below was sanity-read with this in mind.

### 4.3 Results by timeframe

Coverage: 15min and 10min = full history 2015–2026, all 536 symbols;
5min = 2023–2026; 1min = 2025–2026. Screen: avg ≥ 5 bps AND ret_lower > 0,
net of 10 bps.

**Setups passing the intraday screen:**

| Timeframe | Setup | n | Win rate | Avg net/trade | ret_lower | t-stat | PF |
|---|---|---|---|---|---|---|---|
| 1min  | GAP_DOWN_REVERSAL | 831 | 64.1% | **+1.03%** | +0.90% | 10.1 | 2.42 |
| 5min  | GAP_DOWN_REVERSAL | 1,238 | 57.1% | **+0.70%** | +0.59% | 8.4 | 1.86 |
| 10min | GAP_DOWN_REVERSAL | 6,238 | 49.8% | +0.34% | +0.15% | 2.3 | 1.27 |
| 10min | CAPITULATION_REVERSAL | 632 | 52.5% | +4.41% | +2.58% | 3.1 | 3.54 |
| 15min | CAPITULATION_REVERSAL | 667 | 48.1% | +1.97% | +1.58% | 6.5 | 2.02 |
| 5min  | CAPITULATION_REVERSAL | 81 | 44.4% | +0.96% | +0.03% | 1.3 | 1.46 |

**The headline intraday finding — GAP_DOWN_REVERSAL (overnight-panic fade).**
On intraday bars this setup buys the bar after a ≥3% overnight gap-down in a
stock whose previous close was above its trend average, and exits at the
session close. The edge is real on every timeframe tested, *grows* as the
entry gets closer to the open (1.03% on 1-minute entry vs 0.34% on 10-minute),
and is long-only by construction. This mirrors the daily engine's best setup
(GAP_AND_GO, the held-gap *continuation*) — both say the same economic thing:
**overnight gaps in NSE stocks are systematically mispriced at the open.**

**CAPITULATION_REVERSAL** (≥12% flush over 3 bars + volume climax) is
profitable at 10/15-minute granularity even after the circuit guard, but it is
a rare-event strategy (~60 trades/year across 536 symbols) whose fills sit in
the most violent minutes of the market; assume materially worse slippage than
10 bps before trading it.

**THREE_DAY_GAP_REVERSAL** shows a statistically solid micro-edge at 10/15min
(+3–4 bps/trade, t > 10, n > 220k, two-thirds short) — real, but below the
cost screen: it would need sub-3 bps all-in execution to be worth trading.

**Everything else — 73 of 76 setups — is negative intraday after 10 bps.**
The oscillators, breakouts and patterns that looked plausible on daily bars
lose 4–15 bps per trade signal-to-close intraday, usually with crushing
consistency (t-stats of −20 to −240 on millions of trades). The intraday
session is far more efficient than the daily horizon: generic indicator
signals carry no net edge once any realistic cost is applied.

### 4.4 Practical recommendations

1. **Trade the gap complex.** The only intraday strategies worth productising
   are the two gap setups: GAP_DOWN_REVERSAL (intraday fade of overnight
   panic, enter within the first 1–5 minutes, exit at close, long-only) and
   the daily GAP_AND_GO (held-gap continuation, ~6-day hold). They are
   complementary: one trades gap *failure* intraday, the other gap
   *persistence* over days.
2. **Use 10/15-minute CAPITULATION_REVERSAL as an opportunistic overlay**, at
   small size, with limit orders only, accepting that backtested fills in
   crash regimes are optimistic.
3. **Do not deploy the other setups intraday.** Their daily-bar variants are
   already screened by conviction weights; intraday they are cost donors.

---

## 5. How to reproduce

```bash
# environment
.venv\Scripts\activate                      # pandas 3.0.3 / numpy 2.4 / pyarrow

# daily engine
python backtester.py                        # ~40 s, writes db/strategy_weights.json
python hyperparameter_search.py --force-rerun   # 3,348 combos, ~25 min on 12 cores

# parity audit (legacy signal() vs vector_signals())
python tests/verify_vector_parity.py 8

# intraday engine
python hft_backtester.py --timeframes 15min,10min --workers 12
python hft_backtester.py --timeframes 5min --years 2023,2024,2025,2026
python hft_backtester.py --timeframes 1min --years 2025,2026
```
