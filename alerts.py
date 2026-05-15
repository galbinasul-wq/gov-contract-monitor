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
