# Signal Infomer — Trading Setup Analyzer

## Overview

Daily automated trading signal generator for NSE 100 stocks plus AI-powered fundamental/news picks.

Every weekday at **7 AM IST** the news pipeline runs (Ollama LLM, internet search).  
Every weekday at **8 AM IST** the technical pipeline runs:
1. Downloads the previous day's OHLCV data for all stocks
2. Validates, sanitises, and stores it in a local SQLite database
3. Runs 18 technical setups against all stocks
4. Ranks stocks by **weighted conviction** (same-direction signal count × backtested avg return)
5. Sends a formatted WhatsApp alert for the top stocks

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
# edit .env — set WHATSAPP_PHONE, WHATSAPP_PHONES and MIN_AVG_RETURN
# (.env is the single config file; there is no .env.example template)

# 3. Bootstrap: wipes any existing data, downloads history, runs backtester
#    First run: ~15-25 min.  Re-run: same — previous data is wiped first.
python initialize.py

# 4. (Recommended) Tune signal parameters + stoploss distances
#    Grid search — exhaustive, adds sl_pct to every setup
python hyperparameter_search.py
#    Random search — wider ranges, finds multiple local maxima
python hyperparameter_search.py --random --samples 200 --peaks 5

# 5. Register Windows Task Scheduler job (daily 7am news + 8am signals)
python setup_windows_task.py

# 6. Trigger a manual pipeline run to verify signals and WhatsApp
python pipeline.py

# 7. Verify all setups are implemented correctly (optional)
python tests/verify_setups.py

# 8. Verify backtester avg-return + direction-split model (optional)
python tests/simulate_backtester.py   # 269 assertions
```

---

## Web Dashboard (dark theme)

A local browser control panel that ties **every** backend feature into one UI.

```bash
pip install flask          # one-time (also in requirements.txt)
python run_ui.py           # opens http://127.0.0.1:5000 in your browser
python run_ui.py --port 8080 --no-browser
```

Built with Flask + a dependency-free vanilla-JS front end (no build step). It
shells out to the existing CLI scripts as background subprocesses and streams
their live output to a console drawer — nothing in the pipeline logic is
duplicated or forked.

| View | What it does |
|---|---|
| **Dashboard** | DB health + size, row counts, last ingestion run, calibration freshness (weights / optimal params), schedule, Ollama status, quick-run buttons |
| **Run jobs** | One-click launch of every script: technical & news pipelines, backtester (`--quick`), grid/random hyperparameter search, initialize, tests, backup, integrity check, news/scout DB clear, Windows-task register/status/remove — with a live streaming console and a session job history |
| **Signals** | Latest (or any past) signal date, conviction-ranked per stock (reusing `notifications.whatsapp.rank_by_conviction`), per-setup avg-return / confidence / SL-rate, threshold highlighting |
| **News & Scouts** | AI news picks + all three scout lenses with CMP, 1D/5D/20D changes, catalyst, thesis, sent status |
| **Setups** | Every loaded setup with its optimal params, backtested avg return, SL rate, best day, and directional weights |
| **Stocks** | NSE-100 registry with stored-row counts; click any symbol for a close-price sparkline + recent OHLCV table |
| **Config** | View/edit the whitelisted `.env` keys (preserves comments/order); changes apply on next pipeline/server restart |
| **Logs** | Tail of `logs/signal_infomer.log` with error/warning highlighting |

Files: `run_ui.py` (launcher) + `webui/` (`server.py` routes, `queries.py`
read helpers, `jobs.py` subprocess manager, `templates/`, `static/`). The job
registry in `webui/jobs.py` is a fixed whitelist — the UI can only launch
predefined jobs, never arbitrary commands.

> **Security note:** binds to `127.0.0.1` (localhost only) by default. It exposes
> run-anything controls and `.env` editing, so only use `--host 0.0.0.0` on a
> trusted network.

---

## File Structure

```
Signal Infomer/
│
├── README.md                    ← this file
├── requirements.txt
├── .env                         ← single config file (real values + docs)
├── config.py                    ← reads .env, exposes typed settings
│
├── core/
│   └── base_setup.py            ← abstract BaseSetup + SignalResult + sl_pct meta-param
│
├── data/
│   ├── db.py                    ← SQLite WAL + all CRUD + integrity check + news_recommendations table
│   ├── stocks_list.py           ← NSE 100 symbol registry (101 stocks)
│   ├── collector.py             ← yfinance parallel downloader + retry
│   └── sanitizer.py             ← OHLCV validation + corporate action detection
│
├── Trading Setups/              ← drop any *.py here; auto-discovered at runtime
│   ├── _indicators.py           ← shared: RSI, ADX, Stochastic, EMA, HV helpers
│   ├── rsi_setup.py             ← RSI >70 / <30
│   ├── turtle_soup.py           ← Ch 4 — false breakout of 20-day extreme
│   ├── turtle_soup_plus_one.py  ← Ch 5 — day-after Turtle Soup
│   ├── eighty_twenty.py         ← Ch 6 — 80-20 reversal bar
│   ├── momentum_pinball.py      ← Ch 7 — LBR/RSI (3-period RSI of 1-day ROC)
│   ├── two_period_roc.py        ← Ch 8 — 2-period ROC pivot flip
│   ├── the_anti.py              ← Ch 9 — stochastic hook in trend direction
│   ├── holy_grail.py            ← Ch 10 — ADX(14)>30 + price touches 20-EMA
│   ├── adx_gapper.py            ← Ch 11 — gap reversal filtered by ADX(12)/DI(28)
│   ├── whiplash.py              ← Ch 12 — gap + reversal close in opposing half
│   ├── three_day_gap_reversal.py← Ch 13 — unfilled gap within 3 sessions
│   ├── id_nr4.py                ← Ch 19 — Inside Day + Narrowest Range of 4 bars
│   ├── hv_nr4.py                ← Ch 20 — 6-day HV < 50% of 100-day HV + NR4
│   ├── macd_divergence.py       ← MACD histogram divergence
│   ├── bollinger_squeeze.py     ← Bollinger Band squeeze breakout
│   ├── ema_trend_pullback.py    ← EMA trend + pullback entry
│   ├── n_down_reversal.py       ← N consecutive down days reversal
│   └── volume_climax.py         ← volume spike + price reversal
│
├── news_analyzer/               ← AI-powered fundamental/news picker
│   ├── __init__.py
│   ├── fetcher.py               ← 18 RSS feeds + 22 Google News queries + NSE events
│   ├── ollama_client.py         ← local Ollama inference wrapper (auto-selects best model, currently gemma4:12b-it-qat)
│   ├── analyzer.py              ← two-pass LLM analysis: symbol extraction + thesis
│   ├── db.py                    ← news_recommendations table helpers
│   ├── formatter.py             ← WhatsApp formatter for news picks
│   └── pipeline.py              ← orchestrates fetch → analyze → dedup → send
│
├── notifications/
│   └── whatsapp.py              ← pywhatkit sender + conviction ranking + avg-return filter
│
├── utils/
│   ├── logger.py                ← rotating file log + UTF-8 safe console handler
│   └── backup.py                ← WAL checkpoint + backup + integrity check
│
├── tests/
│   ├── verify_setups.py         ← synthetic-data tests (all strategies, both directions)
│   └── simulate_backtester.py  ← 269-assertion avg-return + direction-split verification
│
├── db/
│   ├── market_data.db           ← SQLite database (auto-created)
│   ├── strategy_weights.json    ← backtested avg returns per setup (written by backtester.py)
│   ├── optimal_params.json      ← best params + sl_pct per setup (written by hyperparameter_search.py)
│   └── backups/                 ← rolling 7-day DB backups
│
├── logs/
│   ├── signal_infomer.log       ← rotating 5 × 5 MB application log
│   ├── task_output.log          ← stdout/stderr from the 08:00 technical task
│   └── news_task_output.log     ← stdout/stderr from the 07:00 news task
│
├── backtester.py                ← avg-return engine + sl_pct support (ProcessPool parallel)
├── hyperparameter_search.py     ← grid + random search; optimises signal params + SL distance
├── pipeline.py                  ← daily orchestrator: collect → analyse → notify
├── scheduler.py                 ← APScheduler: 7am news + 8am technical pipeline
├── initialize.py                ← bootstrap: wipes previous data, downloads history, runs backtester
├── setup_windows_task.py        ← registers two Windows tasks (07:00 news + 08:00 technical)
│
├── run_ui.py                    ← launches the dark-theme web dashboard (Flask)
└── webui/                       ← browser control panel (see "Web Dashboard")
    ├── server.py                ← Flask routes + JSON API
    ├── queries.py               ← read-only helpers (reuse db / whatsapp / backtester)
    ├── jobs.py                  ← background subprocess job manager (fixed whitelist)
    ├── templates/index.html     ← single-page shell
    └── static/                  ← style.css (dark theme) + app.js (vanilla JS)
