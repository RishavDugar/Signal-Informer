"""
Plain-English vocabulary — one place, used by the dashboard, WhatsApp reminders and
alerts, so legal jargon never reaches the reader.
"""

from __future__ import annotations

import re

# Case stage → what it means for you
STAGE = {
    "filed": "Just filed",
    "pending": "Waiting for a hearing",
    "hearing": "Being heard",
    "arguments_concluded": "Arguments over, verdict awaited",
    "reserved": "Hearing over, verdict pending",
    "listed_for_pronouncement": "Verdict date fixed",
    "decided": "Verdict given",
    "disposed": "Closed",
    "unknown": "Status unclear",
}

# Document types the user attaches
DOC = {
    "cause_list": "Court schedule",
    "case_status": "Case page",
    "daily_order": "Hearing notes",
    "judgment": "Verdict",
    "notice": "Company notice",
    "other": "Document",
}

# Abbreviations that show up in case titles / court names → short meaning
GLOSSARY = {
    "SC": "Supreme Court",
    "HC": "High Court",
    "NCLT": "company-law court",
    "NCLAT": "appeals court for company-law and competition cases",
    "SAT": "appeals court for SEBI cases",
    "SEBI": "stock-market regulator",
    "CCI": "competition (anti-cartel) regulator",
    "TDSAT": "telecom disputes court",
    "APTEL": "electricity appeals court",
    "ITAT": "income-tax appeals tribunal",
    "CESTAT": "customs & GST appeals tribunal",
    "CIT(A)": "income-tax appeal officer",
    "NGT": "environment court",
    "DRT": "debt-recovery tribunal",
    "SLP": "request to the Supreme Court to hear an appeal",
    "PIL": "public-interest case",
    "CIRP": "insolvency process",
    "IBC": "insolvency law",
    "AGR": "telecom licence-fee dues",
    "PSC": "oil & gas production contract",
    "SIAC": "Singapore arbitration centre",
    "LCIA": "London arbitration court",
    "USFDA": "US drug regulator",
    "OAI": "serious US-FDA inspection finding",
    "ED": "Enforcement Directorate (financial crime agency)",
    "CBI": "Central Bureau of Investigation",
    "SFIO": "corporate-fraud investigator",
    "DG": "CCI's investigation wing",
    "AT1": "high-risk bank bonds",
    "CET1": "bank's core capital",
    "GST": "goods & services tax",
    "AY": "tax year",
    "LPA": "appeal within the High Court",
    "OTSC": "one-time spectrum charge",
    "SUC": "spectrum usage charge",
    "EPFO": "provident-fund authority",
    "MMRDA": "Mumbai development authority",
}

_ABBR_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(k) for k in GLOSSARY), key=len, reverse=True))
                      + r")(?![A-Za-z])")


def decode(*texts: str) -> list[dict]:
    """Abbreviations found in the given texts → [{abbr, meaning}] (first-seen order)."""
    seen: dict[str, str] = {}
    for t in texts:
        for m in _ABBR_RE.finditer(t or ""):
            seen.setdefault(m.group(1), GLOSSARY[m.group(1)])
    return [{"abbr": k, "meaning": v} for k, v in seen.items()]


def court(name: str | None) -> str:
    """'NCLAT' → 'NCLAT (appeals court for company-law and competition cases)'."""
    n = (name or "").strip()
    for k, v in GLOSSARY.items():
        if re.fullmatch(rf"{re.escape(k)}\b.*", n) and len(n) <= len(k) + 12:
            return f"{n} ({v})"
    return n
