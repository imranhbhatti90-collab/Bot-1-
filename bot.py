import os
import json
import time
import hmac
import hashlib
import threading
import logging
from urllib.parse import urlencode, quote

import requests
import pandas as pd
from flask import Flask, jsonify

# =====================================================
# DELTA EXCHANGE INDIA TESTNET - CONFIGURATION
# =====================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINS = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XAUTUSD",
    "PAXGUSD",
    "SILVERUSD"
]

TIMEFRAME = "5m"

ORDER_SIZE = 40
MAX_POSITIONS = 6

TRAIL_PCT = 0.003
COOLDOWN_SECONDS = 300

CHECK_INTERVAL = 30
STATE_FILE = "state.json"

# Stop must remain at least this far from current Last Price.
MIN_STOP_GAP_PCT = 0.0005

# =====================================================
# FLASK SERVER
# =====================================================

app = Flask(__name__)

@app.route("/")
def home():
    return (
        "DELTA TESTNET BOT | EMA CROSS + RSI + VOL "
        "| SAFE LIVE TRAILING STOP | MAX POSITIONS 6"
    )

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "environment": "TESTNET",
        "max_positions": MAX_POSITIONS
    }), 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# =====================================================
# LOGGING
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s"
)


def log(message):
    print(message, flush=True)


# =====================================================
# STATE
# =====================================================

def load_state():

    default = {
        "peaks": {},
        "stops": {},
        "last_trade": {}
    }

    try:
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)

        for key in default:
            if key not in saved or not isinstance(saved[key], dict):
                saved[key] = {}

        return saved

    except Exception:
        return default


def save_state():

    try:
        temp_file = STATE_FILE + ".tmp"

        with open(temp_file, "w") as f:
            json.dump(state, f)

        os.replace(temp_file, STATE_FILE)

    except Exception as e:
        log(f"[STATE SAVE ERROR] {e}")


state = load_state()

# =====================================================
# API REQUEST
# =====================================================

session = requests.Session()


def api_req(method, path, params=None, body=None):

    if not API_KEY or not API_SECRET:
        return {"error": "API credentials missing"}, 401

    method = method.upper()

    if params:
        qs = urlencode(params, quote_via=quote)
        query_string = "?" + qs
    else:
        query_string = ""

    payload = json.dumps(
        body,
        separators=(",", ":")
    ) if body is not None else ""

    timestamp = str(int(time.time()))

    message = (
        method
        + timestamp
        + path
        + query_string
        + payload
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
        "User-Agent": "python-delta-testnet-bot"
    }

    url = BASE_URL + path + query_string

    try:

        response = session.request(
            method=method,
            url=url,
            headers=headers,
            data=payload if payload else None,
            timeout=(5, 15)
        )

        try:
            result = response.json()
        except Exception:
            result = {"error": response.text[:500]}

        return result, response.status_code

    except Exception as e:
        return {"error": str(e)}, 500


# =====================================================
# LIVE TRADE PRICE
# =====================================================

def get_live_price(symbol):

    path = f"/v2/trades/{symbol}"

    try:
        response = requests.get(
            BASE_URL + path,
            headers={
                "Accept": "application/json",
                "User-Agent": "python-delta-testnet-bot"
            },
            timeout=(5, 10)
        )

        if response.status_code != 200:
            log(
                f"[TRADE PRICE ERROR] {symbol} "
                f"HTTP={response.status_code}"
            )
            return None

        data = response.json()

        if data.get("success") is not True:
            log(f"[TRADE PRICE ERROR] {symbol}: {data}")
            return None

        result = data.get("result", {})

        if isinstance(result, dict):
            trades = result.get("trades", [])
        elif isinstance(result, list):
            trades = result
        else:
            trades = []

        if not trades:
            log(f"[NO RECENT TRADES] {symbol}")
            return None

        latest = max(
            trades,
            key=lambda x: int(x.get("timestamp", 0))
        )

        price = float(latest.get("price", 0))

        if price <= 0:
            log(f"[INVALID TRADE PRICE] {symbol}")
            return None

        log(
            f"[LIVE TRADE PRICE] {symbol} "
            f"PRICE={price} "
            f"TIMESTAMP={latest.get('timestamp')}"
        )

        return price

    except Exception as e:
        log(f"[LIVE PRICE ERROR] {symbol}: {e}")
        return None


