"""Risk controls and PAPER-only trade simulator.

This module has no broker adapter and cannot submit external orders.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from copy import deepcopy
import math
import threading

from database import SignalStateInconsistency


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
        numeric_settings = (
            "starting_balance", "risk_per_trade", "max_daily_loss", "max_total_risk",
            "max_notional_multiple", "stop_loss_pct", "take_profit_pct", "fee_rate",
            "slippage_rate", "assumed_spread_rate", "max_spread_rate",
        )
        for name in numeric_settings:
            value = getattr(self.config, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if (self.config.starting_balance <= 0 or self.config.max_notional_multiple <= 0
                or not 0 < self.config.stop_loss_pct < 1
                or not 0 < self.config.take_profit_pct < 1):
            raise ValueError("starting balance and notional multiple must be positive; "
                            "stop loss and take profit must be between 0 and 1")
        for name in ("risk_per_trade", "max_daily_loss", "max_total_risk", "max_spread_rate"):
            value = getattr(self.config, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be greater than 0 and at most 1")
        if self.config.max_open_positions < 1 or self.config.max_data_age_seconds < 1:
            raise ValueError("position and market-data limits must be positive")
        for name in ("fee_rate", "slippage_rate", "assumed_spread_rate"):
            value = getattr(self.config, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if (self.config.slippage_rate + self.config.max_spread_rate / 2 >= 1
                or self.config.slippage_rate + self.config.assumed_spread_rate / 2 >= 1):
            raise ValueError("simulated slippage and spread must remain below the full price")
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
        if not isinstance(self.state.get("last_prices"), dict):
            self.state["last_prices"] = {}
        if not isinstance(self.state.get("last_price_timestamps"), dict):
            self.state["last_price_timestamps"] = {}
        self._roll_day()
        self._persist()

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date().isoformat()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _parse_market_timestamp(value):
        if not value:
            return None
        try:
            timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(
                str(value).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return timestamp.astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _valid_price(value):
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    def _stop_loss_risk_per_unit(self, side, entry_price, stop_price):
        """Estimate loss per unit at the stop, including modeled exit impact and fees."""
        exit_impact = self.config.slippage_rate + self.config.assumed_spread_rate / 2
        exit_fill = stop_price * (1 - exit_impact if side == "BUY" else 1 + exit_impact)
        gross_loss = entry_price - exit_fill if side == "BUY" else exit_fill - entry_price
        total = gross_loss + (entry_price + exit_fill) * self.config.fee_rate
        if not math.isfinite(exit_fill) or exit_fill <= 0 or not math.isfinite(total) or total <= 0:
            return None
        return total

    def _fresh_price_marks(self, prices=None, timestamps=None, now=None):
        """Return only marks whose source timestamps are still within the age limit."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        marks = {}

        def add_mark(symbol, value, observed_at):
            price = self._valid_price(value)
            timestamp = self._parse_market_timestamp(observed_at)
            if price is None or timestamp is None:
                return
            age = (current - timestamp).total_seconds()
            if not math.isfinite(age) or age < 0 or age > self.config.max_data_age_seconds:
                return
            marks[symbol] = {"price": price, "updated_at": timestamp.isoformat(),
                             "age_seconds": age}

        stored_prices = self.state.get("last_prices", {})
        stored_timestamps = self.state.get("last_price_timestamps", {})
        for symbol, price in stored_prices.items():
            add_mark(symbol, price, stored_timestamps.get(symbol))

        if prices:
            supplied_timestamps = timestamps or {}
            for symbol, price in prices.items():
                observed_at = supplied_timestamps.get(symbol) if hasattr(supplied_timestamps, "get") else None
                add_mark(symbol, price, observed_at)
        return marks

    def _position_valuation(self, prices=None, timestamps=None):
        marks = self._fresh_price_marks(prices, timestamps)
        known_unrealized = 0.0
        unpriced = []
        position_values = {}
        for symbol, position in self.positions.items():
            mark = marks.get(symbol)
            if mark is None:
                unpriced.append(symbol)
                last_timestamp = self._parse_market_timestamp(
                    self.state.get("last_price_timestamps", {}).get(symbol))
                raw_last_age = ((datetime.now(timezone.utc) - last_timestamp).total_seconds()
                                if last_timestamp else None)
                last_age = raw_last_age if raw_last_age is not None and raw_last_age >= 0 else None
                position_values[symbol] = {
                    "current_price": None,
                    "unrealized_pnl": None,
                    "valuation_status": "UNCONFIRMED" if last_age is not None else "UNAVAILABLE",
                    "price_updated_at": last_timestamp.isoformat() if last_timestamp else None,
                    "price_age_seconds": last_age,
                    "valuation_warning": "Sveža tržišna cena nije dostupna; trenutna vrednost pozicije nije potvrđena",
                }
                continue
            direction = 1 if position["side"] == "BUY" else -1
            exit_impact = self.config.slippage_rate + self.config.assumed_spread_rate / 2
            liquidation_price = mark["price"] * (
                1 - exit_impact if position["side"] == "BUY" else 1 + exit_impact)
            gross_unrealized = ((liquidation_price - position["entry_price"])
                                * direction * position["quantity"])
            exit_fee = liquidation_price * position["quantity"] * self.config.fee_rate
            # Entry fees are already deducted from balance; this mark-to-liquidation
            # P/L includes the modeled cost of closing the open position.
            unrealized = gross_unrealized - exit_fee
            if not all(math.isfinite(value) for value in (liquidation_price, gross_unrealized,
                                                          exit_fee, unrealized)):
                unpriced.append(symbol)
                position_values[symbol] = {
                    "current_price": mark["price"],
                    "unrealized_pnl": None,
                    "valuation_status": "UNCONFIRMED",
                    "price_updated_at": mark["updated_at"],
                    "price_age_seconds": mark["age_seconds"],
                    "valuation_warning": "Vrednovanje PAPER zatvaranja nije numerički dostupno",
                }
                continue
            known_unrealized += unrealized
            position_values[symbol] = {
                "current_price": mark["price"],
                "unrealized_pnl": unrealized,
                "valuation_status": "CONFIRMED",
                "price_updated_at": mark["updated_at"],
                "price_age_seconds": mark["age_seconds"],
                "valuation_warning": None,
            }
        equity = self.balance + known_unrealized if not unpriced else None
        unrealized_pnl = known_unrealized if not unpriced else None
        warning = (f"Vrednost računa nije dostupna; nedostaje potvrđena tržišna ili likvidaciona procena za: {', '.join(sorted(unpriced))}"
                   if unpriced else None)
        return {"equity": equity, "unrealized_pnl": unrealized_pnl,
                "unpriced_positions": sorted(unpriced), "position_values": position_values,
                "risk_calculation_available": not unpriced, "valuation_warning": warning}

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

    def _mark_storage_failure(self, reason="DATABASE_UNAVAILABLE", warning=None):
        self.storage_failed = True
        warning = warning or "SQLite nije dostupan; PAPER ulasci su blokirani"
        self.state.update(paused=True, pause_reason=reason, emergency_stop=True,
                          last_market_warning=warning, storage_warning=warning)

    def _roll_day(self):
        today = self._today()
        if self.state["day"] != today:
            current_equity = self.equity()
            if current_equity is None:
                return False
            self.state["day"] = today
            self.state["day_start_equity"] = current_equity
            self.state["daily_realized_pnl"] = 0.0
            return True
        return False

    def equity(self, prices=None, timestamps=None):
        """Return confirmed equity, or None if any open position lacks a fresh mark."""
        with self.lock:
            return self._position_valuation(prices, timestamps)["equity"]

    def _daily_limit_hit(self):
        self._roll_day()
        current_equity = self.equity()
        if current_equity is None:
            return None
        day_start_equity = self.state.get("day_start_equity")
        if day_start_equity is None or day_start_equity <= 0:
            return None
        return current_equity - day_start_equity <= -day_start_equity * self.config.max_daily_loss

    def risk_status(self, prices=None, timestamps=None):
        with self.lock:
            self._roll_day()
            valuation = self._position_valuation(prices, timestamps)
            equity = valuation["equity"]
            day_start_equity = self.state.get("day_start_equity")
            daily_loss_used = (max(0, day_start_equity - equity)
                               if equity is not None and day_start_equity is not None else None)
            entry_block_reason = (self.state["pause_reason"] if self.state["paused"]
                                  else "EMERGENCY_STOP" if self.state["emergency_stop"]
                                  else "RISK_UNAVAILABLE" if not valuation["risk_calculation_available"]
                                  else None)
            return {"paused": self.state["paused"], "pause_reason": self.state["pause_reason"],
                    "emergency_stop": self.state["emergency_stop"],
                    "new_entries_blocked": entry_block_reason is not None,
                    "entry_block_reason": entry_block_reason,
                    "daily_loss_limit": (round(day_start_equity*self.config.max_daily_loss, 2)
                                         if day_start_equity is not None else None),
                    "daily_loss_used": round(daily_loss_used, 2) if daily_loss_used is not None else None,
                    "open_positions": len(self.positions), "max_open_positions": self.config.max_open_positions,
                    "equity": round(equity, 2) if equity is not None else None,
                    "equity_status": "CONFIRMED" if valuation["risk_calculation_available"] else "UNCONFIRMED",
                    "risk_calculation_available": valuation["risk_calculation_available"],
                    "unpriced_positions": valuation["unpriced_positions"],
                    "valuation_warning": valuation["valuation_warning"],
                    "storage_failed": self.storage_failed,
                    "storage_warning": self.state.get("storage_warning") if self.storage_failed else None,
                    "market_warning": self.state.get("last_market_warning")}

    def record_signal(self, signal):
        with self.lock:
            signal_id = signal.get("signal_id")
            if not signal_id or any(item.get("signal_id") == signal_id
                                    for item in self.state.get("signals", [])):
                return False
            previous = deepcopy(self.state)
            self.state["signals"].insert(0, dict(signal))
            self.state["signals"] = self.state["signals"][:1000]
            try:
                if not self.store.persist_signal(signal, self.state):
                    self.state = previous
                    return False
            except SignalStateInconsistency as exc:
                self.state = previous
                self._mark_storage_failure(
                    "SIGNAL_STATE_INCONSISTENT",
                    f"Neusaglašen signal i bot state u SQLite bazi: {exc}; PAPER ulasci su blokirani")
                return False
            except Exception:
                self.state = previous
                self._mark_storage_failure()
                return False
            return True

    def update_signal_result(self, signal_id, result):
        """Attach an execution outcome to a claimed, persisted signal."""
        with self.lock:
            signal = next((item for item in self.state.get("signals", [])
                           if item.get("signal_id") == signal_id), None)
            if signal is None:
                return False
            previous_signal = deepcopy(signal)
            signal.update(dict(result))
            try:
                if not self.store.persist_signal_update(signal_id, signal, self.state):
                    signal.clear()
                    signal.update(previous_signal)
                    self._mark_storage_failure()
                    return False
            except Exception:
                signal.clear()
                signal.update(previous_signal)
                self._mark_storage_failure()
                return False
            return True

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
            market_price = self._valid_price(market_price)
            try:
                data_age_seconds = float(data_age_seconds)
            except (TypeError, ValueError, OverflowError):
                data_age_seconds = None
            try:
                spread_rate = float(spread_rate)
            except (TypeError, ValueError, OverflowError):
                spread_rate = None
            if side not in ("BUY", "SELL") or market_price is None:
                return {"ok": False, "reason": "INVALID_ORDER"}
            if (data_age_seconds is None or not math.isfinite(data_age_seconds)
                    or data_age_seconds < 0 or data_age_seconds > self.config.max_data_age_seconds):
                return {"ok": False, "reason": "STALE_MARKET_DATA"}
            if (spread_rate is None or not math.isfinite(spread_rate)
                    or spread_rate < 0 or spread_rate > self.config.max_spread_rate):
                return {"ok": False, "reason": "SPREAD_LIMIT"}
            daily_limit_hit = self._daily_limit_hit()
            if daily_limit_hit is None:
                return {"ok": False, "reason": "RISK_UNAVAILABLE"}
            if daily_limit_hit:
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
            # Size against the simulated fill and its actual stop distance so spread
            # and slippage cannot push the position beyond the configured risk cap.
            impact = self.config.slippage_rate + spread_rate/2
            fill = market_price*(1+impact if side == "BUY" else 1-impact)
            if not math.isfinite(fill) or fill <= 0:
                return {"ok": False, "reason": "INVALID_PRICE"}
            stop = fill*(1-self.config.stop_loss_pct if side == "BUY" else 1+self.config.stop_loss_pct)
            target = fill*(1+self.config.take_profit_pct if side == "BUY" else 1-self.config.take_profit_pct)
            stop_dist = abs(fill-stop)
            if (not math.isfinite(stop_dist) or stop_dist <= 0 or
                    not math.isfinite(stop) or not math.isfinite(target) or target <= 0):
                return {"ok": False, "reason": "INVALID_PRICE"}
            risk_per_unit = self._stop_loss_risk_per_unit(side, fill, stop)
            if risk_per_unit is None:
                return {"ok": False, "reason": "RISK_UNAVAILABLE"}

            current_equity = self.equity()
            if current_equity is None:
                return {"ok": False, "reason": "RISK_UNAVAILABLE"}
            current_equity = max(0, current_equity)
            risk_cash = current_equity * self.config.risk_per_trade
            open_risk = 0.0
            for position in self.positions.values():
                position_risk = self._stop_loss_risk_per_unit(
                    position["side"], position["entry_price"], position["stop_loss"])
                if position_risk is None or not math.isfinite(position["quantity"]):
                    open_risk = math.inf
                    break
                open_risk += position["quantity"] * position_risk
            allowed_risk = max(0, current_equity*self.config.max_total_risk-open_risk)
            qty_risk = min(risk_cash, allowed_risk) / risk_per_unit
            entry_cost_factor = 1+self.config.fee_rate+self.config.slippage_rate+spread_rate/2
            qty_cash = max(self.balance, 0)*self.config.max_notional_multiple/(market_price*entry_cost_factor)
            quantity = min(qty_risk, qty_cash)
            if not all(math.isfinite(value) for value in (risk_cash, open_risk, allowed_risk,
                                                          qty_risk, qty_cash, quantity)):
                return {"ok": False, "reason": "RISK_UNAVAILABLE"}
            if quantity <= 0:
                return {"ok": False, "reason": "MAX_TOTAL_RISK" if allowed_risk <= 0 else "INSUFFICIENT_SIMULATED_BALANCE"}
            fee = fill*quantity*self.config.fee_rate
            if not math.isfinite(fee):
                return {"ok": False, "reason": "RISK_UNAVAILABLE"}
            previous = deepcopy(self.state)
            self.state["balance"] -= fee
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

    def close_position(self, symbol, market_price, reason="MANUAL", *, signal_id=None,
                       signal_outcome=None):
        with self.lock:
            if self.storage_failed:
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            pos = self.positions.get(symbol)
            if pos is None:
                return {"ok": False, "reason": "NO_POSITION"}
            market_price = self._valid_price(market_price)
            if market_price is None:
                return {"ok": False, "reason": "INVALID_PRICE"}
            signal_record = None
            if signal_id is not None:
                signal_record = next((item for item in self.state.get("signals", [])
                                      if item.get("signal_id") == signal_id), None)
                if signal_record is None or not isinstance(signal_outcome, dict):
                    return {"ok": False, "reason": "SIGNAL_NOT_FOUND"}
            if pos.get("side") not in ("BUY", "SELL"):
                return {"ok": False, "reason": "INVALID_POSITION"}
            impact = self.config.slippage_rate+self.config.assumed_spread_rate/2
            fill = market_price*(1-impact if pos["side"] == "BUY" else 1+impact)
            direction = 1 if pos["side"] == "BUY" else -1
            gross = (fill-pos["entry_price"])*direction*pos["quantity"]
            exit_fee = fill*pos["quantity"]*self.config.fee_rate
            net = gross-exit_fee-pos["entry_fee"]
            new_balance = self.balance + gross - exit_fee
            new_daily_realized_pnl = self.state["daily_realized_pnl"] + net
            total_fees = pos["entry_fee"] + exit_fee
            if not all(math.isfinite(value) for value in (
                    fill, gross, exit_fee, net, new_balance, new_daily_realized_pnl, total_fees,
                    pos["entry_price"], pos["quantity"], pos["entry_fee"])):
                return {"ok": False, "reason": "INVALID_PRICE"}
            previous = deepcopy(self.state)
            self.state["balance"] = new_balance
            self.state["daily_realized_pnl"] = new_daily_realized_pnl
            trade = {"symbol": symbol, "side": pos["side"], "entry_price": round(pos["entry_price"], 6),
                     "exit_price": round(fill, 6), "quantity": round(pos["quantity"], 8),
                     "gross_pnl": round(gross, 6), "fees": round(total_fees, 6),
                     "net_pnl": round(net, 6), "pnl": round(net, 6), "reason": reason,
                     "opened_at": pos["opened_at"], "closed_at": self._now()}
            self.state["trades"].insert(0, trade)
            self.state["trades"] = self.state["trades"][:1000]
            del self.positions[symbol]
            if signal_record is not None:
                signal_record.update(signal_outcome)
            if self._daily_limit_hit():
                self.state.update(paused=True, pause_reason="DAILY_LOSS_LIMIT")
            try:
                if signal_record is not None:
                    self.store.persist_closed_trade(trade, self.state, signal_id=signal_id,
                                                    signal=signal_record)
                else:
                    self.store.persist_closed_trade(trade, self.state)
            except Exception:
                self.state = previous
                self._mark_storage_failure()
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            return {"ok": True, "trade": trade}

    def close_on_reverse_signal(self, symbol, signal_side, market_price, *, data_age_seconds,
                                signal_id, signal_outcome):
        """Atomically close an opposite PAPER position on a fresh valid signal."""
        with self.lock:
            if self.storage_failed:
                return {"ok": False, "reason": "DATABASE_UNAVAILABLE"}
            side = str(signal_side).upper()
            if side not in ("BUY", "SELL"):
                return {"ok": False, "reason": "INVALID_SIGNAL"}
            try:
                data_age_seconds = float(data_age_seconds)
                market_price = float(market_price)
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "reason": "INVALID_MARKET_DATA"}
            if (not math.isfinite(data_age_seconds) or data_age_seconds < 0
                    or data_age_seconds > self.config.max_data_age_seconds):
                return {"ok": False, "reason": "STALE_MARKET_DATA"}
            if not math.isfinite(market_price) or market_price <= 0:
                return {"ok": False, "reason": "INVALID_PRICE"}
            position = self.positions.get(symbol)
            if position is None:
                return {"ok": False, "reason": "NO_POSITION"}
            if position.get("side") == side:
                return {"ok": False, "reason": "POSITION_ALREADY_OPEN"}
            return self.close_position(symbol, market_price, "REVERSE_SIGNAL",
                                        signal_id=signal_id, signal_outcome=signal_outcome)

    def check_exits(self, prices, timestamps=None):
        closed = []
        with self.lock:
            fresh_marks = self._fresh_price_marks(prices, timestamps)
            for symbol, pos in list(self.positions.items()):
                mark = fresh_marks.get(symbol)
                if mark is None:
                    continue
                price = mark["price"]
                if pos["side"] == "BUY":
                    reason = "STOP_LOSS" if price <= pos["stop_loss"] else "TAKE_PROFIT" if price >= pos["take_profit"] else None
                else:
                    reason = "STOP_LOSS" if price >= pos["stop_loss"] else "TAKE_PROFIT" if price <= pos["take_profit"] else None
                if reason:
                    result = self.close_position(symbol, price, reason)
                    if not result.get("ok"):
                        result = {**result, "symbol": symbol}
                    closed.append(result)
            return closed

    def set_market_warning(self, warning):
        with self.lock:
            self.state["last_market_warning"] = warning
            self._persist()

    def set_prices(self, prices, timestamps=None):
        with self.lock:
            now = datetime.now(timezone.utc)
            supplied_timestamps = timestamps or {}
            valid_prices = {}
            valid_timestamps = {}
            for symbol, price in prices.items():
                price = self._valid_price(price)
                if price is None:
                    continue
                source = supplied_timestamps.get(symbol) if hasattr(supplied_timestamps, "get") else None
                timestamp = self._parse_market_timestamp(source)
                if timestamp is None:
                    continue
                age = (now - timestamp).total_seconds()
                if not math.isfinite(age) or age < 0 or age > self.config.max_data_age_seconds:
                    continue
                valid_prices[symbol] = price
                valid_timestamps[symbol] = timestamp.isoformat()
            self.state.setdefault("last_prices", {}).update(valid_prices)
            self.state.setdefault("last_price_timestamps", {}).update(valid_timestamps)
            daily_limit_hit = self._daily_limit_hit()
            if daily_limit_hit is True:
                self.state.update(paused=True, pause_reason="DAILY_LOSS_LIMIT")
            self._persist()

    def emergency_stop(self):
        with self.lock:
            self.state.update(emergency_stop=True, paused=True, pause_reason="EMERGENCY_STOP")
            self._persist()
            return self.risk_status()

    def resume(self):
        with self.lock:
            daily_limit_hit = self._daily_limit_hit()
            if daily_limit_hit is None:
                return {"ok": False, "reason": "RISK_UNAVAILABLE"}
            if daily_limit_hit:
                return {"ok": False, "reason": "DAILY_LOSS_LIMIT_STILL_ACTIVE"}
            self.state.update(paused=False, pause_reason=None, emergency_stop=False)
            self._persist()
            return {"ok": True}

    def snapshot(self, prices=None, timestamps=None):
        with self.lock:
            valuation = self._position_valuation(prices, timestamps)
            positions = deepcopy(self.positions)
            for symbol, position in positions.items():
                position.update(valuation["position_values"][symbol])
            return {"mode": "PAPER_ONLY", "balance": round(self.balance, 2),
                    "equity": round(valuation["equity"], 2) if valuation["equity"] is not None else None,
                    "equity_status": "CONFIRMED" if valuation["risk_calculation_available"] else "UNCONFIRMED",
                    "realized_pnl": round(self.balance-self.state["starting_balance"], 2),
                    "unrealized_pnl": (round(valuation["unrealized_pnl"], 2)
                                       if valuation["unrealized_pnl"] is not None else None),
                    "risk": self.risk_status(prices, timestamps), "positions": positions,
                    "trades": self.state["trades"][:100], "signals": self.state["signals"][:100]}
