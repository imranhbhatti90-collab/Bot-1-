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
ENABLE_TRADING = True  # TESTNET ONLY
LEVERAGE = 50  # Informational only; this script does not set leverage.

ATR_STOP_MULTIPLIER = 1.2
ATR_PERIOD = 14
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

# Option B:
# On each NEW closed 15m candle, trade the current EMA direction.
# EMA10 > EMA20 = BUY; EMA10 < EMA20 = SELL.
# No fresh crossover is required. Same candle is never re-used for entry.

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def log(message):
    print(message, flush=True)


@app.route("/")
def home():
    return "DELTA TESTNET AUTO BOT | OPTION B | 15m EMA10/EMA20 DIRECTION | ATR TRAIL"


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "environment": "TESTNET",
        "automatic_trading": ENABLE_TRADING,
        "max_positions": MAX_POSITIONS,
        "strategy": "Option B: EMA10 above EMA20 BUY; below EMA20 SELL, on new closed 15m candle",
        "trailing_method": "ATR-based initial stop, breakeven, chandelier trail",
        "atr_multiplier": ATR_STOP_MULTIPLIER,
    }), 200


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# =====================================================
# STATE
# =====================================================
def load_state():
    default = {
        "peaks": {}, "stops": {}, "last_trade": {}, "last_candle": {},
        "trail_active": {}, "initial_gaps": {}, "stop_order_ids": {},
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
        API_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest()

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "python-delta-testnet-option-b-bot",
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
    if price <= 0 or ts <= 0 or age < -10 or age > MAX_TRADE_AGE_SECONDS:
        log(
            f"[STALE/INVALID LAST PRICE] {symbol} PRICE={price} "
            f"SOURCE={source} AGE={age:.1f}s"
        )
        return None

    log(
        f"[LIVE LAST PRICE] {symbol} PRICE={price:.8f} "
        f"SOURCE={source} AGE={age:.1f}s"
    )
    return price


def get_live_price(symbol):
    # Prefer latest actual trade.
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
                            item.get("timestamp", item.get("t", item.get("created_at")))
                        ),
                    )
                    price = fresh_price(
                        latest.get("price", latest.get("p")),
                        latest.get("timestamp", latest.get("t", latest.get("created_at"))),
                        symbol,
                        "recent_trade",
                    )
                    if price is not None:
                        return price
        else:
            log(f"[TRADE PRICE HTTP] {symbol} HTTP={response.status_code}")
    except Exception as e:
        log(f"[TRADE PRICE ERROR] {symbol}: {e}")

    # Ticker close fallback; deliberately do not use mark_price.
    try:
        response = session.get(
            BASE_URL + f"/v2/tickers/{symbol}",
            headers={"Accept": "application/json"},
            timeout=(5, 10),
        )
        if response.status_code != 200:
            log(f"[TICKER PRICE ERROR] {symbol} HTTP={response.status_code}")
            return None

        data = response.json()
        if data.get("success") is not True:
            log(f"[TICKER PRICE ERROR] {symbol}: {data}")
            return None

        result = data.get("result", {})
        if isinstance(result, list):
            result = next(
                (item for item in result if item.get("symbol") == symbol),
                result[0] if result else {},
            )
        if not isinstance(result, dict) or not result:
            log(f"[TICKER EMPTY] {symbol}: {data}")
            return None

        close_price = result.get("close")
        if close_price in (None, "", 0, "0"):
            log(f"[NO FRESH LAST PRICE] {symbol}; mark_price ignored")
            return None

        return fresh_price(
            close_price, result.get("timestamp"), symbol, "ticker_close"
        )
    except Exception as e:
        log(f"[TICKER PRICE ERROR] {symbol}: {e}")
        return None


