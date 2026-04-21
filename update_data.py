#!/usr/bin/env python3
"""
Daily update script for Strategy (MSTR) capital structure dashboard.

Fetches cash & cash equivalents from SEC EDGAR XBRL API for all available
quarterly filings, pairs each period with the obligation level in effect at
that time, and writes data/snapshots.json consumed by the dashboard.

Run:
    python update_data.py

Required: Python 3.8+ (stdlib only — no pip installs needed)
"""
from __future__ import annotations
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
CIK            = "0001050446"
# companyconcept returns only the one concept we need — smaller payload,
# less aggressive on SEC rate-limits than the full companyfacts endpoint.
XBRL_URL       = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{CIK}/us-gaap/CashAndCashEquivalentsAtCarryingValue.json"
XBRL_URL_ALT   = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{CIK}/us-gaap/CashCashEquivalentsAndShortTermInvestments.json"
DATA_DIR       = Path(__file__).parent / "data"
SNAPSHOTS_FILE = DATA_DIR / "snapshots.json"
# SEC EDGAR requires: "Company-or-App-Name contact@email.com"
HEADERS = {
    "User-Agent":      "StrategyDashboard fersobrini@gmail.com",
    "Accept":          "application/json",
    "Accept-Encoding": "identity",
}

# ── Current obligations ($M) ───────────────────────────────────────────────────
# Keep in sync with DEBT_PAYMENTS and PREF_PAYMENTS in index.html
DEBT_PAYMENTS = [
    {"name": "0.625% Conv 2028",           "annual": 6.31,   "monthly": 0.53},
    {"name": "0% Conv notes (3 tranches)", "annual": 0,      "monthly": 0},
    {"name": "0.625% Conv Mar 2030",       "annual": 5.00,   "monthly": 0.42},
    {"name": "0.875% Conv 2031",           "annual": 5.29,   "monthly": 0.44},
    {"name": "2.25% Conv 2032",            "annual": 18.00,  "monthly": 1.50},
]
PREF_PAYMENTS = [
    {"name": "STRK 8% fixed",       "annual": 111.9,  "monthly": 9.32},
    {"name": "STRF 10% fixed",      "annual": 128.4,  "monthly": 10.70},
    {"name": "STRD 10% fixed",      "annual": 140.2,  "monthly": 11.69},
    {"name": "STRC 11.5% variable", "annual": 730.6,  "monthly": 60.88},
]

# ── Historical obligation schedule ────────────────────────────────────────────
# Maps a period-end date to (monthly_debt, monthly_pref, note).
# Entries are sorted ascending; the last entry whose start_date <= filing_date wins.
# Update this when new instruments are issued or rates change significantly.
OBLIGATION_SCHEDULE = [
    # (start_date,      monthly_debt, monthly_pref, note)
    ("2024-01-01",      2.89,  0.00,  "Convertible notes only (pre-preferred)"),
    ("2025-02-28",      2.89,  9.32,  "+STRK issued Feb 2025 — 8% fixed quarterly"),
    ("2025-04-30",      2.89, 20.02,  "+STRF issued Apr 2025 — 10% fixed quarterly"),
    ("2025-06-30",      2.89, 31.71,  "+STRD issued Jun 2025 — 10% fixed quarterly"),
    ("2025-08-31",      2.89, 48.00,  "+STRC launched Jul/Aug 2025 (~$2B notional, 9.6–10%)"),
    ("2025-10-31",      2.89, 76.50,  "STRC ATM expansion (~$5B notional, rate stepping up)"),
    ("2026-01-31",      2.89, 92.59,  "STRC ~$6.4B notional at 11.5% (current)"),
]


def get_obligations_at(filing_date: str) -> tuple:
    """Return (monthly_debt, monthly_pref, note) for the given ISO date string."""
    md, mp, note = OBLIGATION_SCHEDULE[0][1], OBLIGATION_SCHEDULE[0][2], OBLIGATION_SCHEDULE[0][3]
    for start, debt, pref, n in OBLIGATION_SCHEDULE:
        if filing_date >= start:
            md, mp, note = debt, pref, n
    return md, mp, note


def current_obligations() -> dict:
    """Compute total obligations from the current DEBT_PAYMENTS / PREF_PAYMENTS."""
    m_debt = sum(p["monthly"] for p in DEBT_PAYMENTS)
    m_pref = sum(p["monthly"] for p in PREF_PAYMENTS)
    a_debt = sum(p["annual"]  for p in DEBT_PAYMENTS)
    a_pref = sum(p["annual"]  for p in PREF_PAYMENTS)
    return {
        "monthlyDebt":  round(m_debt, 2),
        "monthlyPref":  round(m_pref, 2),
        "totalMonthly": round(m_debt + m_pref, 2),
        "annualDebt":   round(a_debt, 1),
        "annualPref":   round(a_pref, 1),
        "totalAnnual":  round(a_debt + a_pref, 1),
    }


# ── EDGAR helpers ──────────────────────────────────────────────────────────────