```

---

## Configuration (.env)

```ini
# ── WhatsApp (pywhatkit) ──────────────────────────────────────────────────────
# Single recipient — used by technical-pipeline signal alerts and
# ingestion-failure alerts.
WHATSAPP_PHONE=+91XXXXXXXXXX
# News-analyzer alerts ONLY (main AI picks + all 3 scout lenses): comma-
# separated list, broadcast to every number. Falls back to WHATSAPP_PHONE
# above when left blank, so single-recipient setups need no changes.
WHATSAPP_PHONES=+91XXXXXXXXXX,+91YYYYYYYYYY

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH=db/market_data.db
BACKUP_DIR=db/backups
MAX_BACKUPS=7

# ── Data collection ───────────────────────────────────────────────────────────
MAX_WORKERS=20
RETRY_ATTEMPTS=3
RETRY_DELAY_SECONDS=5
HISTORY_DAYS=1000

# ── Backtester ────────────────────────────────────────────────────────────────
BACKTEST_WINDOW_DAYS=1100
MAX_HOLD_DAYS=5
# Tie-break: prefer earlier exit if avg return is within this margin of best day.
# Now in avg-return units (e.g. 0.005 = 0.5%), not win-rate units.
BEST_DAY_THRESHOLD=0.005

# ── Notifications ─────────────────────────────────────────────────────────────
NOTIFY_ON_SIGNAL=true
NOTIFY_ON_INGESTION_FAILURE=true
# Min conviction-weighted avg return per trade for a stock to appear in alerts.
# Setups with negative expected value are excluded. 0.005 = 0.5%.
MIN_AVG_RETURN=0.005

