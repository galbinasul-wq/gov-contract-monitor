"""Alert sinks with 3-tier classification.

Tiers (based on modification / market_cap):
  Regular     : 1%   -- 3%   --> 🟢
  Important   : 3%   -- 4.5% --> 🟡
  Big Impact  : 4.5%+         --> 🔴
"""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from typing import Tuple

import requests

from config import CONFIG


# ---------- tier classification ----------

def classify_tier(ratio: float) -> Tuple[str, str]:
    """Return (tier_name, emoji) for a given materiality ratio (0.05 == 5%)."""
    if ratio >= CONFIG.tier_big_impact_threshold:
        return ("Big Impact", "🔴")
    if ratio >= CONFIG.tier_important_threshold:
        return ("Important", "🟡")
    return ("Regular", "🟢")


# ---------- formatting ----------

def _award_url(award_id: str) -> str:
    return f"https://www.usaspending.gov/award/{award_id}"


def format_message(alert: dict) -> str:
    tier = alert.get("tier") or "Regular"
    emoji = alert.get("tier_emoji") or "🟢"
    return (
        f"{emoji} {tier.upper()} CONTRACT  [{alert['ticker']}]  {alert['company']}\n"
        f"  Recipient on file:  {alert['recipient_name']}\n"
        f"  Modification date:  {alert['action_date']}   "
            f"(mod #{alert.get('modification_number') or '-'}, "
            f"type: {alert.get('action_type') or '-'})\n"
        f"  New obligation:     ${alert['modification_amount']:>15,.0f}   "
            f"<-- this modification's actual $\n"
        f"  Total award value:  ${alert['total_award_amount']:>15,.0f}   "
            f"(cumulative across all mods)\n"
        f"  Market cap:         ${alert['market_cap']:>15,.0f}   ({alert['cap_band']}-cap)\n"
        f"  Material ratio:     {alert['ratio_pct']:.2f}% of market cap  [{tier} tier]\n"
        f"  Awarding agency:    {alert['agency']}  /  {alert['sub_agency']}\n"
        f"  Period of perf:     {alert['start_date']} -> {alert['end_date']}\n"
        f"  Description:        {(alert['description'] or '')[:240]}\n"
        f"  Award URL:          {_award_url(alert['award_id'])}"
    )


# ---------- sinks ----------

def send_console(alert: dict) -> None:
    bar = "=" * 78
    print("\n" + bar)
    print(format_message(alert))
    print(bar)


def send_discord(alert: dict) -> None:
    if not CONFIG.discord_webhook_url:
        return
    try:
        requests.post(
            CONFIG.discord_webhook_url,
            json={"content": "```\n" + format_message(alert) + "\n```"},
            timeout=10,
        )
    except Exception as e:
        print(f"[alerts] Discord send failed: {e}")


def send_slack(alert: dict) -> None:
    if not CONFIG.slack_webhook_url:
        return
    try:
        requests.post(
            CONFIG.slack_webhook_url,
            json={"text": "```\n" + format_message(alert) + "\n```"},
            timeout=10,
        )
    except Exception as e:
        print(f"[alerts] Slack send failed: {e}")


def send_email(alert: dict) -> None:
    if not (CONFIG.email_username and CONFIG.email_password and CONFIG.email_to):
        return
    try:
        tier = alert.get("tier") or "Regular"
        emoji = alert.get("tier_emoji") or "🟢"
        msg = EmailMessage()
        msg["Subject"] = (
            f"{emoji} [{tier}] [{alert['ticker']}] "
            f"${alert['modification_amount']:,.0f} new "
            f"({alert['ratio_pct']:.1f}% of market cap)"
        )
        msg["From"] = CONFIG.email_from or CONFIG.email_username
        msg["To"] = CONFIG.email_to
        body = format_message(alert) + "\n\n--\nSent by your USAspending contract monitor."
        msg.set_content(body)

        with smtplib.SMTP(CONFIG.email_smtp_host, CONFIG.email_smtp_port, timeout=20) as s:
            s.starttls()
            s.login(CONFIG.email_username, CONFIG.email_password)
            s.send_message(msg)
    except Exception as e:
        print(f"[alerts] Email send failed: {e}")


def append_log(alert: dict) -> None:
    record = {**alert, "logged_at": datetime.now(timezone.utc).isoformat()}
    with open(CONFIG.log_file, "a") as f:
        f.write(json.dumps(record) + "\n")


def dispatch(alert: dict) -> None:
    send_console(alert)
    append_log(alert)
    send_discord(alert)
    send_slack(alert)
    send_email(alert)


# ---------- daily summary email (separate from per-alert dispatch) ----------

def send_summary_email(subject: str, body: str) -> None:
    if not (CONFIG.email_username and CONFIG.email_password and CONFIG.email_to):
        print("[alerts] Email not configured; skipping summary.")
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = CONFIG.email_from or CONFIG.email_username
        msg["To"] = CONFIG.email_to
        msg.set_content(body)
        with smtplib.SMTP(CONFIG.email_smtp_host, CONFIG.email_smtp_port, timeout=20) as s:
            s.starttls()
            s.login(CONFIG.email_username, CONFIG.email_password)
            s.send_message(msg)
        print("[alerts] Daily summary sent.")
    except Exception as e:
        print(f"[alerts] Summary send failed: {e}")


# ---------- SEC 8-K alerts ----------

