from flask import Flask, jsonify, render_template_string
from datetime import datetime, timezone
import threading
import time
import os
import yfinance as yf
import pandas as pd

app = Flask(__name__)

SYMBOLS = {"US100": "^NDX", "US500": "^GSPC"}
STARTING_CAPITAL = 100.0
RISK_PER_TRADE = 0.01
STOP_LOSS_PCT = 0.003
TAKE_PROFIT_PCT = 0.0045
MAX_NOTIONAL_MULTIPLE = 1.0
REFRESH_SECONDS = 60

bot = {
    "name": "AI Trading Bot V3",
    "mode": "PAPER TRADING ONLY",
    "status": "RUNNING",
    "starting_capital": STARTING_CAPITAL,
    "balance": STARTING_CAPITAL,
    "equity": STARTING_CAPITAL,
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "markets": {},
    "positions": {},
    "trades": [],
    "last_update": None,
    "last_error": None
}
lock = threading.RLock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def analyze_market(symbol, ticker):
    try:
        df = yf.download(
            ticker, period="5d", interval="1m",
            progress=False, auto_adjust=True, threads=False
        )
        if df.empty:
            return {"symbol": symbol, "signal": "WAIT", "error": "No market data"}

        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 35:
            return {"symbol": symbol, "signal": "WAIT", "error": "Not enough candles"}

        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        rsi = calculate_rsi(close)
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        macd_signal = macd.ewm(span=9, adjust=False).mean()

        price = float(close.iloc[-1])
        rsi_value = float(rsi.iloc[-1])
        signal = "WAIT"

        if pd.notna(rsi_value):
            if ema9.iloc[-1] > ema21.iloc[-1] and macd.iloc[-1] > macd_signal.iloc[-1] and 50 < rsi_value < 70:
                signal = "BUY"
            elif ema9.iloc[-1] < ema21.iloc[-1] and macd.iloc[-1] < macd_signal.iloc[-1] and 30 < rsi_value < 50:
                signal = "SELL"

        return {
            "symbol": symbol, "price": round(price, 2), "signal": signal,
            "rsi": round(rsi_value, 2) if pd.notna(rsi_value) else None,
            "ema9": round(float(ema9.iloc[-1]), 2),
            "ema21": round(float(ema21.iloc[-1]), 2),
            "macd": round(float(macd.iloc[-1]), 4),
            "updated": utc_now()
        }
    except Exception as error:
        return {"symbol": symbol, "signal": "WAIT", "error": str(error)}


def refresh_account():
    unrealized = 0.0
    for symbol, position in bot["positions"].items():
        price = bot["markets"].get(symbol, {}).get("price")
        if price is not None:
            direction = 1 if position["side"] == "BUY" else -1
            unrealized += (price - position["entry_price"]) * direction * position["quantity"]
    bot["unrealized_pnl"] = round(unrealized, 2)
    bot["equity"] = round(bot["balance"] + unrealized, 2)
    bot["realized_pnl"] = round(bot["balance"] - bot["starting_capital"], 2)


def close_position(symbol, price, reason):
    position = bot["positions"].pop(symbol, None)
    if not position:
        return
    direction = 1 if position["side"] == "BUY" else -1
    pnl = (price - position["entry_price"]) * direction * position["quantity"]
    bot["balance"] = round(bot["balance"] + pnl, 2)
    bot["trades"].insert(0, {
        "symbol": symbol, "side": position["side"],
        "entry_price": position["entry_price"], "exit_price": round(price, 2),
        "quantity": round(position["quantity"], 6),
        "pnl": round(pnl, 2), "reason": reason,
        "opened_at": position["opened_at"], "closed_at": utc_now()
    })
    bot["trades"] = bot["trades"][:100]


def manage_symbol(symbol, market):
    price = market.get("price")
    if price is None:
        return

    position = bot["positions"].get(symbol)
    if position:
        if position["side"] == "BUY":
            if price <= position["stop_loss"]:
                close_position(symbol, price, "STOP_LOSS")
            elif price >= position["take_profit"]:
                close_position(symbol, price, "TAKE_PROFIT")
            elif market.get("signal") == "SELL":
                close_position(symbol, price, "OPPOSITE_SIGNAL")
        else:
            if price >= position["stop_loss"]:
                close_position(symbol, price, "STOP_LOSS")
            elif price <= position["take_profit"]:
                close_position(symbol, price, "TAKE_PROFIT")
            elif market.get("signal") == "BUY":
                close_position(symbol, price, "OPPOSITE_SIGNAL")

    if symbol in bot["positions"] or market.get("signal") not in ("BUY", "SELL"):
        return

    equity = max(bot["equity"], 0)
    risk_amount = equity * RISK_PER_TRADE
    stop_distance = price * STOP_LOSS_PCT
    if stop_distance <= 0 or price <= 0:
        return

    quantity_by_risk = risk_amount / stop_distance
    quantity_by_cash = (max(bot["balance"], 0) * MAX_NOTIONAL_MULTIPLE) / price
    quantity = min(quantity_by_risk, quantity_by_cash)
    if quantity <= 0:
        return

    side = market["signal"]
    if side == "BUY":
        stop_loss = price * (1 - STOP_LOSS_PCT)
        take_profit = price * (1 + TAKE_PROFIT_PCT)
    else:
        stop_loss = price * (1 + STOP_LOSS_PCT)
        take_profit = price * (1 - TAKE_PROFIT_PCT)

    bot["positions"][symbol] = {
        "symbol": symbol, "side": side, "entry_price": round(price, 2),
        "quantity": round(quantity, 6),
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
        "risk_budget": round(risk_amount, 2),
        "opened_at": utc_now()
    }


