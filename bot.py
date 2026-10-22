import os
import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

# =====================================
# DELTA EXCHANGE DEMO / TESTNET
# =====================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
SYMBOL = "BTCUSD"
TIMEFRAME = "5m"

CHECK_INTERVAL = 60

# SAFETY: Signal testing only.
# Automatic trading is NOT implemented.
ENABLE_TRADING = False

# =====================================
# WEB SERVER
# =====================================

@app.route("/")
def home():
    return (
        "BTC EMA Bot is running | "
        "Mode: Signal Testing Only | "
        "Automatic Trading: OFF"
    )


# =====================================
# FETCH MARKET CANDLES
# =====================================

def get_candles():
    try:
        now = int(time.time())

        url = f"{BASE_URL}/v2/history/candles"

        params = {
            "resolution": TIMEFRAME,
            "symbol": SYMBOL,
            "start": now - (24 * 60 * 60),
            "end": now
        }

        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": "BTC-EMA-Demo-Bot/1.0"},
            timeout=15
        )

        print(
            f"[API] HTTP status: {response.status_code}",
            flush=True
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("success"):
            print(f"[API ERROR] {data}", flush=True)
            return []

        candles = data.get("result", [])

        candles = sorted(
            candles,
            key=lambda candle: candle["time"]
        )

        print(
            f"[MARKET] Received {len(candles)} candles",
            flush=True
        )

        return candles

    except Exception as error:
        print(f"[MARKET DATA ERROR] {error}", flush=True)
        return []


# =====================================
# EMA CALCULATION
# =====================================

def calculate_ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


# =====================================
# EMA STRATEGY
# =====================================

def check_signal():
    candles = get_candles()

    if len(candles) < 200:
        print(
            f"[WAIT] Need at least 200 candles. "
            f"Available: {len(candles)}",
            flush=True
        )
        return

    try:
        closes = [
            float(candle["close"])
            for candle in candles
        ]

        price = closes[-1]

        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        ema200 = calculate_ema(closes, 200)

        if None in (ema9, ema21, ema200):
            print("[WAIT] EMA data is incomplete.", flush=True)
            return

        print("=" * 40, flush=True)
        print(f"Symbol : {SYMBOL}", flush=True)
        print(f"Price  : {price:.2f}", flush=True)
        print(f"EMA 9  : {ema9:.2f}", flush=True)
        print(f"EMA 21 : {ema21:.2f}", flush=True)
        print(f"EMA 200: {ema200:.2f}", flush=True)

        if ema9 > ema21 and price > ema200:
            print(
                "[SIGNAL] BUY BIAS — no order placed",
                flush=True
            )

        elif ema9 < ema21 and price < ema200:
            print(
                "[SIGNAL] SELL BIAS — no order placed",
                flush=True
            )

        else:
            print(
                "[SIGNAL] WAIT — no order placed",
                flush=True
            )

        print("=" * 40, flush=True)

    except (KeyError, TypeError, ValueError) as error:
        print(
            f"[CANDLE FORMAT ERROR] {error}",
            flush=True
        )


# =====================================
# CONTINUOUS MONITORING LOOP
# =====================================

def trading_loop():
    print(
        "[BOT] EMA monitoring loop started.",
        flush=True
    )

    while True:
        try:
            check_signal()

        except Exception as error:
            print(
                f"[BOT ERROR] {error}",
                flush=True
            )

        time.sleep(CHECK_INTERVAL)


# =====================================
# START BOT + RENDER SERVER
# =====================================

if __name__ == "__main__":

    print("[BOT] Starting BTC EMA monitoring...", flush=True)

    bot_thread = threading.Thread(
        target=trading_loop,
        daemon=True
    )

    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))

    print(
        f"[SERVER] Starting on port {port}",
        flush=True
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
