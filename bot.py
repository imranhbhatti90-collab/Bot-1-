
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
from flask import Flask, jsonify, request

# =====================================================
# DELTA EXCHANGE INDIA - TESTNET ONLY
# =====================================================
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINS = ["BTCUSD", "ETHUSD", "SOLUSD", "XAUTUSD", "PAXGUSD", "SILVERUSD"]

TIMEFRAME = "15m"
ORDER_SIZE = 40
ORDER_SIZES = {
    "BTCUSD": 40, "ETHUSD": 40, "SOLUSD": 40,
    "XAUTUSD": 200, "PAXGUSD": 200, "SILVERUSD": 200,
}

MAX_POSITIONS = 6
ENABLE_TRADING = True
LEVERAGE = 50

ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.0
PARTIAL_TP_ATR_MULTIPLIER = 1.0
PARTIAL_TP_PERCENT = 0.50

# Skip new entries if initial ATR stop distance exceeds 2% of price.
MAX_ATR_GAP_PCT = 0.02

STOP_TRIGGER_METHOD = "last_traded_price"
COOLDOWN_SECONDS = 300
CHECK_INTERVAL = 30
STATE_FILE = "state.json"

MIN_STOP_GAP_PCT = 0.0005
MAX_CANDLE_LIVE_GAP_PCT = 0.005
MAX_TRADE_AGE_SECONDS = 30

EMA_FAST = 10
EMA_SLOW = 20

CANDLE_LOOKBACK_SECONDS = 10 * 24 * 60 * 60
CANDLE_SECONDS = 900
MIN_CLOSED_CANDLES = 120

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def log(message):
    print(message, flush=True)


@app.route("/")
def home():
    return (
        "DELTA TESTNET BOT | EMA10/20 | ATR 1.2 SL | "
        "50% PARTIAL TP | BREAKEVEN + ATR TRAILING"
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "environment": "TESTNET",
        "automatic_trading": ENABLE_TRADING,
        "max_positions": MAX_POSITIONS,
        "strategy": "15m EMA10/EMA20 Option B",
        "initial_sl": "ATR x 1.2",
        "partial_tp": "50% at raw ATR x 1.0",
        "remaining_sl": "Breakeven then ATR trailing",
        "max_atr_gap_pct": MAX_ATR_GAP_PCT,
    }), 200


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# =====================================================
# STATE
# =====================================================
def load_state():
    default = {
        "peaks": {},
        "stops": {},
        "last_trade": {},
        "last_candle": {},
        "trail_active": {},
        "initial_gaps": {},
        "stop_order_ids": {},
        "partial_tp_done": {},
        "partial_tp_pending": {},
        "partial_tp_targets": {},
        "original_sizes": {},
        "entry_prices": {},
    }

    try:
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)

        if not isinstance(saved, dict):
            return default

        for key in default:
            if not isinstance(saved.get(key), dict):
                saved[key] = {}

        return saved

    except Exception:
        return default


state = load_state()
state_lock = threading.Lock()


def save_state():
    try:
        with state_lock:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"[STATE SAVE ERROR] {e}")


# =====================================================
# SECURE STATE VIEW
# =====================================================
STATE_VIEW_USER = os.getenv("STATE_VIEW_USER", "")
STATE_VIEW_PASSWORD = os.getenv("STATE_VIEW_PASSWORD", "")


@app.route("/state", methods=["GET"])
def view_state():
    if not STATE_VIEW_USER or not STATE_VIEW_PASSWORD:
        return jsonify({"error": "State viewer not configured"}), 503

    auth = request.authorization

    if (
        not auth
        or not hmac.compare_digest(auth.username or "", STATE_VIEW_USER)
        or not hmac.compare_digest(auth.password or "", STATE_VIEW_PASSWORD)
    ):
        response = jsonify({"error": "Authentication required"})
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="Bot State Viewer"'
        return response

    with state_lock:
        snapshot = json.loads(json.dumps(state))

    return jsonify({
        "status": "ok",
        "file": STATE_FILE,
        "state": snapshot,
    }), 200


# =====================================================
# API REQUEST / AUTH
# =====================================================
session = requests.Session()


def api_req(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        return {"error": "API credentials missing"}, 401

    method = method.upper()
    query_string = "?" + urlencode(params, quote_via=quote) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""

    timestamp = str(int(time.time()))
    message = method + timestamp + path + query_string + payload

    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "python-delta-testnet-partial-tp-bot",
    }

    try:
        response = session.request(
            method,
            BASE_URL + path + query_string,
            headers=headers,
            data=payload or None,
            timeout=(5, 15),
        )

        try:
            result = response.json()
        except Exception:
            result = {"error": response.text[:500]}

        return result, response.status_code

    except Exception as e:
        return {"error": str(e)}, 500


# =====================================================
# TIMESTAMP / MARKET DATA
# =====================================================
def parse_timestamp(raw):
    if raw in (None, ""):
        return 0.0

    try:
        value = float(raw)

        if value > 1e14:
            value /= 1_000_000
        elif value > 1e11:
            value /= 1_000

        return value

    except (TypeError, ValueError):
        try:
            from datetime import datetime
            return datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return 0.0


def fresh_price(price_raw, raw_ts, symbol, source):
    try:
        price = float(price_raw or 0)
    except (TypeError, ValueError):
        return None

    ts = parse_timestamp(raw_ts)
    age = time.time() - ts if ts else 999999

    if (
        price <= 0
        or ts <= 0
        or age < -10
        or age > MAX_TRADE_AGE_SECONDS
    ):
        log(
            f"[STALE PRICE] {symbol} PRICE={price} "
            f"SOURCE={source} AGE={age:.1f}s"
        )
        return None

    return price


