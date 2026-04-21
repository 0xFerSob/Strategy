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
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
CIK           = "0001050446"
XBRL_URL      = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
DATA_DIR      = Path(__file__).parent / "data"
SNAPSHOTS_FILE = DATA_DIR / "snapshots.json"
USER_AGENT    = "Strategy Dashboard github.com/0xFerSob"

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

def fetch_xbrl() -> dict | None:
    req = urllib.request.Request(XBRL_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as exc:
        print(f"ERROR: EDGAR fetch failed — {exc}", file=sys.stderr)
        return None


def extract_cash(xbrl_data: dict) -> tuple[str | None, list]:
    """Return (concept_name, list_of_entries) from XBRL facts."""
    facts = xbrl_data.get("facts", {}).get("us-gaap", {})
    for concept in (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
        "Cash",
    ):
        if concept not in facts:
            continue
        entries = facts[concept].get("units", {}).get("USD", [])
        # Prefer period-end (instant) values from quarterly/annual reports
        quarterly = [
            e for e in entries
            if e.get("form") in ("10-Q", "10-K")
            and e.get("fp") in ("Q1", "Q2", "Q3", "FY")
        ]
        if not quarterly:
            quarterly = [e for e in entries if e.get("form") in ("10-Q", "10-K")]
        if quarterly:
            print(f"  Cash concept : {concept}  ({len(quarterly)} quarterly values found)")
            return concept, quarterly
    print("ERROR: no cash concept found in XBRL data", file=sys.stderr)
    return None, []


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

    xbrl = fetch_xbrl()
    if xbrl is None:
        sys.exit(1)

    concept, cash_entries = extract_cash(xbrl)
    if not cash_entries:
        sys.exit(1)

    snapshots = build_snapshots(cash_entries)
    cur = current_obligations()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "lastUpdated":   today,
        "concept":       concept,
        "currentObligations": cur,
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
