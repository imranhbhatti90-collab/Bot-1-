import os
import time
import json
import hmac
import hashlib
import logging
from threading import Thread, Lock

import requests
import pandas as pd
from flask import Flask, jsonify

# ==========================================
# BTCUSD EMA 9/21 CROSSOVER BOT
# DELTA EXCHANGE TESTNET
# ==========================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

ENABLE_TRADING = (
    os.getenv("ENABLE_TRADING", "false").lower().strip() == "true"
)

SYMBOL = "BTCUSD"
RESOLUTION = "5m"

ORDER_SIZE = 40
LEVERAGE_DISPLAY = 50

EMA_FAST = 9
EMA_SLOW = 21

POLL_SECONDS = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)
session = requests.Session()
state_lock = Lock()

bot_status = "STARTING"
last_error = None
last_signal = None
last_candle = None
last_order_id = None


# ==========================================
# API REQUEST
# ==========================================

def api_request(method, path, params=None, payload=None, auth=True):
    params = params or {}
    payload = payload or {}

    request = requests.Request(
        method.upper(),
        BASE_URL + path,
        params=params
    ).prepare()

    query = ""
    if request.url and "?" in request.url:
        query = request.url.split("?", 1)[1]

    body = json.dumps(
        payload, separators=(",", ":")
    ) if payload else ""

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "BTCUSD-EMA-Testnet-Bot"
    }

    if auth:
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "DELTA_API_KEY ya DELTA_API_SECRET missing hai."
            )

        timestamp = str(int(time.time()))

        signature_data = (
            method.upper()
            + timestamp
            + path
            + ("?" + query if query else "")
            + body
        )

        signature = hmac.new(
            API_SECRET.encode(),
            signature_data.encode(),
            hashlib.sha256
        ).hexdigest()

        headers.update({
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": signature
        })

    url = BASE_URL + path
    if query:
        url += "?" + query

    response = session.request(
        method.upper(),
        url,
        headers=headers,
        data=body if body else None,
        timeout=15
    )

    try:
        result = response.json()
    except ValueError:
        result = {"response": response.text[:500]}

    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code}: "
            f"{json.dumps(result)[:500]}"
        )

    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(f"API error: {json.dumps(result)[:500]}")

    return result


# ==========================================
# PRODUCT
# ==========================================

def get_product():
    data = api_request(
        "GET",
        "/v2/products",
        params={"symbol": SYMBOL},
        auth=False
    )

    products = data.get("result", [])

    if isinstance(products, dict):
        products = [products]

    for product in products:
        if product.get("symbol") == SYMBOL:
            return product

    raise RuntimeError(f"{SYMBOL} product nahi mila.")


# ==========================================
# CANDLES AND EMA
# ==========================================

def get_candles():
    now = int(time.time())

    data = api_request(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": RESOLUTION,
            "symbol": SYMBOL,
            "start": now - 250 * 300,
            "end": now
        },
        auth=False
    )

    candles = data.get("result", [])

    if not candles:
        raise RuntimeError("Candle data empty hai.")

    df = pd.DataFrame(candles)

    required = {"time", "open", "high", "low", "close"}

    if not required.issubset(df.columns):
        raise RuntimeError(
            f"Unexpected candle columns: {list(df.columns)}"
        )

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(
        subset=["time", "open", "high", "low", "close"]
    )

    df = (
        df.sort_values("time")
        .drop_duplicates("time", keep="last")
        .reset_index(drop=True)
    )

    if len(df) < 30:
        raise RuntimeError("EMA calculate karne ke liye candles kam hain.")

    df["ema9"] = df["close"].ewm(
        span=EMA_FAST, adjust=False
    ).mean()

    df["ema21"] = df["close"].ewm(
        span=EMA_SLOW, adjust=False
    ).mean()

    return df


# ==========================================
# TRUE EMA CROSSOVER
# ==========================================

def get_signal(df):
    # -2 = latest fully closed candle
    # -3 = previous fully closed candle

    previous = df.iloc[-3]
    current = df.iloc[-2]

    bullish_cross = (
        previous["ema9"] <= previous["ema21"]
        and current["ema9"] > current["ema21"]
    )

    bearish_cross = (
        previous["ema9"] >= previous["ema21"]
        and current["ema9"] < current["ema21"]
    )

    logging.info(
        "[CROSSOVER CHECK] candle=%s close=%.2f "
        "previous_EMA9=%.2f previous_EMA21=%.2f "
        "EMA9=%.2f EMA21=%.2f BUY=%s SELL=%s",
        int(current["time"]),
        float(current["close"]),
        float(previous["ema9"]),
        float(previous["ema21"]),
        float(current["ema9"]),
        float(current["ema21"]),
        bullish_cross,
        bearish_cross
    )

    if bullish_cross:
        return "buy"

    if bearish_cross:
        return "sell"

    return None


