from datetime import datetime, timedelta, timezone
import json
from flask import Flask, jsonify, render_template_string, request
import builtins
import hashlib
import io
import sqlite3
import tempfile
import hmac
import math
import os
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import pandas as pd
import yfinance as yf

from database import PaperStore
from market_data import MarketDataCache, is_rate_limit_error
from risk_engine import PaperRiskEngine, RiskConfig
from runtime_config import validate_runtime_environment

RUNTIME_CONFIG = validate_runtime_environment()
app = Flask(__name__)

@app.after_request
def prevent_response_caching(response):
    """Keep live status, risk, and paper-operation responses out of HTTP caches."""
    response.headers.setdefault("Cache-Control", "no-store")
    return response


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
market_data_cache = MarketDataCache(
    max_data_age_seconds=config.max_data_age_seconds,
    min_fetch_interval_seconds=float(os.environ.get("YF_MIN_FETCH_INTERVAL_SECONDS", "60")),
    backoff_base_seconds=float(os.environ.get("YF_BACKOFF_BASE_SECONDS", "60")),
    backoff_max_seconds=float(os.environ.get("YF_BACKOFF_MAX_SECONDS", "900")),
)
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


def _fetch_massive_history(symbol):
    """Fetch index minute bars from Massive when an API key is explicitly configured."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    massive_ticker = {"US100": "I:NDX", "US500": "I:SPX"}.get(symbol)
    if not api_key or not massive_ticker:
        return None, None
    if os.environ.get("MASSIVE_STRATEGY_LICENSE_CONFIRMED", "").strip().lower() != "true":
        return None, "Massive fallback disabled until data-use rights are confirmed"
    if os.environ.get("MASSIVE_PROVIDER_COSTS_CONFIRMED", "").strip().lower() != "true":
        return None, "Massive fallback disabled until provider costs are explicitly confirmed"

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=5)
    url = (f"https://api.massive.com/v2/aggs/ticker/{quote(massive_ticker, safe='')}"
           f"/range/1/minute/{start_date.isoformat()}/{end_date.isoformat()}"
           "?adjusted=false&sort=asc&limit=50000")
    request = Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return None, f"Massive HTTP {exc.code}"
    except (URLError, TimeoutError, OSError, ValueError):
        return None, "Massive request failed"

    if not isinstance(payload, dict):
        return None, "Massive returned an invalid response"
    bars = payload.get("results")
    if payload.get("status") != "OK" or not isinstance(bars, list) or not bars:
        return None, "Massive returned no index bars"
    try:
        frame = pd.DataFrame(bars)
        frame["Close"] = pd.to_numeric(frame["c"], errors="coerce")
        frame.index = pd.to_datetime(frame["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
        frame = frame[["Close"]].dropna()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "Massive returned invalid index bars"
    return frame, None


def _analyze_market_uncached(symbol, ticker):
    # Two-minute bars reduce Yahoo's dependence on the shortest intraday history.
    # If Yahoo has no usable bars, use the configured independent index provider.
    ticker_client = yf.Ticker(ticker)
    df = None
    data_interval = None
    data_source = None
    yahoo_error = None
    for interval in ("2m", "1m"):
        try:
            candidate = ticker_client.history(period="5d", interval=interval, actions=False,
                                              auto_adjust=True, timeout=10)
        except Exception as exc:
            if is_rate_limit_error(exc):
                raise
            yahoo_error = f"Yahoo request failed ({type(exc).__name__})"
            continue
        if candidate.empty:
            continue
        candidate_close = candidate["Close"]
        if isinstance(candidate_close, pd.DataFrame):
            candidate_close = candidate_close.iloc[:, 0]
        if len(candidate_close.dropna()) < 35:
            yahoo_error = "Yahoo returned fewer than 35 candles"
            continue
        df = candidate
        data_interval = interval
        data_source = "Yahoo Finance"
        break
    if df is None:
        df, massive_error = _fetch_massive_history(symbol)
        if df is not None:
            data_interval = "1m"
            data_source = "Massive"
        else:
            error = massive_error or yahoo_error or "No market data from Yahoo Finance or Massive"
            return {"symbol": symbol, "signal": "WAIT", "error": error, "updated": None}
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
            "data_interval": data_interval, "data_source": data_source,
            "rsi": round(rv, 2) if pd.notna(rv) else None, "ema9": float(ema9.iloc[-1]),
            "ema21": float(ema21.iloc[-1]), "macd": float(macd.iloc[-1]),
            "updated": updated, "data_age_seconds": age, "spread_rate": config.assumed_spread_rate,
            "spread_source": "configured paper estimate (yfinance does not provide bid/ask)"}


def analyze_market(symbol, ticker):
    return market_data_cache.get(symbol, lambda: _analyze_market_uncached(symbol, ticker))


def market_data_age_seconds(market, now=None):
    """Calculate age from the source candle timestamp, never a stored age snapshot."""
    updated = market.get("updated")
    if not updated:
        return None
    try:
        if isinstance(updated, datetime):
            timestamp = updated
        else:
            timestamp = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = (current.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        return age if age >= 0 else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _suppress_unfresh_signal(snapshot):
    signal = snapshot.get("signal")
    if signal in ("BUY", "SELL"):
        snapshot["last_known_signal"] = signal
    else:
        snapshot.pop("last_known_signal", None)
    snapshot["signal"] = "WAIT"


def current_market_snapshot(market, now=None):
    """Copy a market result with age/status recalculated for the current request."""
    snapshot = dict(market)
    age = market_data_age_seconds(snapshot, now)
    snapshot["data_age_seconds"] = age
    if age is None:
        if snapshot.get("price") is not None:
            snapshot["last_known_price"] = snapshot["price"]
            snapshot["price"] = None
        if snapshot.get("updated"):
            snapshot["data_status"] = "invalid"
            snapshot["error"] = "Market data timestamp is invalid or in the future"
        else:
            snapshot["data_status"] = "unavailable"
        _suppress_unfresh_signal(snapshot)
        return snapshot
    if age > config.max_data_age_seconds:
        if snapshot.get("price") is not None:
            snapshot["last_known_price"] = snapshot["price"]
            snapshot["price"] = None
        snapshot["data_status"] = "stale"
        snapshot["error"] = "Stale cached market data"
    else:
        snapshot.pop("last_known_price", None)
        snapshot["data_status"] = "fresh"
        if snapshot.get("error") in ("Stale cached market data",
                                      "Market data timestamp is invalid or in the future",
                                      "Market data timestamp is in the future"):
            snapshot["error"] = None
    if fresh_market_quote(snapshot, now) is None:
        _suppress_unfresh_signal(snapshot)
    else:
        snapshot.pop("last_known_signal", None)
    return snapshot


def fresh_market_quote(market, now=None):
    """Return a quote only when its source candle is fresh at this exact check."""
    if market.get("error") and market.get("error") != "Stale cached market data":
        return None
    try:
        price = float(market.get("price"))
    except (TypeError, ValueError, OverflowError):
        return None
    age = market_data_age_seconds(market, now)
    if age is None:
        return None
    if not math.isfinite(price) or price <= 0 or not math.isfinite(age) or age < 0:
        return None
    if age > config.max_data_age_seconds:
        return None
    return price, age


def process_market_cycle():
    raw_results = {s: analyze_market(s, ticker) for s, ticker in SYMBOLS.items()}
    cycle_now = datetime.now(timezone.utc)
    results = {symbol: current_market_snapshot(market, cycle_now)
               for symbol, market in raw_results.items()}
    fresh_quotes = {symbol: quote for symbol, market in results.items()
                    if (quote := fresh_market_quote(market)) is not None}
    fresh_prices = {symbol: quote[0] for symbol, quote in fresh_quotes.items()}
    fresh_price_timestamps = {symbol: results[symbol].get("updated") for symbol in fresh_quotes}
    with market_lock:
        markets.clear()
        markets.update(results)
    engine.set_prices(fresh_prices, timestamps=fresh_price_timestamps)
    exit_quotes = {symbol: quote for symbol, market in results.items()
                   if (quote := fresh_market_quote(market)) is not None}
    exit_prices = {symbol: quote[0] for symbol, quote in exit_quotes.items()}
    exit_timestamps = {symbol: results[symbol].get("updated") for symbol in exit_quotes}
    exit_results = engine.check_exits(exit_prices, timestamps=exit_timestamps)
    closed_symbols_this_cycle = {result["trade"]["symbol"] for result in exit_results
                                 if result.get("ok") and result.get("trade", {}).get("symbol")}
    stale_symbols = [symbol for symbol, market in results.items()
                     if fresh_market_quote(market) is None]
    warning_parts = []
    for result in exit_results:
        if not result.get("ok"):
            warning_parts.append(
                f"Automatic close failed for {result.get('symbol', 'unknown')}: "
                f"{result.get('reason') or 'CLOSE_FAILED'}")
    fetch_error_symbols = [symbol for symbol, market in results.items() if market.get("fetch_error")]
    if fetch_error_symbols:
        warning_parts.append(f"Yahoo Finance fetch error for {', '.join(fetch_error_symbols)}; cached quotes usable only while fresh")
    if stale_symbols:
        affected_positions = sorted(set(stale_symbols).intersection(engine.positions))
        warning_parts.append(f"Stale or unavailable market data for {', '.join(stale_symbols)}; "
                             "new entries and automatic exits blocked for affected instruments")
        if affected_positions:
            warning_parts.append(f"open positions require attention: {', '.join(affected_positions)}")
    known_signals = {item.get("signal_id"): item for item in engine.state.get("signals", [])
                     if item.get("signal_id")}
    for symbol, market in results.items():
        if market.get("signal") not in ("BUY", "SELL"):
            continue
        quote = fresh_market_quote(market)
        if quote is None:
            continue
        signal_id = hashlib.sha256(f"{symbol}|{market['updated']}|{market['signal']}".encode()).hexdigest()
        if signal_id in known_signals:
            previous_signal = known_signals[signal_id]
            for field in ("execution_action", "execution_status", "execution_reason"):
                if field in previous_signal:
                    market[field] = previous_signal[field]
            if previous_signal.get("execution_status") == "FAILED":
                warning_parts.append(f"Reverse-signal close failed for {symbol}: "
                                     f"{previous_signal.get('execution_reason') or 'CLOSE_FAILED'}")
            continue
        signal = {**market, "signal_id": signal_id, "created_at": utc_now()}
        if not engine.record_signal(signal):
            continue
        known_signals[signal_id] = signal
        quote = fresh_market_quote(market)
        if quote is None:
            continue
        price, age = quote
        position = engine.positions.get(symbol)
        signal_result_persisted = False
        if symbol in closed_symbols_this_cycle:
            outcome = {"execution_action": "ENTRY_SKIPPED", "execution_status": "SKIPPED",
                       "execution_reason": "POSITION_CLOSED_THIS_CYCLE"}
        elif position and position.get("side") != market["signal"]:
            reverse_outcome = {"execution_action": "REVERSE_EXIT", "execution_status": "CLOSED",
                               "execution_reason": "REVERSE_SIGNAL"}
            close_result = engine.close_on_reverse_signal(
                symbol, market["signal"], price, data_age_seconds=age, signal_id=signal_id,
                signal_outcome=reverse_outcome)
            if close_result.get("ok"):
                closed_symbols_this_cycle.add(symbol)
                outcome = reverse_outcome
                signal_result_persisted = True
            else:
                reason = close_result.get("reason") or "CLOSE_FAILED"
                outcome = {"execution_action": "REVERSE_EXIT", "execution_status": "FAILED",
                           "execution_reason": reason}
                warning_parts.append(f"Reverse-signal close failed for {symbol}: {reason}")
        elif position:
            continue
        else:
            result = engine.open_position(symbol, market["signal"], price, signal_id=signal_id,
                                          spread_rate=market.get("spread_rate"), data_age_seconds=age)
            if result.get("ok"):
                continue
            outcome = {"execution_action": "ENTRY", "execution_status": "REJECTED",
                       "execution_reason": result.get("reason") or "ORDER_REJECTED"}
        market.update(outcome)
        signal.update(outcome)
        if not signal_result_persisted:
            engine.update_signal_result(signal_id, outcome)
    engine.set_market_warning("; ".join(warning_parts) if warning_parts else None)


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
<style>body{font-family:Arial;background:#101418;color:#f1f5f9;margin:0;padding:18px}h1{font-size:24px}.muted{color:#9ca3af}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.card{background:#1c252d;border:1px solid #34414c;border-radius:12px;padding:14px;margin:10px 0}.value{font-size:23px;font-weight:bold;margin-top:7px}.buy{color:#4ade80}.sell{color:#fb7185}.warning{color:#fbbf24}input,button{font:inherit;padding:10px;border-radius:6px;border:1px solid #526170}button{cursor:pointer;background:#34414c;color:#f1f5f9;margin-left:6px}input{background:#101418;color:#f1f5f9;min-width:min(360px,80vw)}[hidden]{display:none!important}</style></head><body>
<h1>AI Trading Bot V3</h1><p class="muted">PAPER TRADING ONLY · simulacija · osvežavanje na 60 sekundi</p>
<div class="card" id="access"><h3>Pristup PAPER dashboard-u</h3><p class="muted">Unesite PAPER API token. Token ostaje samo u ovoj sesiji pregledača.</p><form id="loginForm"><input id="token" type="password" autocomplete="current-password" required aria-label="PAPER API token"><button type="submit">Otvori dashboard</button></form><p id="authError" class="warning"></p></div>
<main id="dashboard" hidden><button id="logout" type="button">Zaključaj dashboard</button><p id="warning" class="warning"></p>
<div class="grid" id="summary"></div><div class="card"><h3>Tržišni signali</h3><div id="markets"></div></div><div class="card"><h3>Otvorene pozicije</h3><div id="positions"></div></div><div class="card"><h3>Poslednje trgovine</h3><div id="trades"></div></div><p class="muted" id="updated"></p></main>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function showAuth(message=''){document.getElementById('access').hidden=false;document.getElementById('dashboard').hidden=true;document.getElementById('authError').textContent=message}
async function load(){const token=sessionStorage.getItem('paperApiToken');if(!token){showAuth('');return}try{const r=await fetch('/status',{headers:{Authorization:'Bearer '+token}});if(r.status===401){sessionStorage.removeItem('paperApiToken');showAuth('Token nije prihvaćen');return}if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json();const money=n=>n==null?'Privremeno nedostupno':Number(n).toFixed(2)+' '+(d.currency||'EUR');
document.getElementById('access').hidden=true;document.getElementById('dashboard').hidden=false;
document.getElementById('summary').innerHTML=[['Stanje',money(d.balance)],['Vrednost računa',money(d.equity)],['Realizovani P/L',money(d.realized_pnl)],['Nerealizovani P/L',money(d.unrealized_pnl)],['Režim',d.mode],['Emergency STOP',d.risk?.emergency_stop?'AKTIVAN':'isključen']].map(x=>'<div class="card"><div class="muted">'+esc(x[0])+'</div><div class="value">'+esc(x[1])+'</div></div>').join('');
document.getElementById('warning').textContent=[d.risk?.storage_warning,d.risk?.market_warning,d.risk?.valuation_warning,d.risk?.pause_reason].filter(Boolean).join(' · ');
document.getElementById('markets').innerHTML=Object.values(d.markets).map(m=>'<p><b>'+esc(m.symbol)+'</b> · '+(m.price!=null?esc(m.price):m.last_known_price!=null?'poslednja poznata: '+esc(m.last_known_price):'cena nedostupna')+' · <b class="'+(m.signal==='BUY'?'buy':m.signal==='SELL'?'sell':'')+'">'+esc(m.signal)+'</b> · RSI '+esc(m.rsi??'—')+(m.data_age_seconds!=null?' · starost cene '+Math.round(m.data_age_seconds)+'s':' · starost cene nepoznata')+(m.data_source?' · izvor '+esc(m.data_source):'')+(m.data_interval?' · sveća '+esc(m.data_interval):'')+(m.error?' · '+esc(m.error):'')+(m.fetch_error?' · poslednja greška: '+esc(m.fetch_error):'')+(m.retry_after_seconds>0?' · sledeći pokušaj za '+Math.ceil(m.retry_after_seconds)+'s':'')+(m.last_success_at?' · poslednji uspeh '+esc(m.last_success_at):'')+(m.execution_status?' · izvršenje: '+esc(m.execution_status)+(m.execution_reason?' ('+esc(m.execution_reason)+')':''):'')+'</p>').join('')||'Nema podataka';
document.getElementById('positions').innerHTML=Object.values(d.positions).map(p=>'<p><b>'+esc(p.symbol)+' '+esc(p.side)+'</b> · ulaz '+esc(p.entry_price)+' · količina '+esc(p.quantity)+'<br>Stop Loss: '+esc(p.stop_loss)+' · Take Profit: '+esc(p.take_profit)+'<br>Trenutna cena: '+(p.current_price==null?'nepotvrđena':esc(Number(p.current_price).toFixed(2)))+' · Nerealizovani P/L: '+esc(money(p.unrealized_pnl))+(p.valuation_warning?'<br><span class="warning">'+esc(p.valuation_warning)+'</span>':'')+'</p>').join('')||'Nema otvorenih pozicija';
document.getElementById('trades').innerHTML=d.trades.length?'<table><tr><th>Instrument</th><th>Strana</th><th>P/L</th><th>Razlog</th></tr>'+d.trades.slice(0,15).map(t=>'<tr><td>'+esc(t.symbol)+'</td><td>'+esc(t.side)+'</td><td>'+money(t.pnl)+'</td><td>'+esc(t.reason)+'</td></tr>').join('')+'</table>':'Još nema zatvorenih trgovina';document.getElementById('updated').textContent='Poslednje ažuriranje: '+(d.last_update||'čeka se')+' · Status: '+d.status;
}catch(e){document.getElementById('updated').textContent='Greška pri učitavanju: '+e}}
document.getElementById('loginForm').addEventListener('submit',event=>{event.preventDefault();sessionStorage.setItem('paperApiToken',document.getElementById('token').value);document.getElementById('token').value='';load()});
document.getElementById('logout').addEventListener('click',()=>{sessionStorage.removeItem('paperApiToken');showAuth('')});
load();setInterval(load,15000);
</script></body></html>"""