# =====================================================
# CANDLE DATA
# =====================================================

def get_candles(symbol):

    end = int(time.time())
    start = end - (600 * 60)

    params = {
        "resolution": TIMEFRAME,
        "symbol": symbol,
        "start": str(start),
        "end": str(end)
    }

    res, code = api_req(
        "GET",
        "/v2/history/candles",
        params=params
    )

    if (
        code == 200
        and isinstance(res, dict)
        and res.get("success")
        and res.get("result")
    ):
        return res["result"]

    log(f"[CANDLE ERROR] {symbol} HTTP={code} {res}")

    return []


# =====================================================
# POSITIONS
# =====================================================

def get_pos():

    res, code = api_req(
        "GET",
        "/v2/positions/margined"
    )

    if code != 200 or not isinstance(res, dict):

        log(f"[POSITION ERROR] HTTP={code} {res}")

        return None

    if res.get("success") is not True:

        log(f"[POSITION ERROR] {res}")

        return None

    result = res.get("result", [])

    if isinstance(result, dict):
        result = [result]

    if not isinstance(result, list):
        return None

    positions = []

    for p in result:

        try:

            if float(p.get("size", 0)) != 0:
                positions.append(p)

        except Exception:
            continue

    return positions


def parse_pos_symbol(p):

    product = p.get("product") or {}

    return (
        p.get("product_symbol")
        or product.get("symbol")
        or ""
    )


# =====================================================
# OPEN ORDERS
# =====================================================

def get_orders():

    res, code = api_req(
        "GET",
        "/v2/orders",
        params={
            "states": "open,pending",
            "page_size": 100
        }
    )

    if (
        code == 200
        and isinstance(res, dict)
        and res.get("success") is True
    ):

        result = res.get("result", [])

        if isinstance(result, list):
            return result

    log(f"[ORDER ERROR] HTTP={code} {res}")

    return None


def is_active_stop(order, symbol):

    return (
        order.get("product_symbol") == symbol
        and order.get("stop_order_type") == "stop_loss_order"
        and order.get("state") in ("open", "pending")
    )


# =====================================================
# VERIFY STOP ORDER
# =====================================================

def verify_stop(symbol, order_id):

    orders = get_orders()

    if orders is None:
        return False

    for order in orders:

        if str(order.get("id")) != str(order_id):
            continue

        reduce_only = order.get("reduce_only")

        if (
            is_active_stop(order, symbol)
            and reduce_only in (True, 1, "true", "True")
        ):
            return True

    return False


# =====================================================
# CANCEL ONE STOP
# =====================================================

def cancel_stop(order):

    order_id = order.get("id")
    product_id = order.get("product_id")

    if not order_id or not product_id:

        log("[CANCEL ERROR] Order ID/product ID missing")

        return False

    body = {
        "id": int(order_id),
        "product_id": int(product_id)
    }

    res, code = api_req(
        "DELETE",
        "/v2/orders",
        body=body
    )

    if (
        code == 200
        and isinstance(res, dict)
        and res.get("success")
    ):

        log(f"[CANCEL SENT] ID={order_id}")

        return True

    log(f"[CANCEL FAILED] ID={order_id} HTTP={code} {res}")

    return False


# =====================================================
# CANCEL OLD STOPS AFTER NEW STOP VERIFIED
# =====================================================

def cancel_old_stops(symbol, keep_order_id):

    orders = get_orders()

    if orders is None:

        log("[CANCEL BLOCKED] Cannot verify open orders")

        return False

    success = True

    for order in orders:

        if not is_active_stop(order, symbol):
            continue

        if str(order.get("id")) == str(keep_order_id):
            continue

        if not cancel_stop(order):
            success = False

    return success


# =====================================================
# STOP PRICE VALIDATION
# =====================================================

def valid_stop_price(side, stop_price, live_price):

    if live_price is None or live_price <= 0:
        return False

    if stop_price is None or stop_price <= 0:
        return False

    gap = live_price * MIN_STOP_GAP_PCT

    if side == "buy":
        # LONG position: SELL stop below current price.
        return stop_price < (live_price - gap)

    if side == "sell":
        # SHORT position: BUY stop above current price.
        return stop_price > (live_price + gap)

    return False


# =====================================================
# PLACE STOP LOSS
# =====================================================

