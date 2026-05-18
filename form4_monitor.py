"""Form 4 insider-cluster monitor.

Polls SEC EDGAR for recent Form 4 filings from the watchlist, parses the
(structured) XML, and alerts when either:
  - >=N distinct insiders made OPEN-MARKET PURCHASES of the same company
    within the lookback window (cluster), or
  - any single insider's open-market purchase >= the big-buy threshold.

Only transaction code 'P' with acquired/disposed code 'A' counts. Sales,
option exercises, grants, gifts, and tax withholding are deliberately ignored.

Usage:  python form4_monitor.py --once
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

import requests

from config import CONFIG
from form4_watchlist import WATCHLIST
from sec_edgar import fetch_ticker_to_cik_map, fetch_recent_filings, DEFAULT_HEADERS
from state import (
    is_seen_form4,
    mark_seen_form4,
    form4_alert_already_fired,
    record_form4_alert,
    record_form4_scan,
)
import alerts as alerts_mod


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _find_text(el: Optional[ET.Element], path: str) -> str:
    """Find nested text, namespace-agnostic. path uses '/'-separated tag names."""
    if el is None:
        return ""
    cur = el
    for part in path.split("/"):
        nxt = None
        for child in list(cur):
            if _strip_ns(child.tag) == part:
                nxt = child
                break
        if nxt is None:
            return ""
        cur = nxt
    return (cur.text or "").strip()


def _children(el: Optional[ET.Element], name: str) -> List[ET.Element]:
    if el is None:
        return []
    return [c for c in list(el) if _strip_ns(c.tag) == name]


def _form4_xml_url(cik: str, accession: str, primary_document: str) -> str:
    acc_nodash = accession.replace("-", "")
    cik_int = int(str(cik).lstrip("0") or "0")
    # The raw XML is the last path component at the accession folder root
    # (primary_document is often an XSL wrapper like "xslF345X05/...xml").
    raw = primary_document.split("/")[-1] if "/" in primary_document else primary_document
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{raw}"


def parse_form4(xml_text: str) -> Optional[Dict[str, Any]]:
    """Parse a Form 4 XML doc into insider + open-market-purchase info."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    issuer = next((c for c in list(root) if _strip_ns(c.tag) == "issuer"), None)
    symbol = _find_text(issuer, "issuerTradingSymbol")
    issuer_name = _find_text(issuer, "issuerName")

    owner = next((c for c in list(root) if _strip_ns(c.tag) == "reportingOwner"), None)
    owner_name = _find_text(owner, "reportingOwnerId/rptOwnerName")
    rel = None
    if owner is not None:
        rel = next((c for c in list(owner)
                    if _strip_ns(c.tag) == "reportingOwnerRelationship"), None)

    def _is_true(v: str) -> bool:
        return str(v).strip().lower() in ("1", "true")

    roles = []
    if rel is not None:
        if _is_true(_find_text(rel, "isDirector")):
            roles.append("Director")
        if _is_true(_find_text(rel, "isOfficer")):
            title = _find_text(rel, "officerTitle")
            roles.append(title or "Officer")
        if _is_true(_find_text(rel, "isTenPercentOwner")):
            roles.append("10% Owner")
    role_str = ", ".join(roles) if roles else "Insider"

    nd_table = next((c for c in list(root)
                     if _strip_ns(c.tag) == "nonDerivativeTable"), None)

    purchases = []
    for txn in _children(nd_table, "nonDerivativeTransaction"):
        coding = next((c for c in list(txn)
                       if _strip_ns(c.tag) == "transactionCoding"), None)
        code = _find_text(coding, "transactionCode")
        if code != "P":
            continue  # only open-market / private *purchases*
        amounts = next((c for c in list(txn)
                        if _strip_ns(c.tag) == "transactionAmounts"), None)
        ad = _find_text(amounts, "transactionAcquiredDisposedCode/value") \
            or _find_text(amounts, "transactionAcquiredDisposedCode")
        if ad != "A":
            continue  # must be an acquisition
        shares_s = _find_text(amounts, "transactionShares/value") \
            or _find_text(amounts, "transactionShares")
        price_s = _find_text(amounts, "transactionPricePerShare/value") \
            or _find_text(amounts, "transactionPricePerShare")
        date_s = _find_text(txn, "transactionDate/value") \
            or _find_text(txn, "transactionDate")
        try:
            shares = float(shares_s.replace(",", "")) if shares_s else 0.0
        except ValueError:
            shares = 0.0
        try:
            price = float(price_s.replace(",", "")) if price_s else 0.0
        except ValueError:
            price = 0.0
        purchases.append({
            "shares": shares,
            "price": price,
            "value": shares * price,
            "date": date_s,
        })

    if not purchases:
        return None

    return {
        "symbol": symbol.upper(),
        "issuer_name": issuer_name,
        "owner_name": owner_name,
        "role": role_str,
        "purchases": purchases,
        "total_value": sum(p["value"] for p in purchases),
        "total_shares": sum(p["shares"] for p in purchases),
    }