def market_monitor():
    while True:
        results = {symbol: analyze_market(symbol, ticker) for symbol, ticker in SYMBOLS.items()}
        with lock:
            bot["markets"] = results
            for symbol, market in results.items():
                manage_symbol(symbol, market)
            refresh_account()
            bot["last_update"] = utc_now()
            bot["last_error"] = None if any("error" not in x for x in results.values()) else "Market data unavailable"
        time.sleep(REFRESH_SECONDS)


DASHBOARD = """
<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trading Bot V3</title>
<style>
body{font-family:Arial,sans-serif;background:#101418;color:#f1f5f9;margin:0;padding:18px}
h1{font-size:24px}.muted{color:#9ca3af}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
.card{background:#1c252d;border:1px solid #34414c;border-radius:12px;padding:14px;margin:10px 0}
.value{font-size:23px;font-weight:bold;margin-top:7px}.buy{color:#4ade80}.sell{color:#fb7185}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:8px;border-bottom:1px solid #34414c}
.small{font-size:12px;overflow-wrap:anywhere}
</style></head><body>
<h1>AI Trading Bot V3</h1><p class="muted">PAPER TRADING ONLY · simulacija · osvežavanje na 60 sekundi</p>
<div class="grid" id="summary"></div>
<div class="card"><h3>Tržišni signali</h3><div id="markets">Učitavanje...</div></div>
<div class="card"><h3>Otvorene pozicije</h3><div id="positions">Učitavanje...</div></div>
<div class="card"><h3>Poslednje trgovine</h3><div id="trades">Učitavanje...</div></div>
<p class="muted small" id="updated"></p>
<script>
async function load(){
 try{
  const r=await fetch('/status'); const d=await r.json();
  const money=n=>'€'+Number(n||0).toFixed(2);
  document.getElementById('summary').innerHTML=[
   ['Stanje',money(d.balance)],['Vrednost računa',money(d.equity)],
   ['Realizovani P/L',money(d.realized_pnl)],['Nerealizovani P/L',money(d.unrealized_pnl)]
  ].map(x=>'<div class="card"><div class="muted">'+x[0]+'</div><div class="value">'+x[1]+'</div></div>').join('');
  document.getElementById('markets').innerHTML=Object.values(d.markets).map(m=>
   '<p><b>'+m.symbol+'</b> · '+(m.price??'—')+' · <b class="'+(m.signal==='BUY'?'buy':m.signal==='SELL'?'sell':'')+'">'+m.signal+'</b> · RSI '+(m.rsi??'—')+(m.error?' · '+m.error:'')+'</p>').join('')||'Nema podataka';
  document.getElementById('positions').innerHTML=Object.values(d.positions).map(p=>
   '<p><b>'+p.symbol+' '+p.side+'</b> · ulaz '+p.entry_price+' · količina '+p.quantity+
   '<br>Stop Loss: '+p.stop_loss+' · Take Profit: '+p.take_profit+'</p>').join('')||'Nema otvorenih pozicija';
  document.getElementById('trades').innerHTML=d.trades.length?
   '<table><tr><th>Instrument</th><th>Strana</th><th>P/L</th><th>Razlog</th></tr>'+
   d.trades.slice(0,15).map(t=>'<tr><td>'+t.symbol+'</td><td>'+t.side+'</td><td>'+money(t.pnl)+'</td><td>'+t.reason+'</td></tr>').join('')+'</table>':'Još nema zatvorenih trgovina';
  document.getElementById('updated').textContent='Poslednje ažuriranje: '+(d.last_update||'čeka se')+' · Status: '+d.status;
 }catch(e){document.getElementById('updated').textContent='Greška pri učitavanju: '+e}
}
load();setInterval(load,15000);
</script></body></html>
"""


@app.route("/")
def home():
    return render_template_string(DASHBOARD)


@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD)


@app.route("/status")
def status():
    with lock:
        refresh_account()
        return jsonify(bot)


@app.route("/signals")
def signals():
    with lock:
        return jsonify(bot["markets"])


@app.route("/positions")
def positions():
    with lock:
        return jsonify(bot["positions"])


@app.route("/trades")
def trades():
    with lock:
        return jsonify(bot["trades"])


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "mode": bot["mode"]})


if __name__ == "__main__":
    threading.Thread(target=market_monitor, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
