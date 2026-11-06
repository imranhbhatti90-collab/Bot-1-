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

COINS = ["BTCUSD", "ETHUSD", "PAXGUSD", "SILVERUSD"]

ORDER_SIZE = 60
ORDER_SIZES = {
    "BTCUSD": 60,
    "ETHUSD": 60,
    "PAXGUSD": 300,
    "SILVERUSD": 300,
}

MAX_POSITIONS = 4
ENABLE_TRADING = True

TIMEFRAME = "15m"
EMA_FAST = 10
EMA_SLOW = 20

# Fixed PRICE POINTS (not contract quantity)
POINTS = {
    "BTCUSD": {
        "sl": 200.0,
        "tp": 300.0,
        "trail": 260.0,
        "decimals": 2,
    },
    "ETHUSD": {
        "sl": 20.0,
        "tp": 20.0,
        "trail": 10.0,
        "decimals": 2,
    },
    "PAXGUSD": {
        "sl": 15.0,
        "tp": 15.0,
        "trail": 7.0,
        "decimals": 2,
    },
    "SILVERUSD": {
        "sl": 0.30,
        "tp": 0.30,
        "trail": 0.15,
        "decimals": 3,
    },
}

PARTIAL_TP_PERCENT = 0.50
STOP_TRIGGER_METHOD = "last_traded_price"

COOLDOWN_SECONDS = 300
CHECK_INTERVAL = 30
STATE_FILE = "state.json"

MIN_STOP_GAP_PCT = 0.0005
MAX_CANDLE_LIVE_GAP_PCT = 0.005
MAX_TRADE_AGE_SECONDS = 30

CANDLE_LOOKBACK_SECONDS = 10 * 24 * 60 * 60
CANDLE_SECONDS = 900
MIN_CLOSED_CANDLES = 120

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
session = requests.Session()


def log(message):
    print(message, flush=True)


@app.route("/")
def home():
    return (
        "DELTA TESTNET BOT | EMA10/20 TWO CANDLE CONFIRMATION "
        "| FIXED POINT SL | 50% PARTIAL TP | FIXED TRAILING"
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "environment": "TESTNET",
        "automatic_trading": ENABLE_TRADING,
        "coins": COINS,
        "max_positions": MAX_POSITIONS,
        "strategy": "15m EMA10/EMA20 two-candle confirmation Option B",
        "fixed_points": POINTS,
        "order_sizes": ORDER_SIZES,
        "partial_tp_percent": PARTIAL_TP_PERCENT * 100,
        "remaining_sl": "Breakeven then fixed-point trailing",
    }), 200


def run_flask():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )


# =====================================================
# STATE
# =====================================================
DEFAULT_STATE = {
    "peaks": {},
    "stops": {},
    "last_trade": {},
    "last_candle": {},
    "trail_active": {},
    "stop_order_ids": {},
    "partial_tp_done": {},
    "partial_tp_pending": {},
    "original_sizes": {},
    "entry_prices": {},
}


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            saved = {}
    except Exception:
        saved = {}

    for key in DEFAULT_STATE:
        if not isinstance(saved.get(key), dict):
            saved[key] = {}

    return saved


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
# OPTIONAL AUTHENTICATED STATE VIEW
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

    return jsonify({"status": "ok", "state": snapshot}), 200


# =====================================================
# API AUTH
# =====================================================
def api_req(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        return {"error": "API credentials missing"}, 401

    method = method.upper()
    query = "?" + urlencode(params, quote_via=quote) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""

    timestamp = str(int(time.time()))
    message = method + timestamp + path + query + payload

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
        "User-Agent": "delta-testnet-fixed-point-bot",
    }

    try:
        r = session.request(
            method,
            BASE_URL + path + query,
            headers=headers,
            data=payload or None,
            timeout=(5, 15)
        )

        try:
            return r.json(), r.status_code
        except Exception:
            return {"error": r.text[:500]}, r.status_code

    except Exception as e:
        return {"error": str(e)}, 500


# =====================================================
# PRICE / CANDLES
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


