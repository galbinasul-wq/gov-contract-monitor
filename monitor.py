"""Main entrypoint.

Usage:
    python monitor.py --once         # one scan, then exit
    python monitor.py                # continuous loop
    python monitor.py --dry-run      # alerts to console+log only (no webhooks)
    python monitor.py --test-api     # quick API connectivity / matching check
    python monitor.py --backfill 30  # one-shot scan over the last N days, no
                                       state writes (good for tuning)
"""
from __future__ import annotations

import argparse
import re
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from config import CONFIG
from watchlist import WATCHLIST
from usaspending import fetch_recent_contracts, fetch_transactions_for_award, chunked
from market_data import get_market_cap, cap_band
from state import is_seen, mark_seen, record_alert, record_scan
import alerts as alerts_mod
from alerts import classify_tier


# ---------- Watchlist matching ----------

def _normalize(s: str) -> str:
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_NORMALIZED_WATCHLIST = [
    {
        **c,
        "match_terms_norm": [_normalize(t) for t in c.get("match_terms", [])],
        "exclude_terms_norm": [_normalize(t) for t in c.get("exclude_terms", [])],
    }
    for c in WATCHLIST
]


def match_watchlist(recipient_name: str) -> Optional[Dict[str, Any]]:
    norm = _normalize(recipient_name)
    if not norm:
        return None
    for c in _NORMALIZED_WATCHLIST:
        if any(t in norm for t in c["match_terms_norm"]):
            if any(x in norm for x in c["exclude_terms_norm"]):
                continue
            return c
    return None


def all_search_terms() -> List[str]:
    """Flatten every match_term across the watchlist for server-side filtering."""
    seen = set()
    out: List[str] = []
    for c in WATCHLIST:
        for t in c.get("match_terms", []):
            key = t.upper()
            if key not in seen:
                seen.add(key)
                out.append(t)
    return out


# ---------- Per-award evaluation ----------

def evaluate_award(award: Dict[str, Any], days_back: int) -> List[Dict[str, Any]]:
    """Return a list of alert dicts (one per material recent transaction).

    A "material transaction" is a single modification on this award whose
    own dollar amount (NOT the cumulative award total) is >= the configured
    threshold percentage of the company's market cap.

    Does NOT touch state -- caller decides whether to mark_seen.
    """
    recipient = award.get("Recipient Name") or ""
    company = match_watchlist(recipient)
    if not company:
        return []

    cumulative_amount = float(award.get("Award Amount") or 0)
    if cumulative_amount < CONFIG.min_contract_value:
        # Recent transactions can't exceed the cumulative total, so no point
        # paying for the transactions API call.
        return []

    market_cap = get_market_cap(company["ticker"])
    if not market_cap:
        return []  # try again later

    band = cap_band(market_cap)
    allowed_bands = set(CONFIG.target_cap_bands)
    if company.get("include_when_large"):
        allowed_bands.update({"large", "mega"})
    if band not in allowed_bands:
        return []

    award_id = award.get("generated_internal_id") or award.get("Award ID") or ""
    if not award_id:
        return []

    # Fetch individual transactions on this award within the lookback window
    recent_txs = fetch_transactions_for_award(award_id, days_back=days_back)
    if not recent_txs:
        return []

    threshold_dollars = CONFIG.material_ratio_threshold * market_cap
    alerts_out: List[Dict[str, Any]] = []

    for tx in recent_txs:
        tx_amount = float(tx.get("federal_action_obligation") or 0)
        # Only positive new obligations (de-obligations are negative; ignore them)
        if tx_amount < CONFIG.min_contract_value:
            continue
        if tx_amount < threshold_dollars:
            continue

        ratio = tx_amount / market_cap
        tier_name, tier_emoji = classify_tier(ratio)
        alerts_out.append({
            "ticker": company["ticker"],
            "company": company["name"],
            "recipient_name": recipient,
            "modification_amount": tx_amount,           # actual new $ in this mod
            "total_award_amount": cumulative_amount,    # cumulative for context
            "market_cap": market_cap,
            "cap_band": band,
            "ratio_pct": ratio * 100,
            "tier": tier_name,
            "tier_emoji": tier_emoji,
            "agency": award.get("Awarding Agency") or "",
            "sub_agency": award.get("Awarding Sub Agency") or "",
            "description": tx.get("description") or award.get("Description") or "",
            "action_date": tx.get("action_date") or "",
            "modification_number": tx.get("modification_number") or "",
            "action_type": tx.get("action_type_description") or tx.get("action_type") or "",
            "start_date": award.get("Start Date") or "",
            "end_date": award.get("End Date") or "",
            "award_id": award_id,
            "transaction_id": tx.get("id") or "",
        })

    return alerts_out


# ---------- Scan modes ----------