def get_live_price(symbol):
    try:
        response = session.get(
            BASE_URL + f"/v2/trades/{symbol}",
            headers={"Accept": "application/json"},
            timeout=(5, 10),
        )

        if response.status_code == 200:
            data = response.json()

            if data.get("success") is True:
                result = data.get("result", {})
                trades = (
                    result.get("trades", [])
                    if isinstance(result, dict)
                    else result if isinstance(result, list) else []
                )

                if trades:
                    latest = max(
                        trades,
                        key=lambda item: parse_timestamp(
                            item.get(
                                "timestamp",
                                item.get("t", item.get("created_at")),
                            )
                        ),
                    )

                    price = fresh_price(
                        latest.get("price", latest.get("p")),
                        latest.get(
                            "timestamp",
                            latest.get("t", latest.get("created_at")),
                        ),
                        symbol,
                        "recent_trade",
                    )

                    if price is not None:
                        log(f"[LIVE PRICE] {symbol} {price}")
                        return price

    except Exception as e:
        log(f"[TRADE PRICE ERROR] {symbol}: {e}")

    # Ticker close fallback. Mark price is deliberately not used.
    try:
        response = session.get(
            BASE_URL + f"/v2/tickers/{symbol}",
            headers={"Accept": "application/json"},
            timeout=(5, 10),
        )

        if response.status_code != 200:
            return None

        data = response.json()

        if data.get("success") is not True:
            return None

        result = data.get("result", {})

        if isinstance(result, list):
            result = next(
                (x for x in result if x.get("symbol") == symbol),
                result[0] if result else {},
            )

        if not isinstance(result, dict):
            return None

        return fresh_price(
            result.get("close"),
            result.get("timestamp"),
            symbol,
            "ticker_close",
        )

    except Exception as e:
        log(f"[TICKER ERROR] {symbol}: {e}")
        return None


def get_candles(symbol, resolution=TIMEFRAME):
    end = int(time.time())

    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": str(end - CANDLE_LOOKBACK_SECONDS),
        "end": str(end),
    }

    result, code = api_req(
        "GET",
        "/v2/history/candles",
        params=params,
    )

    if (
        code == 200
        and isinstance(result, dict)
        and result.get("success") is True
        and result.get("result")
    ):
        return result["result"]

    log(f"[CANDLE API ERROR] {symbol} HTTP={code} {result}")
    return []


def candles_to_df(candles, resolution=TIMEFRAME, symbol="?"):
    try:
        if not candles:
            return None

        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles).copy()
        else:
            df = pd.DataFrame(
                candles,
                columns=["time", "open", "high", "low", "close", "volume"],
            )

        required = ["high", "low", "close", "volume"]

        if not all(x in df.columns for x in required):
            log(f"[CANDLE INVALID] {symbol}: missing columns")
            return None

        if "time" not in df.columns:
            log(f"[CANDLE INVALID] {symbol}: missing timestamp")
            return None

        for column in required:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df["time"] = df["time"].apply(parse_timestamp)
        df = df.dropna(subset=["time"]).copy()
        df = df[df["time"] > 0]

        if df.empty:
            return None

        df["time"] = df["time"].astype("int64")
        df = (
            df.sort_values("time")
            .drop_duplicates("time")
            .reset_index(drop=True)
        )

        now = int(time.time())
        interval = 900 if resolution == "15m" else 300

        closed = df[df["time"] + interval <= now].copy()
        closed = closed.dropna(subset=required).reset_index(drop=True)

        log(
            f"[CANDLE DIAGNOSTIC] {symbol} TF={resolution} "
            f"raw={len(df)} closed={len(closed)}"
        )

        if len(closed) < MIN_CLOSED_CANDLES:
            log(f"[CANDLE INSUFFICIENT] {symbol} {len(closed)}")
            return None

        return closed

    except Exception as e:
        log(f"[CANDLE PARSE ERROR] {symbol}: {e}")
        return None


# =====================================================
# INDICATORS / OPTION B
# =====================================================
def add_indicators(df):
    df = df.copy()

    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema10"] = close.ewm(
        span=EMA_FAST,
        adjust=False,
    ).mean()

    df["ema20"] = close.ewm(
        span=EMA_SLOW,
        adjust=False,
    ).mean()

    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["atr"] = true_range.ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False,
        min_periods=ATR_PERIOD,
    ).mean()

    return df


def get_market_indicators(symbol, candles=None):
    candles = candles if candles is not None else get_candles(symbol)

    df = candles_to_df(
        candles,
        resolution=TIMEFRAME,
        symbol=symbol,
    ) if candles else None

    if df is None:
        return None

    df = add_indicators(df)
    last = df.iloc[-1]

    try:
        atr = float(last["atr"])
        ema10 = float(last["ema10"])
        ema20 = float(last["ema20"])
        close = float(last["close"])
        candle_id = int(last["time"])

        if atr <= 0 or close <= 0:
            return None

        return {
            "atr": atr,
            "ema10": ema10,
            "ema20": ema20,
            "close": close,
            "candle_id": candle_id,
        }

    except Exception:
        return None


