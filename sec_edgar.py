"""SEC EDGAR API client for 8-K monitoring.

Free, official, no auth. SEC requires a descriptive User-Agent header
(set SEC_USER_AGENT env var). Rate limit is 10 req/sec/IP -- easy to stay under.

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""
from __future__ import annotations

import os
import re
from typing import List, Dict, Any, Optional

import requests

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except Exception:
    _HAVE_BS4 = False


# SEC asks every automated client to identify itself. Override via env.
USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "GovContractMonitor research contact@example.com",
)
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}


# Hyperscaler / big-AI-buyer detection: display name -> uppercase substrings.
HYPERSCALERS = {
    "Microsoft":       ["MICROSOFT CORP", "MICROSOFT CORPORATION"],
    "Amazon/AWS":      ["AMAZON.COM", "AMAZON WEB SERVICES", " AWS ", "AMAZON DATA SERVICES"],
    "Google/Alphabet": ["ALPHABET INC", "GOOGLE LLC", "GOOGLE CLOUD"],
    "Meta":            ["META PLATFORMS", "FACEBOOK, INC"],
    "Oracle":          ["ORACLE CLOUD", "ORACLE CORPORATION", "ORACLE AMERICA"],
    "Apple":           ["APPLE INC."],
    "OpenAI":          ["OPENAI"],
    "Anthropic":       ["ANTHROPIC"],
    "xAI/Tesla":       ["X.AI", "XAI CORP", "TESLA, INC"],
    "CoreWeave":       ["COREWEAVE"],
    "ByteDance":       ["BYTEDANCE", "TIKTOK"],
    "Tencent":         ["TENCENT"],
    "Alibaba":         ["ALIBABA"],
}


# ---------- Ticker-to-CIK mapping (cached for process lifetime) ----------

_ticker_to_cik: Optional[Dict[str, str]] = None


def fetch_ticker_to_cik_map() -> Dict[str, str]:
    """Fetch SEC's official ticker->CIK mapping (cached)."""
    global _ticker_to_cik
    if _ticker_to_cik is not None:
        return _ticker_to_cik
    url = "https://www.sec.gov/files/company_tickers.json"
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    mapping: Dict[str, str] = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if ticker and cik != "0000000000":
            mapping[ticker] = cik
    _ticker_to_cik = mapping
    return mapping


def cik_for_ticker(ticker: str) -> Optional[str]:
    return fetch_ticker_to_cik_map().get(ticker.upper())


# ---------- Recent filings ----------

def fetch_recent_filings(cik: str) -> List[Dict[str, Any]]:
    """Return recent filings for a company by CIK."""
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()

    recent = data.get("filings", {}).get("recent", {})
    forms        = recent.get("form", [])
    accessions   = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])
    items        = recent.get("items", [])
    report_dates = recent.get("reportDate", [])

    out: List[Dict[str, Any]] = []
    for i in range(len(forms)):
        out.append({
            "form":             forms[i],
            "accession_number": accessions[i]   if i < len(accessions)   else "",
            "filing_date":      filing_dates[i] if i < len(filing_dates) else "",
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            "items":            items[i]        if i < len(items)        else "",
            "report_date":      report_dates[i] if i < len(report_dates) else "",
            "cik":              cik_padded,
        })
    return out


# ---------- Filing content + parsing ----------

def filing_url(cik: str, accession_number: str, primary_document: str) -> str:
    accession_no_dashes = accession_number.replace("-", "")
    cik_int = int(str(cik).lstrip("0") or "0")
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{accession_no_dashes}/{primary_document}"
    )


def fetch_filing_text(cik: str, accession_number: str, primary_document: str) -> str:
    url = filing_url(cik, accession_number, primary_document)
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def _strip_html(html_text: str) -> str:
    if _HAVE_BS4:
        try:
            return BeautifulSoup(html_text, "html.parser").get_text(
                separator=" ", strip=True
            )
        except Exception:
            pass
    text = re.sub(r"<[^>]+>", " ", html_text)
    return re.sub(r"\s+", " ", text).strip()


_DOLLAR_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s?(billion|million|thousand)?",
    re.IGNORECASE,
)


def parse_filing_for_signals(html_text: str) -> Dict[str, Any]:
    """Extract hyperscaler mentions, dollar amounts, and a context snippet."""
    text = _strip_html(html_text)
    text_upper = text.upper()

    found = []
    for name, terms in HYPERSCALERS.items():
        if any(t in text_upper for t in terms):
            found.append(name)

    amounts: List[float] = []
    for amount_str, magnitude in _DOLLAR_RE.findall(text):
        try:
            val = float(amount_str.replace(",", ""))
        except ValueError:
            continue
        mag = (magnitude or "").lower()
        if mag == "billion":
            val *= 1_000_000_000
        elif mag == "million":
            val *= 1_000_000
        elif mag == "thousand":
            val *= 1_000
        if val >= 100_000:  # filter page numbers / trivial figures
            amounts.append(val)

    m = re.search(
        r"(material\s+definitive\s+agreement|purchase\s+order|"
        r"supply\s+agreement|contract|agreement|award)",
        text, re.IGNORECASE,
    )
    if m:
        start = max(0, m.start() - 200)
        end = min(len(text), m.start() + 600)
        snippet = text[start:end].strip()
    else:
        snippet = text[:500].strip()

    return {
        "hyperscalers":  sorted(set(found)),
        "dollar_amounts": amounts,
        "max_amount":     max(amounts) if amounts else 0,
        "snippet":        snippet[:600],
        "text_length":    len(text),
    }