def _fetch_form4_xml(cik: str, accession: str, primary_document: str) -> Optional[str]:
    url = _form4_xml_url(cik, accession, primary_document)
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS,
                          timeout=CONFIG.request_timeout_seconds)
        if r.status_code == 200 and "<ownershipDocument" in r.text:
            return r.text
    except Exception:
        pass
    return None


def run_once() -> None:
    started = datetime.now(timezone.utc).isoformat()
    print(f"[{started}] Polling SEC EDGAR Form 4 (last {CONFIG.form4_lookback_days} days)...")

    try:
        ticker_to_cik = fetch_ticker_to_cik_map()
    except Exception as e:
        print(f"  [error] could not fetch SEC ticker map: {e}")
        return

    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=CONFIG.form4_lookback_days)).isoformat()

    companies = 0
    filings_seen = 0
    fired = 0

    for entry in WATCHLIST:
        ticker = entry["ticker"]
        cik = entry.get("cik") or ticker_to_cik.get(ticker.upper())
        if not cik:
            print(f"  [warn] no CIK for {ticker}")
            continue
        companies += 1

        try:
            filings = fetch_recent_filings(cik)
        except Exception as e:
            print(f"  [warn] filings fetch failed for {ticker}: {e}")
            continue

        recent_form4s = [
            f for f in filings
            if f.get("form") == "4" and f.get("filing_date", "") >= cutoff
        ]
        if not recent_form4s:
            continue

        # Gather every open-market purchase in the window for this company.
        # buyers: owner_name -> {role, value, shares, accessions:set, dates:set}
        buyers: Dict[str, Dict[str, Any]] = {}
        accessions_in_window: List[str] = []

        for f in recent_form4s:
            acc = f.get("accession_number")
            if not acc:
                continue
            filings_seen += 1
            xml_text = _fetch_form4_xml(cik, acc, f.get("primary_document", ""))
            if not xml_text:
                continue
            parsed = parse_form4(xml_text)
            if not parsed or parsed["total_value"] < CONFIG.form4_min_buy_usd:
                continue
            accessions_in_window.append(acc)
            name = parsed["owner_name"] or "(unknown insider)"
            b = buyers.setdefault(name, {
                "role": parsed["role"], "value": 0.0,
                "shares": 0.0, "accessions": set(), "dates": set(),
            })
            b["value"] += parsed["total_value"]
            b["shares"] += parsed["total_shares"]
            b["accessions"].add(acc)
            for p in parsed["purchases"]:
                if p["date"]:
                    b["dates"].add(p["date"])

        if not buyers:
            continue

        n_insiders = len(buyers)
        total_value = sum(b["value"] for b in buyers.values())
        biggest_single = max(b["value"] for b in buyers.values())

        is_cluster = n_insiders >= CONFIG.form4_min_cluster_insiders
        is_big_single = biggest_single >= CONFIG.form4_big_single_buy_usd
        if not (is_cluster or is_big_single):
            # Remember filings so we don't keep refetching them forever.
            for acc in accessions_in_window:
                mark_seen_form4(acc)
            continue

        signature = ticker + "|" + "|".join(sorted(accessions_in_window))
        if form4_alert_already_fired(signature):
            continue

        if is_cluster and is_big_single:
            signal_type = f"CLUSTER ({n_insiders} insiders) + LARGE BUY"
        elif is_cluster:
            signal_type = f"CLUSTER ({n_insiders} insiders)"
        else:
            signal_type = "LARGE SINGLE BUY"

        detail_lines = []
        for name, b in sorted(buyers.items(), key=lambda kv: -kv[1]["value"]):
            dates = ", ".join(sorted(b["dates"])) or "?"
            detail_lines.append(
                f"  - {name} ({b['role']}): "
                f"{b['shares']:,.0f} sh, ${b['value']:,.0f}  [{dates}]"
            )
        detail = "\n".join(detail_lines)

        alert = {
            "ticker": ticker,
            "company": entry["name"],
            "signal_type": signal_type,
            "insiders_count": n_insiders,
            "total_value": total_value,
            "biggest_single": biggest_single,
            "detail": detail,
            "signature": signature,
            "filings_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=40",
        }
        alerts_mod.dispatch_form4(alert)
        record_form4_alert(alert)
        for acc in accessions_in_window:
            mark_seen_form4(acc)
        fired += 1

    record_form4_scan(companies, filings_seen, fired)
    print(f"  -> {companies} companies, {filings_seen} Form 4s inspected, {fired} alerts")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Monitor SEC Form 4 filings for insider open-market buying clusters."
    )
    p.add_argument("--once", action="store_true", help="Run one scan and exit.")
    p.parse_args()
    run_once()


if __name__ == "__main__":
    main()