def check_signal(candles, symbol):
    indicators = get_market_indicators(symbol, candles)

    if indicators is None:
        return None, None

    ema10 = indicators["ema10"]
    ema20 = indicators["ema20"]
    candle_id = indicators["candle_id"]

    log(
        f"[OPTION B SCAN] {symbol} Candle={candle_id} "
        f"EMA10={ema10:.6f} EMA20={ema20:.6f} "
        f"ATR={indicators['atr']:.6f}"
    )

    if ema10 > ema20:
        log(f"[OPTION B BUY] {symbol}")
        return "buy", candle_id

    if ema10 < ema20:
        log(f"[OPTION B SELL] {symbol}")
        return "sell", candle_id

    return None, candle_id


# =====================================================
# POSITIONS / ORDERS
# =====================================================
def get_pos():
    result, code = api_req("GET", "/v2/positions/margined")

    if (
        code != 200
        or not isinstance(result, dict)
        or result.get("success") is not True
    ):
        log(f"[POSITION ERROR] HTTP={code} {result}")
        return None

    positions = result.get("result", [])

    if isinstance(positions, dict):
        positions = [positions]

    if not isinstance(positions, list):
        return None

    active = []

    for position in positions:
        try:
            if float(position.get("size", 0) or 0) != 0:
                active.append(position)
        except (TypeError, ValueError):
            continue

    return active


def parse_pos_symbol(position):
    product = position.get("product") or {}

    return (
        position.get("product_symbol")
        or product.get("symbol")
        or position.get("symbol")
        or ""
    )


def get_orders():
    result, code = api_req(
        "GET",
        "/v2/orders",
        params={"states": "open,pending", "page_size": 100},
    )

    if (
        code == 200
        and isinstance(result, dict)
        and result.get("success") is True
    ):
        orders = result.get("result", [])
        if isinstance(orders, list):
            return orders

    log(f"[ORDER API ERROR] HTTP={code} {result}")
    return None


def is_active_stop(order, symbol):
    return (
        order.get("product_symbol") == symbol
        and order.get("stop_order_type") == "stop_loss_order"
        and order.get("state") in ("open", "pending")
        and order.get("reduce_only") in (True, 1, "true", "True")
    )


def verify_stop(symbol, order_id):
    orders = get_orders()

    if orders is None:
        return False

    return any(
        str(order.get("id")) == str(order_id)
        and is_active_stop(order, symbol)
        for order in orders
    )


def cancel_stop(order):
    order_id = order.get("id")
    product_id = order.get("product_id")

    if not order_id or not product_id:
        log("[CANCEL ERROR] Missing stop ID/product ID")
        return False

    result, code = api_req(
        "DELETE",
        "/v2/orders",
        body={
            "id": int(order_id),
            "product_id": int(product_id),
        },
    )

    if (
        code != 200
        or not isinstance(result, dict)
        or result.get("success") is not True
    ):
        log(f"[CANCEL FAILED] ID={order_id} HTTP={code} {result}")
        return False

    time.sleep(0.7)
    orders = get_orders()

    if orders is None:
        return False

    if any(str(x.get("id")) == str(order_id) for x in orders):
        log(f"[CANCEL NOT CONFIRMED] ID={order_id}")
        return False

    log(f"[CANCEL VERIFIED] ID={order_id}")
    return True


def valid_stop_price(position_side, stop_price, live_price):
    if (
        live_price is None
        or live_price <= 0
        or stop_price is None
        or stop_price <= 0
    ):
        return False

    minimum_gap = live_price * MIN_STOP_GAP_PCT

    if position_side == "buy":
        return stop_price < live_price - minimum_gap

    if position_side == "sell":
        return stop_price > live_price + minimum_gap

    return False


# =====================================================
# STOP PLACEMENT
# New stop is verified BEFORE old stops are cancelled.
# =====================================================
def place_stop(symbol, position_side, size, stop_price):
    quantity = int(abs(float(size)))

    if quantity <= 0 or stop_price <= 0:
        log("[STOP ERROR] Invalid size/price")
        return False

    live = get_live_price(symbol)

    if not valid_stop_price(position_side, stop_price, live):
        log(
            f"[STOP BLOCKED] {symbol} SIDE={position_side} "
            f"STOP={stop_price:.6f} LAST={live}"
        )
        return False

    stop_side = "sell" if position_side == "buy" else "buy"

    body = {
        "product_symbol": symbol,
        "size": quantity,
        "side": stop_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": f"{float(stop_price):.2f}",
        "reduce_only": True,
        "stop_trigger_method": STOP_TRIGGER_METHOD,
    }

    result, code = api_req("POST", "/v2/orders", body=body)

    order_result = (
        result.get("result", {})
        if isinstance(result, dict)
        else {}
    )

    if (
        code != 200
        or not isinstance(result, dict)
        or result.get("success") is not True
        or not order_result.get("id")
    ):
        log(f"[STOP FAILED] {symbol} HTTP={code} {result}")
        return False

    order_id = order_result["id"]

    time.sleep(1)

    if not verify_stop(symbol, order_id):
        log(f"[STOP NOT VERIFIED] {symbol} ID={order_id}; old stop retained")
        return False

    state["stops"][symbol] = float(stop_price)
    state["stop_order_ids"][symbol] = str(order_id)
    save_state()

    log(
        f"[STOP VERIFIED] {symbol} SIZE={quantity} "
        f"STOP={stop_price:.6f} ID={order_id}"
    )

    orders = get_orders()

    if orders is None:
        log(f"[OLD STOP RETAINED] {symbol}: order list unavailable")
        return True

    for old_order in orders:
        if (
            is_active_stop(old_order, symbol)
            and str(old_order.get("id")) != str(order_id)
        ):
            if not cancel_stop(old_order):
                log(
                    f"[OLD STOP NOT CANCELLED] {symbol} "
                    f"ID={old_order.get('id')}"
                )

    return True


