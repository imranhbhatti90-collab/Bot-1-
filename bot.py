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

# ================ SETTINGS ================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
SYMBOL = "BTCUSD"
TIMEFRAME = "5m"

ORDER_SIZE = 40
LEVERAGE = 50
CHECK_INTERVAL = 60

# Safety: verify the order and protection flow before enabling.
ENABLE_TRADING = False

# Fixed stop distance from entry, in percent.
STOP_LOSS_PERCENT = 0.20

# Trailing stop distance, in USD.
TRAIL_AMOUNT_USD = 100

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

session = requests.Session()
last_candle_time = None
last_signal = None
lock = threading.Lock()


# ================ WEB STATUS ================

@app.route("/")
def home():
    mode = "AUTO TRADING OFF"
    if ENABLE_TRADING:
        mode = "AUTO TRADING ON"
    return f"BTCUSD EMA Bot | {mode}"


# ================ SIGNED API ================

def api_request(method, path, params=None, payload=None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError(
            "Missing DELTA_API_KEY or DELTA_API_SECRET in Render."
        )

    params = params or {}
    query = urlencode(params)
    body = json.dumps(payload, separators=(",", ":")) if payload else ""
    timestamp = str(int(time.time()))

    signature_data = (
        method.upper()
        + timestamp
        + path
        + (("?" + query) if query else "")
        + body
    )

    signature = hmac.new(
        API_SECRET.encode(),
        signature_data.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    url = BASE_URL + path
    response = session.request(
        method.upper(),
        url,
        params=params or None,
        data=body if body else None,
        headers=headers,
        timeout=15
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        raise RuntimeError(f"Delta API error: {data}")

    return data.get("result")


# ================ MARKET DATA ================

def get_candles():
    now = int(time.time())

    response = session.get(
        BASE_URL + "/v2/history/candles",
        params={
            "resolution": TIMEFRAME,
            "symbol": SYMBOL,
            "start": now - 86400,
            "end": now
        },
        timeout=15
    )
    response.raise_for_status()

    data = response.json()

    if not data.get("success"):
        raise RuntimeError(f"Candle API error: {data}")

    return sorted(data.get("result", []), key=lambda x: x["time"])


def calculate_ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)

    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result

    return result


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
    matches = [p for p in products if p.get("symbol") == SYMBOL]

    if not matches:
        raise RuntimeError(f"{SYMBOL} not found on Testnet.")

    product = matches[0]

    if not product.get("id"):
        raise RuntimeError("Product ID missing.")

    print(
        "[PRODUCT]",
        {
            "symbol": product.get("symbol"),
            "id": product.get("id"),
            "contract_value": product.get("contract_value"),
            "contract_unit_currency": product.get("contract_unit_currency"),
        },
        flush=True
    )

    return product


# ================ POSITION CHECK ================

def get_positions(product_id):
    result = api_request(
        "GET",
        "/v2/positions",
        params={"product_id": product_id}
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


# ================ LEVERAGE ================

def set_leverage(product_id):
    return api_request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        payload={"leverage": LEVERAGE}
    )


# ================ MARKET ORDER ================

def place_market_order(product_id, side):
    payload = {
        "product_id": product_id,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "market_order",
        "time_in_force": "ioc",
        "client_order_id": f"ema_{int(time.time())}"
    }

    return api_request("POST", "/v2/orders", payload=payload)


# ================ STOP-LOSS ================

def place_stop_loss(product_id, side, size, stop_price):
    closing_side = "sell" if side == "buy" else "buy"

    payload = {
        "product_id": product_id,
        "size": int(size),
        "side": closing_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(round(stop_price, 2)),
        "stop_trigger_method": "mark_price",
        "reduce_only": True,
        "client_order_id": f"sl_{int(time.time())}"
    }

    return api_request("POST", "/v2/orders", payload=payload)


# ================ TRAILING STOP ================

def place_trailing_stop(product_id, side, size):
    closing_side = "sell" if side == "buy" else "buy"

    payload = {
        "product_id": product_id,
        "size": int(size),
        "side": closing_side,
        "order_type": "market_order",
        "trail_amount": str(TRAIL_AMOUNT_USD),
        "stop_trigger_method": "mark_price",
        "reduce_only": True,
        "client_order_id": f"trail_{int(time.time())}"
    }

    return api_request("POST", "/v2/orders", payload=payload)


# ================ SIGNAL PROCESSING ================

def process_signal():
    global last_candle_time, last_signal

    candles = get_candles()

    if len(candles) < 201:
        print(f"[WAIT] Need 201 candles; received {len(candles)}", flush=True)
        return

    # Ignore unfinished candle.
    completed = candles[:-1]
    candle = completed[-1]

    if candle["time"] == last_candle_time:
        return

    last_candle_time = candle["time"]

    closes = [float(c["close"]) for c in completed]
    price = closes[-1]

    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    ema200 = calculate_ema(closes, 200)

    if None in (ema9, ema21, ema200):
        print("[WAIT] EMA data incomplete.", flush=True)
        return

    print(
        f"[MARKET] {SYMBOL} price={price:.2f} "
        f"EMA9={ema9:.2f} EMA21={ema21:.2f} "
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
        print("[SKIP] Same signal; duplicate order blocked.", flush=True)
        return

    if not ENABLE_TRADING:
        print("[SAFE MODE] Automatic trading disabled.", flush=True)
        return

    if not lock.acquire(blocking=False):
        print("[SKIP] Another order operation is active.", flush=True)
        return

    try:
        product = get_product()
        product_id = int(product["id"])

        set_leverage(product_id)

        positions = get_positions(product_id)
        existing = find_open_position(positions)

        if existing:
            print(
                "[SKIP] Position already open:",
                existing,
                flush=True
            )
            return

        # Place the entry order.
        order = place_market_order(product_id, signal)
        print("[ENTRY RESPONSE]", order, flush=True)

        if not isinstance(order, dict):
            raise RuntimeError("Unexpected entry order response.")

        state = str(order.get("state", "")).lower()
        entry_value = order.get("average_fill_price")
        filled_size = abs(int(order.get("size") or 0))

        if state != "closed" or not entry_value or filled_size <= 0:
            print(
                "[WARNING] Fill is not confirmed. "
                "Check Testnet orders before proceeding.",
                flush=True
            )
            return

        entry = float(entry_value)

        if signal == "buy":
            stop_price = entry * (1 - STOP_LOSS_PERCENT / 100)
        else:
            stop_price = entry * (1 + STOP_LOSS_PERCENT / 100)

        # Attach a fixed stop first.
        stop_result = place_stop_loss(
            product_id,
            signal,
            filled_size,
            stop_price
        )
        print("[STOP-LOSS RESPONSE]", stop_result, flush=True)

        # Do not place another protective order automatically:
        # multiple stop orders may interact, even with reduce_only.
        print(
            "[NOTICE] Fixed stop-loss submitted. "
            "Trailing-stop upgrade requires verified order management.",
            flush=True
        )

        last_signal = signal

    except Exception as exc:
        print("[TRADE ERROR]", exc, flush=True)

    finally:
        lock.release()


# ================ MAIN LOOP ================

def trading_loop():
    print("[BOT] Started. Automatic trading is OFF.", flush=True)

    while True:
        try:
            process_signal()
        except Exception as exc:
            print("[BOT ERROR]", exc, flush=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=trading_loop, daemon=True).start()

    port = int(os.environ.get("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
