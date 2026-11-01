"""Risk controls for the AI bot's paper-trading simulator.
This module never connects to a broker and never sends live orders.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date


@dataclass
class RiskConfig:
    starting_balance: float = 100.0
    risk_per_trade: float = 0.005
    max_daily_loss: float = 0.02
    max_open_positions: int = 2
    max_notional_multiple: float = 1.0
    stop_loss_pct: float = 0.003
    take_profit_pct: float = 0.0045
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0002


@dataclass
class PaperPosition:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    opened_at: str
    entry_fee: float


class PaperRiskEngine:
    """Deterministic risk and fill engine for simulated trades."""

    def __init__(self, config=None):
        self.config = config or RiskConfig()
        self.balance = float(self.config.starting_balance)
        self.positions = {}
        self.closed_trades = []
        self.day = datetime.now(timezone.utc).date()
        self.day_start_equity = self.balance
        self.daily_realized_pnl = 0.0
        self.paused = False
        self.pause_reason = None

    def _roll_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.day_start_equity = self.equity({})
            self.daily_realized_pnl = 0.0

    def equity(self, prices):
        unrealized = 0.0
        for symbol, pos in self.positions.items():
            price = prices.get(symbol, pos.entry_price)
            direction = 1 if pos.side == "BUY" else -1
            unrealized += (price - pos.entry_price) * direction * pos.quantity
        return self.balance + unrealized

    def risk_status(self, prices=None):
        self._roll_day()
        limit = self.day_start_equity * self.config.max_daily_loss
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "daily_loss_limit": round(limit, 2),
            "daily_loss_used": round(max(0.0, -self.daily_realized_pnl), 2),
            "open_positions": len(self.positions),
            "max_open_positions": self.config.max_open_positions,
            "equity": round(self.equity(prices or {}), 2),
        }

    def open_position(self, symbol, side, market_price):
        self._roll_day()
        side = str(side).upper()
        if self.paused:
            return {"ok": False, "reason": self.pause_reason or "BOT_PAUSED"}
        if side not in ("BUY", "SELL") or market_price <= 0:
            return {"ok": False, "reason": "INVALID_ORDER"}
        if symbol in self.positions:
            return {"ok": False, "reason": "POSITION_ALREADY_OPEN"}
        if len(self.positions) >= self.config.max_open_positions:
            return {"ok": False, "reason": "MAX_OPEN_POSITIONS"}
        limit = self.day_start_equity * self.config.max_daily_loss
        if self.daily_realized_pnl <= -limit:
            self.paused, self.pause_reason = True, "DAILY_LOSS_LIMIT"
            return {"ok": False, "reason": "DAILY_LOSS_LIMIT"}

        stop_distance = market_price * self.config.stop_loss_pct
        risk_cash = max(0.0, self.equity({})) * self.config.risk_per_trade
        qty_risk = risk_cash / stop_distance
        qty_cash = max(self.balance, 0.0) * self.config.max_notional_multiple / market_price
        quantity = min(qty_risk, qty_cash)
        if quantity <= 0:
            return {"ok": False, "reason": "INSUFFICIENT_SIMULATED_BALANCE"}

        slip = self.config.slippage_rate
        fill = market_price * (1 + slip if side == "BUY" else 1 - slip)
        fee = fill * quantity * self.config.fee_rate
        self.balance -= fee
        if side == "BUY":
            stop, target = fill * (1-self.config.stop_loss_pct), fill * (1+self.config.take_profit_pct)
        else:
            stop, target = fill * (1+self.config.stop_loss_pct), fill * (1-self.config.take_profit_pct)
        pos = PaperPosition(symbol, side, fill, quantity, stop, target,
                            datetime.now(timezone.utc).isoformat(), fee)
        self.positions[symbol] = pos
        return {"ok": True, "position": asdict(pos)}

    def close_position(self, symbol, market_price, reason="MANUAL"):
        pos = self.positions.pop(symbol, None)
        if pos is None or market_price <= 0:
            return {"ok": False, "reason": "NO_POSITION"}
        slip = self.config.slippage_rate
        fill = market_price * (1-slip if pos.side == "BUY" else 1+slip)
        direction = 1 if pos.side == "BUY" else -1
        gross = (fill-pos.entry_price) * direction * pos.quantity
        exit_fee = fill * pos.quantity * self.config.fee_rate
        net = gross - exit_fee - pos.entry_fee
        self.balance += gross - exit_fee
        self.daily_realized_pnl += net
        trade = {
            "symbol": symbol, "side": pos.side,
            "entry_price": round(pos.entry_price, 4), "exit_price": round(fill, 4),
            "quantity": round(pos.quantity, 6), "gross_pnl": round(gross, 4),
            "fees": round(pos.entry_fee+exit_fee, 4), "net_pnl": round(net, 4),
            "reason": reason, "opened_at": pos.opened_at,
            "closed_at": datetime.now(timezone.utc).isoformat()
        }
        self.closed_trades.insert(0, trade)
        return {"ok": True, "trade": trade}

    def check_exits(self, prices):
        results = []
        for symbol, pos in list(self.positions.items()):
            price = prices.get(symbol)
            if price is None:
                continue
            if pos.side == "BUY":
                if price <= pos.stop_loss:
                    results.append(self.close_position(symbol, price, "STOP_LOSS"))
                elif price >= pos.take_profit:
                    results.append(self.close_position(symbol, price, "TAKE_PROFIT"))
            else:
                if price >= pos.stop_loss:
                    results.append(self.close_position(symbol, price, "STOP_LOSS"))
                elif price <= pos.take_profit:
                    results.append(self.close_position(symbol, price, "TAKE_PROFIT"))
        self._roll_day()
        if self.daily_realized_pnl <= -(self.day_start_equity*self.config.max_daily_loss):
            self.paused, self.pause_reason = True, "DAILY_LOSS_LIMIT"
        return results

    def resume(self):
        self._roll_day()
        if self.daily_realized_pnl <= -(self.day_start_equity*self.config.max_daily_loss):
            return {"ok": False, "reason": "DAILY_LOSS_LIMIT_STILL_ACTIVE"}
        self.paused, self.pause_reason = False, None
        return {"ok": True}

    def snapshot(self, prices=None):
        prices = prices or {}
        return {
            "mode": "PAPER_ONLY",
            "balance": round(self.balance, 2),
            "equity": round(self.equity(prices), 2),
            "risk": self.risk_status(prices),
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "closed_trades": self.closed_trades[:100]
        }