# =====================================================
# ATR CALCULATION
# =====================================================
def get_atr_value(symbol):
    indicators = get_market_indicators(symbol)

    if indicators is None:
        return None

    atr = indicators["atr"]

    if atr <= 0:
        return None

    log(f"[RAW ATR] {symbol} ATR14={atr:.8f}")
    return atr


def get_atr_gap(symbol):
    atr = get_atr_value(symbol)

    if atr is None:
        return None

    gap = atr * ATR_STOP_MULTIPLIER

    log(
        f"[ATR STOP GAP] {symbol} ATR={atr:.8f} "
        f"MULTIPLIER={ATR_STOP_MULTIPLIER} GAP={gap:.8f}"
    )

    return gap


# =====================================================
# RECOVER STOP STATE
# =====================================================
def recover_stop_state(symbol, side, orders):
    active = [
        order for order in (orders or [])
        if is_active_stop(order, symbol)
    ]

    parsed = []

    for order in active:
        try:
            parsed.append((float(order.get("stop_price")), order))
        except (TypeError, ValueError):
            continue

    if not parsed:
        return float(state["stops"].get(symbol, 0) or 0), False

    if side == "buy":
        chosen_price, chosen_order = max(
            parsed,
            key=lambda x: x[0],
        )
    else:
        chosen_price, chosen_order = min(
            parsed,
            key=lambda x: x[0],
        )

    state["stops"][symbol] = chosen_price
    state["stop_order_ids"][symbol] = str(chosen_order.get("id", ""))

    return chosen_price, True


# =====================================================
# PARTIAL TAKE PROFIT
# =====================================================
def verify_partial_reduction(symbol, expected_remaining):
    """
    Verify the actual exchange position size.
    Returns:
      True  = expected reduction confirmed
      False = not confirmed / API error
    """
    for attempt in range(12):
        time.sleep(1)

        positions = get_pos()

        if positions is None:
            log(f"[PARTIAL VERIFY ERROR] {symbol}: position API unavailable")
            continue

        position = next(
            (
                p for p in positions
                if parse_pos_symbol(p) == symbol
            ),
            None,
        )

        actual = (
            abs(float(position.get("size", 0) or 0))
            if position else 0
        )

        if abs(actual - expected_remaining) < 0.000001:
            log(
                f"[PARTIAL REDUCTION VERIFIED] {symbol} "
                f"REMAINING={actual}"
            )
            return True

        log(
            f"[PARTIAL VERIFY WAIT] {symbol} "
            f"EXPECTED={expected_remaining} ACTUAL={actual} "
            f"CHECK={attempt + 1}/12"
        )

    return False


def execute_partial_tp(symbol, position, current, atr):
    """
    Close 50% once at +1 raw ATR.
    Never resubmit if a previous result is ambiguous.
    """
    if state["partial_tp_done"].get(symbol):
        return

    if state["partial_tp_pending"].get(symbol):
        log(
            f"[PARTIAL TP BLOCKED] {symbol}: previous partial order "
            "is unresolved. Inspect exchange position manually."
        )
        return

    size = float(position.get("size", 0) or 0)
    original_size = abs(size)

    if original_size < 2:
        log(f"[PARTIAL TP SKIP] {symbol}: size too small for 50% close")
        return

    entry = float(
        position.get("entry_price")
        or position.get("average_price")
        or 0
    )

    if entry <= 0 or atr <= 0:
        return

    side = "buy" if size > 0 else "sell"
    favourable_move = (
        current - entry if side == "buy"
        else entry - current
    )

    target_distance = (
        float(state["partial_tp_targets"].get(symbol, 0) or 0)
    )

    if target_distance <= 0:
        target_distance = atr * PARTIAL_TP_ATR_MULTIPLIER
        state["partial_tp_targets"][symbol] = target_distance
        save_state()

    if favourable_move < target_distance:
        return

    close_quantity = int(original_size * PARTIAL_TP_PERCENT)
    expected_remaining = original_size - close_quantity

    if close_quantity <= 0 or expected_remaining <= 0:
        return

    close_side = "sell" if side == "buy" else "buy"

    # Persist pending BEFORE sending the order.
    # If API response is ambiguous, never automatically submit again.
    state["partial_tp_pending"][symbol] = {
        "time": time.time(),
        "original_size": original_size,
        "expected_remaining": expected_remaining,
        "close_quantity": close_quantity,
        "side": side,
    }
    save_state()

    body = {
        "product_symbol": symbol,
        "size": close_quantity,
        "side": close_side,
        "order_type": "market_order",
        "reduce_only": True,
    }

    log(
        f"[PARTIAL TP TRIGGER] {symbol} "
        f"ENTRY={entry:.6f} CURRENT={current:.6f} "
        f"ATR_TARGET={target_distance:.6f} "
        f"CLOSE={close_quantity} KEEP={expected_remaining}"
    )

    result, code = api_req("POST", "/v2/orders", body=body)

    order_result = (
        result.get("result", {})
        if isinstance(result, dict)
        else {}
    )

    if (
        code != 200
        or not isinstance(result, dict)
        or result.get("success") is not True
        or not order_result.get("id")
    ):
        log(
            f"[PARTIAL TP AMBIGUOUS/FAILED] {symbol} "
            f"HTTP={code} {result}. No automatic retry."
        )
        return

    log(
        f"[PARTIAL TP ORDER ACCEPTED] {symbol} "
        f"ORDER_ID={order_result.get('id')}"
    )

    if not verify_partial_reduction(symbol, expected_remaining):
        log(
            f"[PARTIAL TP NOT CONFIRMED] {symbol}. "
            "Pending lock retained; inspect exchange manually."
        )
        return

    # Exchange confirms actual size reduction.
    state["partial_tp_done"][symbol] = True
    state["original_sizes"][symbol] = original_size
    state["entry_prices"][symbol] = entry
    state["partial_tp_pending"].pop(symbol, None)
    save_state()

    log(
        f"[PARTIAL TP COMPLETE] {symbol}: "
        f"50% booked; remaining={expected_remaining}"
    )

    # Move remaining position's stop to breakeven.
    # If BE is invalid/too close, keep the previous protective stop.
    breakeven = entry

    if not valid_stop_price(side, breakeven, get_live_price(symbol)):
        log(
            f"[BREAKEVEN WAIT] {symbol}: BE stop is too close/wrong side. "
            "Existing protective stop retained."
        )
        return

    if place_stop(symbol, side, expected_remaining, breakeven):
        state["trail_active"][symbol] = True
        log(
            f"[BREAKEVEN VERIFIED] {symbol} "
            f"REMAINING={expected_remaining} STOP={breakeven:.6f}"
        )
        save_state()
    else:
        log(
            f"[URGENT] {symbol}: partial TP done but BE stop "
            "not verified. Existing stop should remain; inspect exchange."
        )


