from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string, request
import builtins
import hashlib
import hmac
import math
import os
import sys
import threading
import time
import pandas as pd
import yfinance as yf

from database import PaperStore
from risk_engine import PaperRiskEngine, RiskConfig
from runtime_config import validate_runtime_environment

RUNTIME_CONFIG = validate_runtime_environment()
app = Flask(__name__)
SYMBOLS = {"US100": "^NDX", "US500": "^GSPC"}
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "60"))
PAPER_CURRENCY = os.environ.get("PAPER_CURRENCY", "EUR").strip().upper() or "EUR"
config = RiskConfig(
    starting_balance=float(os.environ.get("PAPER_STARTING_BALANCE", "100")),
    risk_per_trade=float(os.environ.get("RISK_PER_TRADE", "0.005")),
    max_daily_loss=float(os.environ.get("MAX_DAILY_LOSS", "0.02")),
    max_total_risk=float(os.environ.get("MAX_TOTAL_RISK", "0.01")),
    max_open_positions=int(os.environ.get("MAX_OPEN_POSITIONS", "2")),
    max_spread_rate=float(os.environ.get("MAX_SPREAD_RATE", "0.001")),
    assumed_spread_rate=float(os.environ.get("PAPER_ASSUMED_SPREAD_RATE", "0.0002")),
    max_data_age_seconds=int(os.environ.get("MAX_DATA_AGE_SECONDS", "180")),
    max_notional_multiple=float(os.environ.get("MAX_NOTIONAL_MULTIPLE", "1.0")),
    stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "0.003")),
    take_profit_pct=float(os.environ.get("TAKE_PROFIT_PCT", "0.0045")),
    fee_rate=float(os.environ.get("PAPER_FEE_RATE", "0.0005")),
    slippage_rate=float(os.environ.get("PAPER_SLIPPAGE_RATE", "0.0002")),
)
store = PaperStore()
engine = PaperRiskEngine(store, config)
markets = {}
market_lock = threading.RLock()
monitor_started = False
_MONITOR_REGISTRY_NAME = "_paper_trading_monitor_registry"
if not hasattr(builtins, _MONITOR_REGISTRY_NAME):
    setattr(builtins, _MONITOR_REGISTRY_NAME, {"lock": threading.Lock(), "thread": None,
                                               "started": False, "cycle": None, "interval": REFRESH_SECONDS})
_monitor_registry = getattr(builtins, _MONITOR_REGISTRY_NAME)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def calculate_rsi(series, period=14):
    delta = series.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = -delta.clip(upper=0).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - 100/(1+rs)


def analyze_market(symbol, ticker):
    try:
        df = yf.download(ticker, period="5d", interval="1m", progress=False, auto_adjust=True, threads=False)
        if df.empty:
            return {"symbol": symbol, "signal": "WAIT", "error": "No market data", "updated": None}
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 35:
            return {"symbol": symbol, "signal": "WAIT", "error": "Not enough candles", "updated": None}
        ema9, ema21 = close.ewm(span=9, adjust=False).mean(), close.ewm(span=21, adjust=False).mean()
        rsi = calculate_rsi(close)
        macd = close.ewm(span=12, adjust=False).mean()-close.ewm(span=26, adjust=False).mean()
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        timestamp = pd.Timestamp(close.index[-1])
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        updated = timestamp.tz_convert("UTC").isoformat()
        age = max(0, (datetime.now(timezone.utc)-datetime.fromisoformat(updated)).total_seconds())
        rv = float(rsi.iloc[-1])
        signal = "WAIT"
        if pd.notna(rv):
            if ema9.iloc[-1] > ema21.iloc[-1] and macd.iloc[-1] > macd_signal.iloc[-1] and 50 < rv < 70:
                signal = "BUY"
            elif ema9.iloc[-1] < ema21.iloc[-1] and macd.iloc[-1] < macd_signal.iloc[-1] and 30 < rv < 50:
                signal = "SELL"
        return {"symbol": symbol, "price": float(close.iloc[-1]), "signal": signal,
                "rsi": round(rv, 2) if pd.notna(rv) else None, "ema9": float(ema9.iloc[-1]),
                "ema21": float(ema21.iloc[-1]), "macd": float(macd.iloc[-1]),
                "updated": updated, "data_age_seconds": age, "spread_rate": config.assumed_spread_rate,
                "spread_source": "configured paper estimate (yfinance does not provide bid/ask)"}
    except Exception as exc:
        return {"symbol": symbol, "signal": "WAIT", "error": str(exc), "updated": None}


