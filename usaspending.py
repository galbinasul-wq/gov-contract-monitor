"""Thin USAspending.gov API client.

We hit /search/spending_by_award/ filtered to a recipient list. The query
covers BOTH procurement contracts (codes A-D) and federal financial
assistance -- grants, cooperative agreements, and direct payments (codes
02-11). This is important because programs like the CHIPS and Science Act
fund companies via cooperative agreements (code 05), not procurement
contracts, so a contracts-only filter would miss them entirely.

IMPORTANT: The /search/spending_by_award/ endpoint treats contracts and
assistance as two different searches with DIFFERENT valid field sets per
the API docs:
https://github.com/fedspendingtransparency/usaspending-api/blob/master/
  usaspending_api/api_contracts/contracts/v2/search/spending_by_award.md
Mixing both code groups into a single request returns 500 Server Error.
So we issue two separate queries per scan and merge the results (dedup
on generated_internal_id). If one query fails (e.g. transient assistance
endpoint outage), the other still produces results -- failure isolation
is the whole reason we split rather than mix.

Honest caveat: USAspending publishes contract obligations relatively
quickly (days to ~2 weeks). Assistance/grant data is sometimes slower
to publish (weeks to months, varies by agency). Don't expect a CHIPS
Act award to appear here the day it's announced; it appears when the
agency reports the obligation. Letters of intent never appear -- only
actual obligations do.
"""
from __future__ import annotations

import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Iterator, Dict, Any, List, Optional

from config import CONFIG


# Procurement contract codes (one API group):
#   A = BPA Call, B = Purchase Order, C = Delivery Order, D = Definitive Contract
CONTRACT_CODES = ["A", "B", "C", "D"]
# Grant-type assistance codes (one API group):
#   02 = Block Grant            03 = Formula Grant
#   04 = Project Grant          05 = Cooperative Agreement
# CHIPS Act, NIH, NSF, DARPA, DoE R&D funding all flow through this group.
# The USAspending /search/spending_by_award/ endpoint rejects any request
# whose award_type_codes mix multiple groups (returns 422 with message:
# "'award_type_codes' must only contain types from one group"), so each
# group costs a separate query. Direct payments (06, 10) and "other"
# (09, 11) are deliberately omitted -- they rarely apply to publicly
# traded companies and would double the API load for marginal signal.
GRANT_CODES = ["02", "03", "04", "05"]
# Public alias kept for compatibility with anything that imports it.
ASSISTANCE_CODES = GRANT_CODES
AWARD_TYPE_CODES = CONTRACT_CODES + ASSISTANCE_CODES

# Fields differ by award-type group. The spending_by_award endpoint
# returns 422 if you request a contract-only field on an assistance
# query or vice versa. Keep these lists narrow to what each query needs.

_CONTRACT_FIELDS = [
    "Award ID",
    "generated_internal_id",
    "Recipient Name",
    "recipient_id",
    "Award Amount",
    "Description",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Start Date",
    "End Date",
    "Last Modified Date",
]
_CONTRACT_SORT = "Last Modified Date"

# Conservative -- matches the fields documented as valid for grants.
_GRANT_FIELDS = [
    "Award ID",
    "generated_internal_id",
    "Recipient Name",
    "Award Amount",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Start Date",
    "End Date",
]
_GRANT_SORT = "Award Amount"

# Order of queries per scan. Add more (label, codes, fields, sort) tuples
# here to extend coverage to additional API groups; nothing else changes.
_QUERY_GROUPS = [
    ("contracts", CONTRACT_CODES, _CONTRACT_FIELDS, _CONTRACT_SORT),
    ("grants",    GRANT_CODES,    _GRANT_FIELDS,    _GRANT_SORT),
]


def _today_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- Rate limiting + retry ----------------------------------------------------
# USAspending sits behind AWS CloudFront, which rate-limits aggressively at the
# edge. Empirically, bursting ~500 requests/minute from a single IP triggers
# cascading connection resets and 500 HTML error pages (not JSON) returned by
# the CDN, not the API. Pacing requests to ~5/sec keeps us comfortably under
# the threshold. Module-level state so the limit applies across ALL callers
# (contracts query, grants query, transactions query) in a single run.

_MIN_REQUEST_INTERVAL_S = 0.2          # ~5 requests/second ceiling
_RETRY_BACKOFFS_S       = [1.0, 3.0]   # backoff before retries 1 and 2
_last_request_at        = [0.0]        # mutable singleton; time.monotonic()


