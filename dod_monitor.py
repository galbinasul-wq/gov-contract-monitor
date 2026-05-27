"""DoD daily contracts monitor entrypoint.

Polls the Department of War's daily contracts RSS feed, parses each
day's roundup article, matches contracts against the gov watchlist by
contractor name, and alerts using the same market-cap tier thresholds
as the USAspending bot.

Why this exists: USAspending data for DoD has a documented 90-day
publication delay. The defense.gov / war.gov daily contracts page
publishes same-day at 5pm ET. For DoD-heavy watchlist names this
collapses 90+ days of lag to same-day signal.

Usage:  python dod_monitor.py --once
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from config import CONFIG
from watchlist import WATCHLIST
from dod_news import (
    fetch_recent_daily_articles,
    fetch_article_contracts,
    match_contractor_to_watchlist,
)
from market_data import get_market_cap  # reused from gov bot
from state import (
    is_seen_dod_contract,
    mark_seen_dod_contract,
    record_dod_alert,
    record_dod_scan,
)
import alerts as alerts_mod


# Max number of daily articles to consider per run. The cron runs daily,
# so 7 covers a week of buffer in case of missed runs or weekend gaps.
_MAX_ARTICLES_PER_RUN = 7

# Absolute-dollar floor that overrides the % rule. A $500M+ DoD contract
# is newsworthy even when it's a small % of a mega-cap prime's value.
_ABSOLUTE_BIG_TICKET_USD = 500_000_000.0


def _classify_tier(ratio_pct: float, amount: float) -> Optional[str]:
    """Return the tier label, or None if below the alert threshold.

    Same thresholds as the gov bot, plus an absolute big-ticket floor:
      >= 4.5%        -> 'Big Impact'   (or >= $500M absolute)
      >= 3.0%        -> 'Important'
      >= 1.0%        -> 'Regular'
    """
    big_pct = CONFIG.tier_big_impact_threshold * 100
    imp_pct = CONFIG.tier_important_threshold * 100
    mat_pct = CONFIG.material_ratio_threshold * 100

    if amount >= _ABSOLUTE_BIG_TICKET_USD or ratio_pct >= big_pct:
        return "Big Impact"
    if ratio_pct >= imp_pct:
        return "Important"
    if ratio_pct >= mat_pct:
        return "Regular"
    return None


def _dedup_key(c: Dict[str, Any]) -> str:
    """Stable per-contract key. Prefer the canonical contract id when
    present; otherwise fall back to (article_url, contractor, amount)
    which is also stable across re-runs of the same scan.
    """
    if c.get("contract_id"):
        return f"dod::{c['contract_id']}"
    return (
        f"dod::{c.get('article_url','')}::"
        f"{c.get('contractor','')[:60]}::{int(c.get('amount',0))}"
    )


def evaluate_contract(
    contract: Dict[str, Any], watchlist_entry: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Apply market-cap-tier rule. Return an alert dict or None."""
    ticker = watchlist_entry["ticker"]
    company = watchlist_entry["name"]
    amount = float(contract.get("amount") or 0)
    if amount <= 0:
        return None

    market_cap = get_market_cap(ticker)
    if not market_cap or market_cap <= 0:
        # Without a market cap we can't apply the % rule, but a very
        # large DoD contract is still alert-worthy on its own.
        if amount >= _ABSOLUTE_BIG_TICKET_USD:
            tier = "Big Impact"
            ratio_pct = 0.0
        else:
            return None
    else:
        ratio_pct = (amount / market_cap) * 100.0
        tier = _classify_tier(ratio_pct, amount)
        if tier is None and not watchlist_entry.get("include_when_large"):
            return None
        if tier is None:
            # include_when_large companies always get at least Regular
            # tier on a real DoD signal, even when % is tiny.
            tier = "Regular"

    return {
        "ticker": ticker,
        "company": company,
        "amount": amount,
        "market_cap": market_cap or 0,
        "ratio_pct": ratio_pct,
        "tier": tier,
        "service": contract.get("service", "UNKNOWN"),
        "contractor_as_announced": contract.get("contractor", ""),
        "contract_id": contract.get("contract_id", ""),
        "announce_date": contract.get("announce_date", ""),
        "award_date": contract.get("award_date", ""),
        "description": contract.get("description", ""),
        "article_url": contract.get("article_url", ""),
        "source": "war.gov daily contracts",
    }


def run_once() -> None:
    started = datetime.now(timezone.utc).isoformat()
    print(f"[{started}] Polling Department of War daily contracts...")

    try:
        articles = fetch_recent_daily_articles(max_days=_MAX_ARTICLES_PER_RUN)
    except Exception as e:
        print(f"  [error] could not fetch DoD RSS feed: {e}")
        record_dod_scan(0, 0, 0)
        return

    print(f"  [progress] {len(articles)} daily-roundup article(s) in feed")

    contracts_seen = 0
    matched = 0
    fired = 0

    for art in articles:
        url = art["url"]
        try:
            entries = fetch_article_contracts(art)
        except Exception as e:
            print(f"  [warn] failed to parse {url}: {e}")
            continue
        for contract in entries:
            contracts_seen += 1
            key = _dedup_key(contract)
            if is_seen_dod_contract(key):
                continue
            mark_seen_dod_contract(key)

            wl = match_contractor_to_watchlist(
                contract["contractor"], WATCHLIST
            )
            if not wl:
                continue
            matched += 1
            alert = evaluate_contract(contract, wl)
            if not alert:
                continue
            alerts_mod.dispatch_dod(alert)
            record_dod_alert(alert, signature=key)
            fired += 1

    record_dod_scan(
        articles=len(articles), contracts=contracts_seen, alerts=fired
    )
    print(
        f"  -> {len(articles)} articles, {contracts_seen} contracts seen, "
        f"{matched} matched watchlist, {fired} alerts fired"
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Monitor Department of War daily contract announcements."
    )
    p.add_argument("--once", action="store_true", help="Run one scan and exit.")
    p.parse_args()
    run_once()


if __name__ == "__main__":
    main()