# ==========================================
# OPEN POSITIONS
# ==========================================

def get_open_positions(product_id):
    data = api_request(
        "GET",
        "/v2/positions",
        params={"product_id": product_id},
        auth=True
    )

    positions = data.get("result", [])

    if isinstance(positions, dict):
        positions = [positions]

    return [
        p for p in positions
        if int(p.get("product_id", product_id)) == product_id
        and float(p.get("size", 0) or 0) != 0
    ]


# ==========================================
# MARKET ORDER
# ==========================================

def place_market_order(product_id, side, size):
    if not ENABLE_TRADING:
        raise RuntimeError("Trading disabled hai.")

    payload = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "time_in_force": "ioc",
        "reduce_only": False
    }

    logging.warning(
        "[ORDER ATTEMPT] %s %s size=%s",
        SYMBOL, side, size
    )

    response = api_request(
        "POST",
        "/v2/orders",
        payload=payload,
        auth=True
    )

    order = response.get("result", {})

    logging.info("[ORDER RESPONSE] %s", json.dumps(response)[:800])

    if not order or not order.get("id"):
        raise RuntimeError(
            "Order ID nahi mili. Order execution verify karein."
        )

    return order


# ==========================================
# BOT LOOP
# ==========================================

def bot_loop():
    global bot_status, last_error
    global last_signal, last_candle, last_order_id

    while True:
        try:
            if not API_KEY or not API_SECRET:
                bot_status = "WAITING_FOR_API_KEYS"
                raise RuntimeError("Render API keys missing hain.")

            product = get_product()
            product_id = int(product["id"])

            df = get_candles()
            current = df.iloc[-2]

            candle_time = int(current["time"])
            price = float(current["close"])

            signal = get_signal(df)

            logging.info(
                "[MARKET] %s price=%.2f EMA9=%.2f EMA21=%.2f",
                SYMBOL,
                price,
                float(current["ema9"]),
                float(current["ema21"])
            )

            positions = get_open_positions(product_id)

            if positions:
                bot_status = "POSITION_OPEN"
                logging.warning(
                    "[POSITION OPEN] Automatic stop-loss configured nahi hai. "
                    "Koi naya order nahi bheja jayega."
                )

            elif signal is None:
                bot_status = "NO_CROSSOVER"

            elif candle_time == last_candle:
                bot_status = "CANDLE_ALREADY_PROCESSED"

            else:
                last_candle = candle_time
                last_signal = signal

                if not ENABLE_TRADING:
                    bot_status = "SAFE_MODE"
                    logging.warning(
                        "[SAFE MODE] %s crossover mila; order nahi bheja.",
                        signal.upper()
                    )
                else:
                    order = place_market_order(
                        product_id,
                        signal,
                        ORDER_SIZE
                    )

                    last_order_id = order["id"]
                    bot_status = "ORDER_ACCEPTED"

                    logging.warning(
                        "[ORDER ACCEPTED] id=%s. Fill status verify karein.",
                        last_order_id
                    )

            last_error = None

        except Exception as exc:
            last_error = str(exc)
            bot_status = "ERROR"
            logging.exception("[BOT ERROR] %s", exc)

        time.sleep(POLL_SECONDS)


# ==========================================
# HEALTH ENDPOINTS
# ==========================================

@app.route("/")
def home():
    return jsonify({
        "bot": "BTCUSD EMA 9/21 Crossover",
        "timeframe": RESOLUTION,
        "order_size": ORDER_SIZE,
        "leverage_display_only": LEVERAGE_DISPLAY,
        "trading_enabled": ENABLE_TRADING,
        "status": bot_status,
        "last_signal": last_signal,
        "last_order_id": last_order_id,
        "last_error": last_error
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_status": bot_status,
        "trading_enabled": ENABLE_TRADING,
        "last_error": last_error
    })


# ==========================================
# START
# ==========================================

if __name__ == "__main__":
    logging.info(
        "[BOT START] %s | %s | Size=%s | Trading=%s",
        SYMBOL,
        RESOLUTION,
        ORDER_SIZE,
        ENABLE_TRADING
    )

    Thread(target=bot_loop, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
