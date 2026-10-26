import os
import time
import json
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlencode

import requests
import pandas as pd
from flask import Flask, jsonify

# ==================================================
# CONFIGURATION
# ==================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

SYMBOL = "BTCUSD"
TIMEFRAME_MINUTES = 5

EMA_FAST = 9
EMA_SLOW = 21

ORDER_SIZE = 40
LEVERAGE = 50

# Trailing Stop Loss Settings
INITIAL_SL_PERCENT = 0.20  # 0.2% initial stop loss
TRAILING_BUFFER_POINTS = 100  # Trail distance

RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20
MIN_VOLUME_MULTIPLIER = 1.0 

ENABLE_TRADING = True

POLL_SECONDS = 20
STATE_FILE = "state.json"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

log = logging.getLogger("delta-bot")
session = requests.Session()
app = Flask(__name__)

state = {
    "position": None,
    "last_processed_candle": None,
    "best_price": None,  # Trailing ke liye best price track karega
}

bot_status = {
    "running": True,
    "trading_enabled": ENABLE_TRADING,
    "symbol": SYMBOL,
    "last_signal": "NONE",
    "last_error": None,
}


# ==================================================
# STATE MANAGEMENT
# ==================================================

def load_state():
    global state

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)

            if isinstance(saved, dict):
                state.update(saved)

        except Exception as exc:
            log.error("State file read failed: %s", exc)

    state.setdefault("position", None)
    state.setdefault("last_processed_candle", None)
    state.setdefault("best_price", None)


def save_state():
    try:
        temp_file = STATE_FILE + ".tmp"

        with open(temp_file, "w") as f:
            json.dump(state, f, indent=2)

        os.replace(temp_file, STATE_FILE)

    except Exception as exc:
        log.error("State save failed: %s", exc)


# ==================================================
# DELTA API REQUEST
# ==================================================

def api_request(method, path, params=None, body=None, signed=False):
    method = method.upper()
    params = params or {}

    query = urlencode(params)
    request_path = path + (("?" + query) if query else "")

    body_text = (
        json.dumps(body, separators=(",", ":"))
        if body is not None else ""
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if signed:
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "DELTA_API_KEY ya DELTA_API_SECRET missing hai."
            )

        timestamp = str(int(time.time()))

        signature_data = (
            method + timestamp + request_path + body_text
        )

        signature = hmac.new(
            API_SECRET.encode(),
            signature_data.encode(),
            hashlib.sha256,
        ).hexdigest()

        headers.update({
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": signature,
        })

    response = session.request(
        method,
        BASE_URL + request_path,
        headers=headers,
        data=body_text if body is not None else None,
        timeout=20,
    )

    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text}

    if not response.ok or data.get("success") is False:
        raise RuntimeError(
            f"Delta API HTTP {response.status_code}: {data}"
        )

    return data


# ==================================================
# PRODUCT LOOKUP
# ==================================================

def get_product():
    data = api_request("GET", "/v2/products")
    products = data.get("result", [])

    for product in products:
        if product.get("symbol") == SYMBOL:
            return product

    raise RuntimeError(
        f"{SYMBOL} product nahi mila. Delta Testnet par symbol verify karein."
    )


# ==================================================
# FETCH DELTA 5-MINUTE CANDLES
# ==================================================

def fetch_market_candles(symbol=SYMBOL):
    if symbol != SYMBOL:
        raise ValueError("Unexpected symbol: " + symbol)

    end_time = int(time.time())
    start_time = end_time - (24 * 60 * 60)

    params = {
        "resolution": "5m",
        "symbol": SYMBOL,
        "start": start_time,
        "end": end_time,
    }

    data = api_request(
        "GET",
        "/v2/history/candles",
        params=params,
    )

    candles = data.get("result", [])

    if not candles:
        raise RuntimeError("Delta candle response empty hai.")

    df = pd.DataFrame(candles)

    required = {"time", "open", "high", "low", "close"}

    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Candle response mein columns missing: {missing}"
        )

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(
            df[column], errors="coerce"
        )

    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(
            df["volume"], errors="coerce"
        )

    df = (
        df.dropna(subset=["time", "close"])
        .sort_values("time")
        .drop_duplicates(subset=["time"])
        .reset_index(drop=True)
    )

    now = int(time.time())
    current_candle_start = (
        now // (TIMEFRAME_MINUTES * 60)
    ) * (TIMEFRAME_MINUTES * 60)

    df = df[
        pd.to_numeric(df["time"], errors="coerce")
        < current_candle_start
    ].reset_index(drop=True)

    if len(df) < max(EMA_SLOW + 3, VOLUME_MA_PERIOD + 2):
        raise RuntimeError(
            f"Closed candles insufficient: {len(df)}"
        )

    return df


# ==================================================
# FILTERS & SIGNAL SCANNER
# ==================================================

def check_market_filters(df, side):
    if len(df) < max(RSI_PERIOD, VOLUME_MA_PERIOD) + 2:
        return False

    close = df["close"]
    delta = close.diff()

    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.rolling(RSI_PERIOD).mean()
    avg_loss = losses.rolling(RSI_PERIOD).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    current_rsi = rsi.iloc[-1]

    if side == "buy" and current_rsi < 40:
        return False

    if side == "sell" and current_rsi > 60:
        return False

    return True