# =====================================================
# ATR TRAILING / PARTIAL TP MANAGEMENT
# =====================================================
def handle_trailing():
    positions = get_pos()

    if positions is None:
        log("[TRAIL BLOCKED] Position API unavailable")
        return

    orders = get_orders()

    if orders is None:
        log("[TRAIL BLOCKED] Cannot read open orders")
        return

    active_symbols = set()

    for position in positions:
        try:
            size = float(position.get("size", 0) or 0)

            if size == 0:
                continue

            symbol = parse_pos_symbol(position)

            if symbol not in COINS:
                continue

            active_symbols.add(symbol)

            entry = float(
                position.get("entry_price")
                or position.get("average_price")
                or 0
            )

            if entry <= 0:
                log(f"[TRAIL ERROR] {symbol}: entry missing")
                continue

            current = get_live_price(symbol)

            if current is None:
                log(f"[TRAIL SKIP] {symbol}: no fresh price")
                continue

            side = "buy" if size > 0 else "sell"

            # Use current ATR for trailing distance.
            atr = get_atr_value(symbol)

            if atr is None:
                log(f"[TRAIL PAUSED] {symbol}: ATR unavailable")
                continue

            gap = atr * ATR_STOP_MULTIPLIER

            # Recover initial ATR gap from state or exchange stop.
            old_stop, has_stop = recover_stop_state(
                symbol,
                side,
                orders,
            )

            initial_gap = float(
                state["initial_gaps"].get(symbol, 0) or 0
            )

            if initial_gap <= 0 and old_stop > 0:
                initial_gap = (
                    entry - old_stop if side == "buy"
                    else old_stop - entry
                )

                if initial_gap > 0:
                    state["initial_gaps"][symbol] = initial_gap

            # If no exchange stop exists, install a protective initial stop.
            if not has_stop:
                initial_stop = entry - gap if side == "buy" else entry + gap

                if not valid_stop_price(side, initial_stop, current):
                    log(
                        f"[URGENT STOP BLOCKED] {symbol}: "
                        f"initial stop invalid. Check position manually."
                    )
                    continue

                if not place_stop(symbol, side, size, initial_stop):
                    log(f"[URGENT] {symbol}: initial stop not verified")
                    continue

                old_stop = initial_stop
                initial_gap = gap
                state["initial_gaps"][symbol] = initial_gap
                state["peaks"][symbol] = (
                    max(entry, current) if side == "buy"
                    else min(entry, current)
                )
                save_state()

            if initial_gap <= 0:
                initial_gap = gap
                state["initial_gaps"][symbol] = initial_gap

            # Update favourable peak.
            old_peak = float(
                state["peaks"].get(symbol, entry) or entry
            )

            if side == "buy":
                peak = max(old_peak, entry, current)
            else:
                peak = min(old_peak, entry, current)

            state["peaks"][symbol] = peak

            # Partial TP uses RAW ATR x 1.0, not ATR x 1.2.
            execute_partial_tp(
                symbol,
                position,
                current,
                atr,
            )

            # Refresh position after partial TP.
            refreshed = get_pos()

            if refreshed is None:
                log(f"[TRAIL BLOCKED] {symbol}: cannot refresh position")
                continue

            position = next(
                (
                    p for p in refreshed
                    if parse_pos_symbol(p) == symbol
                ),
                None,
            )

            if position is None:
                continue

            size = float(position.get("size", 0) or 0)

            if size == 0:
                continue

            side = "buy" if size > 0 else "sell"

            old_stop = float(
                state["stops"].get(symbol, old_stop) or old_stop
            )

            # Before partial TP: move to BE after initial ATR gap.
            # After partial TP: BE is the minimum protected level.
            partial_done = bool(
                state["partial_tp_done"].get(symbol)
            )

            favourable = (
                peak - entry if side == "buy"
                else entry - peak
            )

            if not partial_done and favourable < initial_gap:
                log(
                    f"[WAIT BREAKEVEN] {symbol} "
                    f"MOVE={favourable:.6f} REQUIRED={initial_gap:.6f} "
                    f"STOP={old_stop:.6f}"
                )
                save_state()
                continue

            candidate = (
                peak - gap if side == "buy"
                else peak + gap
            )

            # Never allow trailing stop below/above breakeven
            # after partial TP, and never loosen an existing stop.
            if partial_done:
                candidate = (
                    max(candidate, entry) if side == "buy"
                    else min(candidate, entry)
                )

            if old_stop > 0:
                candidate = (
                    max(candidate, old_stop) if side == "buy"
                    else min(candidate, old_stop)
                )

            if not valid_stop_price(side, candidate, current):
                log(
                    f"[TRAIL WAIT] {symbol} candidate={candidate:.6f} "
                    f"LAST={current:.6f}; existing stop retained"
                )
                save_state()
                continue

            improved = (
                old_stop <= 0 or candidate > old_stop
                if side == "buy"
                else old_stop <= 0 or candidate < old_stop
            )

            if improved:
                log(
                    f"[ATR TRAIL] {symbol} SIDE={side.upper()} "
                    f"PEAK={peak:.6f} GAP={gap:.6f} "
                    f"OLD={old_stop:.6f} NEW={candidate:.6f} "
                    f"SIZE={abs(size)}"
                )

                place_stop(
                    symbol,
                    side,
                    size,
                    candidate,
                )

            save_state()

        except Exception as e:
            log(f"[TRAIL ERROR] {e}")

    # Clear local state for positions that have closed.
    # Do not cancel exchange stops here.
    for symbol in list(state["peaks"].keys()):
        if symbol in COINS and symbol not in active_symbols:
            state["peaks"].pop(symbol, None)
            state["stops"].pop(symbol, None)
            state["trail_active"].pop(symbol, None)
            state["initial_gaps"].pop(symbol, None)
            state["stop_order_ids"].pop(symbol, None)
            state["partial_tp_done"].pop(symbol, None)
            state["partial_tp_targets"].pop(symbol, None)
            state["original_sizes"].pop(symbol, None)
            state["entry_prices"].pop(symbol, None)

            # Do not clear unresolved partial pending state automatically.
            if not state["partial_tp_pending"].get(symbol):
                state["partial_tp_pending"].pop(symbol, None)

    save_state()