def fresh_price(raw_price, raw_ts, symbol, source):
    try:
        price = float(raw_price or 0)
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
        log(f"[STALE PRICE] {symbol} SOURCE={source} AGE={age:.1f}s")
        return None

    return price


def get_live_price(symbol):
    try:
        r = session.get(
            BASE_URL + f"/v2/trades/{symbol}",
            timeout=(5, 10)
        )

        if r.status_code == 200:
            data = r.json()

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
                        key=lambda x: parse_timestamp(
                            x.get(
                                "timestamp",
                                x.get("t", x.get("created_at"))
                            )
                        )
                    )

                    p = fresh_price(
                        latest.get("price", latest.get("p")),
                        latest.get(
                            "timestamp",
                            latest.get("t", latest.get("created_at"))
                        ),
                        symbol,
                        "recent_trade"
                    )

                    if p is not None:
                        return p

    except Exception as e:
        log(f"[TRADE PRICE ERROR] {symbol}: {e}")

    try:
        r = session.get(
            BASE_URL + f"/v2/tickers/{symbol}",
            timeout=(5, 10)
        )

        if r.status_code != 200:
            return None

        data = r.json()

        if data.get("success") is not True:
            return None

        result = data.get("result", {})

        if isinstance(result, list):
            result = next(
                (x for x in result if x.get("symbol") == symbol),
                result[0] if result else {}
            )

        if not isinstance(result, dict):
            return None

        return fresh_price(
            result.get("close"),
            result.get("timestamp"),
            symbol,
            "ticker_close"
        )

    except Exception as e:
        log(f"[TICKER ERROR] {symbol}: {e}")
        return None


def get_candles(symbol):
    end = int(time.time())

    params = {
        "resolution": TIMEFRAME,
        "symbol": symbol,
        "start": str(end - CANDLE_LOOKBACK_SECONDS),
        "end": str(end),
    }

    result, code = api_req(
        "GET",
        "/v2/history/candles",
        params=params
    )

    if (
        code == 200
        and isinstance(result, dict)
        and result.get("success") is True
    ):
        return result.get("result", [])

    log(f"[CANDLE API ERROR] {symbol} HTTP={code} {result}")
    return []


