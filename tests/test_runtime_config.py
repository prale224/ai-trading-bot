import importlib
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from database import resolve_database_path
from runtime_config import validate_runtime_environment


TOKEN = "a-test-only-token-value-with-more-than-32-characters"


def test_required_runtime_settings_are_fail_closed(tmp_path):
    with pytest.raises(RuntimeError, match="PAPER_API_TOKEN, PAPER_DB_PATH"):
        validate_runtime_environment({})
    with pytest.raises(RuntimeError, match="PAPER_API_TOKEN"):
        validate_runtime_environment({"PAPER_API_TOKEN": "short", "PAPER_DB_PATH": str(tmp_path / "db.sqlite")})
    with pytest.raises(RuntimeError, match="absolute path"):
        validate_runtime_environment({"PAPER_API_TOKEN": TOKEN, "PAPER_DB_PATH": "relative.sqlite"})
    accepted = validate_runtime_environment({"PAPER_API_TOKEN": TOKEN, "PAPER_DB_PATH": str(tmp_path / "db.sqlite")})
    assert accepted["paper_only"] is True
    assert "token" not in accepted


def test_wsgi_import_rejects_missing_required_environment(tmp_path):
    env = os.environ.copy()
    env.pop("PAPER_API_TOKEN", None)
    env.pop("PAPER_DB_PATH", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", "import main"], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "PAPER_API_TOKEN" in result.stderr
    assert "PAPER_DB_PATH" in result.stderr
    assert TOKEN not in result.stderr


def test_render_db_must_live_under_persistent_disk():
    base = {"PAPER_API_TOKEN": TOKEN, "PAPER_DB_PATH": "/var/data/paper.sqlite3",
            "PAPER_REQUIRE_PERSISTENT_DB": "true", "PAPER_PERSISTENT_DISK_PATH": "/var/data"}
    assert validate_runtime_environment(base)["paper_only"] is True
    with pytest.raises(RuntimeError, match="mounted persistent disk"):
        validate_runtime_environment({**base, "PAPER_DB_PATH": "/tmp/paper.sqlite3"})


def test_ephemeral_render_database_requires_explicit_allow_flag():
    base = {"PAPER_API_TOKEN": TOKEN, "PAPER_DB_PATH": "/tmp/paper_trading.sqlite3",
            "RENDER_SERVICE_ID": "srv-paper-test", "PAPER_REQUIRE_PERSISTENT_DB": "false"}
    with pytest.raises(RuntimeError, match="mounted persistent disk"):
        validate_runtime_environment(base)

    accepted = validate_runtime_environment({**base, "PAPER_ALLOW_EPHEMERAL_DB": "true"})
    assert accepted["database_path"] == "/tmp/paper_trading.sqlite3"
    assert accepted["ephemeral_database"] is True
    assert accepted["paper_only"] is True


def test_ephemeral_mode_uses_only_the_fixed_tmp_database_path():
    base = {"PAPER_API_TOKEN": TOKEN, "RENDER_SERVICE_ID": "srv-paper-test",
            "PAPER_ALLOW_EPHEMERAL_DB": "true"}
    assert validate_runtime_environment(base)["database_path"] == "/tmp/paper_trading.sqlite3"
    with pytest.raises(RuntimeError, match="PAPER_DB_PATH must be /tmp/paper_trading.sqlite3"):
        validate_runtime_environment({**base, "PAPER_DB_PATH": "/tmp/other.sqlite3"})


def test_database_resolver_uses_ephemeral_path_only_when_explicitly_allowed(monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-paper-test")
    monkeypatch.setenv("PAPER_REQUIRE_PERSISTENT_DB", "false")
    monkeypatch.setenv("PAPER_ALLOW_EPHEMERAL_DB", "false")
    monkeypatch.setenv("PAPER_DB_PATH", "/tmp/paper_trading.sqlite3")
    monkeypatch.setattr("database.Path.mkdir", lambda self, *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="inside the Render Persistent Disk mount"):
        resolve_database_path()

    monkeypatch.setenv("PAPER_ALLOW_EPHEMERAL_DB", "true")
    monkeypatch.delenv("PAPER_DB_PATH", raising=False)
    assert resolve_database_path().as_posix() == "/tmp/paper_trading.sqlite3"

    monkeypatch.setenv("PAPER_DB_PATH", "/tmp/other.sqlite3")
    with pytest.raises(RuntimeError, match="PAPER_DB_PATH must be /tmp/paper_trading.sqlite3"):
        resolve_database_path()


def test_free_render_blueprint_is_separate_and_keeps_paid_blueprint_unchanged():
    root = Path(__file__).resolve().parents[1]
    free_text = (root / "render-free.yaml").read_text(encoding="utf-8")
    paid_text = (root / "render.yaml").read_text(encoding="utf-8")
    assert "name: ai-trading-bot-free" in free_text
    assert "branch: paper-trading-integration" in free_text
    assert "plan: free" in free_text
    assert "PAPER_DB_PATH" in free_text and "/tmp/paper_trading.sqlite3" in free_text
    assert "PAPER_ALLOW_EPHEMERAL_DB" in free_text and 'value: "true"' in free_text
    assert "PAPER_CURRENCY" in free_text and "value: EUR" in free_text
    assert "disk:" not in free_text
    assert 'autoDeployTrigger: "off"' in free_text
    assert "plan: starter" in paid_text
    assert "mountPath: /var/data" in paid_text


def test_application_has_no_broker_sdk_or_order_submission_calls():
    root = Path(__file__).resolve().parents[1]
    source_files = ("main.py", "database.py", "risk_engine.py", "runtime_config.py", "requirements.txt")
    forbidden = ("metatrader5", "alpaca", "ib_insync", "ccxt", "order_send(", "create_order(", "place_order(")
    for filename in source_files:
        contents = (root / filename).read_text(encoding="utf-8").lower()
        for pattern in forbidden:
            assert pattern not in contents, f"Unexpected broker integration pattern in {filename}: {pattern}"


def test_wsgi_app_is_callable_and_render_uses_single_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_API_TOKEN", TOKEN)
    monkeypatch.setenv("PAPER_DB_PATH", str(tmp_path / "wsgi.sqlite3"))
    import main
    main = importlib.reload(main)
    assert callable(main.app.wsgi_app)
    assert callable(main.app)
    config = Path(__file__).resolve().parents[1] / "render.yaml"
    text = config.read_text(encoding="utf-8")
    assert "gunicorn" in text and "--workers 1" in text
    assert "main:app" in text
    assert "mountPath: /var/data" in text
    assert "PAPER_DB_PATH" in text and "/var/data/paper_trading.sqlite3" in text
    assert "generateValue: true" in text
    assert 'autoDeployTrigger: "off"' in text
    assert "LIVE" not in text


def test_monitor_is_a_process_singleton_across_module_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_API_TOKEN", TOKEN)
    monkeypatch.setenv("PAPER_DB_PATH", str(tmp_path / "monitor.sqlite3"))
    import main
    main = importlib.reload(main)
    registry = main._monitor_registry
    old = dict(registry)
    created = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.alive = False
            created.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

    registry.update(thread=None, started=False, cycle=None)
    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    try:
        main.start_monitor()
        first = registry["thread"]
        assert registry["cycle"] == main.process_market_cycle
        assert first.kwargs["target"].__name__ == "_monitor_dispatch_loop"
        reloaded = importlib.reload(main)
        reloaded.start_monitor()
        assert registry["thread"] is first
        assert len(created) == 1
    finally:
        registry.update(old)


def test_control_token_is_not_written_to_application_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PAPER_API_TOKEN", TOKEN)
    monkeypatch.setenv("PAPER_DB_PATH", str(tmp_path / "log-test.sqlite3"))
    import main
    main = importlib.reload(main)
    monkeypatch.setattr(main, "start_monitor", lambda: None)
    with caplog.at_level(logging.DEBUG):
        response = main.app.test_client().post(
            "/paper/emergency-stop", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert TOKEN not in caplog.text
