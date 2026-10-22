import os
import time
import json
import hmac
import hashlib
import threading
import requests
from urllib.parse import urlencode
from flask import Flask

app = Flask(__name__)

# ============== SETTINGS ==============

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

SYMBOL = "BTCUSD"
TIMEFRAME = "5m"

ORDER_SIZE = 40
LEVERAGE = 50

CHECK_INTERVAL = 60

# Keep OFF until API access and order flow are verified.
# Enable through Render Environment Variable:
# ENABLE_TRADING=true
ENABLE_TRADING = (
    os.getenv("ENABLE_TRADING", "false").lower() == "true"
)

# Trailing-stop distance in USD.
TRAIL_AMOUNT_USD = 100

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

session = requests.Session()
session.headers.update({
    "User-Agent": "python-requests-delta-testnet-bot",
    "Accept": "application/json",
})

last_candle_time = None
last_signal = None

lock = threading.Lock()


# ============== WEB STATUS ==============

@app.route("/")
def home():
    mode = "AUTO TRADING ON" if ENABLE_TRADING else "AUTO TRADING OFF"

    return (
        f"Delta Testnet EMA Bot | {mode} | "
        f"{SYMBOL} | {TIMEFRAME} | "
        f"Size={ORDER_SIZE} | Leverage={LEVERAGE}x"
    )


# ============== API REQUESTS ==============

def api_request(method, path, params=None, payload=None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError(
            "Missing DELTA_API_KEY or DELTA_API_SECRET in Render."
        )

    method = method.upper()
    params = params or {}

    # Keep query string identical to the one sent in the URL.
    query_string = urlencode(params)
    query_part = f"?{query_string}" if query_string else ""

    # Send exactly the same compact JSON that is signed.
    body = (
        json.dumps(payload, separators=(",", ":"))
        if payload is not None
        else ""
    )

    timestamp = str(int(time.time()))

    signature_data = (
        method
        + timestamp
        + path
        + query_part
        + body
    )

    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        signature_data.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
    }

    response = session.request(
        method=method,
        url=BASE_URL + path,
        params=params or None,
        data=body if body else None,
        headers=headers,
        timeout=15
    )

    if not response.ok:
        raise RuntimeError(
            f"HTTP {response.status_code}: {response.text[:700]}"
        )

    data = response.json()

    if not data.get("success"):
        raise RuntimeError(f"Delta API error: {data}")

    return data.get("result")


# ============== MARKET DATA ==============

