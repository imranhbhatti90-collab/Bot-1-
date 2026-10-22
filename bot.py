import os
import time
import json
import hmac
import hashlib
import logging
from decimal import Decimal, ROUND_HALF_UP
from threading import Thread, Lock

import requests
import pandas as pd
from flask import Flask, jsonify

# =====================================================
# SETTINGS
# =====================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

ENABLE_TRADING = (
    os.getenv("ENABLE_TRADING", "false").strip().lower() == "true"
)

SYMBOL = "BTCUSD"
TIMEFRAME_MINUTES = 5
ORDER_SIZE = 40
LEVERAGE = 50

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 200

INITIAL_SL_PCT = 0.20

# Profit milestones in BTCUSD price points.
TRAILING_STEPS = [
    (100, 0),
    (200, 100),
    (400, 300),
]

POLL_SECONDS = 15

# =====================================================
# LOGGING / WEB SERVER
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)

session = requests.Session()
state_lock = Lock()

last_processed_candle = None
last_signal = None
last_entry_order_id = None
last_trailing_lock = -1
last_stop_price = None
bot_status = "STARTING"
last_error = None


# =====================================================
# SIGNED API REQUEST
# Delta REST signing: METHOD + timestamp + path + query + body
# =====================================================

def api_request(method, path, params=None, payload=None, auth=True):
    params = params or {}
    payload = payload or {}

    query = requests.models.Request(
        "GET", BASE_URL + path, params=params
    ).prepare().url

    query_string = ""
    if query and "?" in query:
        query_string = query.split("?", 1)[1]

    body = json.dumps(payload, separators=(",", ":")) if payload else ""

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if auth:
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "DELTA_API_KEY / DELTA_API_SECRET Render Environment mein missing hain"
            )

        timestamp = str(int(time.time()))
        signature_data = method.upper() + timestamp + path

        if query_string:
            signature_data += "?" + query_string

        signature_data += body

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
        result = {"raw_response": response.text[:500]}

    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code}: {json.dumps(result)[:800]}"
        )

    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(
            f"Delta API error: {json.dumps(result)[:800]}"
        )

    return result


# =====================================================
# MARKET DATA
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
        f"{SYMBOL} product nahi mila: {json.dumps(data)[:500]}"
    )


def get_candles(product_id):
    data = api_request(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": "5",
            "symbol": SYMBOL,
            "start": int(time.time()) - 200 * 5 * 60,
            "end": int(time.time()),
        },
        auth=False
    )

    candles = data.get("result", [])
    if not candles:
        raise RuntimeError(
            f"Candle data empty: {json.dumps(data)[:500]}"
        )

    df = pd.DataFrame(candles)

    required = {"open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        raise RuntimeError(
            f"Unexpected candle format: {list(df.columns)}"
        )

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(
        "time" if "time" in df.columns else df.index.name or df.columns[0]
    )

    df["ema9"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=EMA_MID, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    return df.reset_index(drop=True)


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
        if p.get("product_id") == product_id
        and float(p.get("size", 0) or 0) != 0
    ]


def get_open_orders(product_id):
    data = api_request(
        "GET",
        "/v2/orders",
        params={
            "product_id": product_id,
            "state": "open",
        },
        auth=True
    )

    orders = data.get("result", [])
    return orders if isinstance(orders, list) else []


# =====================================================
# ORDER MANAGEMENT
# =====================================================

def place_market_order(product_id, side, size):
    payload = {
        "product_id": product_id,
        "product_symbol": SYMBOL,
        "size": size,
        "side": side,
        "order_type": "market_order",
        "time_in_force": "ioc",
        "reduce_only": False,
    }

    result = api_request("POST", "/v2/orders", payload=payload)
    logging.info("ORDER RESPONSE: %s", json.dumps(result)[:1000])
    return result.get("result", {})


def place_stop_order(product_id, side, size, stop_price):
    payload = {
        "product_id": product_id,
        "product_symbol": SYMBOL,
        "size": size,
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(stop_price),
        "stop_trigger_method": "last_traded_price",
        "time_in_force": "gtc",
        "reduce_only": True,
    }

    result = api_request("POST", "/v2/orders", payload=payload)
    logging.info("STOP RESPONSE: %s", json.dumps(result)[:1000])
    return result.get("result", {})


def cancel_order(order_id):
    return api_request("DELETE", f"/v2/orders/{order_id}")


def cancel_bot_stop_orders(product_id):
    orders = get_open_orders(product_id)

    for order in orders:
        if (
            order.get("stop_order_type") == "stop_loss_order"
            and order.get("reduce_only") is True
        ):
            order_id = order.get("id")
            if order_id:
                try:
                    cancel_order(order_id)
                    logging.info("Cancelled old stop order id=%s", order_id)
                except Exception as exc:
                    logging.error("Could not cancel stop %s: %s", order_id, exc)


# =====================================================
# EMA SIGNAL
# =====================================================

def get_signal(df):
    if len(df) < EMA_SLOW + 3:
        return None

    # Use the last fully closed candle.
    candle = df.iloc[-2]

    bullish = (
        candle["ema9"] > candle["ema21"]
        and candle["close"] > candle["ema200"]
    )

    bearish = (
        candle["ema9"] < candle["ema21"]
        and candle["close"] < candle["ema200"]
    )

    if bullish:
        return "buy"

    if bearish:
        return "sell"

    return None


# =====================================================
# STOP LOSS / TRAILING STOP
# =====================================================

