"""SQLite persistence for PAPER trading state and audit history."""
import json
import os
from pathlib import Path
import sqlite3
import threading
from contextlib import contextmanager

EPHEMERAL_DB_PATH = "/tmp/paper_trading.sqlite3"


class SignalStateInconsistency(RuntimeError):
    """A signal row cannot be reconciled with the persisted bot snapshot."""


def resolve_database_path():
    configured = os.environ.get("PAPER_DB_PATH", "").strip()
    allow_ephemeral_db = os.environ.get("PAPER_ALLOW_EPHEMERAL_DB", "").strip().lower() == "true"
    on_render = any(os.environ.get(key) for key in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL"))
    if allow_ephemeral_db:
        if configured and configured != EPHEMERAL_DB_PATH:
            raise RuntimeError(f"PAPER_DB_PATH must be {EPHEMERAL_DB_PATH} when PAPER_ALLOW_EPHEMERAL_DB=true")
        path = Path(EPHEMERAL_DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    require_disk = on_render or os.environ.get("PAPER_REQUIRE_PERSISTENT_DB", "").strip().lower() == "true"
    if require_disk and not configured:
        raise RuntimeError("PAPER_DB_PATH must point to a Render Persistent Disk (for example /var/data/paper.sqlite3)")
    if require_disk and configured and not (Path(configured).is_absolute() or configured.startswith("/")):
        raise RuntimeError("On Render, PAPER_DB_PATH must be an absolute path on the mounted Persistent Disk")
    if require_disk and configured:
        mount = Path(os.environ.get("PAPER_PERSISTENT_DISK_PATH", "/var/data")).resolve()
        target = Path(configured).resolve()
        if target != mount and mount not in target.parents:
            raise RuntimeError(f"PAPER_DB_PATH must be inside the Render Persistent Disk mount: {mount}")
    path = Path(configured or "instance/paper_trading.sqlite3").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class PaperStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else resolve_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS signals (signal_id TEXT PRIMARY KEY, symbol TEXT, payload TEXT NOT NULL, created_at TEXT);
                CREATE TABLE IF NOT EXISTS paper_orders (signal_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, payload TEXT NOT NULL, created_at TEXT);
                CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, payload TEXT NOT NULL, closed_at TEXT);
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def load_state(self):
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT payload FROM bot_state WHERE id=1").fetchone()
            return json.loads(row["payload"]) if row else None

    def save_state(self, state):
        with self.lock, self._connect() as conn:
            conn.execute("INSERT INTO bot_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (json.dumps(state, separators=(",", ":")),))

    def persist_signal(self, signal, state):
        """Insert a signal and its matching bot snapshot in one transaction."""
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("INSERT OR IGNORE INTO signals(signal_id,symbol,payload,created_at) VALUES(?,?,?,?)",
                                  (signal["signal_id"], signal.get("symbol"), json.dumps(signal), signal.get("created_at")))
            if cursor.rowcount != 1:
                stored_signal = conn.execute("SELECT payload FROM signals WHERE signal_id=?",
                                             (signal["signal_id"],)).fetchone()
                stored_state = conn.execute("SELECT payload FROM bot_state WHERE id=1").fetchone()
                if stored_signal is None or stored_state is None:
                    raise SignalStateInconsistency("duplicate signal cannot be reconciled with bot state")
                try:
                    stored_signal_payload = json.loads(stored_signal["payload"])
                    stored_state_payload = json.loads(stored_state["payload"])
                except (TypeError, ValueError) as exc:
                    raise SignalStateInconsistency("stored signal or bot state is invalid") from exc
                state_signals = stored_state_payload.get("signals") if isinstance(stored_state_payload, dict) else None
                matching = ([item for item in state_signals if isinstance(item, dict)
                             and item.get("signal_id") == signal["signal_id"]]
                            if isinstance(state_signals, list) else [])
                if len(matching) != 1 or matching[0] != stored_signal_payload:
                    raise SignalStateInconsistency("signal exists in SQLite without matching bot state")
                return False
            conn.execute("INSERT INTO bot_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (json.dumps(state, separators=(",", ":")),))
            return True

    def persist_signal_update(self, signal_id, signal, state):
        """Persist a signal outcome and its corresponding bot state atomically."""
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("UPDATE signals SET payload=? WHERE signal_id=?",
                                  (json.dumps(signal), signal_id))
            if cursor.rowcount != 1:
                return False
            conn.execute("INSERT INTO bot_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (json.dumps(state, separators=(",", ":")),))
            return True

    def order_exists(self, signal_id):
        with self.lock, self._connect() as conn:
            return conn.execute("SELECT 1 FROM paper_orders WHERE signal_id=?", (signal_id,)).fetchone() is not None

    def record_order(self, signal_id, symbol, side, payload):
        if not signal_id:
            raise ValueError("signal_id is required for idempotent paper orders")
        with self.lock, self._connect() as conn:
            conn.execute("INSERT INTO paper_orders(signal_id,symbol,side,payload,created_at) VALUES(?,?,?,?,?)",
                         (signal_id, symbol, side, json.dumps(payload), payload.get("opened_at")))

    def persist_open_order(self, signal_id, symbol, side, position, state):
        if not signal_id:
            raise ValueError("signal_id is required for idempotent paper orders")
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO paper_orders(signal_id,symbol,side,payload,created_at) VALUES(?,?,?,?,?)",
                         (signal_id, symbol, side, json.dumps(position), position.get("opened_at")))
            conn.execute("INSERT INTO bot_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (json.dumps(state, separators=(",", ":")),))

    def record_trade(self, trade):
        with self.lock, self._connect() as conn:
            conn.execute("INSERT INTO trades(symbol,payload,closed_at) VALUES(?,?,?)",
                         (trade["symbol"], json.dumps(trade), trade["closed_at"]))

    def persist_closed_trade(self, trade, state, *, signal_id=None, signal=None):
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO trades(symbol,payload,closed_at) VALUES(?,?,?)",
                         (trade["symbol"], json.dumps(trade), trade["closed_at"]))
            if signal_id is not None:
                if signal is None:
                    raise ValueError("signal payload is required when persisting a signal outcome")
                cursor = conn.execute("UPDATE signals SET payload=? WHERE signal_id=?",
                                      (json.dumps(signal), signal_id))
                if cursor.rowcount != 1:
                    raise RuntimeError("signal record missing while persisting its close")
            conn.execute("INSERT INTO bot_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (json.dumps(state, separators=(",", ":")),))

    def get_signals(self, limit=100):
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT payload FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(row[0]) for row in rows]
