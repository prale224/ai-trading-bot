"""SQLite persistence for PAPER trading state and audit history."""
import json
import os
from pathlib import Path
import sqlite3
import threading
from contextlib import contextmanager


def resolve_database_path():
    configured = os.environ.get("PAPER_DB_PATH")
    on_render = any(os.environ.get(key) for key in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL"))
    if (on_render or os.environ.get("PAPER_REQUIRE_PERSISTENT_DB") == "true") and not configured:
        raise RuntimeError("PAPER_DB_PATH must point to a Render Persistent Disk (for example /var/data/paper.sqlite3)")
    if (on_render or os.environ.get("PAPER_REQUIRE_PERSISTENT_DB") == "true") and configured and not Path(configured).is_absolute():
        raise RuntimeError("On Render, PAPER_DB_PATH must be an absolute path on the mounted Persistent Disk")
    if (on_render or os.environ.get("PAPER_REQUIRE_PERSISTENT_DB") == "true") and configured:
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

    def record_signal(self, signal):
        with self.lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO signals(signal_id,symbol,payload,created_at) VALUES(?,?,?,?)",
                         (signal["signal_id"], signal.get("symbol"), json.dumps(signal), signal.get("created_at")))

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

    def persist_closed_trade(self, trade, state):
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO trades(symbol,payload,closed_at) VALUES(?,?,?)",
                         (trade["symbol"], json.dumps(trade), trade["closed_at"]))
            conn.execute("INSERT INTO bot_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (json.dumps(state, separators=(",", ":")),))

    def get_signals(self, limit=100):
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT payload FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(row[0]) for row in rows]