def _format_sec_message(alert: dict) -> str:
    badge = ("📜 MATERIAL AGREEMENT (Item 1.01)"
             if alert.get("is_material_agreement")
             else "📰 8-K DISCLOSURE")
    conf = alert.get("confidence", "Medium")
    conf_emoji = {"High": "🟢", "Medium": "🟡", "Low": "⚪"}.get(conf, "🟡")
    hyperscalers = alert.get("hyperscalers") or []
    hyper_str = ", ".join(hyperscalers) if hyperscalers else "(none detected)"
    max_amount = alert.get("max_amount") or 0
    amt_str = f"${max_amount:,.0f}" if max_amount else "(no contract-context amount found)"
    return (
        f"SEC 8-K ALERT  {badge}\n"
        f"  Company:            [{alert['ticker']}]  {alert['company']}\n"
        f"  Confidence:         {conf_emoji} {conf}\n"
        f"  Looks like:         {alert.get('agreement_type') or '(unspecified agreement)'}\n"
        f"  Why flagged:        {alert.get('reason','')}\n"
        f"  Filed:              {alert.get('filing_date','')}   "
            f"(items: {alert.get('items','')})\n"
        f"  Buyers detected:    {hyper_str}\n"
        f"  Contract $ (approx):{amt_str}\n"
        f"  Filing URL:         {alert.get('filing_url','')}\n"
        f"  Excerpt:            {(alert.get('snippet') or '')[:420]}"
    )


def send_sec_console(alert: dict) -> None:
    bar = "=" * 78
    print("\n" + bar)
    print(_format_sec_message(alert))
    print(bar)


def append_sec_log(alert: dict) -> None:
    record = {**alert, "logged_at": datetime.now(timezone.utc).isoformat()}
    with open("sec_alerts.jsonl", "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def send_sec_email(alert: dict) -> None:
    if not (CONFIG.email_username and CONFIG.email_password and CONFIG.email_to):
        return
    try:
        prefix = ("📜 [Material Agreement]"
                  if alert.get("is_material_agreement")
                  else "📰 [SEC 8-K]")
        conf = alert.get("confidence", "Medium")
        hyperscalers = alert.get("hyperscalers") or []
        hyper_str = f" w/ {'/'.join(hyperscalers)}" if hyperscalers else ""
        max_amount = alert.get("max_amount") or 0
        amt_str = (f" ~${max_amount/1_000_000:.0f}M"
                   if max_amount >= 1_000_000 else "")

        msg = EmailMessage()
        msg["Subject"] = (
            f"{prefix} [{conf}] [{alert['ticker']}] "
            f"{alert['company']}{hyper_str}{amt_str}"
        )
        msg["From"] = CONFIG.email_from or CONFIG.email_username
        msg["To"] = CONFIG.email_to
        body = _format_sec_message(alert) + "\n\n--\nSent by your SEC 8-K monitor."
        msg.set_content(body)

        with smtplib.SMTP(CONFIG.email_smtp_host, CONFIG.email_smtp_port, timeout=20) as s:
            s.starttls()
            s.login(CONFIG.email_username, CONFIG.email_password)
            s.send_message(msg)
    except Exception as e:
        print(f"[alerts] SEC email send failed: {e}")


# ---------- Form 4 insider-cluster alerts ----------

def _format_form4_message(alert: dict) -> str:
    return (
        f"💰 INSIDER BUYING  [{alert['ticker']}]  {alert['company']}\n"
        f"  Signal:        {alert.get('signal_type','')}\n"
        f"  Insiders:      {alert.get('insiders_count',0)} distinct buyer(s)\n"
        f"  Total bought:  ${alert.get('total_value',0):,.0f}  "
            f"(largest single: ${alert.get('biggest_single',0):,.0f})\n"
        f"  Who bought:\n{alert.get('detail','')}\n"
        f"  All Form 4s:   {alert.get('filings_url','')}\n"
        f"  Note: only open-market purchases (code P) counted; "
            f"sales/grants/option exercises excluded."
    )


def send_form4_console(alert: dict) -> None:
    bar = "=" * 78
    print("\n" + bar)
    print(_format_form4_message(alert))
    print(bar)


def append_form4_log(alert: dict) -> None:
    record = {**alert, "logged_at": datetime.now(timezone.utc).isoformat()}
    with open("form4_alerts.jsonl", "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def send_form4_email(alert: dict) -> None:
    if not (CONFIG.email_username and CONFIG.email_password and CONFIG.email_to):
        return
    try:
        total = alert.get("total_value", 0) or 0
        amt_str = f" ${total/1_000_000:.1f}M" if total >= 1_000_000 else f" ${total:,.0f}"
        msg = EmailMessage()
        msg["Subject"] = (
            f"💰 [Insider Buy] [{alert['ticker']}] {alert['company']} "
            f"-- {alert.get('signal_type','')}{amt_str}"
        )
        msg["From"] = CONFIG.email_from or CONFIG.email_username
        msg["To"] = CONFIG.email_to
        body = _format_form4_message(alert) + "\n\n--\nSent by your Form 4 insider monitor."
        msg.set_content(body)
        with smtplib.SMTP(CONFIG.email_smtp_host, CONFIG.email_smtp_port, timeout=20) as s:
            s.starttls()
            s.login(CONFIG.email_username, CONFIG.email_password)
            s.send_message(msg)
    except Exception as e:
        print(f"[alerts] Form 4 email send failed: {e}")


def dispatch_form4(alert: dict) -> None:
    send_form4_console(alert)
    append_form4_log(alert)
    send_form4_email(alert)
