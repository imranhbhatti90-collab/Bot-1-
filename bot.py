import os
import time
import json
import hmac
import hashlib
import logging
import requests
import pandas as pd
from decimal import Decimal
from threading import Thread
from flask import Flask

# ==========================================
# FLASK WEB SERVER (For Render Free Tier)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "BTCUSD EMA 9/21 Trading Bot is Live and Running!"

# ==========================================
# BTCUSD EMA 9/21 BOT - CONFIGURATION
# ==========================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

ENABLE_TRADING = (
    os.getenv("ENABLE_TRADING", "false").lower() == "true"
)

SYMBOL = "BTCUSD"
TIMEFRAME_MINUTES = 5

EMA_FAST = 9
EMA_SLOW = 21

ORDER_SIZE = 40
LEVERAGE = 50

INITIAL_SL_PERCENT = Decimal("0.20")

# Profit milestones in BTCUSD price points
TRAILING_STEPS = [
    (100, 0),
    (200, 100),
    (400, 300),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()


# ==========================================
# SIGNED DELTA API REQUEST
# ==========================================

def api_request(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Delta API credentials missing")

    method = method.upper()
    params = params or {}

    query = requests.compat.urlencode(params)
    request_path = path + (f"?{query}" if query else "")

    body_text = json.dumps(body, separators=(",", ":")) if body else ""
    timestamp = str(int(time.time()))

    message = (
        method + timestamp + request_path + body_text
    )

    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = session.request(
        method,
        BASE_URL + path,
        params=params,
        data=body_text if body else None,
        headers=headers,
        timeout=15,
    )

    response.raise_for_status()
    result = response.json()

    if result.get("success") is False:
        raise RuntimeError(f"Delta API error: {result}")

    return result


# ==========================================
# MARKET DATA
# ==========================================

def get_product():
    response = session.get(
        BASE_URL + "/v2/products",
        timeout=15,
    )
    response.raise_for_status()

    products = response.json().get("result", [])

    for product in products:
        if product.get("symbol") == SYMBOL:
            return product

    raise RuntimeError(
        f"{SYMBOL} product not found on Testnet"
    )


def get_candles():
    product = get_product()

    response = session.get(
        BASE_URL + "/v2/history/candles",
        params={
            "symbol": SYMBOL,
            "resolution": "5m",
            "start": int(time.time()) - 60 * 60 * 8,
            "end": int(time.time()),
        },
        timeout=15,
    )
    response.raise_for_status()

    candles = response.json().get("result", [])

    if len(candles) < EMA_SLOW + 5:
        raise RuntimeError("Not enough candle data")

    df = pd.DataFrame(candles)

    if "close" not in df.columns:
        raise RuntimeError(
            f"Unexpected candle response: {df.columns.tolist()}"
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

    # Use fully closed candles only.
    return product, df


# ==========================================
# CROSSOVER SIGNAL
# ==========================================

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
        "[CROSSOVER CHECK] "
        "previous_EMA9=%.2f previous_EMA21=%.2f "
        "EMA9=%.2f EMA21=%.2f BUY=%s SELL=%s",
        previous["ema9"],
        previous["ema21"],
        current["ema9"],
        current["ema21"],
        buy,
        sell,
    )

    return "buy" if buy else "sell" if sell else None


# ==========================================
# POSITION CHECK
# ==========================================

def get_positions():
    result = api_request("GET", "/v2/positions")
    return result.get("result", [])


def has_open_position():
    for position in get_positions():
        if (
            position.get("product_symbol") == SYMBOL
            or position.get("symbol") == SYMBOL
        ):
            size = float(position.get("size", 0) or 0)

            if size != 0:
                return True

    return False


# ==========================================
# SAFE ORDER PLACEHOLDER
# ==========================================

def place_entry_order(product, side):
    if not ENABLE_TRADING:
        logging.warning(
            "[SAFE MODE] ENABLE_TRADING=false. "
            "No order sent. Signal=%s",
            side.upper(),
        )
        return

    raise RuntimeError(
        "Trading blocked: exchange-side stop-loss, "
        "trailing-stop updates, and leverage are not "
        "yet verified. No entry order was sent."
    )


# ==========================================
# TRADING BOT BACKGROUND LOOP
# ==========================================

def bot_main_loop():
    logging.info("BTCUSD EMA BOT STARTED")
    logging.info("Trading enabled: %s", ENABLE_TRADING)
    logging.info("Symbol: %s | Timeframe: 5 minutes", SYMBOL)
    logging.info("Order size setting: %s", ORDER_SIZE)
    logging.info("Leverage target: %sx (not configured)", LEVERAGE)

    last_candle = None

    while True:
        try:
            product, df = get_candles()

            candle_time = int(df.iloc[-2]["time"])
            price = float(df.iloc[-2]["close"])

            logging.info(
                "[MARKET] %s price=%.2f EMA9=%.2f EMA21=%.2f",
                SYMBOL,
                price,
                df.iloc[-2]["ema9"],
                df.iloc[-2]["ema21"],
            )

            if candle_time != last_candle:
                last_candle = candle_time
                signal = get_signal(df)

                if signal:
                    logging.info("[SIGNAL] %s", signal.upper())

                    if has_open_position():
                        logging.info(
                            "[NO TRADE] Position already open"
                        )
                    else:
                        place_entry_order(product, signal)

            time.sleep(15)

        except Exception as exc:
            logging.exception("[BOT ERROR] %s", exc)
            time.sleep(15)


# ==========================================
# START WEB SERVER & THREAD
# ==========================================

if __name__ == "__main__":
    # Start the trading bot in a separate background thread
    bot_thread = Thread(target=bot_main_loop, daemon=True)
    bot_thread.start()

    # Start Flask on main thread to keep Render happy with port binding
    port = int(os.environ.get("PORT", 10000))
    logging.info("Starting Flask web server on port %s...", port)
    app.run(host="0.0.0.0", port=port)
