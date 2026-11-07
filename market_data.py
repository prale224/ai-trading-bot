"""Rate-limited, per-symbol cache for PAPER market data."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import random
import threading
import time

from yfinance.exceptions import YFRateLimitError


def _as_utc_datetime(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _header_value(headers, name):
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return getter(name) or getter(name.lower())


def retry_after_seconds(error, now_utc):
    """Read Retry-After from an exception/response when the adapter exposes it."""
    candidates = [error, getattr(error, "response", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        value = getattr(candidate, "retry_after", None)
        if value is None:
            value = _header_value(getattr(candidate, "headers", None), "Retry-After")
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(value))
                seconds = (_as_utc_datetime(retry_at) - _as_utc_datetime(now_utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def is_rate_limit_error(error):
    if isinstance(error, YFRateLimitError):
        return True
    candidates = (error, getattr(error, "response", None))
    for candidate in candidates:
        if candidate is None:
            continue
        status = getattr(candidate, "status_code", getattr(candidate, "status", None))
        try:
            if int(status) == 429:
                return True
        except (TypeError, ValueError):
            pass
    return False


@dataclass
class _CachedMarket:
    result: dict
    source_timestamp: datetime
    fetched_monotonic: float
    last_success_at: str


class MarketDataCache:
    """Serialize Yahoo fetches and preserve only validated last-good results."""

    def __init__(self, *, max_data_age_seconds=180, min_fetch_interval_seconds=60,
                 backoff_base_seconds=60, backoff_max_seconds=900,
                 monotonic=time.monotonic, utcnow=lambda: datetime.now(timezone.utc),
                 uniform=random.uniform):
        try:
            max_data_age_seconds = float(max_data_age_seconds)
            min_fetch_interval_seconds = float(min_fetch_interval_seconds)
            backoff_base_seconds = float(backoff_base_seconds)
            backoff_max_seconds = float(backoff_max_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("market-data timing settings must be finite numbers") from exc
        settings = (max_data_age_seconds, min_fetch_interval_seconds,
                    backoff_base_seconds, backoff_max_seconds)
        if not all(math.isfinite(value) for value in settings):
            raise ValueError("market-data timing settings must be finite numbers")
        if max_data_age_seconds < 1:
            raise ValueError("max_data_age_seconds must be positive")
        if min_fetch_interval_seconds <= 0 or backoff_base_seconds <= 0 or backoff_max_seconds <= 0:
            raise ValueError("fetch interval and backoff settings are invalid")
        self.max_data_age_seconds = max_data_age_seconds
        self.min_fetch_interval_seconds = min_fetch_interval_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._uniform = uniform
        # The lock is held during a fetch: concurrent cycles cannot duplicate or
        # parallelize requests against Yahoo. Other callers re-check cache state.
        self._lock = threading.RLock()
        self._cache = {}
        self._last_attempt = {}
        self._last_error = {}
        self._provider_error = None
        self._rate_limit_count = 0
        self._provider_cooldown_until = 0.0

    @staticmethod
    def _validated_result(symbol, result):
        if not isinstance(result, dict) or result.get("error"):
            return None
        try:
            price = float(result.get("price"))
            timestamp = _as_utc_datetime(result.get("updated"))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None
        if result.get("signal") not in ("BUY", "SELL", "WAIT"):
            return None
        clean = dict(result)
        clean["symbol"] = symbol
        clean["price"] = price
        clean["updated"] = timestamp.isoformat()
        clean.pop("error", None)
        clean.pop("fetch_error", None)
        return clean, timestamp

    def _retry_state(self, symbol, now_monotonic):
        next_allowed = self._provider_cooldown_until
        last_attempt = self._last_attempt.get(symbol)
        if last_attempt is not None:
            next_allowed = max(next_allowed, last_attempt + self.min_fetch_interval_seconds)
        remaining = max(0.0, next_allowed - now_monotonic)
        try:
            retry_at = _as_utc_datetime(self._utcnow()) + timedelta(seconds=remaining)
        except OverflowError:
            # An extreme but finite Retry-After must not break the market monitor.
            retry_at = datetime.max.replace(tzinfo=timezone.utc)
        return remaining, retry_at.isoformat() if remaining > 0 else None

    def _snapshot(self, symbol, now_monotonic):
        now_utc = _as_utc_datetime(self._utcnow())
        remaining, next_attempt_at = self._retry_state(symbol, now_monotonic)
        cached = self._cache.get(symbol)
        fetch_error = self._last_error.get(symbol) or self._provider_error
        if cached is None:
            error = fetch_error or "No valid market data has been fetched yet"
            return {"symbol": symbol, "signal": "WAIT", "price": None, "updated": None,
                    "data_age_seconds": None, "cache_age_seconds": None, "last_success_at": None,
                    "error": error, "fetch_error": fetch_error,
                    "retry_after_seconds": remaining, "next_attempt_at": next_attempt_at,
                    "data_status": "unavailable"}

        raw_age = (now_utc - cached.source_timestamp).total_seconds()
        future_timestamp = raw_age < 0
        age = max(0.0, raw_age)
        result = dict(cached.result)
        result.update({"data_age_seconds": age,
                       "cache_age_seconds": max(0.0, now_monotonic - cached.fetched_monotonic),
                       "last_success_at": cached.last_success_at,
                       "fetch_error": fetch_error,
                       "retry_after_seconds": remaining,
                       "next_attempt_at": next_attempt_at,
                       "data_status": ("invalid" if future_timestamp else
                                       "stale" if age > self.max_data_age_seconds else "fresh")})
        if future_timestamp:
            result["error"] = "Market data timestamp is in the future"
        else:
            result["error"] = "Stale cached market data" if age > self.max_data_age_seconds else None
        return result

    def _set_rate_limit_backoff(self, error, now_monotonic, now_utc):
        self._rate_limit_count += 1
        exponential = min(self.backoff_base_seconds * (2 ** min(self._rate_limit_count - 1, 30)),
                          self.backoff_max_seconds)
        # Equal jitter keeps the delay exponential while spreading client retries.
        delay = self._uniform(exponential / 2, exponential)
        server_delay = retry_after_seconds(error, now_utc)
        if server_delay is not None:
            delay = max(delay, server_delay)
        self._provider_cooldown_until = now_monotonic + delay
        detail = str(error).strip()
        self._provider_error = f"Yahoo Finance HTTP 429: {detail}" if detail else "Yahoo Finance HTTP 429"
        return delay

    def get(self, symbol, fetcher):
        """Return the latest market result, refreshing only when allowed."""
        with self._lock:
            now_monotonic = self._monotonic()
            remaining, _ = self._retry_state(symbol, now_monotonic)
            if remaining > 0:
                return self._snapshot(symbol, now_monotonic)

            try:
                candidate = fetcher()
            except Exception as exc:
                completed_monotonic = self._monotonic()
                self._last_attempt[symbol] = completed_monotonic
                if is_rate_limit_error(exc):
                    self._set_rate_limit_backoff(exc, completed_monotonic, self._utcnow())
                    self._last_error[symbol] = self._provider_error
                else:
                    self._last_error[symbol] = str(exc) or type(exc).__name__
                return self._snapshot(symbol, completed_monotonic)

            completed_monotonic = self._monotonic()
            self._last_attempt[symbol] = completed_monotonic

            validated = self._validated_result(symbol, candidate)
            if validated is None:
                if isinstance(candidate, dict) and candidate.get("error"):
                    error = str(candidate["error"])
                else:
                    error = "Market data response was empty or invalid"
                self._last_error[symbol] = error
                return self._snapshot(symbol, self._monotonic())

            result, source_timestamp = validated
            if source_timestamp > _as_utc_datetime(self._utcnow()):
                self._last_error[symbol] = "Market data timestamp is in the future"
                return self._snapshot(symbol, completed_monotonic)
            previous = self._cache.get(symbol)
            if previous is not None and source_timestamp < previous.source_timestamp:
                self._last_error[symbol] = "Market data response timestamp did not advance"
                return self._snapshot(symbol, completed_monotonic)
            fetched_monotonic = completed_monotonic
            last_success_at = _as_utc_datetime(self._utcnow()).isoformat()
            self._cache[symbol] = _CachedMarket(result, source_timestamp,
                                                fetched_monotonic, last_success_at)
            self._last_error[symbol] = None
            self._provider_error = None
            self._rate_limit_count = 0
            self._provider_cooldown_until = 0.0
            return self._snapshot(symbol, fetched_monotonic)

    def cached_snapshot(self, symbol):
        """Return the latest cache state without attempting a provider fetch."""
        with self._lock:
            return self._snapshot(symbol, self._monotonic())

    @property
    def rate_limit_count(self):
        with self._lock:
            return self._rate_limit_count
