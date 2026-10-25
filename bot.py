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

INITIAL_SL_PERCENT = 0.20
TRAILING_BUFFER_POINTS = 200

RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20
MIN_VOLUME_MULTIPLIER = 1.2

# Safe mode: do not submit orders in this version.
ENABLE_TRADING = False

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
        f"{SYMBOL} product nahi mila. "
        "Delta Testnet par symbol verify karein."
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
        "resolution": str(TIMEFRAME_MINUTES),
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

    # The final candle may still be forming.
    # Only completed candles are used for signals.
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
# RSI AND VOLUME FILTERS
# ==================================================

def check_market_filters(df, side):
    if "volume" not in df.columns:
        log.warning("Volume data missing; signal rejected.")
        return False

    if len(df) < max(RSI_PERIOD, VOLUME_MA_PERIOD) + 2:
        log.warning("Insufficient candles for filters.")
        return False

    close = df["close"]
    volume = df["volume"]

    delta = close.diff()

    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.rolling(RSI_PERIOD).mean()
    avg_loss = losses.rolling(RSI_PERIOD).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    # Previous row is the last completed candle.
    current_rsi = rsi.iloc[-1]

    volume_ma = volume.rolling(
        VOLUME_MA_PERIOD
    ).mean()

    current_volume = volume.iloc[-1]
    average_volume = volume_ma.iloc[-1]

    values = [current_rsi, current_volume, average_volume]

    if any(pd.isna(value) for value in values):
        log.warning("RSI/volume values invalid; signal rejected.")
        return False

    log.info(
        "[FILTER] Side=%s RSI=%.2f Volume=%.2f AvgVolume=%.2f",
        side.upper(),
        current_rsi,
        current_volume,
        average_volume,
    )

    if side == "buy" and current_rsi < 50:
        log.info("[FILTER BLOCKED] BUY: RSI below 50.")
        return False

    if side == "sell" and current_rsi > 50:
        log.info("[FILTER BLOCKED] SELL: RSI above 50.")
        return False

    if average_volume <= 0:
        log.warning("Average volume invalid; signal rejected.")
        return False

    if current_volume < average_volume * MIN_VOLUME_MULTIPLIER:
        log.info("[FILTER BLOCKED] Volume confirmation failed.")
        return False

    log.info("[FILTER PASSED] %s", side.upper())
    return True


# ==================================================
# EMA 9/21 CROSSOVER
# ==================================================

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

    log.info(
        "[MARKET] %s price=%.2f EMA9=%.2f EMA21=%.2f",
        SYMBOL, price, ema9, ema21
    )

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
# SAFE SIGNAL HANDLER
# ==================================================

def handle_signal(df, signal):
    side = signal["side"]

    if not side:
        log.info("[SIGNAL] No new EMA crossover.")
        return

    candle_id = signal["candle_id"]

    if candle_id == state.get("last_processed_candle"):
        log.info("[SIGNAL] Candle already processed.")
        return

    # Record the candle to prevent repeated signal handling.
    state["last_processed_candle"] = candle_id
    save_state()

    if not check_market_filters(df, side):
        log.info("[SIGNAL] %s rejected by filters.", side.upper())
        return

    log.info(
        "[SIGNAL] %s crossover approved by filters.",
        side.upper()
    )

    if not ENABLE_TRADING:
        log.info(
            "[SAFE MODE] No order placed. "
            "Entry/SL API execution is not enabled in this file."
        )
        return

    # Deliberately fail closed: never claim an order was placed
    # when a verified order execution function is unavailable.
    raise RuntimeError(
        "Trading execution is not implemented in this file. "
        "No order was submitted."
    )


# ==================================================
# MAIN BOT LOOP
# ==================================================

def bot_loop():
    log.info("Delta BTCUSD EMA bot started.")
    log.info(
        "Trading=%s | Order size=%s | Leverage setting=%sx",
        ENABLE_TRADING, ORDER_SIZE, LEVERAGE
    )

    while True:
        try:
            df, signal = scan_signal()

            bot_status["last_signal"] = (
                signal["side"].upper()
                if signal["side"] else "NONE"
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
        "timeframe_minutes": TIMEFRAME_MINUTES,
        "trading_enabled": ENABLE_TRADING,
    })


@app.route("/status")
def status():
    return jsonify(bot_status)


@app.route("/check")
def check():
    try:
        df, signal = scan_signal()

        return jsonify({
            "ok": True,
            "signal": signal,
            "candles": len(df),
        })

    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


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