@app.route("/")
@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD)


@app.route("/status")
def status():
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    now = datetime.now(timezone.utc)
    with market_lock:
        current = {k: current_market_snapshot(v, now) for k, v in markets.items()}
    fresh_quotes = {symbol: quote for symbol, market in current.items()
                    if (quote := fresh_market_quote(market, now)) is not None}
    prices = {symbol: quote[0] for symbol, quote in fresh_quotes.items()}
    price_timestamps = {symbol: current[symbol].get("updated") for symbol in fresh_quotes}
    data = engine.snapshot(prices, timestamps=price_timestamps)
    data.update({"name": "AI Trading Bot V3", "mode": "PAPER TRADING ONLY", "status": "RUNNING" if monitor_started else "STARTING",
                 "starting_capital": engine.state["starting_balance"], "markets": current,
                 "currency": PAPER_CURRENCY,
                 "positions": data["positions"], "last_update": max((m.get("updated") or "" for m in current.values()), default=None),
                 "last_error": next((m.get("fetch_error") or m.get("error") for m in current.values()
                                     if m.get("fetch_error") or m.get("error")), None)})
    return jsonify(data)


@app.route("/signals")
def signals():
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    now = datetime.now(timezone.utc)
    with market_lock:
        current = {symbol: current_market_snapshot(market, now)
                   for symbol, market in markets.items()}
    return jsonify(current)