def get_candles(symbol, resolution=TIMEFRAME):
    end = int(time.time())
    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": str(end - CANDLE_LOOKBACK_SECONDS),
        "end": str(end),
    }
    result, code = api_req("GET", "/v2/history/candles", params=params)

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
        if not all(column in df.columns for column in required):
            log(f"[CANDLE INVALID] {symbol} missing columns: {list(df.columns)}")
            return None
        if "time" not in df.columns:
            log(f"[CANDLE INVALID] {symbol} timestamp column missing")
            return None

        for column in required:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["time"] = df["time"].apply(parse_timestamp)
        df = df.dropna(subset=["time"]).copy()
        df = df[df["time"] > 0]
        if df.empty:
            log(f"[CANDLE INVALID] {symbol} no valid timestamps")
            return None

        df["time"] = df["time"].astype("int64")
        df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)

        now = int(time.time())
        interval = 900 if resolution == "15m" else 300
        raw_latest = int(df["time"].iloc[-1])
        latest_age = now - raw_latest

        # Candle timestamps are candle OPEN times. A candle is closed only
        # after open_time + interval has passed.
        closed = df[df["time"] + interval <= now].copy()
        closed = closed.dropna(subset=required).reset_index(drop=True)

        closed_latest = int(closed["time"].iloc[-1]) if len(closed) else 0
        log(
            f"[CANDLE DIAGNOSTIC] {symbol} TF={resolution} raw_rows={len(df)} "
            f"raw_latest={raw_latest} raw_age={latest_age}s "
            f"closed_rows={len(closed)} closed_latest={closed_latest} now={now}"
        )

        if len(closed) < MIN_CLOSED_CANDLES:
            log(f"[CANDLE INSUFFICIENT] {symbol} only {len(closed)} closed candles")
            return None

        # Warn if API's newest candle is unexpectedly far behind wall clock.
        # This is diagnostic only; the normal just-opened candle can be <15m old.
        if latest_age > interval * 2:
            log(
                f"[CANDLE DATA DELAY WARNING] {symbol}: newest candle timestamp "
                f"is {latest_age}s old; verify exchange API response"
            )

        return closed
    except Exception as e:
        log(f"[CANDLE PARSE ERROR] {symbol}: {e}")
        return None


# =====================================================
# INDICATORS / OPTION B SIGNAL
# =====================================================
def add_indicators(df):
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema10"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema20"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

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


def check_signal(candles, symbol):
    """
    OPTION B:
    Every new CLOSED 15m candle:
      EMA10 > EMA20 => BUY
      EMA10 < EMA20 => SELL
      EMA10 == EMA20 => no trade
    """
    df = candles_to_df(candles, resolution=TIMEFRAME, symbol=symbol)
    if df is None or len(df) < 30:
        log(f"[SCAN] {symbol} insufficient closed 15m candles")
        return None, None

    try:
        df = add_indicators(df)
        last = df.iloc[-1]
        if pd.isna(last["ema10"]) or pd.isna(last["ema20"]) or pd.isna(last["atr"]):
            log(f"[SCAN] {symbol} EMA/ATR indicators not ready")
            return None, int(last["time"])

        ema10 = float(last["ema10"])
        ema20 = float(last["ema20"])
        close_price = float(last["close"])
        candle_id = int(last["time"])

        log(
            f"[OPTION B SCAN] {symbol} TF=15m Candle={candle_id} "
            f"Close={close_price:.6f} EMA10={ema10:.6f} EMA20={ema20:.6f}"
        )

        if ema10 > ema20:
            log(f"[OPTION B BUY] {symbol}: EMA10 is above EMA20")
            return "buy", candle_id
        if ema10 < ema20:
            log(f"[OPTION B SELL] {symbol}: EMA10 is below EMA20")
            return "sell", candle_id

        log(f"[OPTION B NO TRADE] {symbol}: EMA10 equals EMA20")
        return None, candle_id
    except Exception as e:
        log(f"[SIGNAL ERROR] {symbol}: {e}")
        return None, None


# =====================================================
# POSITIONS / ORDERS
# =====================================================
def get_pos():
    result, code = api_req("GET", "/v2/positions/margined")
    if code != 200 or not isinstance(result, dict) or result.get("success") is not True:
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
    if code == 200 and isinstance(result, dict) and result.get("success") is True:
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
        str(order.get("id")) == str(order_id) and is_active_stop(order, symbol)
        for order in orders
    )


