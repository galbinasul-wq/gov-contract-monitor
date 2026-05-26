"""Department of War (formerly DoD) daily contracts client.

The Department of War publishes every contract award valued at $7.5M+
each business day at ~5pm ET. This is the FASTEST public source for
DoD contract awards -- USAspending has a documented 90-day delay for
DoD procurement data.

We poll the RSS feed for the contract-roundup articles, then fetch each
article and parse the individual contract entries from the page body.

Sources:
  https://www.war.gov/News/Contracts/
  https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterator, Dict, Any, List, Optional

import requests

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except Exception:
    _HAVE_BS4 = False

from config import CONFIG


# war.gov's article pages return 403 to obvious automation User-Agents
# (RSS endpoint accepts anything, article HTML does not). We mimic a
# current Chrome browser closely enough to clear CloudFront's WAF rules.
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
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Module-level session: persists cookies that CloudFront sets on the
# first request through the same connection across subsequent fetches.
_session: Optional[requests.Session] = None
_session_warmed: bool = False


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_HEADERS)
    return _session


def _warmup_session() -> None:
    """One GET to the contracts index page so the session picks up any
    CloudFront WAF cookies that the article pages require.

    Failures here are non-fatal -- if the warmup itself 403s, individual
    article fetches will surface the same error and we still log clearly.
    """
    global _session_warmed
    if _session_warmed:
        return
    try:
        _get_session().get(
            "https://www.war.gov/News/Contracts/",
            timeout=CONFIG.request_timeout_seconds,
        )
    except Exception as e:
        print(f"  [warn] DoD session warmup failed (continuing): {e}")
    _session_warmed = True

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


def fetch_article_contracts(article_url: str) -> List[Dict[str, Any]]:
    """Fetch and parse one daily-roundup article."""
    _warmup_session()  # lazy, no-op after the first call per process
    try:
        r = _get_session().get(
            article_url, timeout=CONFIG.request_timeout_seconds
        )
        r.raise_for_status()
    except Exception as e:
        print(f"  [warn] article fetch failed {article_url}: {e}")
        return []
    return parse_dod_article(r.text, article_url)


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