# =====================================================
# INITIAL POSITION STOP
# =====================================================
def initialize_new_position(symbol):
    for attempt in range(6):
        time.sleep(2)

        positions = get_pos()

        if positions is None:
            continue

        position = next(
            (
                item for item in positions
                if parse_pos_symbol(item) == symbol
            ),
            None,
        )

        if position is None:
            continue

        size = float(position.get("size", 0) or 0)

        entry = float(
            position.get("entry_price")
            or position.get("average_price")
            or 0
        )

        if not size or entry <= 0:
            continue

        atr = get_atr_value(symbol)

        if atr is None:
            log(f"[INITIAL STOP FAILED] {symbol}: ATR unavailable")
            return False

        gap = atr * ATR_STOP_MULTIPLIER

        if gap / entry > MAX_ATR_GAP_PCT:
            log(
                f"[URGENT] {symbol}: ATR gap became too wide AFTER entry. "
                "Attempting reduce-only emergency close."
            )
            close_position_safely(symbol, position)
            return False

        side = "buy" if size > 0 else "sell"
        stop_price = entry - gap if side == "buy" else entry + gap
        current = get_live_price(symbol)

        if current is None:
            log(f"[INITIAL STOP FAILED] {symbol}: live price unavailable")
            return False

        state["initial_gaps"][symbol] = gap
        state["peaks"][symbol] = (
            max(entry, current) if side == "buy"
            else min(entry, current)
        )

        state["partial_tp_targets"][symbol] = (
            atr * PARTIAL_TP_ATR_MULTIPLIER
        )

        state["original_sizes"][symbol] = abs(size)
        state["entry_prices"][symbol] = entry
        state["partial_tp_done"].pop(symbol, None)
        state["partial_tp_pending"].pop(symbol, None)

        save_state()

        log(
            f"[INITIAL ATR STOP] {symbol} ENTRY={entry:.8f} "
            f"ATR={atr:.8f} GAP={gap:.8f} STOP={stop_price:.8f} "
            f"PARTIAL TARGET={atr * PARTIAL_TP_ATR_MULTIPLIER:.8f}"
        )

        return place_stop(
            symbol,
            side,
            size,
            stop_price,
        )

    log(f"[INITIAL STOP FAILED] {symbol}: position not confirmed")
    return False


# =====================================================
# ENTRY SAFETY
# =====================================================
def price_mismatch_ok(symbol, candle_close):
    live = get_live_price(symbol)

    if live is None or candle_close <= 0:
        log(f"[ENTRY BLOCKED] {symbol}: price unavailable")
        return False

    gap_pct = abs(live - candle_close) / candle_close

    log(
        f"[PRICE CHECK] {symbol} CANDLE={candle_close:.6f} "
        f"LIVE={live:.6f} GAP={gap_pct * 100:.3f}%"
    )

    if gap_pct > MAX_CANDLE_LIVE_GAP_PCT:
        log(f"[ENTRY BLOCKED] {symbol}: candle/live mismatch")
        return False

    return True


