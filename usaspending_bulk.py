"""USAspending bulk-download client.

Replaces the per-company-query architecture in usaspending.py with a single
bulk download per scan. The bulk endpoint is the same one that powers the
"Custom Award Data Download" page at usaspending.gov.

Flow per scan:
    1.  POST a filter to /api/v2/bulk_download/awards/ describing the window
        and award types we care about.
    2.  Server responds with a status_url and a future file_url.
    3.  Poll the status_url every 10s until it reports "finished".
    4.  Stream-download the ZIP (could be hundreds of MB) to disk.
    5.  Unzip and iterate the contained CSVs row-by-row, yielding normalized
        transaction dicts that match the shape monitor.py already expects.

Tradeoffs vs the per-company-query design:
    +  ~5-10 HTTP calls per scan instead of 860 -> no CloudFront rate limits
    +  Every company in the watchlist is matched against every transaction,
       so no company can be "missed" by the rate limiter
    +  Includes contracts AND grants/assistance in one download
    +  Scales linearly with watchlist size at zero extra request cost
    -  One scan failure = no signals that hour (vs partial coverage before)
    -  Local CSV file is large (5-500MB) -> need streaming, not load-into-memory
    -  ZIP generation has variable latency (30s-5min)

Public functions used by monitor.py:
    fetch_recent_contracts(days_back, min_amount, ...)  - drop-in replacement
                                                          for the function of
                                                          the same name in
                                                          usaspending.py.
"""
from __future__ import annotations

import csv
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone, timedelta
from typing import Iterator, Dict, Any, List, Optional

import requests

from config import CONFIG


_BULK_URL    = f"{CONFIG.usaspending_base_url}/bulk_download/awards/"
_STATUS_BASE = f"{CONFIG.usaspending_base_url}/download/status"

# Prime award type codes. A/B/C/D = contracts, IDV_* = indefinite delivery
# vehicles, 02/03/04/05 = grants/cooperative agreements. We exclude loans
# (07/08) and "other" (10/11/06/09) -- they're rare for our public-company
# watchlist and bloat the download.
_PRIME_AWARD_TYPES = [
    "A", "B", "C", "D",
    "IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C",
    "IDV_C", "IDV_D", "IDV_E",
    "02", "03", "04", "05",
]

# A small shared session: ~5 requests per full scan (1 submit + N polls +
# 1 download). Keeping a Session keeps the TLS connection warm across them.
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": "gov-contract-monitor/bulk-download",
            "Accept": "application/json",
        })
    return _session


# ----------------------------------------------------------------------------
# Step 1: submit the bulk-download request
# ----------------------------------------------------------------------------

def request_bulk_download(
    start_date: str, end_date: str, min_amount: Optional[float] = None
) -> Dict[str, Any]:
    """POST a bulk-download request. Returns the API's response dict, which
    contains 'status_url', 'file_name', and 'url' (the eventual ZIP URL).

    start_date / end_date in 'YYYY-MM-DD' form.
    min_amount filters out awards smaller than this (server-side); pass None
    to fetch everything and filter client-side.
    """
    filters: Dict[str, Any] = {
        "prime_award_types": _PRIME_AWARD_TYPES,
        "date_type": "action_date",
        "date_range": {"start_date": start_date, "end_date": end_date},
    }
    if min_amount and min_amount > 0:
        # award_amounts filter expects a list of {lower_bound, upper_bound}
        # ranges; we want everything >= min_amount, no upper bound.
        filters["award_amounts"] = [{"lower_bound": float(min_amount)}]

    payload = {
        "filters": filters,
        "file_format": "csv",
    }

    print(f"  [bulk] submitting download request: {start_date} -> {end_date}, "
          f"min ${min_amount:,.0f}" if min_amount else
          f"  [bulk] submitting download request: {start_date} -> {end_date}")

    r = _get_session().post(
        _BULK_URL, json=payload, timeout=CONFIG.request_timeout_seconds
    )
    if r.status_code >= 400:
        print(f"  [bulk] submit returned HTTP {r.status_code}: {r.text[:500]}")
        r.raise_for_status()

    data = r.json()
    file_name   = data.get("file_name", "(unknown)")
    status_url  = data.get("status_url", "")
    download_url = data.get("url", "")
    print(f"  [bulk] accepted: {file_name}")
    return {
        "file_name": file_name,
        "status_url": status_url,
        "download_url": download_url,
        "raw": data,
    }