def candles_to_df(candles, symbol="?"):
    try:
        if not candles:
            return None

        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles).copy()
        else:
            df = pd.DataFrame(
                candles,
                columns=["time", "open", "high", "low", "close", "volume"]
            )

        needed = ["time", "high", "low", "close", "volume"]

        if not all(c in df.columns for c in needed):
            log(f"[CANDLE INVALID] {symbol}: missing columns")
            return None

        for c in ["high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

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

        closed = df[
            df["time"] + CANDLE_SECONDS <= now
        ].dropna(
            subset=["high", "low", "close", "volume"]
        ).reset_index(drop=True)

        log(
            f"[CANDLE DIAGNOSTIC] {symbol} "
            f"TF={TIMEFRAME} raw={len(df)} closed={len(closed)}"
        )

        if len(closed) < MIN_CLOSED_CANDLES:
            log(f"[CANDLE INSUFFICIENT] {symbol} {len(closed)}")
            return None

        return closed

    except Exception as e:
        log(f"[CANDLE PARSE ERROR] {symbol}: {e}")
        return None


# =====================================================
# EMA 10/20 - TWO CLOSED CANDLE CONFIRMATION
# =====================================================
def get_indicators(candles, symbol):
    df = candles_to_df(candles, symbol)

    if df is None or len(df) < 2:
        return None

    df["ema10"] = df["close"].ewm(
        span=EMA_FAST,
        adjust=False
    ).mean()

    df["ema20"] = df["close"].ewm(
        span=EMA_SLOW,
        adjust=False
    ).mean()

    prev = df.iloc[-2]
    last = df.iloc[-1]

    return {
        "prev_ema10": float(prev["ema10"]),
        "prev_ema20": float(prev["ema20"]),
        "prev_close": float(prev["close"]),
        "ema10": float(last["ema10"]),
        "ema20": float(last["ema20"]),
        "close": float(last["close"]),
        "candle_id": int(last["time"]),
    }


def check_signal(candles, symbol):
    ind = get_indicators(candles, symbol)

    if ind is None:
        return None, None, None

    log(
        f"[EMA CONFIRM SCAN] {symbol} "
        f"PREV EMA10={ind['prev_ema10']:.6f} "
        f"EMA20={ind['prev_ema20']:.6f} "
        f"CLOSE={ind['prev_close']:.6f} | "
        f"CURRENT EMA10={ind['ema10']:.6f} "
        f"EMA20={ind['ema20']:.6f} "
        f"CLOSE={ind['close']:.6f}"
    )

    buy_confirm = (
        ind["prev_ema10"] > ind["prev_ema20"]
        and ind["prev_close"] > ind["prev_ema10"]
        and ind["ema10"] > ind["ema20"]
        and ind["close"] > ind["ema10"]
    )

    sell_confirm = (
        ind["prev_ema10"] < ind["prev_ema20"]
        and ind["prev_close"] < ind["prev_ema10"]
        and ind["ema10"] < ind["ema20"]
        and ind["close"] < ind["ema10"]
    )

    signal = None

    if buy_confirm:
        signal = "buy"
        log(f"[EMA BUY CONFIRMED] {symbol}")

    elif sell_confirm:
        signal = "sell"
        log(f"[EMA SELL CONFIRMED] {symbol}")

    else:
        log(f"[EMA NO CONFIRMATION] {symbol}: weak or mixed trend")

    return signal, ind["candle_id"], ind["close"]


# =====================================================
# POSITIONS / ORDERS
# =====================================================
def parse_pos_symbol(p):
    product = p.get("product") or {}

    return (
        p.get("product_symbol")
        or product.get("symbol")
        or p.get("symbol")
        or ""
    )


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

    for p in positions:
        try:
            if float(p.get("size", 0) or 0) != 0:
                active.append(p)
        except (TypeError, ValueError):
            pass

    return active


def get_orders():
    result, code = api_req(
        "GET",
        "/v2/orders",
        params={"states": "open,pending", "page_size": 100}
    )

    if (
        code == 200
        and isinstance(result, dict)
        and result.get("success") is True
    ):
        orders = result.get("result", [])
        return orders if isinstance(orders, list) else []

    log(f"[ORDER API ERROR] HTTP={code} {result}")
    return None


def is_active_stop(order, symbol):
    return (
        order.get("product_symbol") == symbol
        and order.get("stop_order_type") == "stop_loss_order"
        and order.get("state") in ("open", "pending")
        and order.get("reduce_only") in (True, 1, "true", "True")
    )


def cancel_order(order):
    oid = order.get("id")
    pid = order.get("product_id")

    if not oid or not pid:
        log("[CANCEL ERROR] Missing order/product ID")
        return False

    result, code = api_req(
        "DELETE",
        "/v2/orders",
        body={"id": int(oid), "product_id": int(pid)}
    )

    if (
        code != 200
        or not isinstance(result, dict)
        or result.get("success") is not True
    ):
        log(f"[CANCEL FAILED] ID={oid} HTTP={code} {result}")
        return False

    time.sleep(0.5)

    orders = get_orders()

    if orders is None or any(
        str(x.get("id")) == str(oid) for x in orders
    ):
        log(f"[CANCEL NOT VERIFIED] ID={oid}")
        return False

    log(f"[CANCEL VERIFIED] ID={oid}")
    return True


def valid_stop_price(side, stop, live):
    if live is None or live <= 0 or stop is None or stop <= 0:
        return False

    gap = live * MIN_STOP_GAP_PCT

    return (
        stop < live - gap
        if side == "buy"
        else stop > live + gap
    )


def fmt_price(symbol, price):
    decimals = POINTS[symbol]["decimals"]
    return f"{price:.{decimals}f}"


def verify_stop(symbol, order_id):
    orders = get_orders()

    return orders is not None and any(
        str(o.get("id")) == str(order_id)
        and is_active_stop(o, symbol)
        for o in orders
    )


def place_stop(symbol, side, size, stop_price):
    qty = int(abs(float(size)))

    if qty <= 0:
        return False

    live = get_live_price(symbol)

    if not valid_stop_price(side, stop_price, live):
        log(
            f"[STOP BLOCKED] {symbol} "
            f"SIDE={side} STOP={stop_price} LIVE={live}"
        )
        return False

    stop_side = "sell" if side == "buy" else "buy"

    body = {
        "product_symbol": symbol,
        "size": qty,
        "side": stop_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": fmt_price(symbol, stop_price),
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

    oid = order_result["id"]

    time.sleep(1)

    if not verify_stop(symbol, oid):
        log(f"[STOP NOT VERIFIED] {symbol} ID={oid}; old stop retained")
        return False

    state["stops"][symbol] = float(stop_price)
    state["stop_order_ids"][symbol] = str(oid)
    save_state()

    log(
        f"[STOP VERIFIED] {symbol} SIZE={qty} "
        f"STOP={fmt_price(symbol, stop_price)} ID={oid}"
    )

    # Cancel older stops only after the new stop is verified.
    orders = get_orders()

    if orders is None:
        log(f"[OLD STOP RETAINED] {symbol}: cannot inspect orders")
        return True

    for old in orders:
        if (
            is_active_stop(old, symbol)
            and str(old.get("id")) != str(oid)
        ):
            cancel_order(old)

    return True


def recover_stop(symbol, side, orders):
    stops = []

    for o in (orders or []):
        if is_active_stop(o, symbol):
            try:
                stops.append((float(o.get("stop_price")), o))
            except (TypeError, ValueError):
                pass

    if not stops:
        return float(state["stops"].get(symbol, 0) or 0), False

    price, order = (
        max(stops, key=lambda x: x[0])
        if side == "buy"
        else min(stops, key=lambda x: x[0])
    )

    state["stops"][symbol] = price
    state["stop_order_ids"][symbol] = str(order.get("id", ""))

    return price, True


# =====================================================
# PARTIAL TP + FIXED TRAILING
# =====================================================
def verify_partial_reduction(symbol, expected):
    for i in range(12):
        time.sleep(1)

        positions = get_pos()

        if positions is None:
            continue

        p = next(
            (x for x in positions if parse_pos_symbol(x) == symbol),
            None
        )

        actual = abs(float(p.get("size", 0) or 0)) if p else 0

        if abs(actual - expected) < 0.000001:
            log(
                f"[PARTIAL REDUCTION VERIFIED] "
                f"{symbol} REMAINING={actual}"
            )
            return True

        log(
            f"[PARTIAL VERIFY WAIT] {symbol} "
            f"EXPECTED={expected} ACTUAL={actual} {i+1}/12"
        )

    return False


def execute_partial_tp(
    symbol,
    position,
    current,
    side,
    entry,
    target_distance
):
    if state["partial_tp_done"].get(symbol):
        return

    if state["partial_tp_pending"].get(symbol):
        log(
            f"[PARTIAL TP LOCKED] {symbol}: "
            "unresolved previous order; manual check required"
        )
        return

    size = abs(float(position.get("size", 0) or 0))

    if size < 2:
        log(f"[PARTIAL TP SKIP] {symbol}: position size too small")
        return

    favourable = (
        current - entry
        if side == "buy"
        else entry - current
    )

    if favourable < target_distance:
        return

    close_qty = int(size * PARTIAL_TP_PERCENT)
    remaining = size - close_qty

    if close_qty <= 0 or remaining <= 0:
        return

    close_side = "sell" if side == "buy" else "buy"

    state["partial_tp_pending"][symbol] = {
        "time": time.time(),
        "original_size": size,
        "expected_remaining": remaining,
        "close_quantity": close_qty,
        "side": side,
    }

    save_state()

    log(
        f"[PARTIAL TP TRIGGER] {symbol} "
        f"MOVE={favourable:.6f} "
        f"TARGET={target_distance:.6f} "
        f"CLOSE={close_qty} KEEP={remaining}"
    )

    result, code = api_req(
        "POST",
        "/v2/orders",
        body={
            "product_symbol": symbol,
            "size": close_qty,
            "side": close_side,
            "order_type": "market_order",
            "reduce_only": True,
        }
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
        log(
            f"[PARTIAL TP AMBIGUOUS] {symbol} "
            f"HTTP={code} {result}; no retry"
        )
        return

    if not verify_partial_reduction(symbol, remaining):
        log(
            f"[PARTIAL TP NOT CONFIRMED] {symbol}; "
            "pending lock retained"
        )
        return

    state["partial_tp_done"][symbol] = True
    state["original_sizes"][symbol] = size
    state["entry_prices"][symbol] = entry
    state["partial_tp_pending"].pop(symbol, None)
    save_state()

    log(
        f"[PARTIAL TP COMPLETE] {symbol}: "
        f"50% booked, remaining={remaining}"
    )

    # Breakeven stop on remaining quantity.
    live = get_live_price(symbol)

    if valid_stop_price(side, entry, live):
        if place_stop(symbol, side, remaining, entry):
            state["trail_active"][symbol] = True
            save_state()
            log(f"[BREAKEVEN VERIFIED] {symbol} STOP={entry:.6f}")
        else:
            log(
                f"[URGENT] {symbol}: partial TP done; "
                "BE stop not verified"
            )
    else:
        log(
            f"[BREAKEVEN WAIT] {symbol}: entry stop invalid/too close; "
            "old stop retained"
        )


def handle_trailing():
    positions = get_pos()

    if positions is None:
        log("[TRAIL BLOCKED] Position API unavailable")
        return

    orders = get_orders()

    if orders is None:
        log("[TRAIL BLOCKED] Open orders unavailable")
        return

    active = set()

    for p in positions:
        try:
            signed_size = float(p.get("size", 0) or 0)

            if signed_size == 0:
                continue

            symbol = parse_pos_symbol(p)

            if symbol not in COINS:
                continue

            active.add(symbol)

            side = "buy" if signed_size > 0 else "sell"

            entry = float(
                p.get("entry_price")
                or p.get("average_price")
                or 0
            )

            if entry <= 0:
                log(f"[TRAIL ERROR] {symbol}: entry price missing")
                continue

            current = get_live_price(symbol)

            if current is None:
                log(f"[TRAIL SKIP] {symbol}: fresh price unavailable")
                continue

            cfg = POINTS[symbol]

            sl_gap = cfg["sl"]
            tp_gap = cfg["tp"]
            trail_gap = cfg["trail"]

            old_stop, has_stop = recover_stop(
                symbol,
                side,
                orders
            )

            # Establish initial fixed-point stop if missing.
            if not has_stop:
                initial = (
                    entry - sl_gap
                    if side == "buy"
                    else entry + sl_gap
                )

                if not place_stop(
                    symbol,
                    side,
                    signed_size,
                    initial
                ):
                    log(
                        f"[URGENT] {symbol}: "
                        "initial protective stop not verified"
                    )
                    continue

                old_stop = initial

            peak = float(
                state["peaks"].get(symbol, entry) or entry
            )

            peak = (
                max(peak, current, entry)
                if side == "buy"
                else min(peak, current, entry)
            )

            state["peaks"][symbol] = peak

            execute_partial_tp(
                symbol,
                p,
                current,
                side,
                entry,
                tp_gap
            )

            # Refresh position after possible partial close.
            refreshed = get_pos()

            if refreshed is None:
                log(f"[TRAIL BLOCKED] {symbol}: position refresh failed")
                continue

            p2 = next(
                (
                    x for x in refreshed
                    if parse_pos_symbol(x) == symbol
                ),
                None
            )

            if p2 is None:
                continue

            signed_size = float(p2.get("size", 0) or 0)

            if signed_size == 0:
                continue

            side = "buy" if signed_size > 0 else "sell"

            partial_done = bool(
                state["partial_tp_done"].get(symbol)
            )

            favourable = (
                peak - entry
                if side == "buy"
                else entry - peak
            )

            if not partial_done:
                log(
                    f"[WAIT PARTIAL TARGET] {symbol} "
                    f"MOVE={favourable:.6f} "
                    f"REQUIRED={tp_gap:.6f} "
                    f"STOP={old_stop:.6f}"
                )
                save_state()
                continue

            candidate = (
                peak - trail_gap
                if side == "buy"
                else peak + trail_gap
            )

            # Never trail beyond breakeven in the losing direction.
            candidate = (
                max(candidate, entry)
                if side == "buy"
                else min(candidate, entry)
            )

            old_stop = float(
                state["stops"].get(symbol, old_stop) or old_stop
            )

            candidate = (
                max(candidate, old_stop)
                if side == "buy"
                else min(candidate, old_stop)
            )

            if not valid_stop_price(side, candidate, current):
                log(
                    f"[TRAIL WAIT] {symbol} "
                    f"CANDIDATE={candidate:.6f} "
                    f"LIVE={current:.6f}; old stop retained"
                )
                save_state()
                continue

            improved = (
                candidate > old_stop
                if side == "buy"
                else candidate < old_stop
            )

            if improved:
                log(
                    f"[FIXED TRAIL] {symbol} "
                    f"SIDE={side.upper()} "
                    f"PEAK={peak:.6f} "
                    f"GAP={trail_gap:.6f} "
                    f"OLD={old_stop:.6f} "
                    f"NEW={candidate:.6f}"
                )

                place_stop(
                    symbol,
                    side,
                    signed_size,
                    candidate
                )

            save_state()

        except Exception as e:
            log(f"[TRAIL ERROR] {e}")

    # Clear local state only after exchange reports no open position.
    for symbol in COINS:
        if symbol not in active:
            for key in (
                "peaks",
                "stops",
                "trail_active",
                "stop_order_ids",
                "partial_tp_done",
                "original_sizes",
                "entry_prices",
            ):
                state[key].pop(symbol, None)

            state["partial_tp_pending"].pop(symbol, None)

    save_state()


# =====================================================
# INITIAL STOP AFTER ENTRY
# =====================================================
def initialize_new_position(symbol):
    for attempt in range(8):
        time.sleep(2)

        positions = get_pos()

        if positions is None:
            continue

        p = next(
            (
                x for x in positions
                if parse_pos_symbol(x) == symbol
            ),
            None
        )

        if p is None:
            continue

        size = float(p.get("size", 0) or 0)

        entry = float(
            p.get("entry_price")
            or p.get("average_price")
            or 0
        )

        if size == 0 or entry <= 0:
            continue

        side = "buy" if size > 0 else "sell"

        current = get_live_price(symbol)

        if current is None:
            log(
                f"[INITIAL STOP FAILED] {symbol}: "
                "live price unavailable"
            )
            return False

        gap = POINTS[symbol]["sl"]

        stop = (
            entry - gap
            if side == "buy"
            else entry + gap
        )

        state["peaks"][symbol] = (
            max(entry, current)
            if side == "buy"
            else min(entry, current)
        )

        state["partial_tp_done"].pop(symbol, None)
        state["partial_tp_pending"].pop(symbol, None)

        state["entry_prices"][symbol] = entry
        state["original_sizes"][symbol] = abs(size)

        save_state()

        log(
            f"[INITIAL FIXED STOP] {symbol} "
            f"ENTRY={entry:.8f} "
            f"SL_POINTS={gap} "
            f"STOP={stop:.8f} "
            f"TP_POINTS={POINTS[symbol]['tp']} "
            f"TRAIL={POINTS[symbol]['trail']}"
        )

        return place_stop(
            symbol,
            side,
            size,
            stop
        )

    log(f"[INITIAL STOP FAILED] {symbol}: position not confirmed")
    return False


# =====================================================
# ENTRY SAFETY / ORDERS
# =====================================================
def price_mismatch_ok(symbol, candle_close):
    live = get_live_price(symbol)

    if live is None or candle_close <= 0:
        log(f"[ENTRY BLOCKED] {symbol}: price unavailable")
        return False

    gap = abs(live - candle_close) / candle_close

    log(
        f"[PRICE CHECK] {symbol} "
        f"CANDLE={candle_close:.6f} "
        f"LIVE={live:.6f} "
        f"GAP={gap*100:.3f}%"
    )

    if gap > MAX_CANDLE_LIVE_GAP_PCT:
        log(f"[ENTRY BLOCKED] {symbol}: candle/live mismatch")
        return False

    return True


def place_entry(symbol, side):
    qty = ORDER_SIZES.get(symbol, ORDER_SIZE)

    result, code = api_req(
        "POST",
        "/v2/orders",
        body={
            "product_symbol": symbol,
            "size": qty,
            "side": side,
            "order_type": "market_order",
        }
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
            f"[ENTRY ACCEPTED] {symbol} "
            f"{side.upper()} SIZE={qty} RESULT={order_result}"
        )
        return True

    log(f"[ENTRY FAILED] {symbol} HTTP={code} {result}")
    return False


def close_position_safely(symbol, position):
    size = float(position.get("size", 0) or 0)

    if size == 0:
        return False

    close_side = "sell" if size > 0 else "buy"

    result, code = api_req(
        "POST",
        "/v2/orders",
        body={
            "product_symbol": symbol,
            "size": int(abs(size)),
            "side": close_side,
            "order_type": "market_order",
            "reduce_only": True,
        }
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

    for i in range(12):
        time.sleep(1)

        positions = get_pos()

        if positions is None:
            continue

        remaining = next(
            (
                x for x in positions
                if parse_pos_symbol(x) == symbol
            ),
            None
        )

        if (
            remaining is None
            or float(remaining.get("size", 0) or 0) == 0
        ):
            flat = True
            break

    if not flat:
        log(
            f"[CLOSE BLOCKED] {symbol}: "
            "flat not confirmed; stops retained"
        )
        return False

    orders = get_orders()

    if orders is None:
        log(
            f"[CLOSE BLOCKED] {symbol}: "
            "cannot inspect protective orders"
        )
        return False

    for o in orders:
        if is_active_stop(o, symbol):
            if not cancel_order(o):
                log(f"[CLOSE BLOCKED] {symbol}: old stop remains")
                return False

    for key in (
        "peaks",
        "stops",
        "trail_active",
        "stop_order_ids",
        "partial_tp_done",
        "partial_tp_pending",
        "original_sizes",
        "entry_prices",
    ):
        state[key].pop(symbol, None)

    save_state()

    log(f"[CLOSE COMPLETE] {symbol}: flat and stops cleared")
    return True


# =====================================================
# MAIN LOOP - OPTION B SMART RE-ENTRY
# =====================================================
def main_loop():
    log("==================================================")
    log("DELTA INDIA TESTNET | FIXED POINT PARTIAL TP + TRAILING")
    log(f"AUTO TRADING={ENABLE_TRADING}")
    log(f"COINS={COINS}")
    log(f"ORDER SIZES={ORDER_SIZES}")
    log(f"MAX POSITIONS={MAX_POSITIONS}")
    log(f"EMA={EMA_FAST}/{EMA_SLOW} TF={TIMEFRAME}")
    log(f"FIXED POINT SETTINGS={POINTS}")
    log("EMA CONFIRMATION=2 CLOSED CANDLES")
    log("PARTIAL TP=50%; REMAINING SL=BREAKEVEN THEN FIXED TRAIL")
    log("TESTNET ONLY")
    log("==================================================")

    while True:
        try:
            handle_trailing()

            positions = get_pos()

            if positions is None:
                log("[SCAN BLOCKED] Position API unavailable")
                time.sleep(CHECK_INTERVAL)
                continue

            held = {
                parse_pos_symbol(p): p
                for p in positions
                if (
                    parse_pos_symbol(p) in COINS
                    and float(p.get("size", 0) or 0) != 0
                )
            }

            log(
                f"[POS HELD] {list(held.keys())} "
                f"TOTAL={len(held)}"
            )

            if not ENABLE_TRADING:
                log("[SAFE MODE] New entries disabled")
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol in COINS:
                candles = get_candles(symbol)

                if not candles:
                    continue

                signal, candle_id, candle_close = check_signal(
                    candles,
                    symbol
                )

                if candle_id is None:
                    continue

                previous = int(
                    state["last_candle"].get(symbol, 0) or 0
                )

                if candle_id <= previous:
                    continue

                state["last_candle"][symbol] = candle_id
                save_state()

                current_position = held.get(symbol)

                if current_position is not None:
                    size = float(current_position.get("size", 0) or 0)

                    current_side = (
                        "buy" if size > 0 else "sell"
                    )

                    if signal is None or signal == current_side:
                        log(
                            f"[POSITION HELD] {symbol}: "
                            f"{current_side.upper()} trend continues"
                        )
                        continue

                    log(
                        f"[TREND REVERSAL] {symbol} "
                        f"{current_side.upper()} -> {signal.upper()}"
                    )

                    if not close_position_safely(
                        symbol,
                        current_position
                    ):
                        log(f"[REVERSAL BLOCKED] {symbol}")
                        continue

                    state["last_trade"][symbol] = time.time()
                    save_state()

                    held.pop(symbol, None)

                    log(
                        f"[REVERSAL COOLDOWN] {symbol}: "
                        f"{COOLDOWN_SECONDS}s"
                    )

                    continue

                if signal is None:
                    continue

                if state["partial_tp_pending"].get(symbol):
                    log(
                        f"[ENTRY BLOCKED] {symbol}: "
                        "unresolved partial TP; manual check required"
                    )
                    continue

                open_orders = get_orders()

                if open_orders is None:
                    log(f"[ENTRY BLOCKED] {symbol}: cannot read orders")
                    continue

                orphan_stops = [
                    o for o in open_orders
                    if is_active_stop(o, symbol)
                ]

                cleanup_ok = True

                for old in orphan_stops:
                    if not cancel_order(old):
                        cleanup_ok = False
                        break

                if not cleanup_ok:
                    log(f"[ENTRY BLOCKED] {symbol}: orphan stop remains")
                    continue

                if len(held) >= MAX_POSITIONS:
                    log(f"[MAX POSITIONS] {symbol}: entry skipped")
                    continue

                last_trade = float(
                    state["last_trade"].get(symbol, 0) or 0
                )

                remaining = (
                    COOLDOWN_SECONDS
                    - (time.time() - last_trade)
                )

                if remaining > 0:
                    log(
                        f"[COOLDOWN] {symbol}: "
                        f"{int(remaining)}s remaining"
                    )
                    continue

                if not price_mismatch_ok(symbol, candle_close):
                    continue

                log(
                    f"[SMART ENTRY SIGNAL] {symbol} "
                    f"{signal.upper()} candle={candle_id}"
                )

                state["last_trade"][symbol] = time.time()
                save_state()

                if not place_entry(symbol, signal):
                    log(
                        f"[ENTRY NOT CONFIRMED] {symbol}; "
                        "inspect exchange before retry"
                    )
                    continue

                stop_ok = initialize_new_position(symbol)

                refreshed = get_pos()

                if refreshed is None:
                    log(
                        f"[ENTRY VERIFY ERROR] {symbol}: "
                        "check Testnet position and stop"
                    )
                    continue

                confirmed = next(
                    (
                        p for p in refreshed
                        if (
                            parse_pos_symbol(p) == symbol
                            and float(p.get("size", 0) or 0) != 0
                        )
                    ),
                    None
                )

                if confirmed:
                    held[symbol] = confirmed

                    log(
                        f"[ENTRY COMPLETE] {symbol} "
                        f"INITIAL_STOP_VERIFIED={stop_ok}"
                    )

                    if not stop_ok:
                        log(
                            f"[URGENT CHECK] {symbol}: "
                            "position open but stop not verified"
                        )
                else:
                    log(
                        f"[ENTRY NOT OPEN] {symbol}: "
                        "position not confirmed"
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
        daemon=True
    ).start()

    log("=== FLASK STARTED ===")

    main_loop()