def place_stop(symbol, side, size, stop_price):

    sz = int(abs(float(size)))

    if sz <= 0 or stop_price <= 0:

        log("[STOP ERROR] Invalid size or price")

        return False

    live_price = get_live_price(symbol)

    if live_price is None:

        log(f"[STOP BLOCKED] {symbol} live price unavailable")

        return False

    if not valid_stop_price(side, stop_price, live_price):

        log(
            f"[STOP BLOCKED] {symbol} "
            f"SIDE={side} "
            f"STOP={stop_price:.2f} "
            f"LAST={live_price:.2f} "
            "Wrong-side or too close to Last Price"
        )

        return False

    stop_side = "sell" if side == "buy" else "buy"

    body = {
        "product_symbol": symbol,
        "size": sz,
        "side": stop_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": f"{float(stop_price):.2f}",
        "reduce_only": True,
        "stop_trigger_method": "last_traded_price"
    }

    res, code = api_req(
        "POST",
        "/v2/orders",
        body=body
    )

    result = (
        res.get("result", {})
        if isinstance(res, dict)
        else {}
    )

    if (
        code != 200
        or res.get("success") is not True
        or not result.get("id")
    ):

        log(f"[STOP FAILED] {symbol} HTTP={code} {res}")

        return False

    order_id = result.get("id")

    time.sleep(1)

    if not verify_stop(symbol, order_id):

        log(
            f"[STOP NOT VERIFIED] {symbol} "
            f"ID={order_id}. Old stop will NOT be cancelled."
        )

        return False

    state["stops"][symbol] = float(stop_price)

    save_state()

    log(
        f"[STOP VERIFIED] {symbol} "
        f"{stop_side.upper()} "
        f"STOP={stop_price:.2f} "
        f"ID={order_id}"
    )

    cancel_old_stops(symbol, order_id)

    return True


# =====================================================
# SIGNAL CALCULATION
# =====================================================
def check_signal(candles, symbol):

    if len(candles) < 50:
        log(f"[SCAN] {symbol} not enough candles")
        return None

    try:
        df = pd.DataFrame(candles)

        if "close" not in df.columns:
            df = pd.DataFrame(
                candles,
                columns=[
                    "time", "open", "high",
                    "low", "close", "volume"
                ]
            )

        df["close"] = pd.to_numeric(df["close"])
        df["volume"] = pd.to_numeric(df["volume"])

        df["ema9"] = df["close"].ewm(
            span=9, adjust=False
        ).mean()

        df["ema21"] = df["close"].ewm(
            span=21, adjust=False
        ).mean()

        delta = df["close"].diff()

        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()

        rs = gain / loss.replace(0, float("nan"))

        df["rsi"] = 100 - (100 / (1 + rs))

        df.loc[(loss == 0) & (gain > 0), "rsi"] = 100
        df.loc[(gain == 0) & (loss > 0), "rsi"] = 0
        df.loc[(gain == 0) & (loss == 0), "rsi"] = 50

        df["vol_avg"] = df["volume"].rolling(20).mean()

        last = df.iloc[-1]

        ema9 = float(last["ema9"])
        ema21 = float(last["ema21"])
        rsi = float(last["rsi"])
        vol = float(last["volume"])
        vol_avg = float(last["vol_avg"])
        close_px = float(last["close"])

        if pd.isna(rsi) or pd.isna(vol_avg):
            return None

        log(
            f"[SCAN] {symbol} "
            f"Close={close_px:.2f} "
            f"EMA9={ema9:.2f} "
            f"EMA21={ema21:.2f} "
            f"RSI={rsi:.1f} "
            f"VOL={vol:.0f}/{vol_avg:.0f}"
        )

        # Bullish trend setup
        buy_setup = (
            ema9 > ema21
            and 45 <= rsi <= 70
            and vol > vol_avg * 0.6
        )

        # Bearish trend setup
        sell_setup = (
            ema9 < ema21
            and 30 <= rsi <= 55
            and vol > vol_avg * 0.6
        )

        if buy_setup:
            log(f"[SIGNAL BUY] {symbol}")
            return "buy"

        if sell_setup:
            log(f"[SIGNAL SELL] {symbol}")
            return "sell"

        log(f"[NO TRADE] {symbol} filters not satisfied")
        return None

    except Exception as e:
        log(f"[SIGNAL ERROR] {symbol}: {e}")
        return None


# =====================================================
# ENTRY ORDER
# =====================================================

def place_entry(symbol, side):

    body = {
        "product_symbol": symbol,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "market_order"
    }

    res, code = api_req(
        "POST",
        "/v2/orders",
        body=body
    )

    if (
        code == 200
        and isinstance(res, dict)
        and res.get("success") is True
    ):

        log(
            f"[ENTRY ACCEPTED] {symbol} "
            f"{side} SIZE={ORDER_SIZE}"
        )

        return True

    log(f"[ENTRY FAILED] {symbol} HTTP={code} {res}")

    return False


# =====================================================
# TRAILING STOP MANAGEMENT
# =====================================================

def handle_trailing():

    positions = get_pos()

    if positions is None:

        log("[TRAIL BLOCKED] Position API unavailable")

        return

    active_symbols = set()

    for p in positions:

        try:

            size = float(p.get("size", 0))

            if size == 0:
                continue

            symbol = parse_pos_symbol(p)

            if symbol not in COINS:
                continue

            active_symbols.add(symbol)

            entry = float(
                p.get("entry_price")
                or p.get("average_price")
                or 0
            )

            if entry <= 0:

                log(f"[TRAIL ERROR] {symbol} entry price missing")

                continue

            curr = get_live_price(symbol)

            if curr is None:

                log(f"[TRAIL SKIP] {symbol} live price unavailable")

                continue

            old_peak = float(
                state["peaks"].get(symbol, entry)
            )

            old_stop = float(
                state["stops"].get(symbol, 0)
            )

            # =========================================
            # LONG POSITION
            # =========================================

            if size > 0:

                new_peak = max(old_peak, entry, curr)

                trail_stop = new_peak * (1 - TRAIL_PCT)

                # Discard stale stop if it is on wrong side.
                if old_stop >= curr:
                    old_stop = 0

                if old_stop > 0:
                    trail_stop = max(trail_stop, old_stop)

                # Always keep candidate below Last Price.
                if trail_stop >= curr:
                    trail_stop = curr * (1 - TRAIL_PCT)

                log(
                    f"[TRAIL] {symbol} LONG "
                    f"LIVE={curr:.2f} "
                    f"ENTRY={entry:.2f} "
                    f"PEAK={new_peak:.2f} "
                    f"STOP={trail_stop:.2f}"
                )

                state["peaks"][symbol] = new_peak

                if (
                    old_stop <= 0
                    or trail_stop > old_stop
                ):

                    if place_stop(
                        symbol,
                        "buy",
                        size,
                        trail_stop
                    ):

                        log(f"[TRAIL UPDATED] {symbol} LONG")

            # =========================================
            # SHORT POSITION
            # =========================================

            else:

                # For a short, the best price is the lowest
                # price reached since entry.
                if old_peak <= 0:
                    old_peak = entry

                new_peak = min(
                    old_peak,
                    entry,
                    curr
                )

                trail_stop = new_peak * (1 + TRAIL_PCT)

                # IMPORTANT FIX:
                # A stale stop below current price is invalid
                # for a short protective BUY stop.
                if old_stop > 0 and old_stop <= curr:
                    log(
                        f"[STALE STOP IGNORED] {symbol} "
                        f"OLD_STOP={old_stop:.2f} "
                        f"LAST={curr:.2f}"
                    )
                    old_stop = 0

                if old_stop > 0:
                    trail_stop = min(trail_stop, old_stop)

                # Ensure short stop is above current Last Price.
                if trail_stop <= curr:
                    trail_stop = curr * (1 + TRAIL_PCT)

                log(
                    f"[TRAIL] {symbol} SHORT "
                    f"LIVE={curr:.2f} "
                    f"ENTRY={entry:.2f} "
                    f"PEAK={new_peak:.2f} "
                    f"STOP={trail_stop:.2f}"
                )

                state["peaks"][symbol] = new_peak

                if (
                    old_stop <= 0
                    or trail_stop < old_stop
                ):

                    if place_stop(
                        symbol,
                        "sell",
                        size,
                        trail_stop
                    ):

                        log(f"[TRAIL UPDATED] {symbol} SHORT")

            save_state()

        except Exception as e:

            log(f"[TRAIL ERROR] {e}")


# =====================================================
# INITIAL STOP
# =====================================================

def initialize_new_position(symbol, side):

    time.sleep(2)

    positions = get_pos()

    if positions is None:

        log(f"[INITIAL STOP BLOCKED] {symbol} position API error")

        return False

    position = None

    for p in positions:

        if parse_pos_symbol(p) == symbol:
            position = p
            break

    if position is None:

        log(f"[INITIAL STOP WAIT] {symbol} position not visible yet")

        return False

    size = float(position.get("size", 0))

    if size == 0:
        return False

    entry = float(
        position.get("entry_price")
        or position.get("average_price")
        or 0
    )

    curr = get_live_price(symbol)

    if entry <= 0 or curr is None:

        log(f"[INITIAL STOP ERROR] {symbol} price unavailable")

        return False

    if size > 0:

        stop_price = curr * (1 - TRAIL_PCT)

        state["peaks"][symbol] = max(entry, curr)

        stop_side = "buy"

    else:

        stop_price = curr * (1 + TRAIL_PCT)

        state["peaks"][symbol] = min(entry, curr)

        stop_side = "sell"

    save_state()

    log(
        f"[INITIAL STOP] {symbol} "
        f"ENTRY={entry:.2f} "
        f"LIVE={curr:.2f} "
        f"STOP={stop_price:.2f}"
    )

    return place_stop(
        symbol,
        stop_side,
        size,
        stop_price
    )


# =====================================================
# MAIN BOT LOOP
# =====================================================

def main_loop():

    log("======================================")
    log("DELTA TESTNET AUTO BOT STARTED")
    log("EMA 9/21 + RSI + VOLUME")
    log("SAFE LIVE TRAILING STOP")
    log(f"ORDER SIZE = {ORDER_SIZE}")
    log(f"MAX POSITIONS = {MAX_POSITIONS}")
    log(f"COINS = {COINS}")
    log("======================================")

    while True:

        try:

            # Manage existing positions first.
            handle_trailing()

            positions = get_pos()

            if positions is None:

                log("[SCAN BLOCKED] Position API unavailable")

                time.sleep(CHECK_INTERVAL)

                continue

            held = []

            for p in positions:

                size = float(p.get("size", 0))

                if size == 0:
                    continue

                symbol = parse_pos_symbol(p)

                if symbol in COINS:
                    held.append(symbol)

            held = list(set(held))

            log(
                f"[POS HELD] {held} "
                f"TOTAL={len(held)}"
            )

            if len(held) >= MAX_POSITIONS:

                log("[MAX POSITIONS] No new entry")

                time.sleep(CHECK_INTERVAL)

                continue

            for symbol in COINS:

                if len(held) >= MAX_POSITIONS:
                    break

                if symbol in held:
                    continue

                last_trade = float(
                    state["last_trade"].get(symbol, 0)
                )

                remaining = (
                    COOLDOWN_SECONDS
                    - (time.time() - last_trade)
                )

                if remaining > 0:

                    log(
                        f"[COOLDOWN] {symbol} "
                        f"{int(remaining)}s"
                    )

                    continue

                candles = get_candles(symbol)

                if not candles:
                    continue

                signal = check_signal(candles, symbol)

                if not signal:
                    continue

                log(
                    f"[ENTRY SIGNAL] {symbol} {signal.upper()}"
                )

                state["last_trade"][symbol] = time.time()

                save_state()

                accepted = place_entry(symbol, signal)

                if not accepted:

                    log(f"[ENTRY NOT CONFIRMED] {symbol}")

                    continue

                stop_ok = initialize_new_position(
                    symbol,
                    signal
                )

                held.append(symbol)

                log(
                    f"[ENTRY COMPLETE] {symbol} "
                    f"STOP_VERIFIED={stop_ok}"
                )

                if not stop_ok:

                    log(
                        f"[URGENT CHECK] {symbol}: "
                        "Entry accepted but protective stop "
                        "was not verified. Check Testnet manually."
                    )

            log(f"--- SLEEP {CHECK_INTERVAL}s ---")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:

            log(f"[MAIN LOOP ERROR] {e}")

            import traceback
            traceback.print_exc()

            time.sleep(10)


# =====================================================
# START
# =====================================================

if __name__ == "__main__":

    log("=== BOT STARTING ===")

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    log("=== FLASK STARTED ===")

    main_loop()
