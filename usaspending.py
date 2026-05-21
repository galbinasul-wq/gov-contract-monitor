"""Thin USAspending.gov API client.

We hit /search/spending_by_award/ filtered to a recipient list. The query
covers BOTH procurement contracts (codes A-D) and federal financial
assistance -- grants, cooperative agreements, and direct payments (codes
02-11). This is important because programs like the CHIPS and Science Act
fund companies via cooperative agreements (code 05), not procurement
contracts, so a contracts-only filter would miss them entirely.

Honest caveat: USAspending publishes contract obligations relatively
quickly (days to ~2 weeks). Assistance/grant data is sometimes slower
to publish (weeks to months, varies by agency). Don't expect a CHIPS
Act award to appear here the day it's announced; it appears when the
agency reports the obligation. Letters of intent never appear -- only
actual obligations do.
"""
from __future__ import annotations

import requests
from datetime import datetime, timedelta, timezone
from typing import Iterator, Dict, Any, List, Optional

from config import CONFIG


# Procurement contract codes:
#   A = BPA Call, B = Purchase Order, C = Delivery Order, D = Definitive Contract
CONTRACT_CODES = ["A", "B", "C", "D"]
# Federal financial assistance codes:
#   02 = Block Grant            03 = Formula Grant
#   04 = Project Grant          05 = Cooperative Agreement
#   06 = Direct Payment for Specified Use
#   10 = Direct Payment with Unrestricted Use
#   11 = Other Financial Assistance
ASSISTANCE_CODES = ["02", "03", "04", "05", "06", "10", "11"]

AWARD_TYPE_CODES = CONTRACT_CODES + ASSISTANCE_CODES

# Fields we ask the API to return for each award.
# Note: 'Action Date' is NOT a valid field on this endpoint -- it's only
# usable as a date_type for time_period filtering. We use 'Base Obligation
# Date' as the closest equivalent (when the base transaction was signed).
_FIELDS = [
    "Award ID",
    "generated_internal_id",
    "Recipient Name",
    "recipient_id",
    "Award Amount",
    "Description",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Base Obligation Date",
    "Start Date",
    "End Date",
    "Last Modified Date",
]


def _today_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_recent_contracts(
    days_back: int = 7,
    min_amount: float = 0,
    recipient_search_terms: Optional[List[str]] = None,
    max_pages: int = 25,
) -> Iterator[Dict[str, Any]]:
    """Yield contract awards with action_date in the past `days_back` days.

    If `recipient_search_terms` is given, the API only returns awards whose
    recipient name contains at least one of those substrings (server-side OR).
    """
    end = _today_utc().date()
    start = end - timedelta(days=days_back)

    filters: Dict[str, Any] = {
        "award_type_codes": AWARD_TYPE_CODES,
        "time_period": [{
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "date_type": "action_date",
        }],
    }
    if recipient_search_terms:
        filters["recipient_search_text"] = recipient_search_terms
    if min_amount and min_amount > 0:
        filters["award_amounts"] = [{"lower_bound": float(min_amount)}]

    payload = {
        "filters": filters,
        "fields": _FIELDS,
        "sort": "Last Modified Date",
        "order": "desc",
        "limit": 100,
        "page": 1,
    }

    url = f"{CONFIG.usaspending_base_url}/search/spending_by_award/"

    while payload["page"] <= max_pages:
        r = requests.post(url, json=payload, timeout=CONFIG.request_timeout_seconds)
        r.raise_for_status()
        data = r.json()

        for award in data.get("results", []):
            yield award

        if not data.get("page_metadata", {}).get("hasNext"):
            return
        payload["page"] += 1


def chunked(items: List[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_transactions_for_award(
    award_id: str,
    days_back: Optional[int] = None,
    max_pages: int = 20,
) -> List[Dict[str, Any]]:
    """Fetch individual transactions for a single award, sorted newest first.

    Each transaction has its own dollar amount (`federal_action_obligation`),
    which is the actual amount added/removed in that specific modification --
    NOT the cumulative award total. This is what we want for materiality.

    If `days_back` is set, only returns transactions whose action_date falls
    within that recent window.
    """
    if not award_id:
        return []

    url = f"{CONFIG.usaspending_base_url}/transactions/"
    payload: Dict[str, Any] = {
        "award_id": award_id,
        "sort": "action_date",
        "order": "desc",
        "limit": 100,
        "page": 1,
    }

    transactions: List[Dict[str, Any]] = []
    while payload["page"] <= max_pages:
        try:
            r = requests.post(url, json=payload, timeout=CONFIG.request_timeout_seconds)
            if not r.ok:
                break
            data = r.json()
        except Exception:
            break
        transactions.extend(data.get("results", []))
        if not data.get("page_metadata", {}).get("hasNext"):
            break
        payload["page"] += 1

    if days_back:
        cutoff = (_today_utc().date() - timedelta(days=days_back)).isoformat()
        transactions = [t for t in transactions if (t.get("action_date") or "") >= cutoff]

    return transactions