def cancel_stop(order):
    order_id = order.get("id")
    product_id = order.get("product_id")
    if not order_id or not product_id:
        log("[CANCEL ERROR] Stop order ID/product ID missing")
        return False

    result, code = api_req(
        "DELETE",
        "/v2/orders",
        body={"id": int(order_id), "product_id": int(product_id)},
    )
    if code != 200 or not isinstance(result, dict) or not result.get("success"):
        log(f"[CANCEL FAILED] ID={order_id} HTTP={code} {result}")
        return False

    time.sleep(0.7)
    orders = get_orders()
    if orders is None:
        log(f"[CANCEL UNVERIFIED] ID={order_id}")
        return False
    if any(str(item.get("id")) == str(order_id) for item in orders):
        log(f"[CANCEL NOT CONFIRMED] ID={order_id}")
        return False

    log(f"[CANCEL VERIFIED] ID={order_id}")
    return True


def valid_stop_price(position_side, stop_price, live_price):
    if live_price is None or live_price <= 0 or stop_price is None or stop_price <= 0:
        return False
    minimum_gap = live_price * MIN_STOP_GAP_PCT
    if position_side == "buy":
        return stop_price < live_price - minimum_gap
    if position_side == "sell":
        return stop_price > live_price + minimum_gap
    return False


