import importlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
from yfinance.exceptions import YFRateLimitError

from database import PaperStore
from risk_engine import PaperRiskEngine, RiskConfig


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_DB_PATH", str(tmp_path / "http-test.sqlite3"))
    monkeypatch.setenv("PAPER_API_TOKEN", "integration-test-token-with-at-least-32-characters")
    monkeypatch.delenv("PAPER_CURRENCY", raising=False)
    import main
    main = importlib.reload(main)
    monkeypatch.setattr(main, "start_monitor", lambda: None)
    main.app.config.update(TESTING=True)
    main.engine = PaperRiskEngine(PaperStore(tmp_path / "http-test.sqlite3"), RiskConfig())
    main.markets.clear()
    main.markets.update({
        "US100": {"symbol": "US100", "signal": "WAIT", "price": 20000.0,
                  "updated": main.utc_now(), "data_age_seconds": 1,
                  "spread_rate": 0.0002}
    })
    client = main.app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer integration-test-token-with-at-least-32-characters"
    return main, client


def test_read_only_http_routes_and_dashboard(service, monkeypatch):
    main, client = service
    monkeypatch.setattr(main, "us_cash_session_open", lambda now=None: False)
    assert client.get("/").status_code == 200
    assert b"PAPER TRADING ONLY" in client.get("/dashboard").data
    dashboard = client.get("/dashboard").data
    assert "poslednja greška".encode() in dashboard
    assert "sledeći pokušaj za".encode() in dashboard
    assert "starost cene".encode() in dashboard
    assert "US sesija (standardni sat)".encode() in dashboard
    health = client.get("/health").json
    assert health["mode"] == "PAPER_ONLY"
    assert health["us_cash_session_clock_open"] is False
    assert "risk" not in health
    anonymous = main.app.test_client()
    for path in ("/status", "/signals", "/positions", "/trades"):
        assert anonymous.get(path).status_code == 401
    status = client.get("/status")
    assert status.status_code == 200
    assert status.json["mode"] == "PAPER TRADING ONLY"
    assert status.json["balance"] == 100
    assert status.json["currency"] == "EUR"
    assert status.json["us_cash_session_clock_open"] is False
    assert client.get("/signals").json["US100"]["signal"] == "WAIT"
    assert client.get("/positions").json == {}
    assert client.get("/trades").json == []


def test_yfinance_history_rate_limit_reaches_provider_backoff(service, monkeypatch):
    main, _ = service
    monkeypatch.setattr(main, "us_cash_session_open", lambda: True)
    calls = []

    class FakeTicker:
        def history(self, **kwargs):
            calls.append(kwargs)
            raise YFRateLimitError()

    monkeypatch.setattr(main.yf, "Ticker", lambda ticker: FakeTicker())
    result = main.analyze_market("US100", "^NDX")

    assert calls == [{"period": "5d", "interval": "2m", "actions": False,
                      "auto_adjust": True, "timeout": 10}]
    assert "Yahoo Finance HTTP 429" in result["fetch_error"]
    assert result["retry_after_seconds"] > 0


def test_us_cash_session_clock_handles_dst_and_closed_periods(service):
    main, _ = service
    assert main.us_cash_session_open(datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc))
    assert main.us_cash_session_open(datetime(2026, 11, 6, 14, 30, tzinfo=timezone.utc))
    assert not main.us_cash_session_open(datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc))
    assert not main.us_cash_session_open(datetime(2026, 11, 7, 14, 30, tzinfo=timezone.utc))


def test_closed_session_skips_provider_when_market_data_is_stale_or_unavailable(service, monkeypatch):
    main, _ = service
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    main.market_data_cache.get("US100", lambda: {
        "symbol": "US100", "price": 20000.0, "signal": "WAIT", "updated": stale_timestamp,
    })
    monkeypatch.setattr(main, "us_cash_session_open", lambda: False)
    provider_calls = []

    def unexpected_provider_call(symbol, ticker):
        provider_calls.append((symbol, ticker))
        raise AssertionError("provider must not be called outside the cash session")

    monkeypatch.setattr(main, "_analyze_market_uncached", unexpected_provider_call)
    stale = main.analyze_market("US100", "^NDX")
    unavailable = main.analyze_market("US500", "^GSPC")

    assert provider_calls == []
    assert stale["data_status"] == "stale"
    assert main.fresh_market_quote(stale) is None
    assert unavailable["data_status"] == "unavailable"


