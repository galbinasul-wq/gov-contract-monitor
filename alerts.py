"""Alert sinks. All alerts are always written to console + JSONL log.
Discord, Slack, and Email each fire only if their env vars are configured.
"""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone

import requests

from config import CONFIG


def _award_url(award_id: str) -> str:
    return f"https://www.usaspending.gov/award/{award_id}"


def format_message(alert: dict) -> str:
    return (
        f"GOV CONTRACT ALERT  [{alert['ticker']}]  {alert['company']}\n"
        f"  Recipient on file:  {alert['recipient_name']}\n"
        f"  Modification date:  {alert['action_date']}   "
            f"(mod #{alert.get('modification_number') or '-'}, "
            f"type: {alert.get('action_type') or '-'})\n"
        f"  New obligation:     ${alert['modification_amount']:>15,.0f}   "
            f"<-- this modification's actual $\n"
        f"  Total award value:  ${alert['total_award_amount']:>15,.0f}   "
            f"(cumulative across all mods)\n"
        f"  Market cap:         ${alert['market_cap']:>15,.0f}   ({alert['cap_band']}-cap)\n"
        f"  Material ratio:     {alert['ratio_pct']:.2f}% of market cap (this modification)\n"
        f"  Awarding agency:    {alert['agency']}  /  {alert['sub_agency']}\n"
        f"  Period of perf:     {alert['start_date']} -> {alert['end_date']}\n"
        f"  Description:        {(alert['description'] or '')[:240]}\n"
        f"  Award URL:          {_award_url(alert['award_id'])}"
    )


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
    """Send alert via SMTP (Gmail by default).

    Requires EMAIL_USERNAME, EMAIL_PASSWORD, and EMAIL_TO env vars to be set.
    For Gmail: EMAIL_PASSWORD must be a 16-character App Password generated at
    https://myaccount.google.com/apppasswords (NOT your regular Google password).
    2-Step Verification must be enabled on your Google account first.
    """
    if not (CONFIG.email_username and CONFIG.email_password and CONFIG.email_to):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = (
            f"[{alert['ticker']}] Gov contract -- "
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