def get_candles():
    now = int(time.time())

    response = session.get(
        BASE_URL + "/v2/history/candles",
        params={
            "resolution": TIMEFRAME,
            "symbol": SYMBOL,
            "start": now - 86400,
            "end": now,
        },
        timeout=15
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        raise RuntimeError(f"Candle API error: {data}")

    return sorted(
        data.get("result", []),
        key=lambda candle: candle["time"]
    )


def calculate_ema(values, period):
    if len(values) < period:
        return None

    ema = sum(values[:period]) / period
    alpha = 2 / (period + 1)

    for value in values[period:]:
        ema = alpha * value + (1 - alpha) * ema

    return ema


# ============== PRODUCT ==============

def get_product():
    response = session.get(
        BASE_URL + "/v2/products",
        params={"page_size": 100},
        timeout=15
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        raise RuntimeError(f"Product API error: {data}")

    products = data.get("result", [])

    for product in products:
        if product.get("symbol") == SYMBOL:
            print(
                "[PRODUCT]",
                {
                    "symbol": product.get("symbol"),
                    "id": product.get("id"),
                    "contract_value": product.get("contract_value"),
                },
                flush=True
            )

            if not product.get("id"):
                raise RuntimeError("Product ID missing.")

            return product

    raise RuntimeError(
        f"{SYMBOL} was not found in the returned products."
    )


# ============== LEVERAGE ==============

def set_leverage(product_id):
    result = api_request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        payload={"leverage": LEVERAGE}
    )

    print("[LEVERAGE RESPONSE]", result, flush=True)
    return result


# ============== POSITIONS ==============

def get_positions(product_id):
    result = api_request(
        "GET",
        "/v2/positions/margined",
        params={"product_ids": str(product_id)}
    )

    if isinstance(result, dict):
        result = result.get("positions", [])

    return result or []


def find_open_position(positions):
    for position in positions:
        size = float(position.get("size") or 0)

        if size != 0:
            return position

    return None


# ============== MARKET ORDER ==============

def place_market_order(product_id, side):
    payload = {
        "product_id": product_id,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "market_order",
        "time_in_force": "ioc",
        "client_order_id": f"ema_{int(time.time())}",
    }

    result = api_request(
        "POST",
        "/v2/orders",
        payload=payload
    )

    print("[ENTRY RESPONSE]", result, flush=True)

    if not isinstance(result, dict):
        raise RuntimeError("Unexpected order response.")

    return result


# ============== TRAILING STOP ==============

def place_trailing_stop(product_id):
    """
    Submit a reduce-only trailing stop for the open position.

    This is a trailing stop, not a separate fixed-price stop.
    Confirm the returned order on the Testnet before relying on it.
    """

    payload = {
        "product_id": product_id,
        "stop_loss_order": {
            "order_type": "market_order",
            "trail_amount": str(TRAIL_AMOUNT_USD),
        },
        "bracket_stop_trigger_method": "mark_price",
    }

    result = api_request(
        "POST",
        "/v2/orders/bracket",
        payload=payload
    )

    print("[TRAILING STOP RESPONSE]", result, flush=True)
    return result


# ============== SIGNAL PROCESSING ==============

def process_signal():
    global last_candle_time, last_signal

    candles = get_candles()

    if len(candles) < 201:
        print(
            f"[WAIT] Need 201 candles; got {len(candles)}",
            flush=True
        )
        return

    # Ignore the current unfinished candle.
    completed = candles[:-1]

    if not completed:
        return

    candle = completed[-1]
    candle_time = candle["time"]

    if candle_time == last_candle_time:
        return

    closes = [float(c["close"]) for c in completed]

    price = closes[-1]
    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    ema200 = calculate_ema(closes, 200)

    if any(value is None for value in (ema9, ema21, ema200)):
        print("[WAIT] EMA data incomplete.", flush=True)
        return

    # Process each completed candle once.
    last_candle_time = candle_time

    print(
        f"[MARKET] {SYMBOL} "
        f"price={price:.2f} "
        f"EMA9={ema9:.2f} "
        f"EMA21={ema21:.2f} "
        f"EMA200={ema200:.2f}",
        flush=True
    )

    if ema9 > ema21 and price > ema200:
        signal = "buy"
    elif ema9 < ema21 and price < ema200:
        signal = "sell"
    else:
        signal = "wait"

    print(f"[SIGNAL] {signal.upper()}", flush=True)

    if signal == "wait":
        return

    if signal == last_signal:
        print(
            "[SKIP] Same signal as the previous processed signal.",
            flush=True
        )
        return

    if not ENABLE_TRADING:
        print(
            "[SAFE MODE] Trading is OFF. No order placed.",
            flush=True
        )
        return

    if not lock.acquire(blocking=False):
        print("[SKIP] Another operation is running.", flush=True)
        return

    try:
        product = get_product()
        product_id = int(product["id"])

        # Set requested leverage.
        set_leverage(product_id)

        # Do not add a second position to an existing position.
        positions = get_positions(product_id)
        existing = find_open_position(positions)

        if existing:
            print(
                "[SKIP] Existing position found; no new entry.",
                existing,
                flush=True
            )
            return

        # Submit the entry order.
        order = place_market_order(product_id, signal)

        state = str(order.get("state", "")).lower()
        filled_size = abs(int(order.get("size") or 0))
        entry_price = order.get("average_fill_price")

        # Only proceed when the API confirms a complete fill.
        if state != "closed" or not entry_price or filled_size == 0:
            print(
                "[WARNING] Fill not confirmed. "
                "No trailing stop submitted automatically. "
                "Check Testnet orders and positions.",
                flush=True
            )
            return

        print(
            f"[ENTRY CONFIRMED] side={signal} "
            f"price={entry_price} size={filled_size}",
            flush=True
        )

        # Add trailing protection after confirmed entry.
        try:
            place_trailing_stop(product_id)
        except Exception as stop_error:
            print(
                "[CRITICAL] Entry may be open, but trailing stop "
                "could not be confirmed. Check the position and "
                "orders immediately:",
                str(stop_error),
                flush=True
            )
            return

        last_signal = signal

    except Exception as exc:
        print("[TRADE ERROR]", str(exc), flush=True)

    finally:
        lock.release()


# ============== MAIN LOOP ==============

def trading_loop():
    print(
        "[BOT] Started | "
        f"Symbol={SYMBOL} | "
        f"Timeframe={TIMEFRAME} | "
        f"Size={ORDER_SIZE} | "
        f"Leverage={LEVERAGE}x | "
        f"Trading={'ON' if ENABLE_TRADING else 'OFF'}",
        flush=True
    )

    while True:
        try:
            process_signal()
        except Exception as exc:
            print("[BOT ERROR]", str(exc), flush=True)

        time.sleep(CHECK_INTERVAL)


# ============== START SERVER ==============

if __name__ == "__main__":
    threading.Thread(
        target=trading_loop,
        daemon=True
    ).start()

    port = int(os.environ.get("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
