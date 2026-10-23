import os
import time
import json
import hmac
import hashlib
import logging
import requests

from decimal import Decimal, ROUND_HALF_UP
from threading import Thread, Lock
from urllib.parse import urlencode
from flask import Flask

# ==================================================
# CONFIGURATION — DELTA EXCHANGE TESTNET ONLY
# ==================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

ENABLE_TRADING = (
    os.getenv("ENABLE_TRADING", "false").lower() == "true"
)

SYMBOL = "BTCUSD"
TIMEFRAME = "5m"

EMA_FAST = 9
EMA_SLOW = 21

# This is contract quantity, NOT dollar margin.
ORDER_SIZE = 40

LEVERAGE = 50

INITIAL_SL_PERCENT = Decimal("0.20")

# Profit points reached -> profit points locked by SL.
TRAILING_STEPS = [
    (100, 0),
    (200, 100),
    (400, 300),
]

POLL_SECONDS = 5

STATE_FILE = "bot_state.json"

# ==================================================
# WEB SERVER — FOR RENDER WEB SERVICE
# ==================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "BTCUSD EMA Bot is running on Delta Testnet."


# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
trade_lock = Lock()


# ==================================================
# PERSIST BOT STATE
# ==================================================

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    temp = STATE_FILE + ".tmp"

    with open(temp, "w") as f:
        json.dump(state, f)

    os.replace(temp, STATE_FILE)


state = load_state()


# ==================================================
# AUTHENTICATED DELTA API REQUEST
# ==================================================

def api_request(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError(
            "DELTA_API_KEY or DELTA_API_SECRET is missing"
        )

    method = method.upper()
    params = params or {}

    query = urlencode(params)
    request_path = path + (("?" + query) if query else "")

    body_text = (
        json.dumps(body, separators=(",", ":"))
        if body is not None
        else ""
    )

    timestamp = str(int(time.time()))

    message = (
        method + timestamp + request_path + body_text
    )

    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": "python-3.11",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = session.request(
        method,
        BASE_URL + path,
        params=params,
        data=body_text if body is not None else None,
        headers=headers,
        timeout=15,
    )

    try:
        result = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError("Delta returned a non-JSON response")

    if not response.ok or result.get("success") is False:
        raise RuntimeError(
            f"Delta API error HTTP {response.status_code}: {result}"
        )

    return result


def public_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=15,
        headers={"User-Agent": "python-3.11"},
    )
    response.raise_for_status()

    result = response.json()

    if result.get("success") is False:
        raise RuntimeError(f"Public API error: {result}")

    return result


# ==================================================
# PRODUCT AND MARKET DATA
# ==================================================

def get_product():
    data = public_get("/v2/products")

    for product in data.get("result", []):
        if product.get("symbol") == SYMBOL:
            return product

    raise RuntimeError(
        f"{SYMBOL} product not found on Testnet"
    )


def get_price():
    data = public_get("/v2/tickers/" + SYMBOL)
    ticker = data.get("result", {})

    if isinstance(ticker, list):
        ticker = ticker[0] if ticker else {}

    for key in ("mark_price", "close", "last_price"):
        value = ticker.get(key)

        if value is not None:
            price = Decimal(str(value))
            if price > 0:
                return price

    raise RuntimeError(
        f"Cannot determine current price from ticker: {ticker}"
    )


def get_candles():
    now = int(time.time())

    data = public_get(
        "/v2/history/candles",
        {
            "symbol": SYMBOL,
            "resolution": TIMEFRAME,
            "start": now - 8 * 60 * 60,
            "end": now,
        },
    )

    candles = data.get("result", [])

    if len(candles) < EMA_SLOW + 5:
        raise RuntimeError("Not enough candle data")

    import pandas as pd

    df = pd.DataFrame(candles)

    if "close" not in df.columns or "time" not in df.columns:
        raise RuntimeError(
            f"Unexpected candle fields: {df.columns.tolist()}"
        )

    df["close"] = pd.to_numeric(df["close"])
    df = df.sort_values("time").reset_index(drop=True)

    df["ema9"] = df["close"].ewm(
        span=EMA_FAST,
        adjust=False,
    ).mean()

    df["ema21"] = df["close"].ewm(
        span=EMA_SLOW,
        adjust=False,
    ).mean()

    return df