# ── News pipeline (7 AM IST) ──────────────────────────────────────────────────
NEWS_SCHEDULE_HOUR=7
NEWS_SCHEDULE_MINUTE=0
NEWS_DEDUP_DAYS=28        # days before the same stock can be re-recommended (main news picks)
NEWS_TOP_N=5              # max AI-selected picks per morning run
SCOUT_DEDUP_DAYS=5        # days before a scout lens can resurface the same symbol

# ── Ollama (local inference) ──────────────────────────────────────────────────
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=             # empty = auto-detect best model (recommended: gemma4:12b-it-qat)

# ── Scheduler ─────────────────────────────────────────────────────────────────
SCHEDULE_HOUR=8
SCHEDULE_MINUTE=0
SCHEDULE_TZ=Asia/Kolkata
```

---

## Backtester

Computes the **average % return per trade** per setup using a rolling window over stored OHLCV data.

### Entry / exit model (Indian market rules)

| Rule | Detail |
|---|---|
| Signal fires | End of day D0 — using data through D0 close |
| Entry | D1 **open** (the morning after the signal) |
| Long exits tested | D2 open, D2 close, … D6 open, D6 close (d=1..5 buckets) |
| Short exits | Intraday only — squared off at D1 close or SL |
| Stoploss | Checked from D1 onwards; configurable via `sl_pct` |

### Stoploss

Each setup has a **native stoploss** (signal bar low for buys, high for sells). The hyperparameter search also tunes `sl_pct` — when set, it overrides the native SL with a percentage of entry price:

```
Long:  sl_price = entry_price × (1 − sl_pct / 100)
Short: sl_price = entry_price × (1 + sl_pct / 100)
```

### Avg-return metric (no survivorship bias)

Every signal contributes to **every** exit-day bucket. When an SL is triggered at day k, the SL return propagates to all days d ≥ k — because no matter when you planned to exit, you were already stopped out.

Old (wrong):
> "SL hit on day 2? Record a loss on day 2, break — days 3-5 skip this trade."  
> Result: day 5 win rate only counts survivors → **survivorship bias**.

New (correct):
> "SL hit on day 2? Record SL return on days 2, 3, 4, 5."  
> Result: avg_return[d5] = weighted average of ALL trades including stopped-out ones.

Example for a setup with 73% SL rate:

| Day | Old (survivorship bias) | New (true average) |
|-----|-------------------------|--------------------|
| d1  | 25% win rate            | −2.8% avg return   |
| d4  | **90% win rate** ← misleading | **−0.95% avg** ← true |

### Weight formula

```
weight = clip(1.0 + smoothed_return × 20, 0.10, 2.00)
smoothed_return = (avg_return × n) / (n + 10)   # Bayesian shrinkage toward 0%
```

If `avg_return < MIN_AVG_RETURN` (default 0.5%), weight is clamped to 0.10 (minimum conviction).

| Avg return | Weight |
|---|---|
| < 0.5% (threshold) | 0.10 (minimum) |
| 0% (break-even) | 1.00 |
| +2.5% | ~1.50 |
| +5% | 2.00 (maximum) |

### CLI

```bash
python backtester.py              # full run, all CPUs (stride=2)
python backtester.py --quick      # faster scan (stride=3)
python backtester.py --workers 8  # cap to N worker processes
```

Example output:
```
Setup                            d1(O/C)      d2(O/C)      d3(O/C)      d4(O/C)      d5(O/C)       Best       Weight  SL%
HOLY_GRAIL             +0.8%/+1.2%  +1.5%/+2.1%  +1.8%/+2.4%  +2.0%/+2.3%  +1.9%/+2.1%   d3 +1.80%    1.360   18%
THE_ANTI               +0.4%/+0.6%  +0.9%/+1.1%  +0.8%/+1.0%  +0.7%/+0.9%  +0.6%/+0.8%   d2 +0.90%    1.180   25%
MOMENTUM_PINBALL       -2.8%/-3.1%  -2.3%/-2.0%  -1.8%/-1.6%  -0.9%/-1.1%  -1.2%/-1.4%   d4 -0.95%    0.100   73%
```
`O/C` = avg return for open-exit / close-exit per day.  
`SL%` = percentage of signals that hit the stoploss.

---

## Hyperparameter Search

Finds the optimal parameters for each setup, including the **stoploss distance** (`sl_pct`).

### Combined score

Selection is based on a combined score that rewards high avg return and penalises frequent stop-outs:

```
combined_score = avg_return × (1 − sl_rate × 0.5)
```

Examples:
- avg=+2%, SL=73% → score = 2% × 0.635 = **+1.27%** (high SL penalised)
- avg=+2%, SL=10% → score = 2% × 0.950 = **+1.90%** ← preferred
- avg=+1.5%, SL=10% → score = 1.5% × 0.950 = **+1.43%**

Losing strategies (avg ≤ 0) score as-is and are excluded by `MIN_AVG_RETURN`.

### Two-phase design

1. **Search pass** — fast stride=10, 700 days. Explores many combinations quickly.
2. **Validation pass** — full precision (stride=2, `BACKTEST_WINDOW_DAYS`). What gets saved.

### SL distance as hyperparameter

`sl_pct` is added to every setup's grid. Default grid: `[1.0%, 2.0%, 3.0%, 5.0%]` of entry price.  
Three previously-untuned setups (`TWO_PERIOD_ROC`, `WHIPLASH`, `THREE_DAY_GAP_REVERSAL`) now have `sl_pct` as their only tunable parameter.

### CLI usage

```bash
# Grid search (exhaustive)
python hyperparameter_search.py                           # full grid, all CPUs
python hyperparameter_search.py --quick                   # reduced grid (~3-4 min)
python hyperparameter_search.py --setup HOLY_GRAIL        # single setup only
python hyperparameter_search.py --no-validate             # skip validation (estimates only)

