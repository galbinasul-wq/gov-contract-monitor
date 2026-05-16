"""SEC 8-K monitor entrypoint.

Polls EDGAR for new 8-K filings from the AI-infra watchlist, filters to the
items that signal contract wins, parses for hyperscaler names and dollar
amounts, and emails alerts.

Usage:
    python sec_monitor.py --once
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

from sec_watchlist import WATCHLIST
from sec_edgar import (
    fetch_ticker_to_cik_map,
    fetch_recent_filings,
    fetch_filing_text,
    parse_filing_for_signals,
    filing_url,
)
from state import (
    is_seen_sec_filing,
    mark_seen_sec_filing,
    record_sec_alert,
    record_sec_scan,
)
import alerts as alerts_mod


# 8-K items worth inspecting:
#   1.01 = Entry into a Material Definitive Agreement   (the gold one)
#   7.01 = Regulation FD Disclosure                     (sometimes contracts)
#   8.01 = Other Events                                 (sometimes wins)
ITEMS_OF_INTEREST = {"1.01", "7.01", "8.01"}
LOOKBACK_DAYS = 7


def _parse_items(items_str: str) -> set:
    return {i.strip() for i in (items_str or "").split(",") if i.strip()}


def evaluate_filing(
    ticker: str, company_name: str, cik: str, filing: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    items = _parse_items(filing.get("items", ""))
    if not (items & ITEMS_OF_INTEREST):
        return None

    is_material_agreement = "1.01" in items

    try:
        text = fetch_filing_text(
            cik, filing["accession_number"], filing["primary_document"]
        )
    except Exception as e:
        print(f"  [warn] text fetch failed {ticker} {filing.get('accession_number')}: {e}")
        return None

    signals = parse_filing_for_signals(text)

    # 1.01 always alerts (it's specifically a material agreement).
    # 7.01 / 8.01 only alert if a hyperscaler is named (otherwise noisy).
    should_alert = is_material_agreement or bool(signals["hyperscalers"])
    if not should_alert:
        return None

    return {
        "ticker": ticker,
        "company": company_name,
        "filing_date": filing.get("filing_date", ""),
        "items": ",".join(sorted(items)),
        "is_material_agreement": is_material_agreement,
        "hyperscalers": signals["hyperscalers"],
        "max_amount": signals["max_amount"],
        "dollar_amounts": signals["dollar_amounts"][:5],
        "snippet": signals["snippet"],
        "accession_number": filing["accession_number"],
        "filing_url": filing_url(
            cik, filing["accession_number"], filing["primary_document"]
        ),
        "cik": cik,
    }


def dispatch_sec_alert(alert: Dict[str, Any]) -> None:
    alerts_mod.send_sec_console(alert)
    alerts_mod.append_sec_log(alert)
    alerts_mod.send_sec_email(alert)


def run_once() -> None:
    started = datetime.now(timezone.utc).isoformat()
    print(f"[{started}] Polling SEC EDGAR (last {LOOKBACK_DAYS} days)...")

    try:
        ticker_to_cik = fetch_ticker_to_cik_map()
    except Exception as e:
        print(f"  [error] could not fetch SEC ticker map: {e}")
        return

    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    scanned = 0
    fired = 0

    for entry in WATCHLIST:
        ticker = entry["ticker"]
        cik = entry.get("cik") or ticker_to_cik.get(ticker.upper())
        if not cik:
            print(f"  [warn] no CIK for {ticker}")
            continue

        try:
            filings = fetch_recent_filings(cik)
        except Exception as e:
            print(f"  [warn] filings fetch failed for {ticker}: {e}")
            continue

        recent_8ks = [
            f for f in filings
            if f.get("form") == "8-K" and f.get("filing_date", "") >= cutoff
        ]

        for filing in recent_8ks:
            accession = filing.get("accession_number")
            if not accession or is_seen_sec_filing(accession):
                continue
            scanned += 1

            alert = evaluate_filing(ticker, entry["name"], cik, filing)
            if alert:
                dispatch_sec_alert(alert)
                record_sec_alert(alert)
                fired += 1

            mark_seen_sec_filing(accession)

    record_sec_scan(scanned, fired)
    print(f"  -> scanned {scanned} new 8-K filings, fired {fired} alerts")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Monitor SEC EDGAR 8-K filings for AI-infra contract signals."
    )
    p.add_argument("--once", action="store_true", help="Run one scan and exit.")
    p.parse_args()
    run_once()


if __name__ == "__main__":
    main()