def fetch_url(url: str, retries: int = 3) -> dict | None:
    """Fetch JSON from a SEC EDGAR URL with retries and proper headers."""
    for attempt in range(1, retries + 1):
        time.sleep(0.5)   # SEC asks for ≤10 req/sec; be polite
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            print(f"  attempt {attempt}/{retries}: HTTP {exc.code} — {url}", file=sys.stderr)
            if exc.code == 403 and attempt < retries:
                time.sleep(2 ** attempt)   # exponential back-off: 2s, 4s
                continue
            return None
        except Exception as exc:
            print(f"  attempt {attempt}/{retries}: {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(2)
                continue
            return None
    return None


def fetch_cash_entries() -> tuple[str | None, list]:
    """
    Fetch quarterly cash entries from EDGAR companyconcept endpoint.
    The companyconcept response has shape: {units: {USD: [...]}}
    """
    for url, concept in (
        (XBRL_URL,     "CashAndCashEquivalentsAtCarryingValue"),
        (XBRL_URL_ALT, "CashCashEquivalentsAndShortTermInvestments"),
    ):
        print(f"  Fetching: {url}")
        data = fetch_url(url)
        if data is None:
            continue
        entries = data.get("units", {}).get("USD", [])
        quarterly = [
            e for e in entries
            if e.get("form") in ("10-Q", "10-K")
            and e.get("fp") in ("Q1", "Q2", "Q3", "FY")
        ]
        if not quarterly:
            quarterly = [e for e in entries if e.get("form") in ("10-Q", "10-K")]
        if quarterly:
            print(f"  Concept: {concept} — {len(quarterly)} quarterly values")
            return concept, quarterly
        print(f"  No usable entries for {concept}, trying fallback…")
    print("ERROR: all EDGAR endpoints failed", file=sys.stderr)
    return None, []


# ── strategy.com live BTC holdings ────────────────────────────────────────────

STRATEGY_URL = "https://www.strategy.com/"

def fetch_strategy_btc() -> dict | None:
    """Scrape strategy.com homepage for live BTC holdings & cash/debt snapshot."""
    try:
        req = urllib.request.Request(STRATEGY_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"  strategy.com fetch failed: {exc}", file=sys.stderr)
        return None
    m = re.search(r'"btcTrackerData":(\[\{.*?\}\])', html)
    if not m:
        print("  strategy.com: btcTrackerData not found", file=sys.stderr)
        return None
    try:
        rec = json.loads(m.group(1))[0]
    except Exception as exc:
        print(f"  strategy.com parse failed: {exc}", file=sys.stderr)
        return None
    return {
        "asOfDate":         rec.get("as_of_date"),
        "btcHoldings":      rec.get("btc_holdings"),
        "cashUsd":          rec.get("cash"),
        "debtUsd":          rec.get("debt"),
        "annualDividendsM": rec.get("annual_dividends"),
        "basicShares":      rec.get("basic_shares_outstanding"),
        "btcYieldYtd":      rec.get("btc_yield_ytd"),
    }


# ── Snapshot builder ───────────────────────────────────────────────────────────

def build_snapshots(cash_entries: list) -> list:
    """
    Deduplicate by period-end date (keep latest accession), pair with
    obligation schedule, and return sorted snapshot list.
    """
    # Deduplicate: for the same end-date keep the entry with the highest accn
    by_end: dict[str, dict] = {}
    for e in cash_entries:
        key = e["end"]
        if key not in by_end or e["accn"] > by_end[key]["accn"]:
            by_end[key] = e

    snapshots = []
    for end_date in sorted(by_end):
        entry   = by_end[end_date]
        cash_m  = round(entry["val"] / 1e6, 1)
        md, mp, note = get_obligations_at(end_date)
        total_m = round(md + mp, 2)
        total_a = round(total_m * 12, 1)
        coverage = round(cash_m / total_m, 1) if total_m > 0 else None
        snapshots.append({
            "filingDate":   end_date,
            "form":         entry["form"],
            "cashM":        cash_m,
            "monthlyDebt":  round(md, 2),
            "monthlyPref":  round(mp, 2),
            "totalMonthly": total_m,
            "totalAnnual":  total_a,
            "coverageMonths": coverage,
            "note":         note,
        })
    return snapshots


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    today = date.today().isoformat()
    print(f"Strategy dashboard updater — {today}")
    print(f"Fetching EDGAR XBRL: {XBRL_URL}")

    concept, cash_entries = fetch_cash_entries()
    if not cash_entries:
        sys.exit(1)

    snapshots = build_snapshots(cash_entries)
    cur = current_obligations()

    print("\nFetching live BTC holdings from strategy.com …")
    live_btc = fetch_strategy_btc()
    if live_btc:
        print(f"  BTC holdings: {live_btc['btcHoldings']:,} (as of {live_btc['asOfDate']})")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "lastUpdated":   today,
        "concept":       concept,
        "currentObligations": cur,
        "liveBtc":       live_btc,
        "snapshots":     snapshots,
    }
    with open(SNAPSHOTS_FILE, "w") as fh:
        json.dump(payload, fh, indent=2)

    print(f"\nWrote {len(snapshots)} snapshots → {SNAPSHOTS_FILE}")

    if snapshots:
        latest = snapshots[-1]
        prev   = snapshots[-2] if len(snapshots) >= 2 else None
        print(f"\n{'='*50}")
        print(f"  Latest period   : {latest['filingDate']} ({latest['form']})")
        print(f"  Cash balance    : ${latest['cashM']}M")
        print(f"  Monthly total   : ${latest['totalMonthly']}M")
        print(f"  Annual total    : ${latest['totalAnnual']}M")
        print(f"  Coverage ratio  : {latest['coverageMonths']} months")
        if prev:
            d_cash    = round(latest["cashM"]        - prev["cashM"],        1)
            d_monthly = round(latest["totalMonthly"] - prev["totalMonthly"], 2)
            print(f"\n  vs prior period ({prev['filingDate']}):")
            print(f"  Cash Δ         : ${d_cash:+.1f}M")
            print(f"  Monthly obs Δ  : ${d_monthly:+.2f}M/mo")
        print('='*50)


if __name__ == "__main__":
    main()
