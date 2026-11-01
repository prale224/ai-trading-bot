import importlib
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

from database import PaperStore
from risk_engine import PaperRiskEngine, RiskConfig


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_DB_PATH", str(tmp_path / "http-test.sqlite3"))
    monkeypatch.setenv("PAPER_API_TOKEN", "integration-test-token-with-at-least-32-characters")
    import main
    main = importlib.reload(main)
    monkeypatch.setattr(main, "start_monitor", lambda: None)
    main.app.config.update(TESTING=True)
    main.engine = PaperRiskEngine(PaperStore(tmp_path / "http-test.sqlite3"), RiskConfig())
    main.markets.clear()
    main.markets.update({
        "US100": {"symbol": "US100", "signal": "WAIT", "price": 20000.0,
                  "updated": "2026-11-01T12:00:00+00:00", "data_age_seconds": 1,
                  "spread_rate": 0.0002}
    })
    return main, main.app.test_client()


def test_read_only_http_routes_and_dashboard(service):
    main, client = service
    assert client.get("/").status_code == 200
    assert b"PAPER TRADING ONLY" in client.get("/dashboard").data
    assert client.get("/health").json["mode"] == "PAPER_ONLY"
    status = client.get("/status")
    assert status.status_code == 200
    assert status.json["mode"] == "PAPER TRADING ONLY"
    assert status.json["balance"] == 100
    assert client.get("/signals").json["US100"]["signal"] == "WAIT"
    assert client.get("/positions").json == {}
    assert client.get("/trades").json == []


def test_health_is_degraded_without_fresh_market_data(service):
    main, client = service
    main.monitor_started = True
    main.markets.clear()
    assert client.get("/health").json["status"] == "degraded"
    main.markets["US100"] = {"symbol": "US100", "price": 20000, "signal": "WAIT",
                             "data_age_seconds": 1, "error": None}
    assert client.get("/health").json["status"] == "healthy"


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
        "data_age_seconds": 10_000, "spread_rate": 0.0001,
    })
    main.process_market_cycle()
    assert main.engine.positions == {}
    assert "all new positions blocked" in main.engine.risk_status()["market_warning"]

    monkeypatch.setattr(main, "analyze_market", lambda symbol, ticker: {
        "symbol": symbol, "signal": "BUY", "error": "No market data", "updated": None,
    })
    main.process_market_cycle()
    assert main.engine.positions == {}


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
main.start_monitor = lambda: None
main.markets['US100'] = {'symbol':'US100','signal':'WAIT','price':20000.0,'updated':'2026-11-01T12:00:00+00:00','data_age_seconds':1,'spread_rate':0.0002}
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
        status, payload, _ = _http(base + "/status")
        assert status == 200
        restored = json.loads(payload)
        assert restored["balance"] < 100
        assert restored["positions"] == {}
        assert len(restored["trades"]) == 1
        assert _http(base + "/trades")[0] == 200
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