def atr_entry_allowed(symbol, candles):
    indicators = get_market_indicators(symbol, candles)

    if indicators is None:
        log(f"[ENTRY BLOCKED] {symbol}: ATR unavailable")
        return False

    live = get_live_price(symbol)

    if live is None or live <= 0:
        return False

    atr = indicators["atr"]
    gap = atr * ATR_STOP_MULTIPLIER
    gap_pct = gap / live

    log(
        f"[ATR ENTRY FILTER] {symbol} ATR={atr:.6f} "
        f"STOP_GAP={gap:.6f} GAP_PCT={gap_pct * 100:.3f}% "
        f"MAX={MAX_ATR_GAP_PCT * 100:.2f}%"
    )

    if gap_pct > MAX_ATR_GAP_PCT:
        log(
            f"[ENTRY SKIPPED] {symbol}: ATR stop gap "
            f"{gap_pct * 100:.2f}% exceeds limit"
        )
        return False

    return True


# =====================================================
# ENTRY ORDER
# =====================================================
def place_entry(symbol, side):
    body = {
        "product_symbol": symbol,
        "size": ORDER_SIZES.get(symbol, ORDER_SIZE),
        "side": side,
        "order_type": "market_order",
    }

    result, code = api_req(
        "POST",
        "/v2/orders",
        body=body,
    )

    order_result = (
        result.get("result", {})
        if isinstance(result, dict)
        else {}
    )

    if (
        code == 200
        and isinstance(result, dict)
        and result.get("success") is True
    ):
        log(
            f"[ENTRY ACCEPTED] {symbol} {side.upper()} "
            f"SIZE={ORDER_SIZES.get(symbol, ORDER_SIZE)} "
            f"RESULT={order_result}"
        )
        return True

    log(f"[ENTRY FAILED] {symbol} HTTP={code} {result}")
    return False


# =====================================================
# SAFE POSITION CLOSE / REVERSAL
# =====================================================
def close_position_safely(symbol, position):
    try:
        size = float(position.get("size", 0) or 0)
    except (TypeError, ValueError):
        return False

    if size == 0:
        return False

    close_side = "sell" if size > 0 else "buy"

    body = {
        "product_symbol": symbol,
        "size": int(abs(size)),
        "side": close_side,
        "order_type": "market_order",
        "reduce_only": True,
    }

    log(
        f"[CLOSE REQUEST] {symbol} SIZE={abs(size)} "
        f"SIDE={close_side.upper()} REDUCE_ONLY=True"
    )

    result, code = api_req(
        "POST",
        "/v2/orders",
        body=body,
    )

    order_result = (
        result.get("result", {})
        if isinstance(result, dict)
        else {}
    )

    if (
        code != 200
        or not isinstance(result, dict)
        or result.get("success") is not True
        or not order_result.get("id")
    ):
        log(f"[CLOSE FAILED/AMBIGUOUS] {symbol} HTTP={code} {result}")
        return False

    flat = False

    for attempt in range(12):
        time.sleep(1)

        positions = get_pos()

        if positions is None:
            continue

        remaining = next(
            (
                p for p in positions
                if parse_pos_symbol(p) == symbol
            ),
            None,
        )

        remaining_size = (
            float(remaining.get("size", 0) or 0)
            if remaining else 0
        )

        if remaining_size == 0:
            flat = True
            break

        log(
            f"[CLOSE WAIT] {symbol} REMAINING={remaining_size} "
            f"CHECK={attempt + 1}/12"
        )

    if not flat:
        log(
            f"[CLOSE BLOCKED] {symbol}: flat not confirmed. "
            "Protective stop retained."
        )
        return False

    orders = get_orders()

    if orders is None:
        log(f"[CLOSE BLOCKED] {symbol}: cannot inspect old stops")
        return False

    for old_order in orders:
        if is_active_stop(old_order, symbol):
            if not cancel_stop(old_order):
                log(f"[CLOSE BLOCKED] {symbol}: old stop remains")
                return False

    # Reset position state after verified flat + stop cleanup.
    for key in (
        "peaks",
        "stops",
        "trail_active",
        "initial_gaps",
        "stop_order_ids",
        "partial_tp_done",
        "partial_tp_targets",
        "original_sizes",
        "entry_prices",
        "partial_tp_pending",
    ):
        state.get(key, {}).pop(symbol, None)

    save_state()

    log(f"[CLOSE COMPLETE] {symbol}: flat and stops cleared")
    return True


