"""Daily summary email.

Reads the last 24h of alert_history and scan_log from monitor_state.db,
builds a digest, and emails it.

Run separately from the hourly scan (via .github/workflows/daily_summary.yml).

Sent even when there were 0 alerts -- gives the user proof the system is alive.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Dict

from state import (
    recent_alerts, recent_scans,
    recent_sec_alerts, recent_sec_scans,
    recent_form4_alerts, recent_form4_scans,
)
from alerts import send_summary_email


def build_summary(hours: int = 24) -> tuple[str, str]:
    alerts = recent_alerts(hours=hours)
    scans = recent_scans(hours=hours)

    total_scans = len(scans)
    total_candidates = sum(int(s.get("candidates_scanned") or 0) for s in scans)
    total_fired = sum(int(s.get("alerts_fired") or 0) for s in scans)

    by_tier: Dict[str, List[Dict]] = {"Big Impact": [], "Important": [], "Regular": []}
    for a in alerts:
        tier = a.get("tier") or "Regular"
        by_tier.setdefault(tier, []).append(a)

    big = len(by_tier.get("Big Impact", []))
    important = len(by_tier.get("Important", []))
    regular = len(by_tier.get("Regular", []))

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    subject = (
        f"Daily contract summary -- "
        f"{big} big / {important} important / {regular} regular  ({now_str})"
    )

    lines = []
    lines.append(f"USAspending contract monitor -- {hours}h summary")
    lines.append(f"Generated: {now_str}")
    lines.append("")
    lines.append(f"Scans run in last {hours}h:       {total_scans}")
    lines.append(f"Total candidate awards seen:   {total_candidates}")
    lines.append(f"Total alerts fired:            {total_fired}")
    lines.append("")
    lines.append("Alert breakdown by tier:")
    lines.append(f"  🔴 Big Impact (>=4.5%):  {big}")
    lines.append(f"  🟡 Important (3-4.5%):   {important}")
    lines.append(f"  🟢 Regular (1-3%):       {regular}")
    lines.append("")

    if not alerts:
        lines.append("No gov-contract alerts in this window.")
        lines.append("")
        lines.append("This is normal during quiet weeks (holidays, end-of-month lulls).")
        lines.append("The system is running -- you'll get individual alerts whenever a")
        lines.append("public US-listed company on the watchlist receives a federal")
        lines.append("contract modification crossing 1% of its market cap.")
    else:
        for tier in ("Big Impact", "Important", "Regular"):
            entries = by_tier.get(tier, [])
            if not entries:
                continue
            emoji = {"Big Impact": "🔴", "Important": "🟡", "Regular": "🟢"}[tier]
            lines.append("-" * 78)
            lines.append(f"{emoji} {tier.upper()} ({len(entries)})")
            lines.append("-" * 78)
            for a in entries:
                ratio = float(a.get("ratio_pct") or 0)
                amt = float(a.get("modification_amount") or 0)
                lines.append(
                    f"  [{a.get('ticker','?')}] {a.get('company','?')}  "
                    f"${amt:,.0f}  ({ratio:.2f}% of mkt cap)"
                )
                lines.append(f"      Agency: {a.get('agency','-')}")
                desc = (a.get("description") or "")[:160]
                lines.append(f"      Desc:   {desc}")
                lines.append(f"      Award:  https://www.usaspending.gov/award/{a.get('award_id','')}")
                lines.append("")

    # ---- SEC 8-K section ----
    sec_alerts = recent_sec_alerts(hours=hours)
    sec_scans = recent_sec_scans(hours=hours)
    sec_scan_count = len(sec_scans)
    sec_filings_seen = sum(int(s.get("filings_scanned") or 0) for s in sec_scans)

    lines.append("")
    lines.append("=" * 78)
    lines.append("SEC 8-K MONITOR (AI infrastructure contract signals)")
    lines.append("=" * 78)
    lines.append(f"SEC scans run in last {hours}h:  {sec_scan_count}")
    lines.append(f"New 8-K filings inspected:     {sec_filings_seen}")
    lines.append(f"SEC alerts fired:              {len(sec_alerts)}")
    lines.append("")
    if not sec_alerts:
        lines.append("No SEC 8-K alerts in this window.")
    else:
        for a in sec_alerts:
            material = "📜 Material Agreement" if a.get("is_material_agreement") else "📰 Disclosure"
            hyper = a.get("hyperscalers") or ""
            amt = float(a.get("max_amount") or 0)
            amt_str = f"  ~${amt:,.0f}" if amt else ""
            lines.append("-" * 78)
            lines.append(f"  {material}  [{a.get('ticker','?')}] {a.get('company','?')}{amt_str}")
            lines.append(f"      Filed:  {a.get('filing_date','')}  (items {a.get('items','')})")
            if hyper:
                lines.append(f"      Buyers: {hyper}")
            snip = (a.get("snippet") or "")[:160]
            lines.append(f"      Excerpt: {snip}")
            lines.append(f"      Filing:  {a.get('filing_url','')}")
            lines.append("")

    # ---- Form 4 insider-buying section ----
    f4_alerts = recent_form4_alerts(hours=hours)
    f4_scans = recent_form4_scans(hours=hours)
    f4_scan_count = len(f4_scans)
    f4_filings = sum(int(s.get("filings_seen") or 0) for s in f4_scans)

    lines.append("")
    lines.append("=" * 78)
    lines.append("FORM 4 INSIDER-BUYING MONITOR (open-market purchases only)")
    lines.append("=" * 78)
    lines.append(f"Form 4 scans in last {hours}h:   {f4_scan_count}")
    lines.append(f"Form 4 filings inspected:      {f4_filings}")
    lines.append(f"Insider-buy alerts fired:      {len(f4_alerts)}")
    lines.append("")
    if not f4_alerts:
        lines.append("No insider-buying clusters or large buys in this window.")
    else:
        for a in f4_alerts:
            tv = float(a.get("total_value") or 0)
            lines.append("-" * 78)
            lines.append(
                f"  💰 [{a.get('ticker','?')}] {a.get('company','?')}  "
                f"-- {a.get('signal_type','')}  (${tv:,.0f} total)"
            )
            lines.append(a.get("detail", ""))
            lines.append("")

    body = "\n".join(lines)
    return subject, body


def main() -> None:
    subject, body = build_summary(hours=24)
    print(subject)
    print()
    print(body)
    send_summary_email(subject, body)


if __name__ == "__main__":
    main()
