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

# =====================================================
# CONFIGURATION — TESTNET ONLY
# =====================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

ENABLE_TRADING = (
    os.getenv("ENABLE_TRADING", "false").strip().lower() == "true"
)

SYMBOL = "BTCUSD"
RESOLUTION = "5m"

ORDER_SIZE = 40
LEVERAGE_DISPLAY = 50

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 200

INITIAL_SL_PCT = 0.20

# Profit points -> minimum profit to lock
TRAILING_STEPS = [
    (100, 0),
    (200, 100),
    (400, 300),
]

POLL_SECONDS = 15

# =====================================================
# LOGGING
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)
session = requests.Session()
state_lock = Lock()

bot_status = "STARTING"
last_error = None
last_processed_candle = None
last_signal = None
last_entry_order_id = None

# =====================================================
# API REQUEST
# =====================================================

def api_request(method, path, params=None, payload=None, auth=True):
    params = params or {}
    payload = payload or {}

    prepared = requests.Request(
        method.upper(),
        BASE_URL + path,
        params=params
    ).prepare()

    query_string = ""
    if prepared.url and "?" in prepared.url:
        query_string = prepared.url.split("?", 1)[1]

    body = (
        json.dumps(payload, separators=(",", ":"))
        if payload else ""
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "BTCUSD-Testnet-Bot"
    }

    if auth:
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "Render Environment mein API keys missing hain."
            )

        timestamp = str(int(time.time()))

        signature_data = (
            method.upper()
            + timestamp
            + path
            + ("?" + query_string if query_string else "")
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
            "signature": signature,
        })

    url = BASE_URL + path
    if query_string:
        url += "?" + query_string

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
            f"{json.dumps(result)[:700]}"
        )

    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(
            f"API error: {json.dumps(result)[:700]}"
        )

    return result


# =====================================================
# PRODUCT
# =====================================================

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

    raise RuntimeError(
        f"Product {SYMBOL} nahi mila: "
        f"{json.dumps(data)[:400]}"
    )


# =====================================================
# CANDLES + INDICATORS
# =====================================================

def get_candles():
    now = int(time.time())

    data = api_request(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": RESOLUTION,
            "symbol": SYMBOL,
            "start": now - 250 * 5 * 60,
            "end": now,
        },
        auth=False
    )

    candles = data.get("result", [])

    if not candles:
        raise RuntimeError("API se candle data empty mila.")

    df = pd.DataFrame(candles)

    required = {"open", "high", "low", "close", "time"}

    if not required.issubset(df.columns):
        raise RuntimeError(
            f"Candle columns unexpected: {list(df.columns)}"
        )

    for col in ["open", "high", "low", "close", "time"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(
        subset=["time", "open", "high", "low", "close"]
    )

    # Oldest candle first, newest candle last.
    df = df.sort_values("time").drop_duplicates(
        subset=["time"], keep="last"
    ).reset_index(drop=True)

    if len(df) < EMA_SLOW + 3:
        raise RuntimeError(
            f"EMA200 ke liye candles kam hain: {len(df)}"
        )

    df["ema9"] = df["close"].ewm(
        span=EMA_FAST, adjust=False
    ).mean()

    df["ema21"] = df["close"].ewm(
        span=EMA_MID, adjust=False
    ).mean()

    df["ema200"] = df["close"].ewm(
        span=EMA_SLOW, adjust=False
    ).mean()

    return df


# =====================================================
# SIGNAL
# =====================================================

def get_signal(df):
    # Last candle may still be forming.
    candle = df.iloc[-2]

    close = float(candle["close"])
    ema9 = float(candle["ema9"])
    ema21 = float(candle["ema21"])
    ema200 = float(candle["ema200"])

    bullish = ema9 > ema21 and close > ema200
    bearish = ema9 < ema21 and close < ema200

    logging.info(
        "[SIGNAL CHECK] candle=%s close=%.2f "
        "EMA9=%.2f EMA21=%.2f EMA200=%.2f "
        "BUY=%s SELL=%s",
        int(candle["time"]),
        close,
        ema9,
        ema21,
        ema200,
        bullish,
        bearish
    )

    if bullish:
        return "buy"

    if bearish:
        return "sell"

    return None


# =====================================================
# POSITIONS
# =====================================================

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

    result = []

    for position in positions:
        if position.get("product_id") is not None:
            if int(position["product_id"]) != int(product_id):
                continue

        size = float(position.get("size", 0) or 0)

        if size != 0:
            result.append(position)

    return result


def position_side(position):
    # Prefer the exchange's explicit side when available.
    side = str(position.get("side", "")).lower()

    if side in ("buy", "sell", "long", "short"):
        return "buy" if side in ("buy", "long") else "sell"

    size = float(position.get("size", 0) or 0)

    if size > 0:
        return "buy"

    if size < 0:
        return "sell"

    return None


# =====================================================
# ORDERS
# =====================================================

def place_market_order(product_id, side, size):
    if not ENABLE_TRADING:
        raise RuntimeError("Trading disabled; order blocked.")

    payload = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "time_in_force": "ioc",
        "reduce_only": False,
    }

    logging.warning(
        "[ORDER ATTEMPT] symbol=%s side=%s size=%s",
        SYMBOL, side, size
    )

    response = api_request(
        "POST",
        "/v2/orders",
        payload=payload,
        auth=True
    )

    order = response.get("result", {})

    logging.info(
        "[ORDER RESPONSE] %s",
        json.dumps(response)[:1000]
    )

    if not order or not order.get("id"):
        raise RuntimeError(
            "Order response mein ID nahi mili; "
            "execution verify karein."
        )

    return order