# Random search (wider ranges, finds local maxima)
python hyperparameter_search.py --random                  # 100 samples, top 3 peaks
python hyperparameter_search.py --random --samples 200 --peaks 5
python hyperparameter_search.py --random --setup RSI_EXTREME
```

### Grid search output

```
Setup                          E.Score  V.Score  V.Avg    SL%  Day       n   Combos  Best params
HOLY_GRAIL                     +1.55%   +1.80%  +2.10%   14%   d3     412      144  adx_period=14  ema_period=20  adx_threshold=30  sl_pct=2.0
THE_ANTI                       +0.81%   +0.90%  +1.05%   26%   d2     388       64  k_period=7  d_period=10  sl_pct=3.0
MOMENTUM_PINBALL               -0.52%   -0.95%  -0.95%   73%   d4     210       48  ...
```

`E.Score` / `V.Score` = combined score (fast estimate / validated).  
`V.Avg` = validated avg return per trade.  
`SL%` = stoploss hit rate.

### Recommended workflow

```bash
python hyperparameter_search.py              # 1. Full grid scan (includes sl_pct)
python hyperparameter_search.py --random --samples 200 --peaks 5   # 2. Random peaks
python backtester.py                         # 3. Confirm avg returns with saved params
```

---

## News Pipeline (7 AM IST)

Runs before the technical pipeline. Fetches internet news, uses a local Ollama LLM to identify the best fundamental/sector/event-driven picks, and sends a WhatsApp message.

### Sources (40+)
- **18 RSS feeds**: Economic Times, MoneyControl, LiveMint, Business Standard, FE, CNBCTV18, Business Line, Zee Biz
- **22 Google News RSS queries**: sectors (pharma, IT, banking, auto…), corporate actions (bonus, buyback, M&A), capital flows
- **NSE event calendar**: upcoming corporate events (14-day lookahead)

### LLM (Ollama — free, local)
- Model: `gemma4:12b-it-qat` (~5.2 GB loaded VRAM — QAT keeps a 12B model smaller in memory than the previous `qwen3:8b` at ~5.9 GB)
- Pull: `ollama pull gemma4:12b-it-qat`
- Auto-selection (`ollama_client._score_model`) prefers gemma > qwen > llama > mistral, scored by family + parameter size, with a bonus for QAT variants (lower VRAM at near-fp quality) — so a better-scoring model pulled later is picked up automatically without code changes
- **Why gemma4 over qwen3** (head-to-head on the live Pass-1/Pass-2 prompts, 2026-06-08): gemma4 produced cleaner structured output (5/5 picks parsed vs qwen3's 4/5 — qwen3 emitted the unparseable symbol `L&T` while gemma4 correctly normalized it to `LT`), more grounded reasoning (every pick traced to a specific headline vs qwen3 including one generic/weak "sector tailwind" pick), and *lower* VRAM despite having 50% more parameters. Trade-off: gemma4 is ~2.5–3x slower per call (~55s vs ~24s for Pass 1, ~64s vs ~18s for Pass 2). Accepted because this is a once-daily scheduled batch job — pick quality matters more than the extra ~10–15 min total runtime.
- **Pass 1** (`think=False`): identify NSE 100 symbols from news digest — structured `PICK N: SYMBOL | ...` format. Thinking is disabled for structured-output tasks; this was discovered to be load-bearing on `qwen3:8b`, which returned an empty `response` (all output routed to the internal `thinking` field) when thinking was on — keeping it off is the safe default across models
- **Pass 2** (`think=True`): generate 3-sentence investment thesis per stock — free-form narrative; extended chain-of-thought enabled for better reasoning quality
- **Scout Passes** (`think=False` / `think=True`): three separate searches beyond mainstream news (see below)
- **Auto-start**: if `ollama serve` is not running, the pipeline launches it and waits up to 30s for readiness
- **VRAM release**: model unloaded (`keep_alive=0`) after all passes complete

### Scout Passes (web_scout.py)

After the main news picks, three additional Ollama passes run — each with its own fetch sources, prompts, and WhatsApp message. All share the same fetch → Pass 1 (`think=False`, structured picks) → Pass 2 (`think=True`, free-form thesis) machinery via a `ScoutConfig` in [news_analyzer/web_scout.py](news_analyzer/web_scout.py), so adding a new lens means adding one config, not duplicating the pipeline.

| Lens | Label | Tag | Sources | Prompt focus | Universe |
|---|---|---|---|---|---|
| **Hidden Gems** | `SCOUT PICKS — Hidden Gems` | `HIDDEN GEM` | Reddit (`r/IndiaInvestments`, `r/IndianStockMarket` weekly top + new, via RSS) + 10 Google News queries | Contrarian — stocks NOT in mainstream headlines: promoter accumulation, turnarounds, sector rotation, deep value | NSE_100 only (`restrict_to_universe=True`) |
| **Small-Cap Growth** | `SMALL-CAP PICKS — Growth Potential` | `SMALLCAP GROWTH` | 8 Google News queries (revenue/margin growth, multibagger coverage, breakout volume, capacity expansion) | Growth — genuine small caps (~₹2,000–20,000cr mcap), explicitly excludes Nifty-100 large caps (RELIANCE, TCS, HDFCBANK, INFY…); LLM names any real NSE-listed small cap from its own knowledge, not just the reference list | **Open** — any genuinely NSE-listed small cap (`restrict_to_universe=False`) |
| **Smart Money** | `SMART MONEY PICKS — Investor & Broker Signals` | `SMART MONEY` | 8 Google News queries naming famous investors (Vijay Kedia, Radhakishan Damani/RK Damani, Ashish Kacholia, Dolly Khanna, Mukul Agrawal…) and broking houses (Motilal Oswal, Jefferies, ICICI Securities, Nomura…) | Conviction signals — fresh stake increases, BUY/Outperform ratings + target upgrades, FII/DII bulk & block deals; prompt requires the LLM to *name* the specific investor/broker | NSE_100 only (`restrict_to_universe=True`) |

**Output:** each lens sends its own WhatsApp message (3 in total, after the main news picks message), capped at 3 picks each, fully enriched with price + technical indicators (see below). All news-analyzer messages (main picks + all 3 scouts) broadcast to every number in `WHATSAPP_PHONES`.

> **Analysis text is no longer chopped mid-sentence.** `formatter.ANALYSIS_LIMIT` was raised from 220 → 750 chars — the old limit cut Pass-2's 3-sentence theses (often ~500-650 chars) off partway through, producing fragments like *"Some reasons. Some investors did this thing,..."*. 750 comfortably fits a full thesis (the prompts cap each sentence at ~28-30 words ≈ 550-650 chars total) while `format_messages()`/`format_scout_messages()` still bucket picks across multiple WhatsApp parts to respect the 3,800-char `MAX_CHARS` send limit, and pathologically long model output is still safely bounded with an ellipsis.

> **Small-Cap Growth is intentionally NOT limited to the Nifty 100** — `ScoutConfig.restrict_to_universe=False` lets its Pass-1 prompt ask the LLM to name any genuinely NSE-listed small cap from its own knowledge (real small caps mostly live outside the Nifty 100). `_parse_picks` accepts these "off-list" tickers directly — sanity-checked against a ticker-shape regex and requiring a company name — and assembles the full symbol as `TICKER.NS`, trusting the LLM's naming rather than fuzzy-matching against the NSE_100 reference. Because these symbols have no OHLCV history in the local DB, technicals for them come from an **on-demand fetch** instead (see below) — fetched data is used only for that message and is never persisted.

### Avoiding repeat picks in the search itself

Before each scout lens runs, the pipeline looks up the symbols it already surfaced within the `SCOUT_DEDUP_DAYS` window (`get_recently_scouted`) and passes that set into `run_scout(..., exclude_symbols=...)`. Two things then happen:
1. **Pass-1 prompt context** — the symbol list is appended to the prompt as explicit negative context ("already surfaced... DO NOT pick them again — find DIFFERENT names"), steering the model's analysis of the fetched articles toward genuinely new names rather than re-analysing ones it'll just have analysed for nothing.
2. **Backstop filter** — any repeat that slips through anyway is dropped immediately after Pass 1 (before the cost of a Pass-2 `think=True` thesis call), and the pipeline re-applies the same exclusion set once more before saving/sending — so a stock already sent in the last 5 days never reaches a WhatsApp message twice.

### Technical indicators in WhatsApp messages

Every pick — main news picks **and** all three scout lenses — is enriched via `_enrich_technicals()` in [news_analyzer/pipeline.py](news_analyzer/pipeline.py) before formatting:

| Indicator | Shown as | Notes |
|---|---|---|
| CMP + % change | `CMP Rs850.0  1D: +2.1%  5D: +4.8%  20D: +9.3%` | unchanged from before |
| RSI(14) | `RSI(14) 68 (overbought)` / `(oversold)` | Wilder-smoothed; flagged when ≥70 or ≤30 — standalone calc in `pipeline._rsi()` (avoids importing from `Trading Setups/_indicators.py`, whose directory name has a space and isn't import-clean from `news_analyzer/`) |
| Volume ratio | `Vol 2.3x avg (surge)` | today's volume ÷ trailing 20-day average; flagged "(surge)" at ≥2x |
| 20-SMA trend | `Above 20-SMA (+4.2%)` | price vs 20-day simple moving average, with % distance |

**Data source — DB first, on-demand fetch as fallback:** `_enrich_technicals` first tries `get_ohlcv()` against the local OHLCV DB (covers the NSE_100 universe with zero network cost). For symbols with no/insufficient local history — i.e. off-list Small-Cap Growth picks — it falls back to `_fetch_recent_ohlcv()`, which pulls ~60 days directly from yfinance using the same download/clean pattern as `data/collector.py._fetch_ticker` (flatten MultiIndex columns, lowercase, tz-strip, drop NaN rows). **That fetched data is held in memory only for the indicator calculation and is never written to the DB** — per design, the local OHLCV table stays scoped to the tracked NSE_100 universe.

Both lookups — and the whole enrichment — are wrapped in one try/except that simply leaves the technical fields absent on any failure (unknown symbol, delisted ticker, network error, insufficient history). **The WhatsApp message still sends without the technical-indicator lines** — `_price_line`/`_technical_line` in `formatter.py` already omit empty fields gracefully, so a fetch failure never blocks a send.

### WhatsApp message format
```
*AI Stock Picks — 05 Jun 2026*
_5 picks | news + fundamental + sector_

