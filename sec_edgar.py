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


# --- Classification lexicons ---------------------------------------------

# Financing / M&A / corporate housekeeping. If the agreement is one of these,
# it is NOT a customer contract win, no matter how big the dollar figure.
NEGATIVE_TERMS = [
    "credit agreement", "term loan", "revolving credit", "revolving facility",
    "senior notes", "senior secured notes", "indenture", "promissory note",
    "note purchase agreement", "notes offering", "convertible notes",
    "stock purchase agreement", "share purchase agreement",
    "agreement and plan of merger", "merger agreement", "asset purchase agreement",
    "equity distribution agreement", "underwriting agreement", "sales agreement",
    "at-the-market", "at the market offering", "atm program",
    "registration rights agreement", "securities purchase agreement",
    "lease agreement", "sublease", "ground lease", "lease amendment",
    "settlement agreement", "separation agreement", "employment agreement",
    "severance", "retention agreement", "transition services agreement",
    "amendment to the credit", "forbearance", "loan and security agreement",
    "guaranty agreement", "pledge agreement", "deposit agreement",
    "exchange agreement", "tax receivable agreement", "warrant agreement",
    "rights agreement", "standby equity", "purchase and sale agreement",
]

# Strong customer / supply / production contract signals.
STRONG_POSITIVE_TERMS = [
    "supply agreement", "master supply agreement", "master purchase agreement",
    "purchase order", "blanket purchase order", "master services agreement",
    "manufacturing agreement", "production agreement", "framework agreement",
    "capacity reservation", "capacity agreement", "tolling agreement",
    "offtake agreement", "procurement agreement", "preferred supplier",
    "selected as a supplier", "selected as supplier", "awarded a contract",
    "definitive supply", "long-term supply", "long term supply",
    "power purchase agreement", "energy supply agreement",
    "wafer supply agreement", "strategic supply",
]

# Weak supporting signals -- helpful only alongside a strong signal or buyer.
WEAK_POSITIVE_TERMS = [
    "data center", "data centre", "hyperscale", "cloud capacity",
    "compute capacity", "gpu", "accelerator", "multi-year agreement",
    "multiyear agreement", "strategic agreement", "commercial agreement",
    "collaboration agreement",
]

_ENTERED_INTO_RE = re.compile(
    r"enter(?:ed)?\s+into\s+(?:a|an|the|that\s+certain)?\s*"
    r"([^.;,]{0,90}?\b(?:agreement|order|arrangement|contract))",
    re.IGNORECASE,
)


def _agreement_type(text: str) -> str:
    m = _ENTERED_INTO_RE.search(text)
    return (m.group(1).strip() if m else "")[:120]


def classify_filing(text: str, hyperscalers: list) -> Dict[str, Any]:
    """Decide whether a filing is a real customer/supply contract win.

    Returns dict with: is_contract (bool), confidence ('High'/'Medium'/'Low'),
    agreement_type (str), reason (str).
    """
    low = text.lower()
    agreement_type = _agreement_type(text)
    at_low = agreement_type.lower()

    neg_in_type = [t for t in NEGATIVE_TERMS if t in at_low]
    neg_in_doc = [t for t in NEGATIVE_TERMS if t in low]
    strong = [t for t in STRONG_POSITIVE_TERMS if t in low]
    weak = [t for t in WEAK_POSITIVE_TERMS if t in low]
    strong_in_type = [t for t in STRONG_POSITIVE_TERMS if t in at_low]

    has_buyer = bool(hyperscalers)

    # 1. If the *named agreement type* is a financing/M&A type, veto outright.
    if neg_in_type and not strong_in_type:
        return {
            "is_contract": False,
            "confidence": "Low",
            "agreement_type": agreement_type,
            "reason": f"Agreement type looks like financing/M&A: '{agreement_type}'",
        }

    # 2. Need at least one strong positive OR a named hyperscaler buyer.
    if not strong and not has_buyer:
        return {
            "is_contract": False,
            "confidence": "Low",
            "agreement_type": agreement_type,
            "reason": "No supply/contract language and no named buyer detected",
        }

    # 3. Negative language dominates and nothing strong/buyer -> skip.
    if len(neg_in_doc) >= 2 and not strong and not has_buyer:
        return {
            "is_contract": False,
            "confidence": "Low",
            "agreement_type": agreement_type,
            "reason": "Financing/M&A language dominates the filing",
        }

    # Passed the gate -> rate confidence.
    if strong and has_buyer:
        conf = "High"
        reason = f"Strong contract language ({strong[0]}) + named buyer ({', '.join(hyperscalers)})"
    elif strong:
        conf = "Medium"
        reason = f"Strong contract language detected: {strong[0]}"
    else:  # only has_buyer
        conf = "Medium"
        reason = f"Named buyer detected ({', '.join(hyperscalers)}) without explicit supply-agreement phrasing"

    return {
        "is_contract": True,
        "confidence": conf,
        "agreement_type": agreement_type,
        "reason": reason,
        "strong_terms": strong,
        "weak_terms": weak,
    }


def _contextual_dollar_amounts(text: str) -> List[float]:
    """Return dollar amounts that appear near contract language, not the
    largest number anywhere (which is usually a credit facility / share count).
    """
    anchors = STRONG_POSITIVE_TERMS + WEAK_POSITIVE_TERMS
    low = text.lower()
    anchor_positions = []
    for term in anchors:
        start = 0
        while True:
            idx = low.find(term, start)
            if idx == -1:
                break
            anchor_positions.append(idx)
            start = idx + len(term)
    if not anchor_positions:
        return []

    amounts: List[float] = []
    for m in _DOLLAR_RE.finditer(text):
        pos = m.start()
        if any(abs(pos - a) <= 400 for a in anchor_positions):
            try:
                val = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            mag = (m.group(2) or "").lower()
            if mag == "billion":
                val *= 1_000_000_000
            elif mag == "million":
                val *= 1_000_000
            elif mag == "thousand":
                val *= 1_000
            if val >= 100_000:
                amounts.append(val)
    return amounts


def parse_filing_for_signals(html_text: str) -> Dict[str, Any]:
    """Extract buyers, classify the filing, and pull contextual dollar amounts."""
    text = _strip_html(html_text)
    text_upper = text.upper()

    found = []
    for name, terms in HYPERSCALERS.items():
        if any(t in text_upper for t in terms):
            found.append(name)
    hyperscalers = sorted(set(found))

    classification = classify_filing(text, hyperscalers)
    amounts = _contextual_dollar_amounts(text)

    # Snippet: prefer the area around the agreement type / first strong term.
    anchor_idx = -1
    at = classification.get("agreement_type") or ""
    if at:
        anchor_idx = text.lower().find(at.lower())
    if anchor_idx == -1:
        for t in STRONG_POSITIVE_TERMS:
            anchor_idx = text.lower().find(t)
            if anchor_idx != -1:
                break
    if anchor_idx == -1:
        anchor_idx = 0
    snippet = text[max(0, anchor_idx - 150):anchor_idx + 550].strip()

    return {
        "hyperscalers":   hyperscalers,
        "dollar_amounts": amounts,
        "max_amount":     max(amounts) if amounts else 0,
        "snippet":        snippet[:600],
        "text_length":    len(text),
        "classification": classification,
    }
