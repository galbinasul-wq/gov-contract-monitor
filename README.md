# USAspending Material Contract Monitor

An MVP bot that watches **USAspending.gov** for new federal contract awards going to **small-cap and mid-cap publicly-traded companies**, and alerts you when a contract is **material** relative to that company's market cap.

## How it works

1. **Poll** — every hour (configurable), hits the USAspending public API for contracts with an action date in the last 7 days.
2. **Filter to watchlist** — pushes recipient-name filters to the API server-side, then re-checks each match locally against a curated list (~30 companies in `watchlist.py`). This keeps the firehose manageable.
3. **Look up market cap** — via `yfinance` (cached for 1h).
4. **Compute materiality** — `contract_value / market_cap`. Default threshold: **2%**.
5. **Filter cap band** — only fire on micro / small / mid caps (skip large/mega).
6. **Alert + dedup** — alerts go to console, a `alerts.jsonl` log, and optional Discord/Slack webhooks. Award IDs are stored in a local SQLite file so you don't get duplicates.

## Quick start

```bash
cd gov_contract_monitor
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Verify everything is wired up (prints sample matching awards from last 30 days):
python monitor.py --test-api

# See what would have alerted in the last 30 days, with current thresholds:
python monitor.py --backfill 30

# Run one real scan:
python monitor.py --once

# Run continuously:
python monitor.py
```

Optional: enable webhooks before running.

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

## Files

| File | What it does |
|---|---|
| `monitor.py` | Entrypoint, scan loop, evaluation logic |
| `config.py` | Thresholds, intervals, paths, env-var pickup |
| `watchlist.py` | Curated list of small/mid-cap public gov contractors |
| `usaspending.py` | API client (`/search/spending_by_award/`) |
| `market_data.py` | Market-cap lookup via yfinance, cap-band labels |
| `state.py` | SQLite seen-award tracker (dedup) |
| `alerts.py` | Console / JSONL log / Discord / Slack dispatch |

## Tuning

Edit `config.py`:

| Setting | Default | Effect |
|---|---|---|
| `poll_interval_minutes` | 60 | How often to scan in loop mode |
| `lookback_days` | 7 | Window the scan covers |
| `min_contract_value` | $1,000,000 | Floor for considering an award |
| `material_ratio_threshold` | 0.02 (2%) | Fire only if award ≥ this share of market cap |
| `target_cap_bands` | micro/small/mid | Bands you care about |

The fastest way to tune: run `python monitor.py --backfill 60` and see what the last 60 days would have surfaced. Adjust thresholds. Repeat.

## Editing the watchlist

`watchlist.py` is the single source of truth. Each entry needs:

```python
{
    "ticker": "KTOS",
    "name": "Kratos Defense",
    "match_terms": ["KRATOS"],          # any uppercase substring; matches recipient name
    "exclude_terms": [],                # optional disqualifiers
}
```

Two rules of thumb:
- **Use specific phrases.** `"MERCURY SYSTEMS"` is safe, bare `"MERCURY"` is not.
- **Add aliases for subsidiaries.** A contract awarded to "Kratos Unmanned Aerial Systems Inc." rolls up to KTOS, but only if `"KRATOS"` is in your match_terms. As you spot misses, add to the list.

## Known limitations

- **Reporting lag.** USAspending data lags the actual award by days, sometimes weeks. For real-time, also watch SEC 8-K filings (companies disclose materially significant contracts there).
- **Subsidiary blind spots.** A contract to a subsidiary whose name doesn't share a root with the parent will be missed unless added to `match_terms`.
- **yfinance is unofficial.** Fine for an MVP, sometimes flaky. Swap in Polygon, FMP, or Alpha Vantage for production.
- **Definition of "material" is opinionated.** 2% of market cap is a reasonable starting point but not a legal/SEC definition. Adjust to taste.
- **Curated watchlist only.** This intentionally doesn't try to ticker-match the entire firehose — that's the next step if the MVP proves useful.

## Reasonable next steps

- **SEC 8-K monitoring** via EDGAR full-text RSS for self-disclosed material contracts (catches what USAspending misses).
- **Per-company aggregation** — alert on cumulative awards in a 30-day rolling window, not just single contracts.
- **Telegram bot** output (one more sink in `alerts.py`).
- **Tiny web dashboard** that just reads `alerts.jsonl`.
- **UEI-based matching** instead of name-based — more precise once you've mapped your watchlist tickers to UEIs.
- **Bulk-download mode** using the USAspending CSV download API for a true firehose, with a name→ticker resolver layer.