@app.route("/positions")
def positions():
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    return jsonify(engine.snapshot()["positions"])


@app.route("/trades")
def trades():
    if not require_paper_token():
        return jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"}), 401
    return jsonify(engine.snapshot()["trades"])


@app.route("/health")
def health():
    risk = engine.risk_status()
    now = datetime.now(timezone.utc)
    with market_lock:
        current = {symbol: current_market_snapshot(market, now)
                   for symbol, market in markets.items()}
    market_data = {}
    for symbol in SYMBOLS:
        market = current.get(symbol)
        quote = fresh_market_quote(market, now) if market is not None else None
        market_data[symbol] = {
            "status": market.get("data_status", "unavailable") if market is not None else "unavailable",
            "updated": market.get("updated") if market is not None else None,
            "data_age_seconds": market.get("data_age_seconds") if market is not None else None,
            "fresh": quote is not None,
        }
    all_markets_fresh = all(item["fresh"] for item in market_data.values())
    status = "starting" if not monitor_started else "degraded" if (
        risk.get("storage_failed") or risk.get("market_warning")
        or risk.get("valuation_warning") or not all_markets_fresh) else "healthy"
    return jsonify({"status": status, "mode": "PAPER_ONLY",
                    "markets": market_data})


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
    now = datetime.now(timezone.utc)
    with market_lock:
        market = current_market_snapshot(markets.get(body.get("symbol"), {}), now)
    quote = fresh_market_quote(market, now)
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
    now = datetime.now(timezone.utc)
    with market_lock:
        market = current_market_snapshot(markets.get(symbol, {}), now)
    quote = fresh_market_quote(market, now)
    if quote is None:
        return jsonify({"ok": False, "reason": "STALE_MARKET_DATA"}), 409
    result = engine.close_position(symbol, quote[0], "MANUAL")
    return jsonify(result), paper_result_status(result)



