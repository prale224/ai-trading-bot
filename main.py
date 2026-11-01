
from flask import Flask, jsonify
from datetime import datetime, timezone
import threading
import time
import yfinance as yf
import pandas as pd

app = Flask(__name__)

SYMBOLS = {
    "US100": "^NDX",
    "US500": "^GSPC"
}

bot = {
    "name": "AI Trading Bot V2",
    "mode": "PAPER TRADING",
    "capital": 100.0,
    "balance": 100.0,
    "status": "RUNNING",
    "markets": {},
    "last_update": None
}

lock = threading.Lock()


def calculate_rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period
    ).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze_market(symbol, ticker):
    try:
        df = yf.download(
            ticker,
            period="5d",
            interval="1m",
            progress=False,
            auto_adjust=True,
            threads=False
        )

        if df.empty:
            return {"signal": "WAIT", "error": "No market data"}

        close = df["Close"]

        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]

        close = close.dropna()

        if len(close) < 35:
            return {"signal": "WAIT", "error": "Not enough candles"}

        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()

        rsi = calculate_rsi(close)

        macd = (
            close.ewm(span=12, adjust=False).mean()
            - close.ewm(span=26, adjust=False).mean()
        )

        macd_signal = macd.ewm(
            span=9, adjust=False
        ).mean()

        price = float(close.iloc[-1])
        rsi_value = float(rsi.iloc[-1])

        signal = "WAIT"

        if (
            ema9.iloc[-1] > ema21.iloc[-1]
            and macd.iloc[-1] > macd_signal.iloc[-1]
            and 50 < rsi_value < 70
        ):
            signal = "BUY"

        elif (
            ema9.iloc[-1] < ema21.iloc[-1]
            and macd.iloc[-1] < macd_signal.iloc[-1]
            and 30 < rsi_value < 50
        ):
            signal = "SELL"

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "signal": signal,
            "rsi": round(rsi_value, 2),
            "ema9": round(float(ema9.iloc[-1]), 2),
            "ema21": round(float(ema21.iloc[-1]), 2),
            "macd": round(float(macd.iloc[-1]), 4),
            "updated": datetime.now(timezone.utc).isoformat()
        }

    except Exception as error:
        return {
            "symbol": symbol,
            "signal": "WAIT",
            "error": str(error)
        }


def market_monitor():
    while True:
        results = {}

        for symbol, ticker in SYMBOLS.items():
            results[symbol] = analyze_market(symbol, ticker)

        with lock:
            bot["markets"] = results
            bot["last_update"] = datetime.now(
                timezone.utc
            ).isoformat()

        time.sleep(60)


@app.route("/")
def home():
    return jsonify({
        "message": "AI Trading Bot V2 is online",
        "mode": bot["mode"],
        "markets": list(SYMBOLS.keys()),
        "status": bot["status"]
    })


@app.route("/status")
def status():
    with lock:
        return jsonify(bot)


@app.route("/signals")
def signals():
    with lock:
        return jsonify(bot["markets"])


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    threading.Thread(
        target=market_monitor,
        daemon=True
    ).start()

    import os

    port = int(os.environ.get("PORT", 10000))

    app.run(host="0.0.0.0", port=port)