# ----------------------------------------------------------------------------
# Step 2: poll until the ZIP is ready
# ----------------------------------------------------------------------------

def wait_for_download(
    file_name: str,
    max_wait_seconds: Optional[int] = None,
    poll_interval_seconds: int = 10,
) -> Dict[str, Any]:
    """Poll /download/status until the request reports 'finished' or 'failed'.

    Returns the final status dict (with 'file_url' field on success).
    Raises RuntimeError on failure or timeout.
    """
    if max_wait_seconds is None:
        max_wait_seconds = CONFIG.bulk_download_max_wait_seconds

    started = time.monotonic()
    last_logged_status = None  # only log when the status string actually changes
    while True:
        elapsed = time.monotonic() - started
        if elapsed > max_wait_seconds:
            raise RuntimeError(
                f"bulk download did not finish within {max_wait_seconds}s "
                f"(last status: {last_logged_status!r})"
            )

        try:
            r = _get_session().get(
                _STATUS_BASE,
                params={"file_name": file_name},
                timeout=CONFIG.request_timeout_seconds,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [bulk] status poll exception (will retry): {e}")
            time.sleep(poll_interval_seconds)
            continue

        status = (data.get("status") or "").lower()
        if status != last_logged_status:
            print(f"  [bulk] status -> '{status}' at {elapsed:.0f}s")
            last_logged_status = status

        # USAspending's documented success status is "finished".
        # "ready" is the INITIAL state (request accepted, not yet generated)
        # so it must NOT be treated as success -- we keep polling.
        if status == "finished":
            return data
        if status in ("failed", "error", "cancelled"):
            raise RuntimeError(
                f"bulk download failed: {data.get('message','(no message)')}"
            )
        time.sleep(poll_interval_seconds)


# ----------------------------------------------------------------------------
# Step 3: stream-download the ZIP and extract CSVs
# ----------------------------------------------------------------------------

def download_and_extract(
    file_url: str, work_dir: Optional[str] = None
) -> List[str]:
    """Download the ZIP to disk (streamed, not loaded into memory), extract
    its CSVs, and return the absolute paths of the extracted CSV files.

    work_dir gets created if missing and cleaned of stale files first.
    """
    if work_dir is None:
        work_dir = CONFIG.bulk_download_work_dir
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir, exist_ok=True)

    zip_path = os.path.join(work_dir, "bulk.zip")
    print(f"  [bulk] downloading ZIP to {zip_path} ...")
    with _get_session().get(file_url, stream=True, timeout=CONFIG.request_timeout_seconds * 4) as r:
        r.raise_for_status()
        total = 0
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    print(f"  [bulk] downloaded {total / (1<<20):.1f} MB")

    csv_paths: List[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".csv"):
                continue
            zf.extract(member, work_dir)
            csv_paths.append(os.path.join(work_dir, member))
    print(f"  [bulk] extracted {len(csv_paths)} CSV file(s)")
    return csv_paths


# ----------------------------------------------------------------------------
# Step 4: stream-parse CSV rows into normalized award dicts
# ----------------------------------------------------------------------------