@app.get("/paper/backup")
def download_paper_backup():
    """Download a consistent SQLite backup; requires the existing PAPER bearer token."""
    if not require_paper_token():
        response = jsonify({"ok": False, "reason": "PAPER_API_TOKEN_REQUIRED"})
        response.status_code = 401
        response.headers["Cache-Control"] = "no-store"
        return response

    backup_path = None
    try:
        with store.lock:
            handle = tempfile.NamedTemporaryFile(prefix="paper-backup-", suffix=".sqlite3", delete=False)
            backup_path = handle.name
            handle.close()
            source = sqlite3.connect(str(store.path), timeout=10)
            destination = sqlite3.connect(backup_path, timeout=10)
            try:
                source.backup(destination)
                check = destination.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError("SQLite backup integrity check failed")
            finally:
                destination.close()
                source.close()
            with open(backup_path, "rb") as backup_file:
                payload = backup_file.read()

        response = app.response_class(
            io.BytesIO(payload),
            mimetype="application/vnd.sqlite3",
            headers={
                "Content-Disposition": 'attachment; filename="paper_trading_backup.sqlite3"',
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
        return response
    except Exception:
        app.logger.exception("PAPER database backup failed")
        response = jsonify({"ok": False, "reason": "BACKUP_FAILED"})
        response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        if backup_path:
            try:
                os.unlink(backup_path)
            except OSError:
                app.logger.warning("Could not remove temporary PAPER backup file")



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
