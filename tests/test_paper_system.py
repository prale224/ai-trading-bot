import tempfile
import unittest
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
        self.engine.set_prices({"US100": 90})
        self.assertEqual(self.engine.risk_status()["pause_reason"], "DAILY_LOSS_LIMIT")
        self.assertEqual(self.open_buy(signal_id="signal-2")["reason"], "DAILY_LOSS_LIMIT")

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