def scan_signal():
    df = fetch_market_candles()

    df["ema9"] = df["close"].ewm(
        span=EMA_FAST, adjust=False
    ).mean()

    df["ema21"] = df["close"].ewm(
        span=EMA_SLOW, adjust=False
    ).mean()

    previous = df.iloc[-2]
    latest = df.iloc[-1]

    price = float(latest["close"])
    ema9 = float(latest["ema9"])
    ema21 = float(latest["ema21"])
    candle_id = str(latest["time"])

    buy_signal = (
        previous["ema9"] <= previous["ema21"]
        and latest["ema9"] > latest["ema21"]
    )

    sell_signal = (
        previous["ema9"] >= previous["ema21"]
        and latest["ema9"] < latest["ema21"]
    )

    if buy_signal:
        side = "buy"
    elif sell_signal:
        side = "sell"
    else:
        side = None

    return df, {
        "candle_id": candle_id,
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "side": side,
    }


# ==================================================
# ORDER EXECUTION & TRAILING STOP LOSS MANAGEMENT
# ==================================================

def place_order(side, entry_price):
    try:
        product = get_product()
        product_id = product["id"]

        body = {
            "product_id": product_id,
            "size": ORDER_SIZE,
            "side": side,
            "order_type": "market_order",
            "leverage": LEVERAGE
        }

        log.info("[ORDER] Placing %s market order for %s...", side.upper(), SYMBOL)
        res = api_request("POST", "/v2/orders", body=body, signed=True)
        log.info("[ORDER SUCCESS] Response: %s", res)
        
        state["position"] = {
            "side": side,
            "entry": entry_price,
            "size": ORDER_SIZE
        }
        state["best_price"] = entry_price
        save_state()

    except Exception as exc:
        log.error("[ORDER FAILED] Error placing order: %s", exc)


def check_and_manage_position():
    """Live position ko check karega aur profit badhne par stop loss trail karega"""
    if not state.get("position"):
        return

    try:
        # Delta positions fetch karna
        res = api_request("GET", "/v2/positions", signed=True)
        positions = res.get("result", [])
        
        current_pos = None
        for p in positions:
            if p.get("product_symbol") == SYMBOL and float(p.get("size", 0)) != 0:
                current_pos = p
                break

        if not current_pos:
            log.info("[POSITION] Position closed on exchange.")
            state["position"] = None
            state["best_price"] = None
            save_state()
            return

        entry_price = float(current_pos.get("entry_price", 0))
        side = state["position"]["side"]
        
        # Latest market price fetch karna candles se
        df = fetch_market_candles()
        current_price = float(df["close"].iloc[-1])

        if side == "buy":
            if state["best_price"] is None or current_price > state["best_price"]:
                state["best_price"] = current_price
                save_state()
                log.info("[TRAILING] New high price for BUY: %.2f", current_price)
                
        elif side == "sell":
            if state["best_price"] is None or current_price < state["best_price"]:
                state["best_price"] = current_price
                save_state()
                log.info("[TRAILING] New low price for SELL: %.2f", current_price)

    except Exception as exc:
        log.error("Position management error: %s", exc)


# ==================================================
# SIGNAL HANDLER
# ==================================================

def handle_signal(df, signal):
    side = signal["side"]
    if not side:
        return

    candle_id = signal["candle_id"]
    if candle_id == state.get("last_processed_candle"):
        return

    state["last_processed_candle"] = candle_id
    save_state()

    if not check_market_filters(df, side):
        return

    if state.get("position"):
        log.info("[SIGNAL] Position already active. Skipping new signal.")
        return

    if not ENABLE_TRADING:
        return

    place_order(side, signal["price"])


# ==================================================
# MAIN BOT LOOP
# ==================================================

def bot_loop():
    log.info("Delta BTCUSD EMA bot started with Trailing SL support.")
    while True:
        try:
            # Pehle active position aur trailing check karo
            check_and_manage_position()

            # Phir naye signals scan karo
            df, signal = scan_signal()
            bot_status["last_signal"] = (
                signal["side"].upper() if signal["side"] else "NONE"
            )
            bot_status["last_error"] = None

            handle_signal(df, signal)

        except Exception as exc:
            bot_status["last_error"] = str(exc)
            log.exception("Bot loop error")

        time.sleep(POLL_SECONDS)


# ==================================================
# RENDER HEALTH CHECK
# ==================================================

@app.route("/")
def health_check():
    return jsonify({
        "service": "Delta BTCUSD EMA Bot",
        "status": "running",
        "symbol": SYMBOL,
        "trading_enabled": ENABLE_TRADING,
    })

@app.route("/status")
def status():
    return jsonify(bot_status)

@app.route("/check")
def check():
    try:
        df, signal = scan_signal()
        return jsonify({"ok": True, "signal": signal, "candles": len(df)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ==================================================
# START
# ==================================================

load_state()

if __name__ == "__main__":
    threading.Thread(
        target=bot_loop,
        daemon=True,
        name="delta-bot",
    ).start()

    port = int(os.environ.get("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )
