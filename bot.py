import os
import time
import json
import hmac
import hashlib
import threading
import requests

from flask import Flask

app = Flask(__name__)

# ================= SETTINGS =================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
SYMBOL = "BTCUSD"
TIMEFRAME = "5m"

ORDER_SIZE = 40
LEVERAGE = 50
MARGIN_USD = 69

CHECK_INTERVAL = 60

# Safety: first verify the API and order settings.
ENABLE_TRADING = False

# SL distance as percentage of entry price.
SL_PERCENT = 0.20

# Trailing stop distance as percentage of best price.
TRAIL_PERCENT = 0.15

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

session = requests.Session()
last_candle_time = None
last_signal = None
trade_lock = threading.Lock()


# ================= WEB STATUS =================

@app.route("/")
def home():
    mode = "AUTO TRADING" if ENABLE_TRADING else "SIGNAL TESTING ONLY"
    return f"BTCUSD EMA Bot | {mode}"


# ================= DELTA API =================

def signed_request(method, path, payload=None, query=""):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Delta API credentials missing in Render.")

    timestamp = str(int(time.time()))
    body = json.dumps(payload, separators=(",", ":")) if payload else ""

    signature_data = (
        method.upper() + timestamp + path + query + body
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
        headers=headers,
        params=None,
        data=body if body else None,
        timeout=15,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("success", False):
        raise RuntimeError(f"Delta API rejected request: {result}")

    return result.get("result")


def get_candles():
    now = int(time.time())
    params = {
        "resolution": TIMEFRAME,
        "symbol": SYMBOL,
        "start": now - 86400,
        "end": now,
    }

    response = session.get(
        BASE_URL + "/v2/history/candles",
        params=params,
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"Candle API error: {data}")

    return sorted(data.get("result", []), key=lambda x: x["time"])


def ema(values, period):
    if len(values) < period:
        return None

    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)

    for price in values[period:]:
        value = alpha * price + (1 - alpha) * value

    return value


# ================= ACCOUNT / POSITION =================

def get_product():
    result = signed_request("GET", "/v2/products")
    products = result if isinstance(result, list) else []
    matches = [p for p in products if p.get("symbol") == SYMBOL]

    if not matches:
        raise RuntimeError("BTCUSD product not found on Testnet.")

    return matches[0]


def get_positions(product_id):
    query = f"?product_id={product_id}"
    result = signed_request("GET", "/v2/positions", query=query)

    if isinstance(result, dict):
        result = result.get("positions", [])

    return result or []


def set_leverage(product_id):
    path = f"/v2/products/{product_id}/orders/leverage"
    return signed_request(
        "POST",
        path,
        payload={"leverage": LEVERAGE},
    )


# ================= ORDERS =================

def place_market_order(side, product_id):
    payload = {
        "product_id": product_id,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "market_order",
        "time_in_force": "ioc",
        "client_order_id": f"ema_{SYMBOL}_{int(time.time())}",
    }

    print("[ORDER] Sending market order:", payload, flush=True)
    return signed_request("POST", "/v2/orders", payload=payload)


def place_stop_loss(side, product_id, price):
    closing_side = "sell" if side == "buy" else "buy"

    payload = {
        "product_id": product_id,
        "size": ORDER_SIZE,
        "side": closing_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(round(price, 2)),
        "stop_trigger_method": "mark_price",
        "reduce_only": True,
        "client_order_id": f"sl_{SYMBOL}_{int(time.time())}",
    }

    return signed_request("POST", "/v2/orders", payload=payload)


def place_trailing_stop(side, product_id, trail_amount):
    closing_side = "sell" if side == "buy" else "buy"

    payload = {
        "product_id": product_id,
        "size": ORDER_SIZE,
        "side": closing_side,
        "order_type": "market_order",
        "trail_amount": str(round(trail_amount, 2)),
        "stop_trigger_method": "mark_price",
        "reduce_only": True,
        "client_order_id": f"trail_{SYMBOL}_{int(time.time())}",
    }

    return signed_request("POST", "/v2/orders", payload=payload)


# ================= SIGNAL LOOP =================

def process_signal():
    global last_candle_time, last_signal

    candles = get_candles()

    if len(candles) < 201:
        print(f"[WAIT] Need 201 candles; got {len(candles)}", flush=True)
        return

    # Ignore the unfinished candle.
    completed = candles[:-1]
    candle = completed[-1]

    if candle["time"] == last_candle_time:
        return

    last_candle_time = candle["time"]

    closes = [float(c["close"]) for c in completed]
    price = closes[-1]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema200 = ema(closes, 200)

    print(
        f"[MARKET] {SYMBOL} price={price:.2f} "
        f"EMA9={ema9:.2f} EMA21={ema21:.2f} EMA200={ema200:.2f}",
        flush=True,
    )

    if ema9 > ema21 and price > ema200:
        signal = "buy"
    elif ema9 < ema21 and price < ema200:
        signal = "sell"
    else:
        signal = "wait"

    print(f"[SIGNAL] {signal.upper()}", flush=True)

    if signal == "wait" or signal == last_signal:
        return

    if not ENABLE_TRADING:
        print("[SAFE MODE] Automatic orders disabled.", flush=True)
        last_signal = signal
        return

    if not trade_lock.acquire(blocking=False):
        print("[WAIT] Another order process is active.", flush=True)
        return

    try:
        product = get_product()
        product_id = int(product["id"])

        # Confirm exchange allows the requested leverage.
        leverage_result = set_leverage(product_id)
        print("[LEVERAGE]", leverage_result, flush=True)

        positions = get_positions(product_id)
        open_positions = [
            p for p in positions
            if float(p.get("size", 0) or 0) != 0
        ]

        if open_positions:
            print("[SKIP] Position already open; no new order.", flush=True)
            return

        order_result = place_market_order(signal, product_id)
        print("[ORDER RESULT]", order_result, flush=True)

        # Do not attach protection unless the order was confirmed.
        order_state = str(order_result.get("state", "")).lower()
        filled_size = int(order_result.get("size", 0) or 0)

        if order_state not in ("closed", "filled") and not order_result.get(
            "average_fill_price"
        ):
            print("[WARNING] Fill not confirmed. Check Testnet orders.", flush=True)
            return

        entry = float(order_result.get("average_fill_price") or price)

        if signal == "buy":
            sl_price = entry * (1 - SL_PERCENT / 100)
        else:
            sl_price = entry * (1 + SL_PERCENT / 100)

        try:
            sl_result = place_stop_loss(signal, product_id, sl_price)
            print("[STOP LOSS RESULT]", sl_result, flush=True)
        except Exception as exc:
            print("[CRITICAL] Stop-loss placement failed:", exc, flush=True)
            print("[CRITICAL] Check/close the position manually immediately.", flush=True)
            return

        print(
            "[NOTICE] Trailing stop must be separately verified before enabling.",
            flush=True,
        )
        last_signal = signal

    except Exception as exc:
        print("[TRADE ERROR]", exc, flush=True)

    finally:
        trade_lock.release()


def trading_loop():
    print("[BOT] Started. Automatic trading is disabled.", flush=True)

    while True:
        try:
            process_signal()
        except Exception as exc:
            print("[BOT ERROR]", exc, flush=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
