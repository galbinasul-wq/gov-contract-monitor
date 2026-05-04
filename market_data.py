"""Market cap lookup for tickers, via yfinance.

yfinance is free but unofficial. For production, swap in Polygon, FMP, or
Alpha Vantage. The cache below keeps us from hammering the API on every loop.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Dict, Tuple

import yfinance as yf

# yfinance is chatty about transient HTTP errors. We catch them ourselves
# in get_market_cap, so suppress its own logging to keep output clean.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


_CACHE_TTL_SECONDS = 60 * 60  # 1 hour
_cache: Dict[str, Tuple[float, Optional[float]]] = {}


def get_market_cap(ticker: str) -> Optional[float]:
    """Return current market cap in USD, or None if unavailable."""
    now = time.time()
    if ticker in _cache:
        cached_at, value = _cache[ticker]
        if now - cached_at < _CACHE_TTL_SECONDS:
            return value

    cap: Optional[float] = None
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        raw = info.get("marketCap")
        if raw:
            cap = float(raw)
    except Exception as e:
        print(f"[market_data] yfinance lookup failed for {ticker}: {e}")

    _cache[ticker] = (now, cap)
    return cap


def cap_band(market_cap: float) -> str:
    """Standard cap bands (rough industry conventions)."""
    if market_cap < 300_000_000:
        return "micro"
    if market_cap < 2_000_000_000:
        return "small"
    if market_cap < 10_000_000_000:
        return "mid"
    if market_cap < 200_000_000_000:
        return "large"
    return "mega"