# ==================================================
# EMA CROSSOVER — FULLY CLOSED CANDLES
# ==================================================

def get_signal(df):
    previous = df.iloc[-3]
    current = df.iloc[-2]

    buy = (
        previous["ema9"] <= previous["ema21"]
        and current["ema9"] > current["ema21"]
    )

    sell = (
        previous["ema9"] >= previous["ema21"]
        and current["ema9"] < current["ema21"]
    )

    logging.info(
        "[CROSSOVER] previous EMA9=%.2f EMA21=%.2f | "
        "current EMA9=%.2f EMA21=%.2f | BUY=%s SELL=%s",
        previous["ema9"],
        previous["ema21"],
        current["ema9"],
        current["ema21"],
        buy,
        sell,
    )

    if buy:
        return "buy"

    if sell:
        return "sell"

    return None


# ==================================================
# POSITION CHECK (FIXED)
# ==================================================

def get_positions():
    product = get_product()
    product_id = int(product["id"])

    result = api_request(
        "GET",
        "/v2/positions",
        params={"product_id": product_id},
    )

    positions = result.get("result", [])

    if isinstance(positions, dict):
        positions = [positions]

    return positions


def get_open_position():
    for position in get_positions():
        symbol = (
            position.get("product_symbol")
            or position.get("symbol")
        )

        if symbol != SYMBOL:
            continue

        size = Decimal(str(position.get("size", 0) or 0))

        if size == 0:
            continue

        side = position.get("side")

        if side not in ("buy", "sell", "long", "short"):
            side = "buy" if size > 0 else "sell"

        if side == "long":
            side = "buy"
        elif side == "short":
            side = "sell"

        entry = position.get("entry_price")

        if entry is None:
            raise RuntimeError(
                "Open position has no entry_price in API response"
            )

        return {
            "side": side,
            "size": int(abs(size)),
            "entry": Decimal(str(entry)),
            "product_id": position.get("product_id"),
        }

    return None


# ==================================================
# PRICE ROUNDING
# ==================================================

def round_price(price, product):
    tick = Decimal(str(product.get("tick_size") or "0.5"))

    if tick <= 0:
        tick = Decimal("0.5")

    rounded = (
        (price / tick).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        ) * tick
    )

    return rounded


# ==================================================
# SET LEVERAGE
# ==================================================

def set_leverage(product):
    product_id = int(product["id"])

    result = api_request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        body={"leverage": LEVERAGE},
    )

    logging.info("[LEVERAGE RESPONSE] %s", result)

    return result


# ==================================================
# MARKET ENTRY
# ==================================================

