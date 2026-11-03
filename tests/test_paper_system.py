import tempfile
import unittest
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from database import PaperStore
from risk_engine import PaperRiskEngine, RiskConfig


class PaperSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper.sqlite3"
        self.cfg = RiskConfig(starting_balance=100, risk_per_trade=0.01, max_daily_loss=0.02,
                              max_total_risk=0.02, max_open_positions=2, max_notional_multiple=1,
                              stop_loss_pct=0.01, take_profit_pct=0.02, fee_rate=0,
                              slippage_rate=0, assumed_spread_rate=0, max_spread_rate=0.001,
                              max_data_age_seconds=180)
        self.engine = PaperRiskEngine(PaperStore(self.path), self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def open_buy(self, engine=None, signal_id="signal-1", price=100):
        engine = engine or self.engine
        return engine.open_position("US100", "BUY", price, signal_id=signal_id,
                                    spread_rate=0.0002, data_age_seconds=1)

    def set_fresh_prices(self, engine, prices):
        timestamp = datetime.now(timezone.utc).isoformat()
        engine.set_prices(prices, timestamps={symbol: timestamp for symbol in prices})

    def test_signal_and_bot_state_are_persisted_atomically(self):
        signal = {"signal_id": "atomic-signal", "symbol": "US100", "signal": "BUY",
                  "created_at": datetime.now(timezone.utc).isoformat()}

        self.assertTrue(self.engine.record_signal(signal))

        persisted_state = self.engine.store.load_state()
        self.assertEqual(persisted_state["signals"][0], signal)
        self.assertEqual(self.engine.store.get_signals(), [signal])
        self.assertFalse(self.engine.record_signal(signal))
        self.assertFalse(self.engine.storage_failed)
        self.assertEqual(len(self.engine.store.get_signals()), 1)

    def test_signal_row_without_matching_bot_state_fails_closed(self):
        signal = {"signal_id": "orphan-signal", "symbol": "US100", "signal": "BUY",
                  "created_at": datetime.now(timezone.utc).isoformat()}
        with self.engine.store._connect() as conn:
            conn.execute("INSERT INTO signals(signal_id,symbol,payload,created_at) VALUES(?,?,?,?)",
                         (signal["signal_id"], signal["symbol"], json.dumps(signal), signal["created_at"]))

        self.assertFalse(self.engine.record_signal(signal))

        self.assertEqual(self.engine.state["signals"], [])
        self.assertEqual(self.engine.store.load_state()["signals"], [])
        self.assertEqual(self.engine.risk_status()["pause_reason"], "SIGNAL_STATE_INCONSISTENT")
        self.assertTrue(self.engine.risk_status()["storage_failed"])
        self.assertIn("Neusaglašen signal", self.engine.risk_status()["storage_warning"])
        blocked = self.open_buy(signal_id="blocked-by-orphan-signal")
        self.assertEqual(blocked, {"ok": False, "reason": "DATABASE_UNAVAILABLE"})

    def test_signal_transaction_failure_rolls_back_signal_and_bot_state(self):
        signal = {"signal_id": "atomic-rollback", "symbol": "US100", "signal": "SELL",
                  "created_at": datetime.now(timezone.utc).isoformat()}
        original_db_state = self.engine.store.load_state()
        with self.engine.store._connect() as conn:
            conn.execute("CREATE TRIGGER reject_bot_state_update BEFORE UPDATE ON bot_state "
                         "BEGIN SELECT RAISE(ABORT, 'injected state write failure'); END")

        self.assertFalse(self.engine.record_signal(signal))

        self.assertEqual(self.engine.state["signals"], [])
        self.assertEqual(self.engine.store.get_signals(), [])
        self.assertEqual(self.engine.store.load_state(), original_db_state)
        self.assertTrue(self.engine.storage_failed)
        self.assertEqual(self.engine.state["pause_reason"], "DATABASE_UNAVAILABLE")

    def test_same_signal_can_retry_after_atomic_transaction_rollback(self):
        signal = {"signal_id": "atomic-retry", "symbol": "US500", "signal": "BUY",
                  "created_at": datetime.now(timezone.utc).isoformat()}
        with self.engine.store._connect() as conn:
            conn.execute("CREATE TRIGGER reject_bot_state_update BEFORE UPDATE ON bot_state "
                         "BEGIN SELECT RAISE(ABORT, 'injected state write failure'); END")

        self.assertFalse(self.engine.record_signal(signal))
        with self.engine.store._connect() as conn:
            conn.execute("DROP TRIGGER reject_bot_state_update")

        self.assertTrue(self.engine.record_signal(signal))

        persisted_state = self.engine.store.load_state()
        persisted_signals = self.engine.store.get_signals()
        self.assertEqual([item["signal_id"] for item in persisted_signals], [signal["signal_id"]])
        self.assertEqual(persisted_state["signals"][0]["signal_id"], signal["signal_id"])
        blocked_entry = self.engine.open_position("US500", "BUY", 5000,
                                                  signal_id="after-signal-retry",
                                                  spread_rate=0.0002, data_age_seconds=1)
        self.assertEqual(blocked_entry, {"ok": False, "reason": "DATABASE_UNAVAILABLE"})

    def test_failed_signal_result_update_restores_memory_to_database_value(self):
        signal = {"signal_id": "signal-update-rollback", "symbol": "US100", "signal": "BUY",
                  "created_at": datetime.now(timezone.utc).isoformat()}
        self.assertTrue(self.engine.record_signal(signal))
        before = dict(self.engine.state["signals"][0])

        def fail_update(*args, **kwargs):
            raise OSError("injected SQLite write failure")

        self.engine.store.persist_signal_update = fail_update

        self.assertFalse(self.engine.update_signal_result(signal["signal_id"], {
            "execution_status": "CLOSED", "execution_reason": "REVERSE_SIGNAL"}))

        self.assertEqual(self.engine.state["signals"][0], before)
        self.assertEqual(self.engine.store.get_signals()[0], before)
        self.assertTrue(self.engine.storage_failed)
        self.assertEqual(self.engine.state["pause_reason"], "DATABASE_UNAVAILABLE")

    def test_emergency_stop_blocks_new_positions_and_persists(self):
        self.engine.emergency_stop()
        self.assertEqual(self.open_buy()["reason"], "EMERGENCY_STOP")
        recovered = PaperRiskEngine(PaperStore(self.path), self.cfg)
        self.assertTrue(recovered.risk_status()["emergency_stop"])
        self.assertEqual(self.open_buy(recovered)["reason"], "EMERGENCY_STOP")

    def test_restart_recovers_open_position_balance_and_trades(self):
        self.assertTrue(self.open_buy()["ok"])
        self.engine.close_position("US100", 101, "MANUAL")
        previous = self.engine.snapshot()
        recovered = PaperRiskEngine(PaperStore(self.path), self.cfg).snapshot()
        self.assertEqual(recovered["balance"], previous["balance"])
        self.assertEqual(recovered["positions"], previous["positions"])
        self.assertEqual(recovered["trades"], previous["trades"])

    def test_database_recovers_open_position_after_restart(self):
        self.assertTrue(self.open_buy()["ok"])
        recovered = PaperRiskEngine(PaperStore(self.path), self.cfg)
        self.assertIn("US100", recovered.positions)
        self.assertEqual(recovered.positions["US100"]["signal_id"], "signal-1")

    def test_daily_loss_limit_pauses_and_blocks_new_positions(self):
        self.assertTrue(self.open_buy()["ok"])
        self.engine.close_position("US100", 70, "TEST_LOSS")
        self.assertEqual(self.engine.risk_status()["pause_reason"], "DAILY_LOSS_LIMIT")
        self.assertEqual(self.open_buy(signal_id="signal-2")["reason"], "DAILY_LOSS_LIMIT")

    def test_unrealized_daily_loss_also_pauses_new_positions(self):
        self.assertTrue(self.open_buy()["ok"])
        self.set_fresh_prices(self.engine, {"US100": 90})
        self.assertEqual(self.engine.risk_status()["pause_reason"], "DAILY_LOSS_LIMIT")
        self.assertEqual(self.open_buy(signal_id="signal-2")["reason"], "DAILY_LOSS_LIMIT")

    def test_fresh_price_confirms_equity_and_position_valuation(self):
        self.assertTrue(self.open_buy()["ok"])
        self.set_fresh_prices(self.engine, {"US100": 110})

        status = self.engine.risk_status()
        snapshot = self.engine.snapshot()
        position = self.engine.positions["US100"]
        opened_price = position["entry_price"]
        expected_equity = round(self.engine.balance + (110 - opened_price) * position["quantity"], 2)

        self.assertEqual(status["equity_status"], "CONFIRMED")
        self.assertTrue(status["risk_calculation_available"])
        self.assertEqual(status["equity"], expected_equity)
        self.assertEqual(snapshot["positions"]["US100"]["valuation_status"], "CONFIRMED")
        self.assertEqual(snapshot["positions"]["US100"]["current_price"], 110)
        self.assertAlmostEqual(snapshot["positions"]["US100"]["unrealized_pnl"],
                               (110 - opened_price) * position["quantity"])

    def test_stale_price_makes_position_equity_and_daily_loss_unconfirmed(self):
        self.assertTrue(self.open_buy()["ok"])
        self.set_fresh_prices(self.engine, {"US100": 110})
        self.engine.state["last_price_timestamps"]["US100"] = (
            datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()

        status = self.engine.risk_status()
        snapshot = self.engine.snapshot()
        position = snapshot["positions"]["US100"]

        self.assertIsNone(self.engine.equity())
        self.assertIsNone(status["equity"])
        self.assertIsNone(status["daily_loss_used"])
        self.assertEqual(status["equity_status"], "UNCONFIRMED")
        self.assertFalse(status["risk_calculation_available"])
        self.assertEqual(status["unpriced_positions"], ["US100"])
        self.assertTrue(status["new_entries_blocked"])
        self.assertEqual(status["entry_block_reason"], "RISK_UNAVAILABLE")
        self.assertEqual(snapshot["equity_status"], "UNCONFIRMED")
        self.assertIsNone(position["current_price"])
        self.assertIsNone(position["unrealized_pnl"])
        self.assertEqual(position["valuation_status"], "UNCONFIRMED")
        self.assertIsNotNone(position["valuation_warning"])

    def test_stale_open_position_blocks_new_entry_when_risk_is_unavailable(self):
        self.assertTrue(self.open_buy()["ok"])
        self.set_fresh_prices(self.engine, {"US100": 100})
        self.engine.state["last_price_timestamps"]["US100"] = (
            datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()

        result = self.engine.open_position("US500", "BUY", 100, signal_id="unknown-risk",
                                           spread_rate=0.0002, data_age_seconds=1)

        self.assertEqual(result["reason"], "RISK_UNAVAILABLE")
        self.assertEqual(set(self.engine.positions), {"US100"})
        self.assertIsNone(self.engine.risk_status()["pause_reason"])

    def test_daily_loss_pause_is_not_cleared_when_equity_becomes_unavailable(self):
        self.assertTrue(self.open_buy()["ok"])
        self.set_fresh_prices(self.engine, {"US100": 90})
        self.assertEqual(self.engine.risk_status()["pause_reason"], "DAILY_LOSS_LIMIT")
        self.engine.state["last_price_timestamps"]["US100"] = (
            datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()

        status = self.engine.risk_status()
        resume = self.engine.resume()

        self.assertIsNone(status["daily_loss_used"])
        self.assertEqual(resume, {"ok": False, "reason": "RISK_UNAVAILABLE"})
        self.assertTrue(self.engine.risk_status()["paused"])
        self.assertEqual(self.engine.risk_status()["pause_reason"], "DAILY_LOSS_LIMIT")

    def test_fresh_data_restores_equity_and_unrealized_pnl_calculation(self):
        self.assertTrue(self.open_buy()["ok"])
        self.set_fresh_prices(self.engine, {"US100": 110})
        self.engine.state["last_price_timestamps"]["US100"] = (
            datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()
        self.assertIsNone(self.engine.equity())

        self.set_fresh_prices(self.engine, {"US100": 120})

        status = self.engine.risk_status()
        expected_equity = round(self.engine.balance +
                                (120 - self.engine.positions["US100"]["entry_price"])
                                * self.engine.positions["US100"]["quantity"], 2)
        self.assertEqual(status["equity"], expected_equity)
        self.assertEqual(status["equity_status"], "CONFIRMED")
        self.assertTrue(status["risk_calculation_available"])
        self.assertAlmostEqual(self.engine.snapshot()["positions"]["US100"]["unrealized_pnl"],
                               (120 - self.engine.positions["US100"]["entry_price"])
                               * self.engine.positions["US100"]["quantity"])

    def test_stale_price_is_ignored_by_automatic_sl_tp_check(self):
        opened = self.open_buy()
        self.assertTrue(opened["ok"])
        self.set_fresh_prices(self.engine, {"US100": 100})
        stale_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()
        self.engine.state["last_price_timestamps"]["US100"] = stale_timestamp

        results = self.engine.check_exits({"US100": opened["position"]["stop_loss"] - 1},
                                          timestamps={"US100": stale_timestamp})

        self.assertEqual(results, [])
        self.assertIn("US100", self.engine.positions)
        self.assertEqual(self.engine.state["trades"], [])

    def test_price_without_source_timestamp_is_not_used_for_risk_or_exits(self):
        opened = self.open_buy()
        self.assertTrue(opened["ok"])
        stop_trigger = opened["position"]["stop_loss"] - 1

        self.engine.set_prices({"US100": stop_trigger})
        results = self.engine.check_exits({"US100": stop_trigger})

        self.assertIsNone(self.engine.equity())
        self.assertFalse(self.engine.risk_status()["risk_calculation_available"])
        self.assertEqual(results, [])
        self.assertIn("US100", self.engine.positions)
        self.assertEqual(self.engine.state["trades"], [])

    def test_position_sizing_uses_unrealized_pnl_in_current_equity(self):
        cfg = RiskConfig(starting_balance=100, risk_per_trade=0.10, max_daily_loss=0.50,
                         max_total_risk=0.50, max_open_positions=2, max_notional_multiple=10,
                         stop_loss_pct=0.10, take_profit_pct=0.20, fee_rate=0,
                         slippage_rate=0, assumed_spread_rate=0, max_spread_rate=0.001)
        engine = PaperRiskEngine(PaperStore(Path(self.temp.name) / "unrealized-sizing.sqlite3"), cfg)
        first = engine.open_position("US100", "BUY", 100, signal_id="unrealized-first",
                                      spread_rate=0, data_age_seconds=1)
        self.assertTrue(first["ok"])
        self.set_fresh_prices(engine, {"US100": 150})
        self.assertEqual(engine.equity(), 150)

        second = engine.open_position("US500", "BUY", 100, signal_id="unrealized-second",
                                       spread_rate=0, data_age_seconds=1)

        self.assertTrue(second["ok"])
        self.assertAlmostEqual(second["position"]["quantity"], 1.5)

    def test_day_rollover_uses_last_valid_prices_for_starting_equity(self):
        cfg = RiskConfig(starting_balance=100, risk_per_trade=0.01, max_daily_loss=0.50,
                         max_total_risk=0.50, max_notional_multiple=2, stop_loss_pct=0.01,
                         take_profit_pct=0.02, fee_rate=0, slippage_rate=0,
                         assumed_spread_rate=0, max_spread_rate=0.001)
        engine = PaperRiskEngine(PaperStore(Path(self.temp.name) / "day-rollover.sqlite3"), cfg)
        self.assertTrue(engine.open_position("US100", "BUY", 100, signal_id="rollover",
                                             spread_rate=0, data_age_seconds=1)["ok"])
        self.set_fresh_prices(engine, {"US100": 99})
        self.assertEqual(engine.equity(), 99)
        engine.state["daily_realized_pnl"] = 12
        engine._today = lambda: "2099-01-01"

        engine._roll_day()

        self.assertEqual(engine.state["day_start_equity"], 99)
        self.assertEqual(engine.state["daily_realized_pnl"], 0)

    def test_day_rollover_waits_for_fresh_equity_without_resetting_loss_state(self):
        self.assertTrue(self.open_buy()["ok"])
        self.set_fresh_prices(self.engine, {"US100": 99})
        previous_day = self.engine.state["day"]
        previous_start = self.engine.state["day_start_equity"]
        self.engine._today = lambda: "2099-01-01"
        self.engine.state["last_price_timestamps"]["US100"] = (
            datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()

        self.engine._roll_day()

        self.assertEqual(self.engine.state["day"], previous_day)
        self.assertEqual(self.engine.state["day_start_equity"], previous_start)

        self.set_fresh_prices(self.engine, {"US100": 98})

        self.assertEqual(self.engine.state["day"], "2099-01-01")
        self.assertAlmostEqual(self.engine.state["day_start_equity"], self.engine.equity())
        self.assertEqual(self.engine.state["daily_realized_pnl"], 0)

    def test_stale_data_and_excessive_spread_are_rejected(self):
        result = self.engine.open_position("US100", "BUY", 100, signal_id="s1", spread_rate=0.0001, data_age_seconds=181)
        self.assertEqual(result["reason"], "STALE_MARKET_DATA")
        result = self.engine.open_position("US100", "BUY", 100, signal_id="s2", spread_rate=0.002, data_age_seconds=1)
        self.assertEqual(result["reason"], "SPREAD_LIMIT")

    def test_duplicate_signal_cannot_open_twice(self):
        self.assertTrue(self.open_buy()["ok"])
        self.engine.close_position("US100", 100, "MANUAL")
        self.assertEqual(self.open_buy()["reason"], "DUPLICATE_SIGNAL")

    def test_invalid_close_price_does_not_discard_position(self):
        self.assertTrue(self.open_buy()["ok"])
        self.assertFalse(self.engine.close_position("US100", 0)["ok"])
        self.assertIn("US100", self.engine.positions)


if __name__ == "__main__":
    unittest.main()
