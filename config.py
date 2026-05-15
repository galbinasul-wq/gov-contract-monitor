"""Central configuration. Override by editing this file or via env vars."""
import os
from dataclasses import dataclass


@dataclass
class Config:
    # --- Polling ---
    poll_interval_minutes: int = 60          # how often to scan when running in loop mode
    lookback_days: int = 7                   # how far back to fetch awards on each scan

    # --- Alert thresholds ---
    min_contract_value: float = 1_000_000    # ignore awards smaller than $1M

    # 1% is the minimum to fire any alert at all (the "Regular" tier floor).
    # 3% promotes to "Important". 4.5%+ promotes to "Big Impact".
    material_ratio_threshold: float       = 0.01   # 1% --> Regular tier
    tier_important_threshold: float       = 0.03   # 3% --> Important tier
    tier_big_impact_threshold: float      = 0.045  # 4.5%+ --> Big Impact tier

    # Cap bands to alert on (anything outside this is filtered out)
    target_cap_bands: tuple = ("micro", "small", "mid")

    # --- Alert sinks (set via env) ---
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    slack_webhook_url: str = os.getenv("SLACK_WEBHOOK_URL", "")

    # --- Email alerts (Gmail by default; needs an app password, NOT your main one) ---
    email_smtp_host: str = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
    email_smtp_port: int = int(os.getenv("EMAIL_SMTP_PORT", "587"))
    email_username: str  = os.getenv("EMAIL_USERNAME", "")  # full address e.g. you@gmail.com
    email_password: str  = os.getenv("EMAIL_PASSWORD", "")  # 16-char Google App Password
    email_from: str      = os.getenv("EMAIL_FROM", "")      # defaults to email_username
    email_to: str        = os.getenv("EMAIL_TO", "")        # comma-separated for multiple

    # --- Persistence ---
    state_db_path: str = "monitor_state.db"
    log_file: str = "alerts.jsonl"

    # --- API ---
    usaspending_base_url: str = "https://api.usaspending.gov/api/v2"
    request_timeout_seconds: int = 30


CONFIG = Config()
