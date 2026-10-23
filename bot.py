import os
import time
import json
import hmac
import hashlib
import logging
import requests
import pandas as pd
from decimal import Decimal

# ==========================================
# BTCUSD EMA 9/21 BOT - DELTA TESTNET
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
# STOP-LOSS PRICE CALCULATION
# ==========================================

def initial_stop_loss(entry_price, side):
    entry = Decimal(str(entry_price))
    pct = INITIAL_SL_PERCENT / Decimal("100")

    if side == "buy":
        return entry * (Decimal("1") - pct)

    return entry * (Decimal("1") + pct)


def trailing_stop_price(entry_price, side, profit_points):
    """
    Returns a stop price only when one of the configured
    profit milestones is reached.
    """
    entry = Decimal(str(entry_price))
    locked_points = None

    for trigger, lock in TRAILING_STEPS:
        if profit_points >= trigger:
            locked_points = lock

    if locked_points is None:
        return None

    locked = Decimal(str(locked_points))

    if side == "buy":
        return entry + locked

    return entry - locked


# ==========================================
# SAFE ORDER PLACEHOLDER
# ==========================================

def place_entry_order(product, side):
    """
    Trading is deliberately blocked until the actual
    exchange stop-loss and leverage handling are verified.
    """
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
# MAIN LOOP
# ==========================================

def main():
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

        except KeyboardInterrupt:
            logging.info("Bot stopped")
            break

        except Exception as exc:
            logging.exception("[BOT ERROR] %s", exc)
            time.sleep(15)


if __name__ == "__main__":
    main()
