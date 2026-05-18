"""SQLite-backed state for the monitor.

Three tables:
- seen_awards    : dedup keys so we don't re-alert on the same transaction
- alert_history  : every alert we've ever fired (used by the daily summary)
- scan_log       : a record of each scan run (used by the daily summary)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from config import CONFIG


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.state_db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_awards (
            award_id TEXT PRIMARY KEY,
            seen_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            transaction_id      TEXT PRIMARY KEY,
            fired_at            TEXT NOT NULL,
            ticker              TEXT,
            company             TEXT,
            tier                TEXT,
            modification_amount REAL,
            total_award_amount  REAL,
            market_cap          REAL,
            cap_band            TEXT,
            ratio_pct           REAL,
            agency              TEXT,
            description         TEXT,
            action_date         TEXT,
            award_id            TEXT,
            recipient_name      TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            scan_at            TEXT PRIMARY KEY,
            candidates_scanned INTEGER NOT NULL,
            alerts_fired       INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sec_seen_filings (
            accession_number TEXT PRIMARY KEY,
            seen_at          TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sec_alert_history (
            accession_number      TEXT PRIMARY KEY,
            fired_at              TEXT NOT NULL,
            ticker                TEXT,
            company               TEXT,
            filing_date           TEXT,
            items                 TEXT,
            is_material_agreement INTEGER,
            hyperscalers          TEXT,
            max_amount            REAL,
            filing_url            TEXT,
            snippet               TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sec_scan_log (
            scan_at         TEXT PRIMARY KEY,
            filings_scanned INTEGER NOT NULL,
            alerts_fired    INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS form4_seen_filings (
            accession_number TEXT PRIMARY KEY,
            seen_at          TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS form4_alert_history (
            signature        TEXT PRIMARY KEY,
            fired_at         TEXT NOT NULL,
            ticker           TEXT,
            company          TEXT,
            signal_type      TEXT,
            insiders_count   INTEGER,
            total_value      REAL,
            detail           TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS form4_scan_log (
            scan_at           TEXT PRIMARY KEY,
            companies_scanned INTEGER NOT NULL,
            filings_seen      INTEGER NOT NULL,
            alerts_fired      INTEGER NOT NULL
        )
    """)
    return conn


# ---------- dedup ----------

def is_seen(award_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("SELECT 1 FROM seen_awards WHERE award_id = ?", (award_id,))
        return cur.fetchone() is not None


def mark_seen(award_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO seen_awards (award_id, seen_at) VALUES (?, ?)",
            (award_id, datetime.now(timezone.utc).isoformat()),
        )


# ---------- alert history ----------

def record_alert(alert: Dict[str, Any]) -> None:
    """Append an alert to the history table (idempotent on transaction_id)."""
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO alert_history (
                transaction_id, fired_at, ticker, company, tier,
                modification_amount, total_award_amount, market_cap, cap_band,
                ratio_pct, agency, description, action_date, award_id, recipient_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.get("transaction_id") or alert.get("award_id"),
                datetime.now(timezone.utc).isoformat(),
                alert.get("ticker"),
                alert.get("company"),
                alert.get("tier"),
                alert.get("modification_amount"),
                alert.get("total_award_amount"),
                alert.get("market_cap"),
                alert.get("cap_band"),
                alert.get("ratio_pct"),
                alert.get("agency"),
                (alert.get("description") or "")[:500],
                alert.get("action_date"),
                alert.get("award_id"),
                alert.get("recipient_name"),
            ),
        )


def recent_alerts(hours: int = 24) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT * FROM alert_history WHERE fired_at >= ? ORDER BY ratio_pct DESC",
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- scan log ----------

def record_scan(candidates: int, alerts: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO scan_log (scan_at, candidates_scanned, alerts_fired) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), candidates, alerts),
        )


def recent_scans(hours: int = 24) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT * FROM scan_log WHERE scan_at >= ? ORDER BY scan_at DESC",
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- SEC: dedup ----------

def is_seen_sec_filing(accession: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "SELECT 1 FROM sec_seen_filings WHERE accession_number = ?", (accession,)
        )
        return cur.fetchone() is not None


def mark_seen_sec_filing(accession: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO sec_seen_filings (accession_number, seen_at) VALUES (?, ?)",
            (accession, datetime.now(timezone.utc).isoformat()),
        )


# ---------- SEC: alert history ----------

def record_sec_alert(alert: Dict[str, Any]) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO sec_alert_history (
                accession_number, fired_at, ticker, company, filing_date,
                items, is_material_agreement, hyperscalers, max_amount,
                filing_url, snippet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.get("accession_number"),
                datetime.now(timezone.utc).isoformat(),
                alert.get("ticker"),
                alert.get("company"),
                alert.get("filing_date"),
                alert.get("items"),
                1 if alert.get("is_material_agreement") else 0,
                ",".join(alert.get("hyperscalers", [])),
                alert.get("max_amount") or 0,
                alert.get("filing_url"),
                (alert.get("snippet") or "")[:1000],
            ),
        )


def recent_sec_alerts(hours: int = 24) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT * FROM sec_alert_history WHERE fired_at >= ? ORDER BY filing_date DESC",
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- SEC: scan log ----------

def record_sec_scan(filings: int, alerts: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sec_scan_log (scan_at, filings_scanned, alerts_fired) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), filings, alerts),
        )


def recent_sec_scans(hours: int = 24) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT * FROM sec_scan_log WHERE scan_at >= ? ORDER BY scan_at DESC",
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- Form 4: dedup + alert history ----------

def is_seen_form4(accession: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "SELECT 1 FROM form4_seen_filings WHERE accession_number = ?", (accession,)
        )
        return cur.fetchone() is not None


def mark_seen_form4(accession: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO form4_seen_filings (accession_number, seen_at) VALUES (?, ?)",
            (accession, datetime.now(timezone.utc).isoformat()),
        )


def form4_alert_already_fired(signature: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "SELECT 1 FROM form4_alert_history WHERE signature = ?", (signature,)
        )
        return cur.fetchone() is not None


def record_form4_alert(alert: Dict[str, Any]) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO form4_alert_history (
                signature, fired_at, ticker, company, signal_type,
                insiders_count, total_value, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.get("signature"),
                datetime.now(timezone.utc).isoformat(),
                alert.get("ticker"),
                alert.get("company"),
                alert.get("signal_type"),
                alert.get("insiders_count"),
                alert.get("total_value") or 0,
                (alert.get("detail") or "")[:2000],
            ),
        )


def recent_form4_alerts(hours: int = 24) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT * FROM form4_alert_history WHERE fired_at >= ? ORDER BY total_value DESC",
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


def record_form4_scan(companies: int, filings: int, alerts: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO form4_scan_log "
            "(scan_at, companies_scanned, filings_seen, alerts_fired) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), companies, filings, alerts),
        )


def recent_form4_scans(hours: int = 24) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT * FROM form4_scan_log WHERE scan_at >= ? ORDER BY scan_at DESC",
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]
