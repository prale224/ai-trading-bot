from flask import Flask, jsonify
from datetime import datetime
import threading
import time
import random

app = Flask(__name__)

bot = {
    "name": "AI Trading Bot",
    "mode": "PAPER TRADING",
    "market": "INDICES",
    "strategy": "SCALPING",
    "capital": 100.0,
    "balance": 100.0,
    "trades": 0,
    "status": "RUNNING",
    "price": 10000.0,
    "last_update": None
}

def simulation():
    while True:
        change = random.uniform(-0.002, 0.002)

        bot["price"] *= (1 + change)
        bot["last_update"] = datetime.utcnow().isoformat()

        time.sleep(5)

threading.Thread(target=simulation, daemon=True).start()

@app.route("/")
def home():
    return jsonify({
        "message": "AI Trading Bot is online",
        "mode": bot["mode"],
        "status": bot["status"]
    })

@app.route("/status")
def status():
    return jsonify(bot)

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