# =====================================================
# MAIN LOOP
# =====================================================
def main_loop():
    log("==================================================")
    log("DELTA INDIA TESTNET PARTIAL TP + ATR TRAILING BOT")
    log(f"AUTO TRADING={ENABLE_TRADING}")
    log(f"COINS={COINS}")
    log(f"ORDER SIZES={ORDER_SIZES}")
    log(f"MAX POSITIONS={MAX_POSITIONS}")
    log(f"EMA={EMA_FAST}/{EMA_SLOW} TF={TIMEFRAME}")
    log(f"INITIAL SL=ATR x {ATR_STOP_MULTIPLIER}")
    log(
        f"PARTIAL TP={PARTIAL_TP_PERCENT * 100:.0f}% "
        f"AT RAW ATR x {PARTIAL_TP_ATR_MULTIPLIER}"
    )
    log("REMAINING SL=BREAKEVEN THEN ATR TRAILING")
    log(f"MAX ATR GAP={MAX_ATR_GAP_PCT * 100:.2f}%")
    log("TESTNET ONLY")
    log("==================================================")

    while True:
        try:
            # Manage all existing positions first.
            handle_trailing()

            positions = get_pos()

            if positions is None:
                log("[SCAN BLOCKED] Position API unavailable")
                time.sleep(CHECK_INTERVAL)
                continue

            held_positions = {
                parse_pos_symbol(position): position
                for position in positions
                if parse_pos_symbol(position) in COINS
                and float(position.get("size", 0) or 0) != 0
            }

            log(
                f"[POS HELD] {list(held_positions.keys())} "
                f"TOTAL={len(held_positions)}"
            )

            if not ENABLE_TRADING:
                log("[SAFE MODE] New entries disabled")
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol in COINS:
                candles = get_candles(symbol)

                if not candles:
                    continue

                signal, candle_id = check_signal(candles, symbol)

                if candle_id is None:
                    continue

                previous_candle = int(
                    state["last_candle"].get(symbol, 0) or 0
                )

                if candle_id <= previous_candle:
                    continue

                # Consume each closed candle only once.
                state["last_candle"][symbol] = candle_id
                save_state()

                current_position = held_positions.get(symbol)

                # -----------------------------------------
                # EXISTING POSITION / REVERSAL
                # -----------------------------------------
                if current_position is not None:
                    current_size = float(
                        current_position.get("size", 0) or 0
                    )

                    current_side = (
                        "buy" if current_size > 0 else "sell"
                    )

                    if signal is None:
                        continue

                    if signal == current_side:
                        log(
                            f"[POSITION HELD] {symbol}: "
                            f"{signal.upper()} trend continues"
                        )
                        continue

                    log(
                        f"[TREND REVERSAL] {symbol} "
                        f"{current_side.upper()} -> {signal.upper()}"
                    )

                    if not close_position_safely(
                        symbol,
                        current_position,
                    ):
                        log(f"[REVERSAL BLOCKED] {symbol}")
                        continue

                    state["last_trade"][symbol] = time.time()
                    save_state()

                    log(
                        f"[REVERSAL COOLDOWN] {symbol}: "
                        f"{COOLDOWN_SECONDS}s"
                    )

                    held_positions.pop(symbol, None)
                    continue

                # -----------------------------------------
                # NO POSITION: NEW ENTRY
                # -----------------------------------------
                if signal is None:
                    continue

                # Never enter with an unresolved partial close.
                if state["partial_tp_pending"].get(symbol):
                    log(
                        f"[ENTRY BLOCKED] {symbol}: unresolved "
                        "partial TP state. Manual inspection required."
                    )
                    continue

                open_orders = get_orders()

                if open_orders is None:
                    log(f"[ENTRY BLOCKED] {symbol}: cannot read orders")
                    continue

                orphan_stops = [
                    order for order in open_orders
                    if is_active_stop(order, symbol)
                ]

                cleanup_ok = True

                for old_stop in orphan_stops:
                    if not cancel_stop(old_stop):
                        cleanup_ok = False
                        break

                if not cleanup_ok:
                    log(f"[ENTRY BLOCKED] {symbol}: orphan stop remains")
                    continue

                if len(held_positions) >= MAX_POSITIONS:
                    log(f"[MAX POSITIONS] {symbol}: entry skipped")
                    continue

                last_trade = float(
                    state["last_trade"].get(symbol, 0) or 0
                )

                remaining = (
                    COOLDOWN_SECONDS - (time.time() - last_trade)
                )

                if remaining > 0:
                    log(
                        f"[COOLDOWN] {symbol}: "
                        f"{int(remaining)}s remaining"
                    )
                    continue

                df = candles_to_df(
                    candles,
                    resolution=TIMEFRAME,
                    symbol=symbol,
                )

                if df is None:
                    continue

                candle_close = float(df.iloc[-1]["close"])

                if not price_mismatch_ok(symbol, candle_close):
                    continue

                if not atr_entry_allowed(symbol, candles):
                    continue

                log(
                    f"[SMART ENTRY SIGNAL] {symbol} "
                    f"{signal.upper()} candle={candle_id}"
                )

                # Cooldown is set before order submission to prevent duplicates.
                state["last_trade"][symbol] = time.time()
                save_state()

                if not place_entry(symbol, signal):
                    log(
                        f"[ENTRY NOT CONFIRMED] {symbol}; "
                        "inspect exchange before retrying."
                    )
                    continue

                stop_ok = initialize_new_position(symbol)

                refreshed = get_pos()

                if refreshed is None:
                    log(
                        f"[ENTRY VERIFY ERROR] {symbol}: "
                        "check Testnet position and protective stop."
                    )
                    continue

                confirmed_position = next(
                    (
                        p for p in refreshed
                        if parse_pos_symbol(p) == symbol
                        and float(p.get("size", 0) or 0) != 0
                    ),
                    None,
                )

                if confirmed_position:
                    held_positions[symbol] = confirmed_position

                    log(
                        f"[ENTRY COMPLETE] {symbol} "
                        f"INITIAL_STOP_VERIFIED={stop_ok}"
                    )

                else:
                    log(
                        f"[ENTRY NOT OPEN] {symbol}: "
                        "position not confirmed."
                    )

                if not stop_ok and confirmed_position:
                    log(
                        f"[URGENT CHECK] {symbol}: position open "
                        "but protective stop not verified. "
                        "Check Testnet immediately."
                    )

            log(f"--- SLEEP {CHECK_INTERVAL}s ---")
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log(f"[MAIN LOOP ERROR] {e}")

            import traceback
            traceback.print_exc()

            time.sleep(10)


if __name__ == "__main__":
    log("=== BOT STARTING ===")

    threading.Thread(
        target=run_flask,
        daemon=True,
    ).start()

    log("=== FLASK STARTED ===")

    main_loop()