def test_market_data_timing_defaults_remain_aligned(service, monkeypatch):
    main, _ = service
    monkeypatch.setenv("REFRESH_SECONDS", "60")
    monkeypatch.setenv("YF_MIN_FETCH_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("MAX_DATA_AGE_SECONDS", "180")
    main = importlib.reload(main)
    monkeypatch.setattr(main, "start_monitor", lambda: None)

    assert main.REFRESH_SECONDS == 60
    assert main.market_data_cache.min_fetch_interval_seconds == 60
    assert main.config.max_data_age_seconds == 180


def test_paper_currency_can_be_configured_explicitly(service, monkeypatch):
    main, _ = service
    monkeypatch.setenv("PAPER_CURRENCY", "CAD")
    main = importlib.reload(main)
    monkeypatch.setattr(main, "start_monitor", lambda: None)
    client = main.app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer integration-test-token-with-at-least-32-characters"
    assert client.get("/status").json["currency"] == "CAD"


def test_health_requires_fresh_data_for_each_market(service):
    main, client = service
    main.monitor_started = True
    main.markets.clear()
    assert client.get("/health").json["status"] == "degraded"
    main.markets["US100"] = {"symbol": "US100", "price": 20000, "signal": "WAIT",
                             "updated": main.utc_now(), "data_age_seconds": 1, "error": None}
    assert client.get("/health").json["status"] == "degraded"
    main.markets["US500"] = {"symbol": "US500", "price": 5000, "signal": "WAIT",
                             "updated": main.utc_now(), "data_age_seconds": 1, "error": None}
    assert client.get("/health").json["status"] == "healthy"


def test_quote_freshness_uses_candle_timestamp_not_saved_age(service):
    main, _ = service
    now = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)
    recent = {"price": 100, "updated": (now - timedelta(seconds=179)).isoformat(),
              "data_age_seconds": 10_000}
    expired = {"price": 100, "updated": (now - timedelta(seconds=181)).isoformat(),
               "data_age_seconds": 1}

    assert main.fresh_market_quote(recent, now) == (100.0, 179.0)
    assert main.fresh_market_quote(expired, now) is None


def test_manual_entry_and_close_reject_stale_snapshots(service):
    main, client = service
    auth = {"Authorization": "Bearer integration-test-token-with-at-least-32-characters"}
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()
    stale_snapshot = {"signal": "WAIT", "price": 19_900.0, "updated": stale_timestamp,
                      "data_age_seconds": 1, "data_status": "fresh", "spread_rate": 0.0002}
    main.markets["US100"] = {"symbol": "US100", **stale_snapshot}
    main.markets["US500"] = {"symbol": "US500", **stale_snapshot}
    status = client.get("/status").json
    signals = client.get("/signals").json
    assert status["markets"]["US100"]["data_age_seconds"] > main.config.max_data_age_seconds
    assert signals["US500"]["data_age_seconds"] > main.config.max_data_age_seconds
    opened = main.engine.open_position("US100", "BUY", 20_000,
                                       signal_id="stale-manual-close",
                                       spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]

    entry = client.post("/paper/orders", json={"symbol": "US500", "side": "BUY",
                                                "signal_id": "stale-manual-entry"}, headers=auth)
    close = client.post("/paper/positions/US100/close", headers=auth)

    assert entry.status_code == 409
    assert entry.json["reason"] == "STALE_MARKET_DATA"
    assert close.status_code == 409
    assert close.json["reason"] == "STALE_MARKET_DATA"
    assert "US100" in main.engine.positions
    assert main.engine.state["trades"] == []


