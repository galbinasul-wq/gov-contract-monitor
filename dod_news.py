"""Department of War (formerly DoD) daily contracts client.

The Department of War publishes every contract award valued at $7.5M+
each business day at ~5pm ET. This is the FASTEST public source for
DoD contract awards -- USAspending has a documented 90-day delay for
DoD procurement data.

Architecture for the fetch:
  1. Pull the daily-roundup index from war.gov's RSS endpoint (this
     endpoint accepts automated requests freely).
  2. For each article, TRY to fetch directly from war.gov first.
  3. If the direct fetch fails (war.gov's CloudFront WAF actively
     blocks scrapers from cloud-provider IP ranges), FALL BACK to
     the Internet Archive's Wayback Machine, which caches war.gov
     pages and is reachable from anywhere.

Honest trade-off: the Wayback fallback path introduces a 1-3 day lag
(sometimes more, sometimes less, depending on how recently archive.org
crawled the target URL). We check the snapshot timestamp against the
article's RSS pubDate and only accept snapshots taken AFTER the article
was published, so we never parse stale content -- but we may legitimately
have to skip articles for which no fresh-enough snapshot yet exists.

Sources:
  https://www.war.gov/News/Contracts/
  https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945
  https://archive.org/wayback/available?url=...   (lookup)
  https://web.archive.org/web/{TS}id_/{URL}       (raw content fetch)
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator, Dict, Any, List, Optional, Tuple

import requests

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except Exception:
    _HAVE_BS4 = False

from config import CONFIG


# Browser-realistic headers (used for the direct attempt; cheap to keep in
# case war.gov ever stops blocking obvious automation).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Module-level session for connection reuse on both war.gov and archive.org.
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_HEADERS)
    return _session

_RSS_URL = (
    "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx"
    "?ContentType=400&Site=945&max={max}"
)

# Service-branch headers we expect inside each daily-roundup article.
# The page renders these as bold standalone lines.
_SERVICE_HEADERS = {
    "AIR FORCE", "ARMY", "NAVY", "MARINE CORPS", "SPACE FORCE",
    "DEFENSE LOGISTICS AGENCY", "MISSILE DEFENSE AGENCY",
    "DEFENSE INFORMATION SYSTEMS AGENCY",
    "DEFENSE ADVANCED RESEARCH PROJECTS AGENCY",
    "DEFENSE FINANCE AND ACCOUNTING SERVICE",
    "DEFENSE THREAT REDUCTION AGENCY",
    "DEFENSE HEALTH AGENCY",
    "WASHINGTON HEADQUARTERS SERVICES",
    "U.S. SPECIAL OPERATIONS COMMAND",
    "U.S. TRANSPORTATION COMMAND",
    "U.S. CYBER COMMAND",
}

# Dollar amount with optional magnitude word.
_DOLLAR_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s?(billion|million|thousand)?",
    re.IGNORECASE,
)
# Last parenthesized contract-id-looking string in a paragraph.
# Examples: (FA8820-24-D-B001), (N00189-26-D-L003), (W9128Z-26-D-A012)
_CONTRACT_ID_RE = re.compile(r"\(([A-Z][A-Z0-9-]{6,})\)")


def _strip_html(html_text: str) -> str:
    if _HAVE_BS4:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            # Drop nav/footer noise -- the article body is usually inside
            # a main / article / content region but selectors vary, so we
            # just remove obvious chrome.
            for tag in soup(["nav", "footer", "header", "script", "style", "aside"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except Exception:
            pass
    return re.sub(r"<[^>]+>", "\n", html_text)


# ---------- RSS feed ---------------------------------------------------------

def fetch_recent_daily_articles(max_days: int = 20) -> List[Dict[str, Any]]:
    """Return up to `max_days` recent daily contract-roundup articles."""
    url = _RSS_URL.format(max=max_days)
    r = _get_session().get(url, timeout=CONFIG.request_timeout_seconds)
    r.raise_for_status()
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"  [warn] DoD RSS parse failed: {e}")
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()
        if not link:
            continue
        items.append({"title": title, "url": link, "pubdate": pub})
    return items


# ---------- Article parsing --------------------------------------------------

def _parse_dollar(match: re.Match) -> float:
    try:
        val = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    mag = (match.group(2) or "").lower()
    if mag == "billion":
        val *= 1_000_000_000
    elif mag == "million":
        val *= 1_000_000
    elif mag == "thousand":
        val *= 1_000
    return val


def _extract_contractor(paragraph: str) -> str:
    """Best-effort contractor name = text up to the first comma.

    DoD format reliably starts with: "ContractorName, City, State, has been
    awarded ..." or "ContractorName, City, State, is awarded ...". The first
    comma-separated chunk is the legal entity name.
    """
    head = paragraph.split(",", 1)[0].strip()
    # Strip leading bullet/asterisk decorations sometimes left by HTML
    return re.sub(r"^[\s\*\-]+", "", head)[:160]


def _is_contract_paragraph(p: str) -> bool:
    """Heuristic: looks like one of the DoD contract entries."""
    low = p.lower()
    if "awarded" not in low:
        return False
    if not _DOLLAR_RE.search(p):
        return False
    # Skip very short or obvious chrome paragraphs
    return 80 <= len(p) <= 4000


def parse_dod_article(html_text: str, article_url: str) -> List[Dict[str, Any]]:
    """Parse a daily contracts article into individual contract entries."""
    text = _strip_html(html_text)
    contracts: List[Dict[str, Any]] = []
    current_service = "UNKNOWN"

    # Paragraphs come out blank-line-delimited after BeautifulSoup separator.
    # Re-split on blank lines OR on tag-bracket markers, then strip.
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]

    for p in paragraphs:
        upper = p.upper().strip(":* ")
        if upper in _SERVICE_HEADERS:
            current_service = upper
            continue
        if not _is_contract_paragraph(p):
            continue

        dollar_match = _DOLLAR_RE.search(p)
        amount = _parse_dollar(dollar_match) if dollar_match else 0.0
        if amount < 1_000_000:  # safety net; DoD floor is $7.5M
            continue

        contractor = _extract_contractor(p)
        # Pick the LAST contract-id-looking token; tends to be canonical.
        cid_matches = list(_CONTRACT_ID_RE.finditer(p))
        contract_id = cid_matches[-1].group(1) if cid_matches else ""

        contracts.append({
            "service": current_service,
            "contractor": contractor,
            "amount": amount,
            "contract_id": contract_id,
            "description": p[:1200],
            "article_url": article_url,
        })

    return contracts


# ---------- Wayback Machine fallback ----------------------------------------
# When war.gov's WAF blocks our direct fetches (it currently does from
# cloud-provider IPs), we look up the article in the Internet Archive's
# Wayback Machine and fetch the most recent cached copy.

_WAYBACK_AVAIL_URL = "https://archive.org/wayback/available"


def _pubdate_to_wayback_ts(pubdate_str: str) -> Optional[str]:
    """RFC822 pubDate ('Thu, 22 May 2026 21:00:00 GMT') -> 'YYYYMMDDhhmmss'."""
    if not pubdate_str:
        return None
    try:
        dt = parsedate_to_datetime(pubdate_str)
        return dt.strftime("%Y%m%d%H%M%S")
    except Exception:
        return None


def _fetch_wayback_snapshot(
    article_url: str,
    min_timestamp: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Find the most recent Wayback snapshot of `article_url` and return its
    raw HTML content + the snapshot timestamp.

    If `min_timestamp` (Wayback YYYYMMDDhhmmss) is provided, snapshots taken
    BEFORE that time are rejected -- this prevents us from parsing a stale
    capture from before the article was even published.

    Returns (text, timestamp) on success, (None, None) on any failure.
    """
    # Use a plain requests session (NOT _get_session) so the browser-realistic
    # headers used for war.gov don't leak into archive.org calls.
    headers = {"User-Agent": "GovContractMonitor/1.0 (archive.org fallback)"}
    try:
        r = requests.get(
            _WAYBACK_AVAIL_URL,
            params={"url": article_url},
            headers=headers,
            timeout=CONFIG.request_timeout_seconds,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [warn] Wayback availability lookup failed: {e}")
        return None, None

    closest = (data.get("archived_snapshots") or {}).get("closest") or {}
    if not closest.get("available"):
        print(f"  [info] no Wayback snapshot exists for {article_url}")
        return None, None

    snap_ts = str(closest.get("timestamp", ""))
    if min_timestamp and snap_ts and snap_ts < min_timestamp:
        print(
            f"  [info] Wayback snapshot {snap_ts} is older than article pub "
            f"{min_timestamp}; skipping (would parse stale content)"
        )
        return None, None

    # `id_` flag -> identity, returns original HTML as captured, no toolbar.
    raw_url = f"https://web.archive.org/web/{snap_ts}id_/{article_url}"
    try:
        r = requests.get(
            raw_url, headers=headers,
            timeout=CONFIG.request_timeout_seconds,
        )
        if r.status_code != 200:
            print(f"  [warn] Wayback raw fetch returned HTTP {r.status_code}")
            return None, None
        if len(r.text) < 1000:
            print(f"  [warn] Wayback content suspiciously small ({len(r.text)} bytes); skipping")
            return None, None
        return r.text, snap_ts
    except Exception as e:
        print(f"  [warn] Wayback raw fetch failed: {e}")
        return None, None


# ---------- Public fetch (direct + fallback) --------------------------------

def fetch_article_contracts(article: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch and parse one daily-roundup article.

    `article` is the dict returned by fetch_recent_daily_articles() with at
    least 'url' and 'pubdate' keys. Tries war.gov directly, then falls back
    to the Wayback Machine if direct fetch fails.
    """
    article_url = article.get("url", "")
    pubdate = article.get("pubdate", "")
    if not article_url:
        return []

    text: Optional[str] = None
    source = "failed"

    # --- Path 1: direct from war.gov ---
    try:
        r = _get_session().get(
            article_url, timeout=CONFIG.request_timeout_seconds
        )
        if r.status_code == 200 and len(r.text) > 1000:
            text = r.text
            source = "direct"
        else:
            print(
                f"  [info] direct fetch from war.gov returned "
                f"HTTP {r.status_code} for {article_url}; trying Wayback"
            )
    except Exception as e:
        print(f"  [info] direct fetch from war.gov failed ({e}); trying Wayback")

    # --- Path 2: Wayback Machine fallback ---
    if text is None:
        min_ts = _pubdate_to_wayback_ts(pubdate)
        text, snap_ts = _fetch_wayback_snapshot(article_url, min_timestamp=min_ts)
        if text and snap_ts:
            source = f"wayback@{snap_ts}"

    if text is None:
        print(f"  [warn] article fetch failed (all paths) {article_url}")
        return []

    contracts = parse_dod_article(text, article_url)
    if contracts:
        print(f"  [info] {article_url} -> {len(contracts)} contracts via {source}")
        # Stamp the source onto each contract so the alert tells you whether
        # it was same-day (direct) or N days delayed (wayback@timestamp).
        for c in contracts:
            c["fetch_source"] = source
    else:
        print(f"  [info] {article_url} -> 0 contracts parsed (via {source})")
    return contracts


# ---------- Matching against the gov watchlist ------------------------------

def match_contractor_to_watchlist(
    contractor_text: str, watchlist: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Return the watchlist entry whose match_terms appear in the contractor
    text (case-insensitive substring), or None.

    Reuses the same legal-name match_terms the gov bot already uses, so a
    single watchlist drives both pipelines.
    """
    up = contractor_text.upper()
    for entry in watchlist:
        terms = entry.get("match_terms") or [entry["name"].upper()]
        for t in terms:
            if t.upper() in up:
                return entry
    return None
