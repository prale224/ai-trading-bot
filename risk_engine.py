"""Risk controls and PAPER-only trade simulator.

This module has no broker adapter and cannot submit external orders.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from copy import deepcopy
import math
import threading


@dataclass
class RiskConfig:
    starting_balance: float = 100.0
    risk_per_trade: float = 0.005
    max_daily_loss: float = 0.02
    max_total_risk: float = 0.01
    max_open_positions: int = 2
    max_notional_multiple: float = 1.0
    stop_loss_pct: float = 0.003
    take_profit_pct: float = 0.0045
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0002
    assumed_spread_rate: float = 0.0002
    max_spread_rate: float = 0.001
    max_data_age_seconds: int = 180


class PaperRiskEngine:
    """Single in-process source of truth, persisted after every state change."""

    def __init__(self, store, config=None):
        self.store = store
        self.config = config or RiskConfig()
        if self.config.starting_balance <= 0 or self.config.stop_loss_pct <= 0 or self.config.take_profit_pct <= 0:
            raise ValueError("starting balance, stop loss, and take profit must be positive")
        for name in ("risk_per_trade", "max_daily_loss", "max_total_risk", "max_spread_rate"):
            value = getattr(self.config, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be greater than 0 and at most 1")
        if self.config.max_open_positions < 1 or self.config.max_data_age_seconds < 1:
            raise ValueError("position and market-data limits must be positive")
        if min(self.config.fee_rate, self.config.slippage_rate, self.config.assumed_spread_rate) < 0:
            raise ValueError("fees, slippage, and spread estimates cannot be negative")
        self.lock = threading.RLock()
        self.storage_failed = False
        self.state = store.load_state() or {
            "balance": float(self.config.starting_balance), "positions": {},
            "starting_balance": float(self.config.starting_balance),
            "trades": [], "signals": [], "paused": False,
            "pause_reason": None, "emergency_stop": False,
            "day": self._today(), "day_start_equity": float(self.config.starting_balance),
            "daily_realized_pnl": 0.0, "last_market_warning": None, "last_prices": {},
        }
        self._roll_day()
        self._persist()

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date().isoformat()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @property
    def balance(self):
        return self.state["balance"]

    @property
    def positions(self):
        return self.state["positions"]

    def _persist(self):
        try:
            self.store.save_state(self.state)
        except Exception:
            self._mark_storage_failure()
            raise

    def _mark_storage_failure(self):
        self.storage_failed = True
        self.state.update(paused=True, pause_reason="DATABASE_UNAVAILABLE", emergency_stop=True,
                          last_market_warning="Database unavailable; PAPER trading is fail-closed")

    def _roll_day(self):
        today = self._today()
        if self.state["day"] != today:
            self.state["day"] = today
            self.state["day_start_equity"] = self.equity()
            self.state["daily_realized_pnl"] = 0.0

    def equity(self, prices=None):
        effective_prices = dict(self.state.get("last_prices", {}))
        if prices:
            for symbol, value in prices.items():
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    effective_prices[symbol] = value
        unrealized = sum((effective_prices.get(s, p["entry_price"])-p["entry_price"])
                         * (1 if p["side"] == "BUY" else -1) * p["quantity"]
                         for s, p in self.positions.items())
        return self.balance + unrealized

    def _daily_limit_hit(self):
        self._roll_day()
        return self.equity() - self.state["day_start_equity"] <= -self.state["day_start_equity"] * self.config.max_daily_loss

    def risk_status(self, prices=None):
        self._roll_day()
        return {"paused": self.state["paused"], "pause_reason": self.state["pause_reason"],
                "emergency_stop": self.state["emergency_stop"],
                "daily_loss_limit": round(self.state["day_start_equity"]*self.config.max_daily_loss, 2),
                "daily_loss_used": round(max(0, self.state["day_start_equity"]-self.equity(prices)), 2),
                "open_positions": len(self.positions), "max_open_positions": self.config.max_open_positions,
                "equity": round(self.equity(prices), 2),
                "market_warning": self.state.get("last_market_warning")}

    def record_signal(self, signal):
        with self.lock:
            previous = deepcopy(self.state)
            self.state["signals"].insert(0, dict(signal))
            self.state["signals"] = self.state["signals"][:1000]
            try:
                self.store.record_signal(signal)
                self._persist()
            except Exception:
                self.state = previous
                self._mark_storage_failure()

    def open_position(self, symbol, side, market_price, *, signal_id, spread_rate, data_age_seconds):
        with self.lock:
            self._roll_day()
            if self.storage_failed:
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            if not signal_id:
                return {"ok": False, "reason": "SIGNAL_ID_REQUIRED"}
            side = str(side).upper()
            if self.state["emergency_stop"] or self.state["paused"]:
                return {"ok": False, "reason": self.state["pause_reason"] or "EMERGENCY_STOP"}
            if side not in ("BUY", "SELL") or market_price is None or market_price <= 0:
                return {"ok": False, "reason": "INVALID_ORDER"}
            if data_age_seconds is None or data_age_seconds > self.config.max_data_age_seconds:
                return {"ok": False, "reason": "STALE_MARKET_DATA"}
            if spread_rate is None or spread_rate > self.config.max_spread_rate:
                return {"ok": False, "reason": "SPREAD_LIMIT"}
            if self._daily_limit_hit():
                self.state.update(paused=True, pause_reason="DAILY_LOSS_LIMIT")
                self._persist()
                return {"ok": False, "reason": "DAILY_LOSS_LIMIT"}
            try:
                if self.store.order_exists(signal_id):
                    return {"ok": False, "reason": "DUPLICATE_SIGNAL"}
            except Exception:
                self._mark_storage_failure()
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            if symbol in self.positions:
                return {"ok": False, "reason": "POSITION_ALREADY_OPEN"}
            if len(self.positions) >= self.config.max_open_positions:
                return {"ok": False, "reason": "MAX_OPEN_POSITIONS"}
            stop_dist = market_price * self.config.stop_loss_pct
            current_equity = max(0, self.equity())
            risk_cash = current_equity * self.config.risk_per_trade
            open_risk = sum(p["quantity"] * abs(p["entry_price"]-p["stop_loss"]) for p in self.positions.values())
            allowed_risk = max(0, current_equity*self.config.max_total_risk-open_risk)
            qty_risk = min(risk_cash, allowed_risk) / stop_dist
            entry_cost_factor = 1+self.config.fee_rate+self.config.slippage_rate+spread_rate/2
            qty_cash = max(self.balance, 0)*self.config.max_notional_multiple/(market_price*entry_cost_factor)
            quantity = min(qty_risk, qty_cash)
            if quantity <= 0:
                return {"ok": False, "reason": "MAX_TOTAL_RISK" if allowed_risk <= 0 else "INSUFFICIENT_SIMULATED_BALANCE"}
            # Paper fill includes half-spread plus configured slippage.
            impact = self.config.slippage_rate + spread_rate/2
            fill = market_price*(1+impact if side == "BUY" else 1-impact)
            fee = fill*quantity*self.config.fee_rate
            previous = deepcopy(self.state)
            self.state["balance"] -= fee
            stop = fill*(1-self.config.stop_loss_pct if side == "BUY" else 1+self.config.stop_loss_pct)
            target = fill*(1+self.config.take_profit_pct if side == "BUY" else 1-self.config.take_profit_pct)
            position = {"symbol": symbol, "side": side, "entry_price": fill, "quantity": quantity,
                        "stop_loss": stop, "take_profit": target, "opened_at": self._now(),
                        "entry_fee": fee, "signal_id": signal_id}
            self.positions[symbol] = position
            try:
                self.store.persist_open_order(signal_id, symbol, side, position, self.state)
            except Exception:
                self.state = previous
                self._mark_storage_failure()
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            return {"ok": True, "position": position}

    def close_position(self, symbol, market_price, reason="MANUAL"):
        with self.lock:
            if self.storage_failed:
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            pos = self.positions.get(symbol)
            if pos is None or market_price is None or market_price <= 0:
                return {"ok": False, "reason": "NO_POSITION" if pos is None else "INVALID_PRICE"}
            impact = self.config.slippage_rate+self.config.assumed_spread_rate/2
            fill = market_price*(1-impact if pos["side"] == "BUY" else 1+impact)
            direction = 1 if pos["side"] == "BUY" else -1
            gross = (fill-pos["entry_price"])*direction*pos["quantity"]
            exit_fee = fill*pos["quantity"]*self.config.fee_rate
            net = gross-exit_fee-pos["entry_fee"]
            previous = deepcopy(self.state)
            self.state["balance"] += gross-exit_fee
            self.state["daily_realized_pnl"] += net
            trade = {"symbol": symbol, "side": pos["side"], "entry_price": round(pos["entry_price"], 6),
                     "exit_price": round(fill, 6), "quantity": round(pos["quantity"], 8),
                     "gross_pnl": round(gross, 6), "fees": round(pos["entry_fee"]+exit_fee, 6),
                     "net_pnl": round(net, 6), "pnl": round(net, 6), "reason": reason,
                     "opened_at": pos["opened_at"], "closed_at": self._now()}
            self.state["trades"].insert(0, trade)
            self.state["trades"] = self.state["trades"][:1000]
            del self.positions[symbol]
            if self._daily_limit_hit():
                self.state.update(paused=True, pause_reason="DAILY_LOSS_LIMIT")
            try:
                self.store.persist_closed_trade(trade, self.state)
            except Exception:
                self.state = previous
                self._mark_storage_failure()
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            return {"ok": True, "trade": trade}

    def check_exits(self, prices):
        closed = []
        with self.lock:
            for symbol, pos in list(self.positions.items()):
                price = prices.get(symbol)
                if price is None:
                    continue
                if pos["side"] == "BUY":
                    reason = "STOP_LOSS" if price <= pos["stop_loss"] else "TAKE_PROFIT" if price >= pos["take_profit"] else None
                else:
                    reason = "STOP_LOSS" if price >= pos["stop_loss"] else "TAKE_PROFIT" if price <= pos["take_profit"] else None
                if reason:
                    closed.append(self.close_position(symbol, price, reason))
            return closed

    def set_market_warning(self, warning):
        with self.lock:
            self.state["last_market_warning"] = warning
            self._persist()

    def set_prices(self, prices):
        with self.lock:
            valid_prices = {}
            for symbol, price in prices.items():
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(price) and price > 0:
                    valid_prices[symbol] = price
            self.state.setdefault("last_prices", {}).update(valid_prices)
            if self._daily_limit_hit():
                self.state.update(paused=True, pause_reason="DAILY_LOSS_LIMIT")
            self._persist()

    def emergency_stop(self):
        with self.lock:
            self.state.update(emergency_stop=True, paused=True, pause_reason="EMERGENCY_STOP")
            self._persist()
            return self.risk_status()

    def resume(self):
        with self.lock:
            if self._daily_limit_hit():
                return {"ok": False, "reason": "DAILY_LOSS_LIMIT_STILL_ACTIVE"}
            self.state.update(paused=False, pause_reason=None, emergency_stop=False)
            self._persist()
            return {"ok": True}

    def snapshot(self, prices=None):
        with self.lock:
            prices = prices or {}
            return {"mode": "PAPER_ONLY", "balance": round(self.balance, 2),
                    "equity": round(self.equity(prices), 2),
                    "realized_pnl": round(self.balance-self.state["starting_balance"], 2),
                    "unrealized_pnl": round(self.equity(prices)-self.balance, 2),
                    "risk": self.risk_status(prices), "positions": self.positions.copy(),
                    "trades": self.state["trades"][:100], "signals": self.state["signals"][:100]}