@pytest.mark.parametrize("path,payload", [
    ("/paper/emergency-stop", {}),
    ("/paper/resume", {}),
    ("/paper/orders", {"symbol": "US100", "side": "BUY", "signal_id": "manual-a"}),
    ("/paper/positions/US100/close", {}),
])
def test_missing_and_wrong_token_are_rejected(service, monkeypatch, path, payload):
    _, client = service
    monkeypatch.delenv("PAPER_API_TOKEN")
    assert client.post(path, json=payload).status_code == 401
    monkeypatch.setenv("PAPER_API_TOKEN", "integration-test-token-with-at-least-32-characters")
    assert client.post(path, json=payload, headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert client.get("/positions").json == {}


def test_protected_emergency_resume_and_manual_paper_trade(service):
    _, client = service
    auth = {"Authorization": "Bearer integration-test-token-with-at-least-32-characters"}
    stopped = client.post("/paper/emergency-stop", headers=auth)
    assert stopped.status_code == 200
    assert stopped.json["risk"]["emergency_stop"] is True
    assert client.post("/paper/orders", json={"symbol": "US100", "side": "BUY", "signal_id": "manual-a"}, headers=auth).status_code == 409
    assert client.post("/paper/resume", headers=auth).status_code == 200
    opened = client.post("/paper/orders", json={"symbol": "US100", "side": "BUY", "signal_id": "manual-a"}, headers=auth)
    assert opened.status_code == 200
    assert "US100" in client.get("/positions").json
    closed = client.post("/paper/positions/US100/close", headers=auth)
    assert closed.status_code == 200
    assert client.get("/positions").json == {}
    assert len(client.get("/trades").json) == 1


def test_stale_or_missing_market_data_blocks_new_positions(service, monkeypatch):
    main, _ = service
    monkeypatch.setattr(main, "analyze_market", lambda symbol, ticker: {
        "symbol": symbol, "price": 20000, "signal": "BUY", "updated": "2026-11-01T12:00:00+00:00",
        "data_age_seconds": 1, "spread_rate": 0.0001,
    })
    main.process_market_cycle()
    assert main.engine.positions == {}
    assert "blocked for affected instruments" in main.engine.risk_status()["market_warning"]

    monkeypatch.setattr(main, "analyze_market", lambda symbol, ticker: {
        "symbol": symbol, "signal": "BUY", "error": "No market data", "updated": None,
    })
    main.process_market_cycle()
    assert main.engine.positions == {}


def test_stale_market_data_does_not_block_fresh_instrument(service, monkeypatch):
    main, _ = service

    def fake_market(symbol, ticker):
        if symbol == "US100":
            return {"symbol": symbol, "price": 20000, "signal": "BUY",
                    "updated": "2026-11-01T12:00:00+00:00", "data_age_seconds": 10_000,
                    "spread_rate": 0.0002}
        return {"symbol": symbol, "price": 5000, "signal": "BUY",
                "updated": main.utc_now(), "data_age_seconds": 1,
                "spread_rate": 0.0002}

    monkeypatch.setattr(main, "analyze_market", fake_market)
    main.process_market_cycle()

    assert "US100" not in main.engine.positions
    assert "US500" in main.engine.positions
    assert len(main.engine.state["signals"]) == 1
    assert main.engine.state["signals"][0]["symbol"] == "US500"
    assert "US100" in main.engine.risk_status()["market_warning"]


def test_stale_price_crossing_stop_loss_does_not_close_position(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="stale-exit",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    main.engine.set_prices({"US100": 20000}, timestamps={"US100": main.utc_now()})
    stop_loss = opened["position"]["stop_loss"]

    def fake_market(symbol, ticker):
        if symbol == "US100":
            return {"symbol": symbol, "price": stop_loss - 100, "signal": "WAIT",
                    "updated": "2026-11-01T12:00:00+00:00", "data_age_seconds": 1,
                    "spread_rate": 0.0002}
        return {"symbol": symbol, "price": 5000, "signal": "WAIT",
                "updated": main.utc_now(), "data_age_seconds": 1,
                "spread_rate": 0.0002}

    monkeypatch.setattr(main, "analyze_market", fake_market)
    main.process_market_cycle()

    assert "US100" in main.engine.positions
    assert main.engine.state["trades"] == []
    warning = main.engine.risk_status()["market_warning"]
    assert "US100" in warning
    assert "open positions require attention" in warning


def test_status_and_dashboard_mark_open_position_value_unconfirmed(service):
    main, client = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="unconfirmed-equity",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    fresh_timestamp = main.utc_now()
    main.engine.set_prices({"US100": 20000}, timestamps={"US100": fresh_timestamp})
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()
    main.engine.state["last_price_timestamps"]["US100"] = stale_timestamp
    main.markets["US100"] = {"symbol": "US100", "price": 20000, "signal": "WAIT",
                             "updated": stale_timestamp, "data_age_seconds": 1,
                             "data_status": "fresh", "spread_rate": 0.0002}

    status = client.get("/status").json
    dashboard = client.get("/dashboard").get_data(as_text=True)

    assert status["equity"] is None
    assert status["risk"]["equity_status"] == "UNCONFIRMED"
    assert status["risk"]["new_entries_blocked"] is True
    assert status["risk"]["entry_block_reason"] == "RISK_UNAVAILABLE"
    assert status["risk"]["daily_loss_used"] is None
    assert "US100" in status["risk"]["unpriced_positions"]
    assert status["positions"]["US100"]["current_price"] is None
    assert status["positions"]["US100"]["unrealized_pnl"] is None
    assert status["positions"]["US100"]["valuation_status"] == "UNCONFIRMED"
    assert status["risk"]["valuation_warning"]
    assert "Privremeno nedostupno" in dashboard
    assert "nepotvrđena" in dashboard
    main.monitor_started = True
    assert client.get("/health").json["status"] == "degraded"


def test_signal_state_storage_warning_is_visible_in_status_and_dashboard(service):
    main, client = service
    signal = {"signal_id": "orphan-http-signal", "symbol": "US100", "signal": "BUY",
              "created_at": main.utc_now()}
    with main.engine.store._connect() as conn:
        conn.execute("INSERT INTO signals(signal_id,symbol,payload,created_at) VALUES(?,?,?,?)",
                     (signal["signal_id"], signal["symbol"], json.dumps(signal), signal["created_at"]))

    assert not main.engine.record_signal(signal)
    response = client.get("/status").json["risk"]
    dashboard = client.get("/dashboard").get_data(as_text=True)

    assert response["storage_failed"] is True
    assert response["pause_reason"] == "SIGNAL_STATE_INCONSISTENT"
    assert "Neusaglašen signal" in response["storage_warning"]
    assert "storage_warning" in dashboard
    main.monitor_started = True
    assert client.get("/health").json["status"] == "degraded"


def test_stale_market_route_does_not_expose_last_price_as_current(service):
    main, client = service
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()
    main.markets["US100"] = {"symbol": "US100", "price": 123.45, "signal": "BUY",
                             "updated": stale_timestamp, "data_age_seconds": 1,
                             "data_status": "fresh", "spread_rate": 0.0002}

    market = client.get("/signals").json["US100"]
    dashboard = client.get("/dashboard").get_data(as_text=True)

    assert market["data_status"] == "stale"
    assert market["price"] is None
    assert market["last_known_price"] == 123.45
    assert market["data_age_seconds"] > main.config.max_data_age_seconds
    assert "poslednja poznata: " in dashboard


def test_missing_market_timestamp_overrides_existing_fresh_status(service):
    main, client = service
    main.markets["US100"] = {"symbol": "US100", "price": 123.45, "signal": "BUY",
                             "updated": None, "data_age_seconds": 1,
                             "data_status": "fresh", "spread_rate": 0.0002}

    market = client.get("/signals").json["US100"]

    assert market["data_status"] == "unavailable"
    assert market["data_age_seconds"] is None
    assert market["price"] is None
    assert market["last_known_price"] == 123.45


def test_fresh_price_crossing_stop_loss_still_closes_position(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="fresh-exit",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    stop_loss = opened["position"]["stop_loss"]
    fresh_timestamp = main.utc_now()
    monkeypatch.setattr(main, "analyze_market", lambda symbol, ticker: {
        "symbol": symbol, "price": stop_loss - 100 if symbol == "US100" else 5000,
        "signal": "WAIT", "updated": fresh_timestamp,
        "data_age_seconds": 1, "spread_rate": 0.0002,
    })

    main.process_market_cycle()

    assert "US100" not in main.engine.positions
    assert main.engine.state["trades"][0]["reason"] == "STOP_LOSS"


def test_stale_cached_price_below_stop_loss_never_closes_position(service, monkeypatch):
    main, _ = service
    monkeypatch.setattr(main, "us_cash_session_open", lambda: True)
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="cache-stale-exit",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    stale_trigger = opened["position"]["stop_loss"] - 100
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=10_000)).isoformat()
    fresh_timestamp = datetime.now(timezone.utc).isoformat()

    def fake_analysis(symbol, ticker):
        if symbol == "US100":
            return {"symbol": symbol, "price": stale_trigger, "signal": "WAIT",
                    "updated": stale_timestamp, "data_age_seconds": 10_000,
                    "spread_rate": 0.0002}
        return {"symbol": symbol, "price": 5000, "signal": "WAIT",
                "updated": fresh_timestamp, "data_age_seconds": 1,
                "spread_rate": 0.0002}

    monkeypatch.setattr(main, "_analyze_market_uncached", fake_analysis)
    main.process_market_cycle()

    assert "US100" in main.engine.positions
    assert main.engine.state["trades"] == []
    assert main.markets["US100"]["data_status"] == "stale"
    assert main.markets["US100"]["data_age_seconds"] > main.config.max_data_age_seconds


def test_same_cached_candle_does_not_record_or_open_duplicate_signal(service, monkeypatch):
    main, _ = service
    candle_timestamp = main.utc_now()
    monkeypatch.setattr(main, "analyze_market", lambda symbol, ticker: {
        "symbol": symbol, "price": 20000 if symbol == "US100" else 5000,
        "signal": "BUY" if symbol == "US100" else "WAIT",
        "updated": candle_timestamp, "data_age_seconds": 1,
        "spread_rate": 0.0002,
    })

    main.process_market_cycle()
    assert "US100" in main.engine.positions
    assert len(main.engine.state["signals"]) == 1
    main.engine.close_position("US100", 20000, "MANUAL")

    main.process_market_cycle()

    assert main.engine.positions == {}
    assert len(main.engine.state["signals"]) == 1


def _install_us100_signal(main, monkeypatch, signal, *, price=20000.0, timestamp=None):
    timestamp = timestamp or main.utc_now()

    def fake_analysis(symbol, ticker):
        return {"symbol": symbol, "price": price if symbol == "US100" else 5000.0,
                "signal": signal if symbol == "US100" else "WAIT",
                "updated": timestamp, "data_age_seconds": 1,
                "spread_rate": 0.0002}

    monkeypatch.setattr(main, "analyze_market", fake_analysis)
    return timestamp


def test_reverse_sell_signal_closes_buy_position_with_paper_costs(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-buy-open",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]

    def fail_if_separate_signal_write(*args, **kwargs):
        raise OSError("separate signal update must not run after a close")

    monkeypatch.setattr(main.engine.store, "persist_signal_update", fail_if_separate_signal_write)
    _install_us100_signal(main, monkeypatch, "SELL", price=20020)

    main.process_market_cycle()
    main.process_market_cycle()

    assert main.engine.positions == {}
    assert main.engine.storage_failed is False
    trade = main.engine.state["trades"][0]
    assert trade["side"] == "BUY"
    assert trade["reason"] == "REVERSE_SIGNAL"
    assert trade["fees"] > 0
    assert trade["exit_price"] != 20020
    assert main.engine.state["signals"][0]["execution_reason"] == "REVERSE_SIGNAL"
    assert main.engine.store.get_signals()[0]["execution_reason"] == "REVERSE_SIGNAL"
    with main.engine.store._connect() as conn:
        persisted_trades = conn.execute("SELECT payload FROM trades").fetchall()
    assert len(persisted_trades) == 1


def test_reverse_buy_signal_closes_sell_position(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "SELL", 20000, signal_id="reverse-sell-open",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    _install_us100_signal(main, monkeypatch, "BUY", price=20000)

    main.process_market_cycle()

    assert main.engine.positions == {}
    assert main.engine.state["trades"][0]["side"] == "SELL"
    assert main.engine.state["trades"][0]["reason"] == "REVERSE_SIGNAL"


def test_reverse_exit_does_not_open_opposite_position_in_same_cycle(service, monkeypatch):
    main, _ = service
    assert main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-no-reopen",
                                     spread_rate=0.0002, data_age_seconds=1)["ok"]
    _install_us100_signal(main, monkeypatch, "SELL", price=20020)

    main.process_market_cycle()

    assert "US100" not in main.engine.positions
    assert len(main.engine.state["trades"]) == 1
    assert main.engine.state["trades"][0]["reason"] == "REVERSE_SIGNAL"


def test_stale_reverse_signal_does_not_close_position(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-stale",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    stale_timestamp = (datetime.now(timezone.utc) - timedelta(
        seconds=main.config.max_data_age_seconds + 1)).isoformat()
    _install_us100_signal(main, monkeypatch, "SELL", price=20020, timestamp=stale_timestamp)

    main.process_market_cycle()

    assert "US100" in main.engine.positions
    assert main.engine.state["trades"] == []
    assert main.engine.state["signals"] == []


def test_invalid_reverse_signal_does_not_close_position(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-invalid",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    _install_us100_signal(main, monkeypatch, "SHORT", price=20020)

    main.process_market_cycle()

    assert "US100" in main.engine.positions
    assert main.engine.state["trades"] == []
    assert main.engine.state["signals"] == []


def test_same_reverse_signal_cannot_close_twice(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-duplicate",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    _install_us100_signal(main, monkeypatch, "SELL", price=20020)

    main.process_market_cycle()
    main.process_market_cycle()

    assert main.engine.positions == {}
    assert len(main.engine.state["signals"]) == 1
    assert len(main.engine.state["trades"]) == 1
    assert main.engine.state["trades"][0]["reason"] == "REVERSE_SIGNAL"
    assert main.markets["US100"]["execution_status"] == "CLOSED"
    with main.engine.store._connect() as conn:
        persisted_trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert persisted_trade_count == 1


def test_failed_reverse_close_preserves_position_and_records_failure(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-close-fails",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]

    def fail_close(*args, **kwargs):
        raise OSError("simulated paper database write failure")

    monkeypatch.setattr(main.engine.store, "persist_closed_trade", fail_close)
    _install_us100_signal(main, monkeypatch, "SELL", price=20020)

    main.process_market_cycle()

    assert main.engine.positions["US100"] == opened["position"]
    assert main.engine.state["trades"] == []
    assert main.markets["US100"]["execution_status"] == "FAILED"
    assert main.markets["US100"]["execution_reason"] == "DATABASE_UNAVAILABLE"
    assert main.engine.state["signals"][0]["execution_reason"] == "DATABASE_UNAVAILABLE"
    assert main.engine.store.get_signals()[0]["execution_reason"] == "DATABASE_UNAVAILABLE"
    assert "Reverse-signal close failed" in main.engine.risk_status()["market_warning"]
    blocked_entry = main.engine.open_position("US500", "BUY", 5000,
                                              signal_id="db-failure-blocks-entry",
                                              spread_rate=0.0002, data_age_seconds=1)
    assert blocked_entry == {"ok": False, "reason": "DATABASE_UNAVAILABLE"}

    main.process_market_cycle()

    assert main.markets["US100"]["execution_status"] == "FAILED"
    assert main.markets["US100"]["execution_reason"] == "DATABASE_UNAVAILABLE"
    assert "Reverse-signal close failed" in main.engine.risk_status()["market_warning"]


def test_no_position_still_opens_on_fresh_signal(service, monkeypatch):
    main, _ = service
    _install_us100_signal(main, monkeypatch, "BUY", price=20000)

    main.process_market_cycle()

    assert "US100" in main.engine.positions


def test_reverse_exit_keeps_emergency_stop_active_for_next_cycle(service, monkeypatch):
    main, _ = service
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-emergency-stop",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    main.engine.emergency_stop()
    _install_us100_signal(main, monkeypatch, "SELL", price=20020)

    main.process_market_cycle()

    assert main.engine.positions == {}
    assert main.engine.state["trades"][0]["reason"] == "REVERSE_SIGNAL"
    assert main.engine.risk_status()["emergency_stop"] is True

    _install_us100_signal(main, monkeypatch, "SELL", price=20020)
    main.process_market_cycle()

    assert main.engine.positions == {}
    assert main.markets["US100"]["execution_status"] == "REJECTED"
    assert main.markets["US100"]["execution_reason"] == "EMERGENCY_STOP"


def test_reverse_close_applies_daily_loss_limit_before_later_entry(service, monkeypatch):
    main, _ = service
    main.engine.config.risk_per_trade = 0.05
    main.engine.config.max_daily_loss = 0.001
    main.engine.config.max_total_risk = 0.5
    main.engine.config.max_notional_multiple = 5
    main.engine.config.stop_loss_pct = 0.10
    main.engine.config.take_profit_pct = 0.20
    opened = main.engine.open_position("US100", "BUY", 20000, signal_id="reverse-daily-loss",
                                        spread_rate=0.0002, data_age_seconds=1)
    assert opened["ok"]
    _install_us100_signal(main, monkeypatch, "SELL", price=19000)

    main.process_market_cycle()

    assert main.engine.positions == {}
    assert main.engine.state["trades"][0]["reason"] == "REVERSE_SIGNAL"
    assert main.engine.risk_status()["pause_reason"] == "DAILY_LOSS_LIMIT"

    _install_us100_signal(main, monkeypatch, "SELL", price=19000)
    main.process_market_cycle()

    assert main.engine.positions == {}
    assert main.markets["US100"]["execution_reason"] == "DAILY_LOSS_LIMIT"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(url, token=None, payload=None, method=None):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=2) as response:
        content_type = response.headers.get("Content-Type", "")
        return response.status, response.read(), content_type


def test_local_server_dashboard_and_full_process_restart(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    runner = tmp_path / "run_paper_app.py"
    runner.write_text("""
import main
from datetime import datetime, timezone
main.start_monitor = lambda: None
main.markets['US100'] = {'symbol':'US100','signal':'WAIT','price':20000.0,'updated':datetime.now(timezone.utc).isoformat(),'data_age_seconds':1,'spread_rate':0.0002}
main.app.run(host='127.0.0.1', port=int(__import__('os').environ['PORT']), use_reloader=False)
""", encoding="utf-8")
    port = _free_port()
    env = os.environ.copy()
    env.update({"PAPER_DB_PATH": str(db_path), "PAPER_API_TOKEN": "integration-test-token-with-at-least-32-characters", "PORT": str(port)})
    project_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")

    def launch():
        process = subprocess.Popen([sys.executable, str(runner)], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            if process.poll() is not None:
                pytest.fail("Local PAPER application exited before becoming ready")
            try:
                return process, base, _http(base + "/health")
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                time.sleep(0.1)
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        pytest.fail(f"Local PAPER application did not become ready: {last_error}; {stderr.decode(errors='replace')}")

    process, base, _ = launch()
    try:
        status, page, content_type = _http(base + "/")
        assert status == 200 and "text/html" in content_type
        assert b"PAPER TRADING ONLY" in page
        assert _http(base + "/paper/orders", "integration-test-token-with-at-least-32-characters",
                     {"symbol": "US100", "side": "BUY", "signal_id": "restart-trade"})[0] == 200
        assert _http(base + "/paper/positions/US100/close", "integration-test-token-with-at-least-32-characters", method="POST")[0] == 200
    finally:
        process.terminate()
        process.wait(timeout=10)

    process, base, _ = launch()
    try:
        status, payload, _ = _http(base + "/status", "integration-test-token-with-at-least-32-characters")
        assert status == 200
        restored = json.loads(payload)
        assert restored["balance"] < 100
        assert restored["positions"] == {}
        assert len(restored["trades"]) == 1
        assert _http(base + "/trades", "integration-test-token-with-at-least-32-characters")[0] == 200
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_database_outage_rolls_back_close_and_fails_closed(tmp_path):
    db_path = tmp_path / "ok.sqlite3"
    engine = PaperRiskEngine(PaperStore(db_path), RiskConfig())
    result = engine.open_position("US100", "BUY", 100, signal_id="db-test",
                                  spread_rate=0.0001, data_age_seconds=1)
    assert result["ok"]
    engine.store.path = tmp_path / "missing" / "unavailable.sqlite3"
    result = engine.close_position("US100", 101)
    assert result == {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
    assert "US100" in engine.positions
    assert engine.risk_status()["emergency_stop"] is True
    assert engine.open_position("US500", "BUY", 100, signal_id="db-test-2",
                                spread_rate=0.0001, data_age_seconds=1)["reason"] == "DATABASE_UNAVAILABLE"