*TATAMOTORS*  [M&A]  CMP Rs850.0  1D: +2.1%  5D: +4.8%
RSI(14) 58  Vol 1.4x avg  Above 20-SMA (+3.1%)
Tata Motors announced ... (Ollama thesis)

*DRREDDY*  [USFDA]  CMP Rs1,240.0  1D: +0.8%  5D: +1.2%
RSI(14) 71 (overbought)  Vol 2.6x avg (surge)  Below 20-SMA (-1.4%)
Dr Reddy's received ...
```

### Send confirmation, retries & multi-recipient broadcast

`send_whatsapp()` in [notifications/whatsapp.py](notifications/whatsapp.py) wraps `pywhatkit.sendwhatmsg_instantly` with one automatic retry (`retries=1` — pywhatkit drives WhatsApp Web through a real browser tab, so a slow page load or focus glitch is the typical transient failure and usually succeeds on the second try). It logs `whatsapp: message sent OK` / `whatsapp: send FAILED after N attempt(s)` so a failed send is never silently swallowed, and only ever returns `True` on a confirmed send.

`send_news_picks_alert()` — the single entry point for **all** news-analyzer messages (main AI picks + Hidden Gems + Small-Cap Growth + Smart Money) — broadcasts every message part to **every** number in `WHATSAPP_PHONES` (.env, comma-separated; falls back to the single `WHATSAPP_PHONE` when unset), sleeping `WAIT_TIME + CLOSE_TIME + 3` seconds between sends, and logs a final `X/Y send(s) FAILED across N recipient(s)` / `all X send(s) confirmed` summary. It returns `True` only if every part reached every recipient.

**This multi-recipient broadcast is scoped to news-analyzer alerts only** — `send_batch_signal_alert()` (technical-pipeline signal alerts, 8 AM IST) and `send_ingestion_failure_alert()` still call `send_whatsapp()` without a `phone=` override, so they continue to go to the single `WHATSAPP_PHONE` recipient.

### Deduplication

Two independent windows, each backed by its own table:

| Pipeline | Window | Config | Table | Key |
|---|---|---|---|---|
| Main news picks | 28 days | `NEWS_DEDUP_DAYS` | `news_recommendations` | `(symbol, rec_date)` |
| Scout lenses (each lens independently) | 5 days | `SCOUT_DEDUP_DAYS` | `scout_recommendations` | `(scout_type, symbol, rec_date)` |

The shorter scout window reflects that scout lenses re-scan a fast-moving feed daily, vs. the slower mainstream-news cycle the 28-day window governs. Scout dedup is scoped **per lens** via `scout_type` — the same symbol can appear in, say, both Hidden Gems and Smart Money on the same day without tripping either lens's dedup.

`get_recently_scouted()` / `get_recently_recommended()` return the deduped symbol sets used both (a) as negative context fed to the LLM before it searches (see above) and (b) as the final filter before saving + sending. `purge_expired()` removes rows from both tables once their `expires_at` (today + the relevant dedup window) has passed.

To force a clean slate (e.g. testing): `python -m news_analyzer.db --clear` wipes both tables — see [CLI Reference](#cli-reference).

---

## Conviction Ranking

Stocks are scored before sending to WhatsApp:

```
buy_w     = sum(weight[setup] for each buy-direction signal on this stock)
sell_w    = sum(weight[setup] for each sell-direction signal)
neutral_w = sum(weight[setup] × 0.5 for each neutral signal)
dominant  = BUY if buy_w >= sell_w else SELL
score     = max(buy_w, sell_w) + neutral_w − min(buy_w, sell_w) × 0.25
```

Only stocks whose conviction-weighted avg return ≥ `MIN_AVG_RETURN` (0.5%) are included.  
Setups with avg return below 0.5% get weight = 0.10 (negligible) even if they fire.

---

## WhatsApp Message Format (Technical)

```
*Signal Alert -- 2026-06-05*
_176 signal(s) | 12 stock(s) >= 0.50% avg return_