def _iter_matching_awards(days_back: int):
    """Stream awards that the API thinks match our watchlist names.

    USAspending's `recipient_search_text` filter accepts only ONE term
    per request (per their official docs), so we issue one request per
    search term and yield results as we go.

    We dedupe by award_id across the entire scan so the same contract
    isn't yielded twice when multiple search terms (e.g. PARSONS GOVERNMENT
    and PARSONS CORPORATION) return overlapping results.
    """
    seen_in_scan = set()
    terms = list(all_search_terms())
    # Shuffle the iteration order each run. CloudFront's burst budget gets
    # exhausted partway through any long scan, after which a chunk of the
    # later requests hit transient failures. Without shuffling, the SAME
    # companies in the middle of the watchlist would fail every single run
    # (their term position always lands in the throttled window). Random
    # ordering spreads that failure across the whole watchlist so every
    # company gets cleanly scanned some hours of the day, and the 7-day
    # lookback ensures we never lose a material signal entirely.
    import random
    random.shuffle(terms)
    total = len(terms)
    print(f"  [progress] scanning {total} search terms (randomized order)...")
    for i, term in enumerate(terms, start=1):
        if i % 25 == 0 or i == total:
            print(f"  [progress] {i}/{total} terms scanned, {len(seen_in_scan)} candidate awards so far")
        try:
            for award in fetch_recent_contracts(
                days_back=days_back,
                min_amount=CONFIG.min_contract_value,
                recipient_search_terms=[term],
            ):
                aid = award.get("generated_internal_id") or award.get("Award ID")
                if not aid or aid in seen_in_scan:
                    continue
                seen_in_scan.add(aid)
                yield award
        except Exception as e:
            print(f"  [warn] search '{term}' failed: {e}")


def _dedup_key(alert: Dict[str, Any]) -> str:
    """Each material modification gets its own dedup key so we re-alert
    only when a new transaction appears."""
    tx = alert.get("transaction_id") or "no-tx"
    return f"tx::{alert['award_id']}::{tx}"


def run_once() -> None:
    started = datetime.now(timezone.utc).isoformat()
    print(f"[{started}] Polling USAspending (last {CONFIG.lookback_days} days)...")
    scanned = 0
    fired = 0
    for award in _iter_matching_awards(CONFIG.lookback_days):
        scanned += 1
        for alert in evaluate_award(award, days_back=CONFIG.lookback_days):
            key = _dedup_key(alert)
            if is_seen(key):
                continue
            alerts_mod.dispatch(alert)
            record_alert(alert)
            mark_seen(key)
            fired += 1
    record_scan(scanned, fired)
    print(f"  -> scanned {scanned} candidate awards, fired {fired} alerts")


def test_api() -> None:
    print("Testing USAspending API connectivity and watchlist matching...")
    print(f"Watchlist size: {len(WATCHLIST)} companies")
    print(f"Search terms:   {len(all_search_terms())} unique terms\n")
    found = 0
    for award in _iter_matching_awards(days_back=30):
        found += 1
        recipient = award.get("Recipient Name") or ""
        company = match_watchlist(recipient)
        amt = float(award.get("Award Amount") or 0)
        marker = f"[{company['ticker']}]" if company else "[no match]"
        print(f"  {marker:10s} ${amt:>14,.0f}  {recipient[:60]}")
        if found >= 25:
            print("  ... (truncated to 25 sample awards)")
            break
    if found == 0:
        print("  No candidate awards returned. Either the API is unreachable")
        print("  or no watchlist company received a contract in the last 30 days.")


def backfill(days_back: int, dry_run: bool = True) -> None:
    """Scan a custom lookback window and report what *would* alert.

    Doesn't write to seen-awards state, so you can iterate on thresholds.
    """
    print(f"Backfilling last {days_back} days (dry_run={dry_run})...")
    scanned = 0
    fired = 0
    seen_in_run = set()
    for award in _iter_matching_awards(days_back):
        scanned += 1
        for alert in evaluate_award(award, days_back=days_back):
            key = _dedup_key(alert)
            if key in seen_in_run:
                continue
            seen_in_run.add(key)
            fired += 1
            if dry_run:
                alerts_mod.send_console(alert)
            else:
                alerts_mod.dispatch(alert)
    print(f"\n  -> scanned {scanned} candidates, would have fired {fired} alerts")


# ---------- Entrypoint ----------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Monitor USAspending for material contracts to small/mid-cap public companies."
    )
    p.add_argument("--once", action="store_true", help="Run one scan and exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Suppress webhook sends; alerts still go to console + log.")
    p.add_argument("--test-api", action="store_true",
                   help="Print a sample of recent watchlist-matching awards and exit.")
    p.add_argument("--backfill", type=int, metavar="DAYS",
                   help="Scan last N days and print would-be alerts (no state writes).")
    args = p.parse_args()

    if args.dry_run:
        alerts_mod.send_discord = lambda a: None  # type: ignore
        alerts_mod.send_slack = lambda a: None    # type: ignore

    if args.test_api:
        test_api()
        return

    if args.backfill is not None:
        backfill(args.backfill, dry_run=True)
        return

    if args.once:
        run_once()
        return

    print(f"Starting continuous loop (interval = {CONFIG.poll_interval_minutes} min)")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[monitor] scan failed: {e}")
        time.sleep(CONFIG.poll_interval_minutes * 60)


if __name__ == "__main__":
    main()
