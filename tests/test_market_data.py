from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import threading
import pytest

from yfinance.exceptions import YFRateLimitError

from market_data import MarketDataCache, is_rate_limit_error


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.start = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)

    def monotonic(self):
        return self.elapsed

    def utcnow(self):
        return self.start + timedelta(seconds=self.elapsed)

    def advance(self, seconds):
        self.elapsed += seconds


def valid_quote(clock, symbol="US100", *, price=100.0, age=0):
    return {"symbol": symbol, "price": price, "signal": "WAIT",
            "updated": (clock.utcnow() - timedelta(seconds=age)).isoformat(),
            "data_age_seconds": age}


def test_default_fetch_interval_is_60_seconds():
    cache = MarketDataCache()
    assert cache.min_fetch_interval_seconds == 60


class RateLimitWithResponse(YFRateLimitError):
    def __init__(self, retry_after):
        super().__init__()
        self.response = type("Response", (), {
            "status_code": 429,
            "headers": {"Retry-After": str(retry_after)},
        })()


def test_per_symbol_cache_recomputes_candle_age_and_suppresses_duplicate_fetch():
    clock = FakeClock()
    cache = MarketDataCache(max_data_age_seconds=180, min_fetch_interval_seconds=90,
                            monotonic=clock.monotonic, utcnow=clock.utcnow,
                            uniform=lambda low, high: high)
    calls = {"US100": 0, "US500": 0}

    def fetch(symbol):
        def run():
            calls[symbol] += 1
            return valid_quote(clock, symbol, price=100 if symbol == "US100" else 500)
        return run

    first = cache.get("US100", fetch("US100"))
    clock.advance(60)
    second = cache.get("US100", fetch("US100"))
    other = cache.get("US500", fetch("US500"))

    assert calls == {"US100": 1, "US500": 1}
    assert first["price"] == second["price"] == 100
    assert second["data_age_seconds"] == 60
    assert other["price"] == 500
    assert other["data_status"] == "fresh"


def test_fetch_interval_starts_after_slow_request_completes():
    clock = FakeClock()
    cache = MarketDataCache(min_fetch_interval_seconds=90, monotonic=clock.monotonic,
                            utcnow=clock.utcnow)
    calls = []

    def slow_fetch():
        calls.append("fetch")
        result = valid_quote(clock)
        clock.advance(120)
        return result

    cache.get("US100", slow_fetch)
    cached = cache.get("US100", slow_fetch)

    assert calls == ["fetch"]
    assert cached["retry_after_seconds"] == 90
    assert cached["data_age_seconds"] == 120


def test_sixty_second_monitor_cycles_fetch_each_symbol_with_http_time():
    clock = FakeClock()
    cache = MarketDataCache(max_data_age_seconds=180, min_fetch_interval_seconds=60,
                            monotonic=clock.monotonic, utcnow=clock.utcnow)
    calls = {"US100": 0, "US500": 0}

    def cycle():
        results = {}
        for symbol in ("US100", "US500"):
            def fetch(symbol=symbol):
                calls[symbol] += 1
                result = valid_quote(clock, symbol)
                clock.advance(5)  # simulate the HTTP request duration
                return result
            results[symbol] = cache.get(symbol, fetch)
        return results

    first = cycle()
    clock.advance(60)  # monitor sleeps REFRESH_SECONDS after finishing its cycle
    second = cycle()

    assert calls == {"US100": 2, "US500": 2}
    assert first["US100"]["data_age_seconds"] == 5
    assert first["US500"]["data_age_seconds"] == 5
    assert second["US100"]["data_age_seconds"] == 5
    assert second["US500"]["data_age_seconds"] == 5


def test_429_retry_after_uses_provider_wide_cooldown_without_same_cycle_retry():
    clock = FakeClock()
    cache = MarketDataCache(max_data_age_seconds=180, min_fetch_interval_seconds=1,
                            backoff_base_seconds=10, backoff_max_seconds=60,
                            monotonic=clock.monotonic, utcnow=clock.utcnow,
                            uniform=lambda low, high: high)
    calls = []

    def rate_limited():
        calls.append("US100")
        raise RateLimitWithResponse(25)

    first = cache.get("US100", rate_limited)
    other = cache.get("US500", lambda: calls.append("US500") or valid_quote(clock, "US500"))
    same_symbol = cache.get("US100", rate_limited)

    assert calls == ["US100"]
    assert first["fetch_error"]
    assert first["retry_after_seconds"] == 25
    assert other["retry_after_seconds"] == 25
    assert same_symbol["retry_after_seconds"] == 25
    assert cache.rate_limit_count == 1