def _throttled_post(url: str, payload: dict) -> requests.Response:
    """POST that paces requests across the module to avoid CDN throttling."""
    delta = time.monotonic() - _last_request_at[0]
    if delta < _MIN_REQUEST_INTERVAL_S:
        time.sleep(_MIN_REQUEST_INTERVAL_S - delta)
    try:
        return requests.post(
            url, json=payload, timeout=CONFIG.request_timeout_seconds
        )
    finally:
        _last_request_at[0] = time.monotonic()


def _post_with_retry(url: str, payload: dict,
                     max_retries: int = 2) -> requests.Response:
    """Throttled POST with retry on transient failures.

    Retries on 5xx (CloudFront edge error / upstream flake) and on connection
    errors (RemoteDisconnected, timeouts). Does NOT retry on 4xx -- those
    indicate our payload is wrong and retrying will just waste API quota.
    """
    last_response: Optional[requests.Response] = None
    for attempt in range(max_retries + 1):
        try:
            r = _throttled_post(url, payload)
            last_response = r
            # 2xx: success. 4xx: don't retry (payload is wrong, our fault).
            if r.status_code < 500:
                return r
            # 5xx: retry if we have attempts left, else return the bad response.
            if attempt == max_retries:
                return r
            time.sleep(_RETRY_BACKOFFS_S[attempt])
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            if attempt == max_retries:
                raise
            time.sleep(_RETRY_BACKOFFS_S[attempt])
    # Unreachable: every path either returns or raises. Kept for type clarity.
    return last_response  # type: ignore[return-value]


def _query_one_type(
    award_type_codes: List[str],
    fields: List[str],
    sort_field: str,
    days_back: int,
    min_amount: float,
    recipient_search_terms: Optional[List[str]],
    max_pages: int,
) -> Iterator[Dict[str, Any]]:
    """Issue a single spending_by_award query for one award-type group."""
    end = _today_utc().date()
    start = end - timedelta(days=days_back)

    filters: Dict[str, Any] = {
        "award_type_codes": award_type_codes,
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
        "fields": fields,
        "sort": sort_field,
        "order": "desc",
        "limit": 100,
        "page": 1,
    }

    url = f"{CONFIG.usaspending_base_url}/search/spending_by_award/"

    while payload["page"] <= max_pages:
        r = _post_with_retry(url, payload)
        if not r.ok:
            # Surface the actual API error so we can diagnose 4xx/5xx
            # without redeploying. The API consistently returns a JSON
            # body with the specific complaint (field name, value, etc).
            body = (r.text or "")[:600].replace("\n", " ")
            print(f"  [debug] HTTP {r.status_code} for codes={award_type_codes}, "
                  f"sort={sort_field!r}")
            print(f"  [debug] response body: {body}")
            r.raise_for_status()
        data = r.json()
        for award in data.get("results", []):
            yield award
        if not data.get("page_metadata", {}).get("hasNext"):
            return
        payload["page"] += 1


def fetch_recent_contracts(
    days_back: int = 7,
    min_amount: float = 0,
    recipient_search_terms: Optional[List[str]] = None,
    max_pages: int = 25,
) -> Iterator[Dict[str, Any]]:
    """Yield contract AND grant awards with action_date in the past
    `days_back` days. Issues one API query per group in _QUERY_GROUPS
    (currently: contracts, grants); merges and dedupes results across
    groups by generated_internal_id.

    Failure isolation: if one group's query fails (transient API error,
    rate limit, connection reset), the other groups still run and
    produce results. The next scheduled scan picks up anything missed.

    If `recipient_search_terms` is given, the API only returns awards
    whose recipient name contains at least one of those substrings
    (server-side OR).
    """
    seen: set = set()

    for label, codes, fields, sort_field in _QUERY_GROUPS:
        try:
            for award in _query_one_type(
                award_type_codes=codes,
                fields=fields,
                sort_field=sort_field,
                days_back=days_back,
                min_amount=min_amount,
                recipient_search_terms=recipient_search_terms,
                max_pages=max_pages,
            ):
                key = award.get("generated_internal_id") or award.get("Award ID")
                if not key or key in seen:
                    continue
                seen.add(key)
                yield award
        except requests.HTTPError as e:
            print(f"  [warn] {label} query failed: {e}")
        except Exception as e:
            print(f"  [warn] {label} query exception: {e}")


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
            r = _post_with_retry(url, payload)
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