def place_stop_order(product_id, side, size, stop_price):
    payload = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(round(float(stop_price), 1)),
        "stop_trigger_method": "last_traded_price",
        "time_in_force": "gtc",
        "reduce_only": True,
    }

    response = api_request(
        "POST",
        "/v2/orders",
        payload=payload,
        auth=True
    )

    order = response.get("result", {})

    logging.info(
        "[STOP RESPONSE] %s",
        json.dumps(response)[:1000]
    )

    if not order or not order.get("id"):
        raise RuntimeError(
            "Stop order response mein ID nahi mili."
        )

    return order


# =====================================================
# POSITION / STOP MONITOR
# =====================================================

def manage_position(product_id, position, price):
    side = position_side(position)

    if side is None:
        logging.error("Position side identify nahi hui: %s", position)
        return

    entry = float(position.get("entry_price", 0) or 0)
    raw_size = abs(float(position.get("size", 0) or 0))
    size = int(raw_size)

    if entry <= 0 or size <= 0:
        logging.error(
            "Invalid position details: %s",
            json.dumps(position)[:500]
        )
        return

    close_side = "sell" if side == "buy" else "buy"

    profit_points = (
        price - entry if side == "buy"
        else entry - price
    )

    if side == "buy":
        stop_price = entry * (1 - INITIAL_SL_PCT / 100)
    else:
        stop_price = entry * (1 + INITIAL_SL_PCT / 100)

    lock_points = None

    for trigger, lock in TRAILING_STEPS:
        if profit_points >= trigger:
            lock_points = lock

    if lock_points is not None:
        if side == "buy":
            stop_price = max(stop_price, entry + lock_points)
        else:
            stop_price = min(stop_price, entry - lock_points)

    logging.info(
        "[POSITION] side=%s entry=%.2f price=%.2f "
        "profit_points=%.2f proposed_stop=%.2f",
        side, entry, price, profit_points, stop_price
    )

    logging.warning(
        "[STOP CHECK ONLY] Proposed stop is logged. "
        "Existing stop orders are not automatically replaced."
    )


# =====================================================
# BOT LOOP
# =====================================================

def bot_loop():
    global bot_status, last_error
    global last_processed_candle, last_signal, last_entry_order_id

    while True:
        try:
            if not API_KEY or not API_SECRET:
                bot_status = "WAITING_FOR_API_KEYS"
                raise RuntimeError(
                    "Render Environment mein API keys missing hain."
                )

            product = get_product()
            product_id = int(product["id"])

            candles = get_candles()

            # Latest price is informational; signal uses closed candle.
            price = float(candles.iloc[-1]["close"])
            signal_candle = candles.iloc[-2]
            candle_time = int(signal_candle["time"])

            signal = get_signal(candles)

            logging.info(
                "[MARKET] %s price=%.2f "
                "EMA9=%.2f EMA21=%.2f EMA200=%.2f",
                SYMBOL,
                price,
                float(signal_candle["ema9"]),
                float(signal_candle["ema21"]),
                float(signal_candle["ema200"])
            )

            positions = get_open_positions(product_id)

            if positions:
                bot_status = "POSITION_OPEN"
                manage_position(product_id, positions[0], price)

            elif signal is None:
                bot_status = "NO_SIGNAL"
                logging.info("[SIGNAL] No trade")

            elif not ENABLE_TRADING:
                bot_status = "SAFE_MODE"
                logging.warning(
                    "[SAFE MODE] %s signal hai, lekin trading disabled hai.",
                    signal.upper()
                )

            elif candle_time == last_processed_candle:
                bot_status = "WAITING_NEXT_CANDLE"
                logging.info(
                    "[ENTRY SKIPPED] Is candle par entry pehle attempt hui."
                )

            else:
                # Record attempt before sending to avoid duplicate entries
                # if the request times out after the exchange accepted it.
                last_processed_candle = candle_time
                last_signal = signal

                order = place_market_order(
                    product_id,
                    signal,
                    ORDER_SIZE
                )

                last_entry_order_id = order["id"]
                bot_status = "ORDER_ACCEPTED"

                logging.warning(
                    "[ORDER ACCEPTED] id=%s side=%s. "
                    "Check Testnet positions to verify fill.",
                    last_entry_order_id,
                    signal
                )

            last_error = None

        except Exception as exc:
            last_error = str(exc)
            bot_status = "ERROR"
            logging.exception("[BOT LOOP ERROR] %s", exc)

        time.sleep(POLL_SECONDS)


# =====================================================
# HEALTH ENDPOINTS
# =====================================================

@app.route("/")
def home():
    return jsonify({
        "bot": "BTCUSD EMA Bot",
        "timeframe": RESOLUTION,
        "size": ORDER_SIZE,
        "leverage_setting_display_only": LEVERAGE_DISPLAY,
        "trading_enabled": ENABLE_TRADING,
        "status": bot_status,
        "last_signal": last_signal,
        "last_entry_order_id": last_entry_order_id,
        "last_error": last_error,
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_status": bot_status,
        "trading_enabled": ENABLE_TRADING,
        "last_error": last_error,
    })


if __name__ == "__main__":
    logging.info(
        "[BOT] Starting | %s | %s | Size=%s | "
        "Leverage display=%sx | Trading=%s",
        SYMBOL,
        RESOLUTION,
        ORDER_SIZE,
        LEVERAGE_DISPLAY,
        "ON" if ENABLE_TRADING else "OFF"
    )

    Thread(target=bot_loop, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