----------------------------------
*OFSS*  (BUY | score 2.27 | 3 setup(s) | Avg +1.8%)
_OHLC 04 Jun: O 9875  H 10209  L 9775  C 10098  Chg: +100_

*ADX_GAPPER*  (BUY)  d1: O +1.2%/C +1.5% (n=18)
_Gap against a strong ADX trend — reversal back in trend direction_

*EIGHTY_TWENTY*  (BUY)  Best d2: O +1.8%/C +2.1% (n=45) | d1: O +0.9%/C +1.1%
_Prior day opened at one extreme, closed at the opposite_
```

**Avg-return tag format:**
- `d1: O +1.2%/C +1.5% (n=48)` — day 1 is best; O = open exit, C = close exit avg return
- `Best d2: O +1.8%/C +2.1% (n=45) | d1: O +0.9%/C +1.1%` — day 2 is better; shows day-1 comparison

---

## Implemented Trading Setups

All setups tunable via hyperparameter search including `sl_pct` (stoploss distance as % of entry price).

| Setup | Source | Description | Signal fires when |
|---|---|---|---|
| `RSI_EXTREME` | — | RSI overbought/oversold | RSI(14) > 70 or < 30 |
| `TURTLE_SOUP` | Ch 4 | False 20-day breakout | New 20-day extreme + prior ≥4 sessions ago |
| `TURTLE_SOUP_PLUS_ONE` | Ch 5 | Day-after Turtle Soup | Yesterday was Turtle Soup setup |
| `EIGHTY_TWENTY` | Ch 6 | 80-20 reversal bar | Prior day opened top-20%, closed bottom-20% |
| `MOMENTUM_PINBALL` | Ch 7 | LBR/RSI Day-1 | 3-period RSI of 1-day ROC ≤30 or ≥70 |
| `TWO_PERIOD_ROC` | Ch 8 | 2-period ROC pivot | Close crosses the 2-period ROC pivot |
| `THE_ANTI` | Ch 9 | Stochastic hook | %D trending + %K hooks back in %D's direction |
| `HOLY_GRAIL` | Ch 10 | ADX + EMA pullback | ADX(14) > 30 rising + price touches 20-EMA |
| `ADX_GAPPER` | Ch 11 | Gap + strong trend | ADX(12) > 30 + gap against the trend |
| `WHIPLASH` | Ch 12 | Gap reversal close | Gap beyond prior extreme + close in opposing half |
| `THREE_DAY_GAP_REVERSAL` | Ch 13 | Unfilled gap | Unfilled gap within 3 sessions |
| `ID_NR4` | Ch 19 | Range contraction | Inside Day + Narrowest Range of 4 bars |
| `HV_NR4` | Ch 20 | Volatility contraction | 6-day HV < 50% of 100-day HV + Inside/NR4 |
| `MACD_DIVERGENCE` | — | MACD histogram divergence | MACD diverges from price |
| `BOLLINGER_SQUEEZE` | — | BB squeeze breakout | Bands tighten then expand |
| `EMA_TREND_PULLBACK` | — | EMA trend + pullback | Price pulls back to fast EMA in uptrend |
| `N_DOWN_REVERSAL` | — | N consecutive down days | N bearish closes in uptrending stock |
| `VOLUME_CLIMAX` | — | Volume spike reversal | Abnormal volume + price reversal candle |

---

## Database

**Engine**: SQLite with WAL journal mode  
**Location**: `db/market_data.db`

| Table | Purpose |
|---|---|
| `stocks` | Symbol registry |
| `ohlcv` | Adjusted daily OHLCV; UNIQUE(stock_id, date) |
| `ingestion_runs` | Audit log per daily run |
| `setup_signals` | Per-stock per-setup signal + metadata JSON |
| `adjustment_log` | Retroactive corporate action adjustments |
| `news_recommendations` | AI news picks with 28-day dedup window |
| `scout_recommendations` | Scout-lens picks (Hidden Gems / Small-Cap Growth / Smart Money), keyed by `(scout_type, symbol, rec_date)`, 5-day dedup window |

---

## CLI Reference

### `initialize.py` — Bootstrap (first run or full reset)

```bash
python initialize.py
# Wipes existing market data, downloads BACKTEST_WINDOW_DAYS of OHLCV history
# for all NSE 100 stocks, then runs the backtester.
# No arguments. Takes ~15-25 min on first run.
```

---

### `backtester.py` — Avg-return backtester

```bash
python backtester.py                   # full run, all CPU cores (stride=2)
python backtester.py --quick           # faster scan using stride=3
python backtester.py --workers N       # cap parallel workers to N processes
```

| Flag | Default | Effect |
|---|---|---|
| `--quick` | off | stride=3 (skips every 3rd bar); ~2× faster, less precise |
| `--workers N` | CPU count | limit ProcessPoolExecutor to N workers |

Writes results to `db/strategy_weights.json` with `long` and `short` sub-dicts per setup.

---

### `hyperparameter_search.py` — Signal parameter + SL tuning

```bash
# Grid search (exhaustive)
python hyperparameter_search.py                              # full grid, all CPUs
python hyperparameter_search.py --quick                      # reduced grid (~3-4 min)
python hyperparameter_search.py --setup HOLY_GRAIL           # single setup only
python hyperparameter_search.py --no-validate                # skip validation pass (estimates only)
python hyperparameter_search.py --force-rerun                # re-tune all setups, ignore existing params
python hyperparameter_search.py --workers N                  # cap to N worker processes