# The bulk CSV uses long, snake_case column names that are stable across
# downloads. We map them to the keys monitor.py / state.py already use.
# When the header doesn't contain one of these we fall back to None.
_COLUMN_ALIASES: Dict[str, List[str]] = {
    # Award identity
    "Award ID":           ["award_id_piid", "award_id_fain", "award_id_uri"],
    "generated_internal_id": ["generated_internal_id", "generated_pragmatic_obligations_unique_key"],
    # Recipient
    "Recipient Name":     ["recipient_name", "awardee_or_recipient_legal_entity_name"],
    "recipient_uei":      ["recipient_uei", "awardee_or_recipient_uei"],
    # Award amounts (we want the modification's value, not the cumulative)
    "amount":             ["federal_action_obligation",
                            "current_total_value_of_award",
                            "total_dollars_obligated"],
    "base_and_all_options_value": ["current_total_value_of_award",
                                   "potential_total_value_of_award"],
    # Dates
    "action_date":        ["action_date"],
    "Last Modified Date": ["last_modified_date", "action_date"],
    # Agency
    "Awarding Agency":    ["awarding_agency_name"],
    "Awarding Sub Agency": ["awarding_sub_agency_name"],
    # Description / type
    "Description":        ["transaction_description", "prime_award_base_transaction_description",
                            "award_description"],
    "award_type":         ["award_type", "award_type_code"],
    # Transaction-level identifier (so we dedup per modification)
    "transaction_id":     ["modification_number", "transaction_unique_id",
                            "award_modification_amendment_number"],
}


def _lookup(row: Dict[str, str], key: str) -> str:
    """Look up our canonical key in the row using any of the known aliases."""
    for col in _COLUMN_ALIASES.get(key, []):
        if col in row and row[col] not in (None, "", "NULL"):
            return row[col]
    return ""


def iter_csv_transactions(csv_path: str) -> Iterator[Dict[str, Any]]:
    """Stream a bulk-download CSV, yielding one normalized transaction per row.

    The shape of the yielded dict mirrors what fetch_recent_contracts() in
    usaspending.py used to yield, so monitor.py logic can consume it directly.
    """
    with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            recipient = _lookup(raw, "Recipient Name")
            if not recipient:
                continue
            # Parse the modification amount; rows with no dollar value are noise.
            amt_str = _lookup(raw, "amount").strip()
            try:
                amount = float(amt_str.replace(",", "")) if amt_str else 0.0
            except ValueError:
                amount = 0.0

            base_str = _lookup(raw, "base_and_all_options_value").strip()
            try:
                base_total = float(base_str.replace(",", "")) if base_str else 0.0
            except ValueError:
                base_total = 0.0

            yield {
                "Award ID":         _lookup(raw, "Award ID"),
                "generated_internal_id": _lookup(raw, "generated_internal_id"),
                "Recipient Name":   recipient,
                "recipient_uei":    _lookup(raw, "recipient_uei"),
                "amount":           amount,
                "base_and_all_options_value": base_total,
                "action_date":      _lookup(raw, "action_date"),
                "Last Modified Date": _lookup(raw, "Last Modified Date"),
                "Awarding Agency":  _lookup(raw, "Awarding Agency"),
                "Awarding Sub Agency": _lookup(raw, "Awarding Sub Agency"),
                "Description":      _lookup(raw, "Description"),
                "award_type":       _lookup(raw, "award_type"),
                "transaction_id":   _lookup(raw, "transaction_id"),
                "source":           "usaspending bulk download",
            }


# ----------------------------------------------------------------------------
# Step 5: top-level drop-in replacement
# ----------------------------------------------------------------------------