def test_retry_after_http_date_is_supported():
    clock = FakeClock()
    cache = MarketDataCache(min_fetch_interval_seconds=1, backoff_base_seconds=10,
                            monotonic=clock.monotonic, utcnow=clock.utcnow,
                            uniform=lambda low, high: high)
    retry_at = format_datetime(clock.utcnow() + timedelta(seconds=30), usegmt=True)
    result = cache.get("US100", lambda: (_ for _ in ()).throw(RateLimitWithResponse(retry_at)))
    assert result["retry_after_seconds"] == 30


def test_exponential_backoff_jitter_caps_and_resets_after_success():
    clock = FakeClock()
    jitter_bounds = []

    def choose_upper_bound(low, high):
        jitter_bounds.append((low, high))
        return high

    cache = MarketDataCache(min_fetch_interval_seconds=1, backoff_base_seconds=10,
                            backoff_max_seconds=40, monotonic=clock.monotonic,
                            utcnow=clock.utcnow, uniform=choose_upper_bound)

    def rate_limit():
        raise YFRateLimitError()

    first = cache.get("US100", rate_limit)
    assert first["retry_after_seconds"] == 10
    clock.advance(10)
    second = cache.get("US100", rate_limit)
    assert second["retry_after_seconds"] == 20
    clock.advance(20)
    third = cache.get("US100", rate_limit)
    assert third["retry_after_seconds"] == 40
    clock.advance(40)
    successful = cache.get("US100", lambda: valid_quote(clock))
    assert successful["fetch_error"] is None
    assert cache.rate_limit_count == 0

    clock.advance(1)
    reset = cache.get("US100", rate_limit)
    assert reset["retry_after_seconds"] == 10
    assert cache.rate_limit_count == 1
    assert jitter_bounds == [(5, 10), (10, 20), (20, 40), (5, 10)]


def test_stale_cache_is_returned_with_dynamic_age_but_marked_unusable_after_failure():
    clock = FakeClock()
    cache = MarketDataCache(max_data_age_seconds=180, min_fetch_interval_seconds=1,
                            monotonic=clock.monotonic, utcnow=clock.utcnow,
                            uniform=lambda low, high: high)
    cache.get("US100", lambda: valid_quote(clock, price=90))
    clock.advance(181)
    stale = cache.get("US100", lambda: (_ for _ in ()).throw(TimeoutError("network timeout")))

    assert stale["price"] == 90
    assert stale["data_age_seconds"] == 181
    assert stale["data_status"] == "stale"
    assert stale["error"] == "Stale cached market data"
    assert stale["fetch_error"] == "network timeout"


def test_out_of_order_response_does_not_replace_newer_good_cache():
    clock = FakeClock()
    cache = MarketDataCache(max_data_age_seconds=180, min_fetch_interval_seconds=1,
                            monotonic=clock.monotonic, utcnow=clock.utcnow)
    cache.get("US100", lambda: valid_quote(clock, price=101))
    clock.advance(1)
    older = valid_quote(clock, price=99, age=60)
    result = cache.get("US100", lambda: older)

    assert result["price"] == 101
    assert result["fetch_error"] == "Market data response timestamp did not advance"


def test_future_timestamp_is_never_accepted_as_fresh():
    clock = FakeClock()
    cache = MarketDataCache(max_data_age_seconds=180, min_fetch_interval_seconds=1,
                            monotonic=clock.monotonic, utcnow=clock.utcnow)
    future = valid_quote(clock, price=999, age=-1)
    result = cache.get("US100", lambda: future)

    assert result["price"] is None
    assert result["data_status"] == "unavailable"
    assert result["fetch_error"] == "Market data timestamp is in the future"

    cache.get("US500", lambda: valid_quote(clock, "US500"))
    clock.start -= timedelta(seconds=5)  # simulate a wall-clock correction after caching
    cached = cache.get("US500", lambda: pytest.fail("cooldown should suppress this fetch"))
    assert cached["data_status"] == "invalid"
    assert cached["error"] == "Market data timestamp is in the future"


def test_parallel_requests_for_same_symbol_are_single_flight():
    clock = FakeClock()
    cache = MarketDataCache(max_data_age_seconds=180, min_fetch_interval_seconds=30,
                            monotonic=clock.monotonic, utcnow=clock.utcnow)
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    calls = []

    def fetch():
        calls.append("fetch")
        started.set()
        assert release.wait(timeout=5)
        return valid_quote(clock)

    def first_call():
        return cache.get("US100", fetch)

    def second_call():
        second_started.set()
        return cache.get("US100", fetch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_call)
        assert started.wait(timeout=5)
        second = pool.submit(second_call)
        assert second_started.wait(timeout=5)
        release.set()
        one, two = first.result(timeout=5), second.result(timeout=5)

    assert len(calls) == 1
    assert one["price"] == two["price"] == 100


def test_rate_limit_classifier_supports_yfinance_exception_and_http_status():
    assert is_rate_limit_error(YFRateLimitError())
    response_error = RuntimeError("http failure")
    response_error.response = type("Response", (), {"status_code": 429})()
    assert is_rate_limit_error(response_error)