# Random search (wider ranges, finds multiple local maxima)
python hyperparameter_search.py --random                     # 100 samples/setup, top 3 peaks
python hyperparameter_search.py --random --samples 200       # 200 random samples per setup
python hyperparameter_search.py --random --peaks 5           # keep top 5 local maxima
python hyperparameter_search.py --random --setup RSI_EXTREME # random search, single setup
python hyperparameter_search.py --random --samples 200 --peaks 5 --workers 8
```

| Flag | Default | Effect |
|---|---|---|
| `--quick` | off | Reduced grid; stride=10, 700-day window for search pass |
| `--random` | off | Random search instead of exhaustive grid |
| `--samples N` | 100 | Number of random samples per setup (with `--random`) |
| `--peaks N` | 3 | Top N local maxima to keep per setup (with `--random`) |
| `--setup NAME` | all setups | Run for a single setup name (e.g. `HOLY_GRAIL`) |
| `--no-validate` | off | Skip full-precision validation pass; saves estimated params |
| `--force-rerun` | off | Re-tune setups that already have saved params |
| `--workers N` | CPU count | Cap ProcessPoolExecutor to N workers |

Writes results to `db/optimal_params.json`.

---

### `pipeline.py` — Technical signal pipeline (manual run)

```bash
python pipeline.py                                  # run for all NSE 100 stocks
python pipeline.py --symbols RELIANCE.NS TCS.NS     # run for specific symbols only
```

| Flag | Default | Effect |
|---|---|---|
| `--symbols SYM ...` | all active stocks | Space-separated yfinance symbols to process |

---

### `news_analyzer/pipeline.py` — News + LLM pipeline (manual run)

```bash
python -m news_analyzer.pipeline
# Fetches news, runs Ollama two-pass analysis, sends WhatsApp.
# No arguments. Uses config: NEWS_TOP_N, NEWS_DEDUP_DAYS, OLLAMA_HOST, OLLAMA_MODEL.
# Unloads Ollama model from VRAM after inference completes.
```

---

### `news_analyzer/db.py` — News-analyzer DB maintenance

```bash
python -m news_analyzer.db --clear
# Wipes ALL rows from news_recommendations + scout_recommendations.
# Resets the 28-day main-news dedup window (NEWS_DEDUP_DAYS) AND the 5-day
# scout dedup window (SCOUT_DEDUP_DAYS) to empty — the next pipeline run may
# re-send picks that were already sent before. Does NOT touch ohlcv/stocks/
# setup_signals — only the news analyzer's own two tables.
#
# Run with no flags to print this command's description without deleting anything.
```

---

### `scheduler.py` — In-process scheduler (dev/Linux)

```bash
python scheduler.py
# Runs APScheduler: news pipeline at NEWS_SCHEDULE_HOUR (default 7 AM IST),
# technical pipeline at SCHEDULE_HOUR (default 8 AM IST).
# No arguments. Use setup_windows_task.py on Windows instead.
```

---

### `setup_windows_task.py` — Windows Task Scheduler integration

Registers two headless weekday tasks that wake the laptop and WhatsApp the
results (times come from `.env`):

| Task | Time | Runs | Log |
|---|---|---|---|
| `SignalInfomer\NewsPipeline`  | 07:00 | `python -m news_analyzer.pipeline` | `logs/news_task_output.log` |
| `SignalInfomer\DailyPipeline` | 08:00 | `python pipeline.py`               | `logs/task_output.log` |

```bash
python setup_windows_task.py           # register BOTH tasks (+ enable wake timers)
python setup_windows_task.py --status  # show status of both
python setup_windows_task.py --remove  # remove both
```

| Flag | Effect |
|---|---|
| _(none)_ | Register news (7 AM) + technical (8 AM) tasks in Task Scheduler |
| `--status` | Print current status of both tasks without making changes |
| `--remove` | Delete both registered tasks from Task Scheduler |

Each task runs through `cmd.exe` (so `>> log 2>&1` redirection works) with
`WakeToRun=true` + `StartWhenAvailable=true` (fires even from Modern Standby /
locked-screen, catches up on next wake) and `RestartOnFailure` (3× / 5 min).

**Managing the tasks directly with `schtasks`** (PowerShell or Command Prompt;
use the full `SignalInfomer\...` path, quoted):

```powershell
# Run now — on demand, ignores the 07:00/08:00 schedule (fires the real
# pipeline and sends real WhatsApp messages). Good for a live end-to-end test.
schtasks /Run /TN "SignalInfomer\DailyPipeline"
schtasks /Run /TN "SignalInfomer\NewsPipeline"