def place_entry(product, side):
    if not ENABLE_TRADING:
        logging.warning(
            "[SAFE MODE] ENABLE_TRADING=false; no order sent. "
            "Signal=%s",
            side.upper(),
        )
        return

    if not API_KEY or not API_SECRET:
        raise RuntimeError("API credentials missing")

    if state.get("position"):
        logging.warning(
            "Saved bot position exists; refusing another entry"
        )
        return

    current = get_open_position()

    if current:
        logging.warning(
            "An exchange position is already open; refusing entry"
        )
        return

    set_leverage(product)

    product_id = int(product["id"])

    body = {
        "product_id": product_id,
        "product_symbol": SYMBOL,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "market_order",
        "time_in_force": "gtc",
        "reduce_only": False,
        "client_order_id": (
            f"ema921_{int(time.time())}"
        ),
    }

    logging.info("[ENTRY REQUEST] %s", body)

    result = api_request(
        "POST",
        "/v2/orders",
        body=body,
    )

    order = result.get("result", {})
    order_id = order.get("id")

    logging.info("[ENTRY RESPONSE] %s", result)

    position = None

    for _ in range(10):
        time.sleep(1)
        position = get_open_position()

        if position:
            break

    if not position:
        raise RuntimeError(
            "Entry order was accepted but no open position appeared. "
            "Check Testnet order history before restarting."
        )

    entry = position["entry"]
    size = position["size"]
    actual_side = position["side"]

    if actual_side != side:
        raise RuntimeError(
            "Exchange position side differs from signal; "
            "manual review required"
        )

    stop = initial_stop(actual_side, entry, product)

    state["position"] = {
        "entry_order_id": order_id,
        "side": actual_side,
        "entry": str(entry),
        "size": size,
        "product_id": product_id,
        "stop_order_id": None,
        "stop_price": str(stop),
        "highest_profit_step": 0,
    }
    save_state(state)

    try:
        stop_result = place_stop_order(
            product,
            actual_side,
            size,
            stop,
        )

        stop_order = stop_result.get("result", {})
        stop_id = stop_order.get("id")

        if not stop_id:
            raise RuntimeError(
                f"Stop order ID missing from response: {stop_result}"
            )

        state["position"]["stop_order_id"] = stop_id
        save_state(state)

        logging.info(
            "[PROTECTED POSITION] side=%s entry=%s size=%s SL=%s "
            "stop_order_id=%s",
            actual_side,
            entry,
            size,
            stop,
            stop_id,
        )

    except Exception:
        logging.exception(
            "CRITICAL: protective stop order failed after entry."
        )
        raise


# ==================================================
# INITIAL STOP-LOSS
# ==================================================

def initial_stop(side, entry, product):
    distance = (
        entry * INITIAL_SL_PERCENT / Decimal("100")
    )

    if side == "buy":
        stop = entry - distance
    else:
        stop = entry + distance

    return round_price(stop, product)


# ==================================================
# PLACE ACTUAL EXCHANGE STOP ORDER
# ==================================================

def place_stop_order(product, side, size, stop_price):
    product_id = int(product["id"])
    stop_side = "sell" if side == "buy" else "buy"

    body = {
        "product_id": product_id,
        "product_symbol": SYMBOL,
        "size": int(size),
        "side": stop_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(stop_price),
        "stop_trigger_method": "last_traded_price",
        "reduce_only": True,
        "time_in_force": "gtc",
    }

    logging.info("[STOP ORDER REQUEST] %s", body)

    return api_request(
        "POST",
        "/v2/orders",
        body=body,
    )


# ==================================================
# UPDATE EXISTING STOP ORDER
# ==================================================

def update_stop_order(product, position_state, new_stop):
    stop_id = position_state.get("stop_order_id")

    if not stop_id:
        raise RuntimeError(
            "Cannot update trailing SL: stop order ID is missing"
        )

    side = position_state["side"]
    stop_side = "sell" if side == "buy" else "buy"

    body = {
        "id": int(stop_id),
        "product_id": int(product["id"]),
        "product_symbol": SYMBOL,
        "size": int(position_state["size"]),
        "side": stop_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(new_stop),
        "stop_trigger_method": "last_traded_price",
        "reduce_only": True,
        "time_in_force": "gtc",
    }

    logging.info("[TRAIL UPDATE REQUEST] %s", body)

    result = api_request(
        "PUT",
        "/v2/orders",
        body=body,
    )

    logging.info("[TRAIL UPDATE RESPONSE] %s", result)

    return result


# ==================================================
# TRAILING STOP MANAGEMENT
# ==================================================