def manage_position(product_id, position, price):
    global last_trailing_lock, last_stop_price

    raw_size = float(position.get("size", 0) or 0)
    entry = float(position.get("entry_price", 0) or 0)

    if raw_size == 0 or entry <= 0:
        return

    side = "buy" if raw_size > 0 else "sell"
    close_side = "sell" if side == "buy" else "buy"
    size = int(abs(raw_size))

    if size <= 0:
        return

    profit_points = (
        price - entry if side == "buy"
        else entry - price
    )

    # Initial SL.
    if side == "buy":
        stop_price = entry * (1 - INITIAL_SL_PCT / 100)
    else:
        stop_price = entry * (1 + INITIAL_SL_PCT / 100)

    lock_points = -1

    for trigger, lock in TRAILING_STEPS:
        if profit_points >= trigger:
            lock_points = lock

    if lock_points >= 0:
        if side == "buy":
            stop_price = max(stop_price, entry + lock_points)
        else:
            stop_price = min(stop_price, entry - lock_points)

    # Avoid repeatedly replacing the same stop.
    rounded_stop = round(stop_price, 1)

    if (
        last_stop_price is not None
        and rounded_stop == last_stop_price
        and lock_points == last_trailing_lock
    ):
        return

    logging.info(
        "POSITION side=%s entry=%.2f price=%.2f profit_points=%.2f stop=%.2f",
        side, entry, price, profit_points, rounded_stop
    )

    # Cancel existing stop only when replacement is ready to be attempted.
    try:
        new_order = place_stop_order(
            product_id, close_side, size, rounded_stop
        )
    except Exception as exc:
        logging.error("STOP PLACEMENT FAILED: %s", exc)
        return

    new_order_id = new_order.get("id")
    if not new_order_id:
        logging.error("Stop API response mein order ID nahi mila.")
        return

    # Only after new stop is accepted, cancel older stops.
    orders = get_open_orders(product_id)
    for order in orders:
        if (
            order.get("id") != new_order_id
            and order.get("stop_order_type") == "stop_loss_order"
            and order.get("reduce_only") is True
        ):
            try:
                cancel_order(order["id"])
            except Exception as exc:
                logging.error("Old stop cancel failed: %s", exc)

    last_stop_price = rounded_stop
    last_trailing_lock = lock_points


# =====================================================
# MAIN BOT LOOP
# =====================================================

def bot_loop():
    global last_processed_candle, last_signal, bot_status, last_error

    while True:
        try:
            if not API_KEY or not API_SECRET:
                bot_status = "WAITING_FOR_API_KEYS"
                logging.error(
                    "Render Environment mein DELTA_API_KEY aur "
                    "DELTA_API_SECRET set karein."
                )
                time.sleep(15)
                continue

            product = get_product()
            product_id = int(product["id"])

            candles = get_candles(product_id)
            price = float(candles.iloc[-1]["close"])

            # Prevent repeated entries during the same candle.
            closed_candle = candles.iloc[-2]
            candle_time = str(
                closed_candle["time"]
                if "time" in candles.columns
                else len(candles) - 2
            )

            signal = get_signal(candles)

            logging.info(
                "[MARKET] %s price=%.2f EMA9=%.2f EMA21=%.2f EMA200=%.2f",
                SYMBOL,
                price,
                candles.iloc[-2]["ema9"],
                candles.iloc[-2]["ema21"],
                candles.iloc[-2]["ema200"]
            )

            positions = get_open_positions(product_id)

            if positions:
                # Manage an existing position first; don't open another.
                manage_position(product_id, positions[0], price)
                bot_status = "POSITION_MANAGED"

            elif signal:
                logging.info("[SIGNAL] %s", signal.upper())

                if not ENABLE_TRADING:
                    bot_status = "SAFE_MODE"
                    logging.warning(
                        "[SAFE MODE] Trading is OFF. No order placed."
                    )

                elif candle_time != last_processed_candle:
                    # Only one entry attempt per closed candle.
                    last_processed_candle = candle_time
                    last_signal = signal

                    order = place_market_order(
                        product_id, signal, ORDER_SIZE
                    )

                    if order:
                        last_entry_order_id = order.get("id")
                        bot_status = "ORDER_SENT"
                        logging.info(
                            "ENTRY ORDER SENT id=%s side=%s",
                            last_entry_order_id, signal
                        )
                    else:
                        bot_status = "ORDER_RESPONSE_EMPTY"
                        logging.error("Order response empty: %s", order)

                else:
                    bot_status = "WAITING_NEXT_CANDLE"

            else:
                bot_status = "NO_SIGNAL"
                logging.info("[SIGNAL] No trade")

            last_error = None

        except Exception as exc:
            last_error = str(exc)
            bot_status = "ERROR"
            logging.exception("BOT LOOP ERROR: %s", exc)

        time.sleep(POLL_SECONDS)


# =====================================================
# HEALTH ENDPOINTS
# =====================================================

@app.route("/")
def home():
    return jsonify({
        "bot": "BTCUSD EMA Bot",
        "timeframe": "5m",
        "size": ORDER_SIZE,
        "leverage_setting": LEVERAGE,
        "trading_enabled": ENABLE_TRADING,
        "status": bot_status,
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
        "[BOT] Starting | BTCUSD | 5m | Size=%s | Leverage=%sx | Trading=%s",
        ORDER_SIZE,
        LEVERAGE,
        "ON" if ENABLE_TRADING else "OFF"
    )

    Thread(target=bot_loop, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