def fetch_recent_contracts_bulk(
    days_back: int,
    min_amount: float = 0.0,
    watchlist_match_terms: Optional[List[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """One-shot: submit, wait, download, parse, filter by watchlist terms.

    Yields transaction dicts matching the watchlist. Designed to be called
    ONCE per scan (not once per company).

    watchlist_match_terms: a flat list of uppercased match-term strings. Any
    transaction whose recipient name contains any of these substrings is
    yielded. If empty/None, every transaction is yielded.
    """
    end_date   = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days_back)
    sub = request_bulk_download(
        start_date.isoformat(), end_date.isoformat(), min_amount or None
    )
    status = wait_for_download(sub["file_name"])

    # The polling response should contain the canonical download URL once the
    # ZIP is actually built. Fall back to the submit response's URL fields
    # only as last resort -- those tend to be the *future* URL, not yet valid.
    file_url = status.get("file_url") or status.get("url") or ""
    url_source = "status.file_url" if status.get("file_url") else (
        "status.url" if status.get("url") else "")
    if not file_url:
        file_url = sub.get("download_url") or sub["raw"].get("url", "")
        url_source = "submit.url (fallback; may be premature)"
    if not file_url:
        raise RuntimeError("download finished but no file_url in status response")
    print(f"  [bulk] download URL from {url_source}: {file_url[:90]}...")

    csv_paths = download_and_extract(file_url)
    if not csv_paths:
        print("  [bulk] no CSV files in extracted ZIP -- empty result window?")
        return

    # Build a fast-match structure. Match terms come in already uppercased
    # (see all_search_terms() in monitor.py).
    terms = [t.upper() for t in (watchlist_match_terms or []) if t]
    rows_scanned = 0
    rows_matched = 0

    for path in csv_paths:
        for tx in iter_csv_transactions(path):
            rows_scanned += 1
            if rows_scanned % 25_000 == 0:
                print(f"  [bulk] {rows_scanned:,} rows scanned, "
                      f"{rows_matched} watchlist matches so far")
            if not terms:
                rows_matched += 1
                yield tx
                continue
            up = tx["Recipient Name"].upper()
            if any(t in up for t in terms):
                rows_matched += 1
                yield tx

    print(f"  [bulk] FINAL: {rows_scanned:,} rows scanned, "
          f"{rows_matched} watchlist matches")


# ----------------------------------------------------------------------------
# Probe mode: tiny test run from the command line
# ----------------------------------------------------------------------------

def _probe() -> int:
    """Run a 1-day bulk download with a high min_amount, report timing+size,
    show a sample row. Helps validate that the bulk path works end-to-end
    without committing the full pipeline yet.

    Usage: python usaspending_bulk.py --probe
    """
    print("== bulk download probe ==")
    started = time.monotonic()
    end_date   = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=1)

    sub = request_bulk_download(
        start_date.isoformat(), end_date.isoformat(), min_amount=10_000_000
    )
    print(f"  submit -> {time.monotonic() - started:.1f}s")

    status = wait_for_download(sub["file_name"], max_wait_seconds=300)
    print(f"  generated -> {time.monotonic() - started:.1f}s")

    file_url = status.get("file_url") or sub.get("download_url") or sub["raw"].get("url","")
    csv_paths = download_and_extract(file_url)
    print(f"  downloaded -> {time.monotonic() - started:.1f}s")

    if not csv_paths:
        print("  NO CSVs extracted")
        return 1

    # Show file size + first 2 sample rows
    for path in csv_paths:
        size_mb = os.path.getsize(path) / (1 << 20)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            header = f.readline()
            print(f"\n  {os.path.basename(path)}  ({size_mb:.1f} MB)")
            print(f"  headers ({header.count(',')+1} columns):")
            cols = [c.strip() for c in header.split(",")][:25]
            for c in cols:
                print(f"    {c}")
            if len([_ for _ in iter_csv_transactions(path)]) > 0:
                count = 0
                for tx in iter_csv_transactions(path):
                    count += 1
                    if count <= 2:
                        print(f"\n  sample row {count}:")
                        for k, v in tx.items():
                            if v:
                                print(f"    {k}: {str(v)[:80]}")
                print(f"\n  total transactions in this CSV: {count}")
    return 0


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        sys.exit(_probe())
    print("Usage: python usaspending_bulk.py --probe")
    sys.exit(2)