def place_stop(symbol, position_side, size, stop_price):
    quantity = int(abs(float(size)))
    if quantity <= 0 or stop_price <= 0:
        log("[STOP ERROR] Invalid stop size/price")
        return False

    live = get_live_price(symbol)
    if live is None or not valid_stop_price(position_side, stop_price, live):
        log(
            f"[STOP BLOCKED] {symbol} SIDE={position_side} "
            f"STOP={stop_price:.6f} LAST={live}; wrong side/too close"
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
    order_result = result.get("result", {}) if isinstance(result, dict) else {}

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
    state.setdefault("stop_order_ids", {})[symbol] = str(order_id)
    save_state()
    log(f"[STOP VERIFIED] {symbol} {stop_side.upper()} STOP={stop_price:.6f} ID={order_id}")

    orders = get_orders()
    if orders is None:
        log(f"[OLD STOP RETAINED] {symbol}: could not verify order list")
        return True

    for old_order in orders:
        if (
            is_active_stop(old_order, symbol)
            and str(old_order.get("id")) != str(order_id)
        ):
            if not cancel_stop(old_order):
                log(f"[OLD STOP NOT CANCELLED] {symbol} ID={old_order.get('id')}")
    return True


# =====================================================
# ATR INITIAL STOP + BREAKEVEN + TRAILING
# =====================================================
def get_atr_gap(symbol):
    candles = get_candles(symbol)
    df = candles_to_df(candles, symbol=symbol) if candles else None
    if df is None:
        return None

    df = add_indicators(df)
    atr = float(df.iloc[-1].get("atr", 0) or 0)
    if atr <= 0:
        log(f"[ATR INVALID] {symbol} ATR={atr}")
        return None

    gap = atr * ATR_STOP_MULTIPLIER
    log(f"[ATR GAP] {symbol} ATR14={atr:.8f} GAP={gap:.8f}")
    return gap


def recover_stop_state(symbol, side, orders):
    active = [order for order in (orders or []) if is_active_stop(order, symbol)]
    parsed = []
    for order in active:
        try:
            parsed.append((float(order.get("stop_price")), order))
        except (TypeError, ValueError):
            continue

    if not parsed:
        return float(state["stops"].get(symbol, 0) or 0), False

    if side == "buy":
        chosen_price, chosen_order = max(parsed, key=lambda item: item[0])
    else:
        chosen_price, chosen_order = min(parsed, key=lambda item: item[0])

    state["stops"][symbol] = chosen_price
    state.setdefault("stop_order_ids", {})[symbol] = str(chosen_order.get("id", ""))
    return chosen_price, True


def handle_trailing():
    positions = get_pos()
    if positions is None:
        log("[TRAIL BLOCKED] Position API unavailable")
        return

    orders = get_orders()
    if orders is None:
        log("[TRAIL BLOCKED] Cannot read open orders; existing stops untouched")
        return

    active_symbols = set()

    for position in positions:
        try:
            size = float(position.get("size", 0) or 0)
            if not size:
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
                log(f"[TRAIL ERROR] {symbol} entry price missing")
                continue

            current = get_live_price(symbol)
            if current is None:
                log(f"[TRAIL SKIP] {symbol} no fresh last price; exchange stop retained")
                continue

            side = "buy" if size > 0 else "sell"
            old_stop, _ = recover_stop_state(symbol, side, orders)
            gap = float(state.get("initial_gaps", {}).get(symbol, 0) or 0)

            if gap <= 0 and old_stop > 0:
                gap = (entry - old_stop) if side == "buy" else (old_stop - entry)
                if gap > 0:
                    state.setdefault("initial_gaps", {})[symbol] = gap
                    log(f"[GAP RECOVERED] {symbol} GAP={gap:.8f}")

            if gap <= 0:
                gap = get_atr_gap(symbol) or 0
                if gap > 0:
                    state.setdefault("initial_gaps", {})[symbol] = gap
                    initial_stop = entry - gap if side == "buy" else entry + gap
                    if old_stop <= 0:
                        log(f"[INITIAL STOP MISSING] {symbol}; placing ATR stop={initial_stop:.8f}")
                        if not place_stop(symbol, side, size, initial_stop):
                            log(f"[URGENT] {symbol} initial ATR stop could not be verified")
                            continue
                        old_stop = initial_stop
                else:
                    log(f"[TRAIL PAUSED] {symbol} ATR unavailable; existing stop untouched")
                    continue

            old_peak = float(state["peaks"].get(symbol, entry) or entry)
            if side == "buy":
                peak = max(old_peak, entry, current)
            else:
                peak = min(old_peak, entry, current)
            state["peaks"][symbol] = peak

            favourable = (peak - entry) if side == "buy" else (entry - peak)
            if favourable < gap:
                log(
                    f"[WAIT BREAKEVEN] {symbol} SIDE={side.upper()} ENTRY={entry:.6f} "
                    f"PEAK={peak:.6f} MOVE={favourable:.6f} REQUIRED={gap:.6f} "
                    f"STOP={old_stop:.6f}"
                )
                save_state()
                continue

            candidate = peak - gap if side == "buy" else peak + gap
            candidate = max(candidate, entry) if side == "buy" else min(candidate, entry)
            if old_stop > 0:
                candidate = max(candidate, old_stop) if side == "buy" else min(candidate, old_stop)

            if not valid_stop_price(side, candidate, current):
                log(f"[TRAIL WAIT] {symbol} candidate={candidate:.6f} LAST={current:.6f}; stop retained")
                save_state()
                continue

            improved = (
                old_stop <= 0 or candidate > old_stop
                if side == "buy"
                else old_stop <= 0 or candidate < old_stop
            )
            log(
                f"[ATR TRAIL] {symbol} SIDE={side.upper()} ENTRY={entry:.6f} "
                f"PEAK={peak:.6f} GAP={gap:.6f} OLD={old_stop:.6f} NEW={candidate:.6f}"
            )
            if improved:
                place_stop(symbol, side, size, candidate)
            save_state()

        except Exception as e:
            log(f"[TRAIL ERROR] {position.get('product_symbol', '?')}: {e}")

    # Clear local state for closed positions only; never cancel exchange stops here.
    for symbol in list(state["peaks"].keys()):
        if symbol in COINS and symbol not in active_symbols:
            state["peaks"].pop(symbol, None)
            state["stops"].pop(symbol, None)
            state.get("trail_active", {}).pop(symbol, None)
            state.get("initial_gaps", {}).pop(symbol, None)
            state.get("stop_order_ids", {}).pop(symbol, None)
    save_state()


def initialize_new_position(symbol):
    for attempt in range(6):
        time.sleep(2)
        positions = get_pos()
        if positions is None:
            log(f"[INITIAL STOP BLOCKED] {symbol} position API error")
            continue

        position = next(
            (item for item in positions if parse_pos_symbol(item) == symbol),
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

        gap = get_atr_gap(symbol)
        if gap is None or gap <= 0:
            log(f"[INITIAL STOP FAILED] {symbol}: ATR unavailable; manually check Testnet position")
            return False

        side = "buy" if size > 0 else "sell"
        stop_price = entry - gap if side == "buy" else entry + gap
        current = get_live_price(symbol)
        if current is None:
            log(f"[INITIAL STOP FAILED] {symbol}: live last price unavailable")
            return False

        state.setdefault("initial_gaps", {})[symbol] = gap
        state["peaks"][symbol] = (
            max(entry, current) if side == "buy" else min(entry, current)
        )
        state["trail_active"][symbol] = False
        save_state()

        log(
            f"[INITIAL ATR STOP] {symbol} ENTRY={entry:.8f} GAP={gap:.8f} "
            f"STOP={stop_price:.8f}; breakeven after favourable move={gap:.8f}"
        )
        return place_stop(symbol, side, size, stop_price)

    log(f"[INITIAL STOP FAILED] {symbol}: position not confirmed; check Testnet")
    return False


# =====================================================
# ENTRY SAFETY / ORDER
# =====================================================
def price_mismatch_ok(symbol, candle_close):
    live = get_live_price(symbol)
    if live is None or candle_close <= 0:
        log(f"[ENTRY BLOCKED] {symbol} live/candle price unavailable or stale")
        return False

    gap_pct = abs(live - candle_close) / candle_close
    log(
        f"[PRICE CHECK] {symbol} CANDLE={candle_close:.6f} LIVE={live:.6f} "
        f"GAP={gap_pct * 100:.3f}% LIMIT={MAX_CANDLE_LIVE_GAP_PCT * 100:.2f}%"
    )
    if gap_pct > MAX_CANDLE_LIVE_GAP_PCT:
        log(f"[ENTRY BLOCKED] {symbol} candle/live mismatch {gap_pct * 100:.3f}%")
        return False
    return True


def place_entry(symbol, side):
    body = {
        "product_symbol": symbol,
        "size": ORDER_SIZES.get(symbol, ORDER_SIZE),
        "side": side,
        "order_type": "market_order",
    }
    result, code = api_req("POST", "/v2/orders", body=body)
    order_result = result.get("result", {}) if isinstance(result, dict) else {}

    if code == 200 and isinstance(result, dict) and result.get("success") is True:
        log(
            f"[ENTRY ACCEPTED] {symbol} {side.upper()} "
            f"SIZE={ORDER_SIZES.get(symbol, ORDER_SIZE)} RESULT={order_result}"
        )
        return True

    log(f"[ENTRY FAILED] {symbol} HTTP={code} {result}")
    return False


def close_position_safely(symbol, position):
    """
    Close an existing position with a reduce-only market order.
    Do not cancel its protective stop until the exchange confirms the
    position is flat. Do not permit a reverse entry unless all old stops
    are confirmed cancelled.
    """
    try:
        size = float(position.get("size", 0) or 0)
    except (TypeError, ValueError):
        log(f"[REVERSAL BLOCKED] {symbol}: invalid position size")
        return False

    if size == 0:
        log(f"[REVERSAL BLOCKED] {symbol}: position already flat")
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
        f"[REVERSAL CLOSE REQUEST] {symbol} "
        f"POSITION_SIZE={size} CLOSE_SIDE={close_side.upper()} REDUCE_ONLY=True"
    )
    result, code = api_req("POST", "/v2/orders", body=body)
    order_result = result.get("result", {}) if isinstance(result, dict) else {}

    if (
        code != 200
        or not isinstance(result, dict)
        or result.get("success") is not True
        or not order_result.get("id")
    ):
        log(f"[REVERSAL CLOSE FAILED] {symbol} HTTP={code} {result}")
        return False

    log(
        f"[REVERSAL CLOSE ACCEPTED] {symbol} "
        f"ORDER_ID={order_result.get('id')}; verifying flat position"
    )

    # Exchange acceptance is not proof of a completed close.
    flat = False
    for attempt in range(12):
        time.sleep(1)
        positions = get_pos()
        if positions is None:
            log(f"[REVERSAL CLOSE VERIFY ERROR] {symbol}: position API unavailable")
            continue

        remaining = next(
            (p for p in positions if parse_pos_symbol(p) == symbol),
            None,
        )
        remaining_size = float(remaining.get("size", 0) or 0) if remaining else 0.0
        if remaining_size == 0:
            flat = True
            break

        log(
            f"[REVERSAL CLOSE WAIT] {symbol} "
            f"REMAINING_SIZE={remaining_size} CHECK={attempt + 1}/12"
        )

    if not flat:
        log(
            f"[REVERSAL BLOCKED] {symbol}: exchange has not confirmed flat; "
            "protective stop retained and opposite entry NOT sent"
        )
        return False

    log(f"[REVERSAL POSITION FLAT] {symbol}: exchange confirms size=0")

    # Only after the position is confirmed flat, cancel any old protective stops.
    orders = get_orders()
    if orders is None:
        log(f"[REVERSAL BLOCKED] {symbol}: cannot verify/cancel old stops")
        return False

    old_stops = [o for o in orders if is_active_stop(o, symbol)]
    for old_order in old_stops:
        if not cancel_stop(old_order):
            log(
                f"[REVERSAL BLOCKED] {symbol}: old stop "
                f"{old_order.get('id')} was not confirmed cancelled"
            )
            return False

    # Reset per-position trailing state only after close and stop cleanup.
    state["peaks"].pop(symbol, None)
    state["stops"].pop(symbol, None)
    state.get("trail_active", {}).pop(symbol, None)
    state.get("initial_gaps", {}).pop(symbol, None)
    state.get("stop_order_ids", {}).pop(symbol, None)
    save_state()

    log(f"[REVERSAL CLOSE COMPLETE] {symbol}: flat and old stops cleared")
    return True


# =====================================================
# MAIN LOOP
# =====================================================
def main_loop():
    log("==================================================")
    log("DELTA EXCHANGE INDIA TESTNET SMART RE-ENTRY BOT STARTED")
    log(f"AUTO TRADING = {'ON' if ENABLE_TRADING else 'OFF'}")
    log(f"LEVERAGE SETTING = {LEVERAGE}x (informational only; not set by script)")
    log("SMART RE-ENTRY: EMA10 > EMA20 BUY | EMA10 < EMA20 SELL")
    log("REVERSAL: reduce-only close -> verify flat -> cancel old stop -> cooldown -> opposite entry")
    log("RE-ENTRY: after a position closes, re-enter on a later NEW closed 15m candle if trend remains")
    log("ENTRY: one evaluation per NEW CLOSED 15m candle; no crossover required")
    log(
        f"ATR STOP/TRAIL: ATR({ATR_PERIOD}) x {ATR_STOP_MULTIPLIER}; "
        "breakeven after one favourable gap, then trail peak"
    )
    log(f"MAX POSITIONS={MAX_POSITIONS} | COINS={COINS}")
    log(f"ORDER SIZES={ORDER_SIZES} | COOLDOWN={COOLDOWN_SECONDS}s")
    log("TESTNET ONLY - verify every order and protective stop in the exchange account")
    log("==================================================")

    while True:
        try:
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
            held = list(held_positions.keys())
            log(f"[POS HELD] {held} TOTAL={len(held)}")

            if not ENABLE_TRADING:
                log("[SAFE MODE] New entries disabled; stop management remains active")
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol in COINS:
                candles = get_candles(symbol)
                if not candles:
                    continue

                signal, candle_id = check_signal(candles, symbol)
                if candle_id is None:
                    continue

                previous_candle = int(state["last_candle"].get(symbol, 0) or 0)
                if candle_id <= previous_candle:
                    log(
                        f"[CANDLE ALREADY PROCESSED] {symbol} candle={candle_id} "
                        f"last_processed={previous_candle}"
                    )
                    continue

                # Consume each closed candle once, including candles used for reversal checks.
                state["last_candle"][symbol] = candle_id
                save_state()

                current_position = held_positions.get(symbol)

                if current_position is not None:
                    current_size = float(current_position.get("size", 0) or 0)
                    current_side = "buy" if current_size > 0 else "sell"

                    if signal is None:
                        log(f"[POSITION HELD] {symbol} no directional signal; keep {current_side.upper()}")
                        continue

                    if signal == current_side:
                        log(
                            f"[POSITION HELD] {symbol} trend still {signal.upper()}; "
                            "no duplicate entry"
                        )
                        continue

                    log(
                        f"[TREND REVERSAL] {symbol} OLD={current_side.upper()} "
                        f"NEW={signal.upper()} candle={candle_id}"
                    )
                    if not close_position_safely(symbol, current_position):
                        log(f"[REVERSAL NOT COMPLETED] {symbol}; opposite entry blocked")
                        continue

                    # Start a fresh cooldown after the old position is verifiably closed.
                    state["last_trade"][symbol] = time.time()
                    save_state()
                    log(
                        f"[REVERSAL COOLDOWN] {symbol} {COOLDOWN_SECONDS}s; "
                        "will consider the opposite signal on a later new 15m candle"
                    )
                    held_positions.pop(symbol, None)
                    held = list(held_positions.keys())
                    continue

                # No position: normal entry or smart re-entry after a close.
                if signal is None:
                    continue

                # Never open a fresh position while an orphaned protective stop
                # from a previously closed position is still active.
                open_orders = get_orders()
                if open_orders is None:
                    log(f"[ENTRY BLOCKED] {symbol}: cannot verify old protective orders")
                    continue
                orphan_stops = [
                    order for order in open_orders if is_active_stop(order, symbol)
                ]
                orphan_cleanup_ok = True
                for old_stop in orphan_stops:
                    if not cancel_stop(old_stop):
                        orphan_cleanup_ok = False
                        log(
                            f"[ENTRY BLOCKED] {symbol}: orphan stop "
                            f"{old_stop.get('id')} remains active"
                        )
                        break
                if not orphan_cleanup_ok:
                    continue

                if len(held_positions) >= MAX_POSITIONS:
                    log(f"[MAX POSITIONS] {symbol} entry skipped; {len(held_positions)} positions already open")
                    continue

                last_trade = float(state["last_trade"].get(symbol, 0) or 0)
                remaining = COOLDOWN_SECONDS - (time.time() - last_trade)
                if remaining > 0:
                    log(f"[COOLDOWN] {symbol} {int(remaining)}s remaining")
                    continue

                log(f"[SMART ENTRY SIGNAL] {symbol} {signal.upper()} candle={candle_id}")

                df = candles_to_df(candles, resolution=TIMEFRAME, symbol=symbol)
                if df is None:
                    continue
                candle_close = float(df.iloc[-1]["close"])
                if not price_mismatch_ok(symbol, candle_close):
                    continue

                # Set cooldown before submitting to avoid duplicate submissions
                # if the exchange response is delayed or ambiguous.
                state["last_trade"][symbol] = time.time()
                save_state()

                if not place_entry(symbol, signal):
                    log(f"[ENTRY NOT CONFIRMED] {symbol}; inspect exchange orders/positions")
                    continue

                # Confirm position and install its protective stop before considering it held.
                stop_ok = initialize_new_position(symbol)
                refreshed = get_pos()
                if refreshed is None:
                    log(f"[ENTRY VERIFY ERROR] {symbol}; check Testnet positions and stop manually")
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
                    held = list(held_positions.keys())
                    log(f"[ENTRY COMPLETE] {symbol} INITIAL_STOP_VERIFIED={stop_ok}")
                else:
                    log(
                        f"[ENTRY NOT FILLED/NOT OPEN] {symbol}; no position confirmed. "
                        "Check exchange orders before retrying."
                    )

                if not stop_ok and confirmed_position:
                    log(
                        f"[URGENT CHECK] {symbol}: position is open but initial "
                        "protective stop was NOT verified. Check Testnet immediately."
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
    threading.Thread(target=run_flask, daemon=True).start()
    log("=== FLASK STARTED ===")
    main_loop()
