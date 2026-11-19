"""
Litigation tracker — pending court / tribunal / regulatory cases on listed Indian
companies, scored for price impact and tracked through their stages.

  sources.py   free data: BSE announcements + filing PDFs, BSE scrip master
               (market cap), NSE F&O universe + lot sizes, NSE F&O bhavcopy
  analyzer.py  LLM (Claude Sonnet by default, web-enabled) — discover cases,
               refresh case status, assess impact; deterministic trade idea
  db.py        lit_cases / lit_events / lit_filings tables (same SQLite DB)
  tracker.py   orchestration, WhatsApp alerts, signal gate, CLI
  case_study.py  Yes Bank AT1 point-in-time replay

Run:  python -m litigation.tracker daily | discover SYM.. | report SYM | casestudy
"""