def fresh_market_quote(market):
    """Return a normalized quote only when both price and timestamp are trustworthy."""
    if market.get("error"):
        return None
    try:
        price = float(market.get("price"))
        age = float(market.get("data_age_seconds"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0 or not math.isfinite(age) or age < 0:
        return None
    if age > config.max_data_age_seconds:
        return None
    return price, age


def process_market_cycle():
    results = {s: analyze_market(s, ticker) for s, ticker in SYMBOLS.items()}
    fresh_quotes = {symbol: quote for symbol, market in results.items()
                    if (quote := fresh_market_quote(market)) is not None}
    fresh_prices = {symbol: quote[0] for symbol, quote in fresh_quotes.items()}
    stale_symbols = [symbol for symbol in results if symbol not in fresh_quotes]
    with market_lock:
        markets.clear()
        markets.update(results)
    engine.set_prices(fresh_prices)
    engine.check_exits(fresh_prices)
    if stale_symbols:
        affected_positions = sorted(set(stale_symbols).intersection(engine.positions))
        warning = (f"Stale or unavailable market data for {', '.join(stale_symbols)}; "
                   "new entries and automatic exits blocked for affected instruments")
        if affected_positions:
            warning += f"; open positions require attention: {', '.join(affected_positions)}"
        engine.set_market_warning(warning)
    for symbol, market in results.items():
        if symbol not in fresh_quotes:
            continue
        if market.get("signal") not in ("BUY", "SELL"):
            continue
        signal_id = hashlib.sha256(f"{symbol}|{market['updated']}|{market['signal']}".encode()).hexdigest()
        signal = {**market, "signal_id": signal_id, "created_at": utc_now()}
        engine.record_signal(signal)
        price, age = fresh_quotes[symbol]
        engine.open_position(symbol, market["signal"], price, signal_id=signal_id,
                             spread_rate=market.get("spread_rate"), data_age_seconds=age)
    if stale_symbols:
        return
    engine.set_market_warning(None)


def market_monitor():
    while True:
        try:
            process_market_cycle()
        except Exception as exc:
            engine.set_market_warning(f"Market monitor error: {exc}")
        time.sleep(REFRESH_SECONDS)


def _monitor_dispatch_loop():
    """Process singleton loop that follows current module globals after reloads."""
    registry = getattr(builtins, _MONITOR_REGISTRY_NAME)
    while True:
        cycle = registry.get("cycle")
        if cycle is not None:
            try:
                cycle()
            except Exception as exc:
                registry["on_error"](exc)
        time.sleep(max(1, int(registry.get("interval", 60))))


DASHBOARD = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Trading Bot V3</title>
<style>body{font-family:Arial;background:#101418;color:#f1f5f9;margin:0;padding:18px}h1{font-size:24px}.muted{color:#9ca3af}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.card{background:#1c252d;border:1px solid #34414c;border-radius:12px;padding:14px;margin:10px 0}.value{font-size:23px;font-weight:bold;margin-top:7px}.buy{color:#4ade80}.sell{color:#fb7185}.warning{color:#fbbf24}</style></head><body>
<h1>AI Trading Bot V3</h1><p class="muted">PAPER TRADING ONLY · simulacija · osvežavanje na 60 sekundi</p><div id="warning" class="warning"></div>
<div class="grid" id="summary"></div><div class="card"><h3>Tržišni signali</h3><div id="markets"></div></div><div class="card"><h3>Otvorene pozicije</h3><div id="positions"></div></div><div class="card"><h3>Poslednje trgovine</h3><div id="trades"></div></div><p class="muted" id="updated"></p>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){try{const r=await fetch('/status');const d=await r.json();const money=n=>Number(n||0).toFixed(2)+' '+(d.currency||'EUR');
document.getElementById('summary').innerHTML=[['Stanje',money(d.balance)],['Vrednost računa',money(d.equity)],['Realizovani P/L',money(d.realized_pnl)],['Nerealizovani P/L',money(d.unrealized_pnl)],['Režim',d.mode],['Emergency STOP',d.risk?.emergency_stop?'AKTIVAN':'isključen']].map(x=>'<div class="card"><div class="muted">'+esc(x[0])+'</div><div class="value">'+esc(x[1])+'</div></div>').join('');
document.getElementById('warning').textContent=d.risk?.market_warning||d.risk?.pause_reason||'';
document.getElementById('markets').innerHTML=Object.values(d.markets).map(m=>'<p><b>'+esc(m.symbol)+'</b> · '+esc(m.price??'—')+' · <b class="'+(m.signal==='BUY'?'buy':m.signal==='SELL'?'sell':'')+'">'+esc(m.signal)+'</b> · RSI '+esc(m.rsi??'—')+(m.error?' · '+esc(m.error):'')+(m.data_age_seconds!=null?' · starost '+Math.round(m.data_age_seconds)+'s':'')+'</p>').join('')||'Nema podataka';
document.getElementById('positions').innerHTML=Object.values(d.positions).map(p=>'<p><b>'+esc(p.symbol)+' '+esc(p.side)+'</b> · ulaz '+esc(p.entry_price)+' · količina '+esc(p.quantity)+'<br>Stop Loss: '+esc(p.stop_loss)+' · Take Profit: '+esc(p.take_profit)+'</p>').join('')||'Nema otvorenih pozicija';
document.getElementById('trades').innerHTML=d.trades.length?'<table><tr><th>Instrument</th><th>Strana</th><th>P/L</th><th>Razlog</th></tr>'+d.trades.slice(0,15).map(t=>'<tr><td>'+esc(t.symbol)+'</td><td>'+esc(t.side)+'</td><td>'+money(t.pnl)+'</td><td>'+esc(t.reason)+'</td></tr>').join('')+'</table>':'Još nema zatvorenih trgovina';document.getElementById('updated').textContent='Poslednje ažuriranje: '+(d.last_update||'čeka se')+' · Status: '+d.status;
}catch(e){document.getElementById('updated').textContent='Greška pri učitavanju: '+e}}load();setInterval(load,15000);
</script></body></html>"""


@app.route("/")
@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD)


@app.route("/status")
def status():
    with market_lock:
        current = {k: dict(v) for k, v in markets.items()}
    prices = {symbol: quote[0] for symbol, market in current.items()
              if (quote := fresh_market_quote(market)) is not None}
    data = engine.snapshot(prices)
    data.update({"name": "AI Trading Bot V3", "mode": "PAPER TRADING ONLY", "status": "RUNNING" if monitor_started else "STARTING",
                 "starting_capital": engine.state["starting_balance"], "markets": current,
                 "currency": PAPER_CURRENCY,
                 "positions": data["positions"], "last_update": max((m.get("updated") or "" for m in current.values()), default=None),
                 "last_error": next((m["error"] for m in current.values() if m.get("error")), None)})
    return jsonify(data)


@app.route("/signals")
def signals():
    with market_lock:
        return jsonify(markets.copy())


@app.route("/positions")
def positions():
    return jsonify(engine.snapshot()["positions"])


@app.route("/trades")
def trades():
    return jsonify(engine.snapshot()["trades"])


@app.route("/health")
def health():
    risk = engine.risk_status()
    with market_lock:
        current = list(markets.values())
    has_fresh_market = any(fresh_market_quote(market) is not None for market in current)
    status = "starting" if not monitor_started else "degraded" if risk.get("market_warning") or not has_fresh_market else "healthy"
    return jsonify({"status": status, "mode": "PAPER_ONLY", "risk": risk})


def paper_result_status(result):
    if result.get("ok"):
        return 200
    return 503 if result.get("reason") == "DATABASE_UNAVAILABLE" else 409


def require_paper_token():
    token = os.environ.get("PAPER_API_TOKEN")
    if not token:
        return False
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied, f"Bearer {token}")


@app.post("/paper/emergency-stop")
def emergency_stop():
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    return jsonify({"ok": True, "risk": engine.emergency_stop()})


@app.post("/paper/resume")
def resume_paper():
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    result = engine.resume()
    return jsonify(result), paper_result_status(result)


@app.post("/paper/orders")
def manual_paper_order():
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    body = request.get_json(silent=True) or {}
    market = markets.get(body.get("symbol"), {})
    quote = fresh_market_quote(market)
    if quote is None:
        return jsonify({"ok": False, "reason": "STALE_MARKET_DATA"}), 409
    result = engine.open_position(body.get("symbol"), body.get("side"), quote[0],
                                 signal_id=body.get("signal_id"), spread_rate=market.get("spread_rate"),
                                 data_age_seconds=quote[1])
    return jsonify(result), paper_result_status(result)


@app.post("/paper/positions/<symbol>/close")
def close_paper_position(symbol):
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    market = markets.get(symbol, {})
    quote = fresh_market_quote(market)
    if quote is None:
        return jsonify({"ok": False, "reason": "STALE_MARKET_DATA"}), 409
    result = engine.close_position(symbol, quote[0], "MANUAL")
    return jsonify(result), paper_result_status(result)


def start_monitor():
    global monitor_started
    registry = _monitor_registry
    with registry["lock"]:
        registry["cycle"] = process_market_cycle
        registry["interval"] = REFRESH_SECONDS
        registry["on_error"] = lambda exc: engine.set_market_warning(f"Market monitor error: {exc}")
        existing = registry.get("thread")
        if existing is not None and existing.is_alive():
            registry["started"] = True
            monitor_started = True
            return
        registry["started"] = True
        monitor_started = True
        thread = threading.Thread(target=_monitor_dispatch_loop, daemon=True, name="paper-market-monitor")
        registry["thread"] = thread
        thread.start()


@app.before_request
def ensure_monitor_started():
    start_monitor()


if __name__ == "__main__":
    start_monitor()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), use_reloader=False)