def manage_trailing_stop(product):
    position = get_open_position()
    saved = state.get("position")

    if not position:
        if saved:
            logging.info(
                "No exchange position found. Clearing saved bot state."
            )
            state.pop("position", None)
            save_state(state)
        return

    if not saved:
        logging.error(
            "An open exchange position exists but bot state is missing."
        )
        return

    if position["side"] != saved["side"]:
        logging.error(
            "Position side mismatch; refusing automatic stop changes."
        )
        return

    entry = Decimal(saved["entry"])
    current_price = get_price()
    side = saved["side"]

    if side == "buy":
        profit_points = current_price - entry
    else:
        profit_points = entry - current_price

    old_stop = Decimal(saved["stop_price"])
    desired_stop = initial_stop(side, entry, product)
    reached_step = 0

    for trigger_points, locked_points in TRAILING_STEPS:
        if profit_points >= Decimal(str(trigger_points)):
            reached_step = trigger_points

            if side == "buy":
                desired_stop = entry + Decimal(str(locked_points))
            else:
                desired_stop = entry - Decimal(str(locked_points))

    desired_stop = round_price(desired_stop, product)

    if side == "buy":
        new_stop = max(old_stop, desired_stop)
        improves = new_stop > old_stop
    else:
        new_stop = min(old_stop, desired_stop)
        improves = new_stop < old_stop

    logging.info(
        "[POSITION] side=%s entry=%s price=%s profit_points=%s "
        "current_SL=%s desired_SL=%s milestone=%s",
        side,
        entry,
        current_price,
        profit_points,
        old_stop,
        desired_stop,
        reached_step,
    )

    if not improves:
        return

    update_stop_order(product, saved, new_stop)

    saved["stop_price"] = str(new_stop)
    saved["highest_profit_step"] = max(
        int(saved.get("highest_profit_step", 0)),
        reached_step,
    )
    save_state(state)

    logging.info(
        "[TRAILING SL UPDATED] %s -> %s",
        old_stop,
        new_stop,
    )


# ==================================================
# MAIN BOT LOOP
# ==================================================

def bot_main_loop():
    logging.info("BTCUSD EMA 9/21 BOT STARTED")
    logging.info("Testnet URL: %s", BASE_URL)
    logging.info("Trading enabled: %s", ENABLE_TRADING)
    logging.info("Symbol: %s | Timeframe: %s", SYMBOL, TIMEFRAME)
    logging.info("Order size: %s contracts", ORDER_SIZE)
    logging.info("Leverage target: %sx", LEVERAGE)
    logging.info("Initial SL: %s%%", INITIAL_SL_PERCENT)
    logging.info("Trailing milestones: %s", TRAILING_STEPS)

    last_candle = None
    product = None

    while True:
        try:
            if product is None:
                product = get_product()
                logging.info(
                    "Product loaded: %s id=%s tick_size=%s",
                    SYMBOL,
                    product.get("id"),
                    product.get("tick_size"),
                )

            if ENABLE_TRADING:
                with trade_lock:
                    manage_trailing_stop(product)

            df = get_candles()

            candle_time = int(df.iloc[-2]["time"])
            candle_price = Decimal(str(df.iloc[-2]["close"]))

            logging.info(
                "[MARKET] price=%s EMA9=%.2f EMA21=%.2f",
                candle_price,
                df.iloc[-2]["ema9"],
                df.iloc[-2]["ema21"],
            )

            if candle_time != last_candle:
                last_candle = candle_time
                signal = get_signal(df)

                if signal:
                    logging.info("[SIGNAL] %s", signal.upper())

                    if not ENABLE_TRADING:
                        logging.warning(
                            "[SAFE MODE] Signal only; no order sent."
                        )
                    else:
                        with trade_lock:
                            place_entry(product, signal)

            time.sleep(POLL_SECONDS)

        except Exception as exc:
            logging.exception("[BOT ERROR] %s", exc)
            time.sleep(10)


# ==================================================
# START
# ==================================================

if __name__ == "__main__":
    Thread(
        target=bot_main_loop,
        daemon=True,
    ).start()

    port = int(os.environ.get("PORT", "10000"))

    logging.info("Starting web server on port %s", port)

    app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
    )