# Status / last result (Last Result: 0 = success)
schtasks /Query /TN "SignalInfomer\DailyPipeline" /V /FO LIST
schtasks /Query /TN "SignalInfomer\NewsPipeline"  /V /FO LIST

# Stop a running task
schtasks /End /TN "SignalInfomer\NewsPipeline"

# Disable / enable without deleting
schtasks /Change /TN "SignalInfomer\DailyPipeline" /DISABLE
schtasks /Change /TN "SignalInfomer\DailyPipeline" /ENABLE
```

---

### `tests/`

```bash
python tests/verify_setups.py         # setup signal logic (synthetic OHLCV, both directions)
python tests/simulate_backtester.py   # avg-return + direction-split model (269 assertions)
```

---

### Database / utilities

```bash
# Check DB integrity
python -c "from data.db import init_db, integrity_check; init_db(); print(integrity_check())"

# Restore latest backup
python -c "from utils.backup import restore_latest_backup; restore_latest_backup()"
```

---

### Recommended workflows

```bash
# ── First-time setup ─────────────────────────────────────────────────────────
pip install -r requirements.txt
# edit .env  — set WHATSAPP_PHONE, WHATSAPP_PHONES, MIN_AVG_RETURN, OLLAMA_*
python initialize.py                # ~15-25 min

# ── Tune parameters (after initialize) ───────────────────────────────────────
python hyperparameter_search.py                              # full grid scan
python hyperparameter_search.py --random --samples 200 --peaks 5  # find local maxima
python backtester.py                                         # confirm avg returns

# ── Daily run (automated via Task Scheduler) ──────────────────────────────────
python setup_windows_task.py        # register once; runs automatically every weekday

# ── Manual one-off runs ───────────────────────────────────────────────────────
python -m news_analyzer.pipeline    # news picks now
python pipeline.py                  # technical signals now
python pipeline.py --symbols INFY.NS WIPRO.NS   # specific stocks only
```
