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

# ==================================================
# DELTA EXCHANGE INDIA TESTNET ONLY
# ==================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINS = [
    "BTCUSD", "ETHUSD", "SOLUSD",
    "XAUTUSD", "PAXGUSD", "SILVERUSD"
]

ORDER_SIZES = {
    "BTCUSD": 40,
    "ETHUSD": 40,
    "SOLUSD": 40,
    "XAUTUSD": 200,
    "PAXGUSD": 200,
    "SILVERUSD": 200
}

MAX_POSITIONS = 6
ENABLE_TRADING = True

# Informational only; this script does not set exchange leverage.
LEVERAGE = 50

# ==================================================
# STRATEGY SETTINGS
# ==================================================

TIMEFRAME = "5m"
CHECK_INTERVAL = 15
COOLDOWN_SECONDS = 300

STATE_FILE = "state.json"

EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 100

RSI_PERIOD = 14
ADX_PERIOD = 14
ATR_PERIOD = 14

MIN_ADX = 18.0

VOLUME_PERIOD = 20
MIN_VOLUME_MULTIPLIER = 1.0

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ==================================================
# EXIT SETTINGS
# ==================================================

INITIAL_STOP_ATR = 1.0
TAKE_PROFIT_ATR = 1.2

PROFIT_LOCK_TRIGGER_ATR = 0.5
TRAIL_GAP_ATR = 0.5

STOP_TRIGGER_METHOD = "last_traded_price"

MIN_STOP_GAP_PCT = 0.0005
MAX_CANDLE_LIVE_GAP_PCT = 0.005

MAX_TRADE_AGE_SECONDS = 30

# ==================================================
# ESTIMATED FEES AND CONTRACT MULTIPLIERS
# These are estimates, not verified exchange values.
# ==================================================

FEE_RATE_PER_SIDE = {
    "BTCUSD": 0.00065,
    "ETHUSD": 0.00065,
    "SOLUSD": 0.00065,
    "XAUTUSD": 0.00015,
    "PAXGUSD": 0.00015,
    "SILVERUSD": 0.00015
}

CONTRACT_MULTIPLIER = {
    "BTCUSD": 0.001,
    "ETHUSD": 0.01,
    "SOLUSD": 1.0,
    "XAUTUSD": 0.001,
    "PAXGUSD": 0.001,
    "SILVERUSD": 0.1
}

ESTIMATED_SLIPPAGE_PER_SIDE = 0.00025

MIN_NET_TARGET_USD = 2.00

MAX_ESTIMATED_COST_TO_ATR_PROFIT = 0.55

LOOKBACK_SECONDS = 3 * 24 * 60 * 60

# ==================================================
# FLASK SERVER
# ==================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s"
)


def log(msg):
    print(msg, flush=True)


@app.route("/")
def home():
    return (
        "DELTA TESTNET AUTO BOT | "
        "5M SCALPING | FEE-AWARE NET TARGET"
    )


@app.route("/health")
def health():
    return jsonify(
        status="ok",
        environment="TESTNET",
        automatic_trading=ENABLE_TRADING,
        coins=COINS,
        max_positions=MAX_POSITIONS,
        strategy="EMA 9/21/100 + RSI + ADX + MACD + Volume",
        stop_atr=INITIAL_STOP_ATR,
        target_atr=TAKE_PROFIT_ATR,
        profit_lock_atr=PROFIT_LOCK_TRIGGER_ATR,
        trail_atr=TRAIL_GAP_ATR
    ), 200


def run_flask():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )


# ==================================================
# STATE MANAGEMENT
# ==================================================

DEFAULT_STATE = {
    "last_trade": {},
    "last_candle": {},
    "peaks": {},
    "initial_gaps": {},
    "targets": {},
    "stops": {},
    "profit_locked": {},
    "stop_order_ids": {}
}


def load_state():
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)

        if not isinstance(saved, dict):
            return {k: {} for k in DEFAULT_STATE}

        for key in DEFAULT_STATE:
            if not isinstance(saved.get(key), dict):
                saved[key] = {}

        return saved

    except Exception:
        return {k: {} for k in DEFAULT_STATE}


state = load_state()
state_lock = threading.Lock()


def save_state():
    try:
        with state_lock:
            with open(STATE_FILE + ".tmp", "w") as f:
                json.dump(state, f)

            os.replace(STATE_FILE + ".tmp", STATE_FILE)

    except Exception as e:
        log(f"[STATE SAVE ERROR] {e}")


# ==================================================
# API SESSION AND SIGNATURE
# ==================================================

session = requests.Session()


def api_req(method, path, params=None, body=None):

    if not API_KEY or not API_SECRET:
        return {"error": "API credentials missing"}, 401

    method = method.upper()

    qs = (
        "?" + urlencode(params, quote_via=quote)
        if params else ""
    )

    payload = (
        json.dumps(body, separators=(",", ":"))
        if body is not None else ""
    )

    timestamp = str(int(time.time()))

    message = (
        method
        + timestamp
        + path
        + qs
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
        "User-Agent": "python-delta-testnet-scalper"
    }

    try:
        response = session.request(
            method,
            BASE_URL + path + qs,
            headers=headers,
            data=payload or None,
            timeout=(5, 15)
        )

        try:
            return response.json(), response.status_code

        except Exception:
            return {"error": response.text[:500]}, response.status_code

    except Exception as e:
        return {"error": str(e)}, 500


# ==================================================
# TIMESTAMP AND LIVE PRICE
# ==================================================

def parse_ts(raw):

    if raw in (None, ""):
        return 0.0

    try:
        value = float(raw)

        if value > 1e14:
            value /= 1e6

        elif value > 1e11:
            value /= 1e3

        return value

    except (TypeError, ValueError):

        try:
            from datetime import datetime

            return datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            ).timestamp()

        except Exception:
            return 0.0


def fresh_price(raw, ts, symbol, source):

    try:
        price = float(raw or 0)

    except (TypeError, ValueError):
        return None

    stamp = parse_ts(ts)

    age = time.time() - stamp if stamp else 999999

    if (
        price <= 0
        or not stamp
        or age < -10
        or age > MAX_TRADE_AGE_SECONDS
    ):
        log(
            f"[STALE PRICE] {symbol} "
            f"{source} age={age:.1f}s"
        )
        return None

    return price


def get_live_price(symbol):

    # Recent trade price first.
    try:
        response = session.get(
            BASE_URL + f"/v2/trades/{symbol}",
            timeout=(5, 10)
        )

        if response.status_code == 200:

            data = response.json()

            if data.get("success"):

                result = data.get("result", {})

                trades = (
                    result.get("trades", [])
                    if isinstance(result, dict)
                    else (
                        result
                        if isinstance(result, list)
                        else []
                    )
                )

                if trades:

                    trade = max(
                        trades,
                        key=lambda item: parse_ts(
                            item.get(
                                "timestamp",
                                item.get(
                                    "t",
                                    item.get("created_at")
                                )
                            )
                        )
                    )

                    price = fresh_price(
                        trade.get("price", trade.get("p")),
                        trade.get(
                            "timestamp",
                            trade.get(
                                "t",
                                trade.get("created_at")
                            )
                        ),
                        symbol,
                        "trade"
                    )

                    if price is not None:
                        return price

    except Exception as e:
        log(f"[TRADE PRICE ERROR] {symbol}: {e}")

    # Ticker close fallback.
    try:
        response = session.get(
            BASE_URL + f"/v2/tickers/{symbol}",
            timeout=(5, 10)
        )

        if response.status_code != 200:
            return None

        data = response.json()

        if not data.get("success"):
            return None

        result = data.get("result", {})

        if isinstance(result, list):

            result = next(
                (
                    item for item in result
                    if item.get("symbol") == symbol
                ),
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


# ==================================================
# CANDLE DATA
# ==================================================

def get_candles(symbol):

    end = int(time.time())

    params = {
        "resolution": TIMEFRAME,
        "symbol": symbol,
        "start": str(end - LOOKBACK_SECONDS),
        "end": str(end)
    }

    result, code = api_req(
        "GET",
        "/v2/history/candles",
        params=params
    )

    if (
        code == 200
        and isinstance(result, dict)
        and result.get("success")
        and result.get("result")
    ):
        return result["result"]

    log(f"[CANDLE ERROR] {symbol} HTTP={code} {result}")

    return []


def candles_df(candles):

    try:

        if not candles:
            return None

        if isinstance(candles[0], dict):

            df = pd.DataFrame(candles).copy()

        else:

            df = pd.DataFrame(
                candles,
                columns=[
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume"
                ]
            )

        required = [
            "time",
            "high",
            "low",
            "close",
            "volume"
        ]

        if not all(col in df.columns for col in required):
            return None

        for col in required:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df = df.dropna(subset=required).copy()

        def normalize_time(value):

            value = float(value)

            if value > 1e14:
                value /= 1e6

            elif value > 1e11:
                value /= 1e3

            return int(value)

        df["time"] = df["time"].apply(normalize_time)

        df = (
            df.sort_values("time")
            .drop_duplicates("time")
        )

        # Only completed 5-minute candles.
        df = df[
            df["time"] + 300 <= int(time.time())
        ].reset_index(drop=True)

        return df if len(df) >= 120 else None

    except Exception as e:

        log(f"[CANDLE PARSE ERROR] {e}")

        return None  
# ==================================================
# TECHNICAL INDICATORS
# ==================================================

def calculate_indicators(df):
    df = df.copy()

    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False).mean()
    df["ema_trend"] = close.ewm(span=EMA_TREND, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    previous_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False
    ).mean()

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where(
        (plus_dm > minus_dm) & (plus_dm > 0), 0
    )

    minus_dm = minus_dm.where(
        (minus_dm > plus_dm) & (minus_dm > 0), 0
    )

    atr_adx = tr.ewm(
        alpha=1 / ADX_PERIOD,
        adjust=False
    ).mean()

    plus_di = (
        100 * plus_dm.ewm(
            alpha=1 / ADX_PERIOD,
            adjust=False
        ).mean() / atr_adx.replace(0, float("nan"))
    )

    minus_di = (
        100 * minus_dm.ewm(
            alpha=1 / ADX_PERIOD,
            adjust=False
        ).mean() / atr_adx.replace(0, float("nan"))
    )

    dx = (
        100 * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0, float("nan"))
    )

    df["adx"] = dx.ewm(
        alpha=1 / ADX_PERIOD,
        adjust=False
    ).mean()

    macd_fast = close.ewm(
        span=MACD_FAST,
        adjust=False
    ).mean()

    macd_slow = close.ewm(
        span=MACD_SLOW,
        adjust=False
    ).mean()

    df["macd"] = macd_fast - macd_slow

    df["macd_signal"] = df["macd"].ewm(
        span=MACD_SIGNAL,
        adjust=False
    ).mean()

    df["volume_avg"] = df["volume"].rolling(
        VOLUME_PERIOD
    ).mean()

    return df


# ==================================================
# BUY / SELL SIGNAL
# ==================================================

def get_signal(df):

    if df is None or len(df) < 120:
        return None, "Not enough candles"

    df = calculate_indicators(df)
    current = df.iloc[-1]
    recent = df.iloc[-4:-1]

    values = [
        current["ema_fast"],
        current["ema_slow"],
        current["ema_trend"],
        current["rsi"],
        current["adx"],
        current["atr"],
        current["macd"],
        current["macd_signal"],
        current["volume_avg"]
    ]

    if any(pd.isna(value) for value in values):
        return None, "Indicator data incomplete"

    close = float(current["close"])
    atr = float(current["atr"])

    if close <= 0 or atr <= 0:
        return None, "Invalid price or ATR"

    if atr / close > 0.05:
        return None, "ATR too large relative to price"

    pullback_buy = (
        (recent["low"] <= recent["ema_fast"] * 1.002)
        | (recent["low"] <= recent["ema_slow"] * 1.002)
    ).any()

    pullback_sell = (
        (recent["high"] >= recent["ema_fast"] * 0.998)
        | (recent["high"] >= recent["ema_slow"] * 0.998)
    ).any()

    volume_ok = (
        current["volume"]
        >= current["volume_avg"] * MIN_VOLUME_MULTIPLIER
    )

    buy = (
        close > current["ema_trend"]
        and current["ema_fast"] > current["ema_slow"]
        and close > current["ema_fast"]
        and pullback_buy
        and 48 <= current["rsi"] <= 68
        and current["adx"] >= MIN_ADX
        and current["macd"] > current["macd_signal"]
        and volume_ok
    )

    sell = (
        close < current["ema_trend"]
        and current["ema_fast"] < current["ema_slow"]
        and close < current["ema_fast"]
        and pullback_sell
        and 32 <= current["rsi"] <= 52
        and current["adx"] >= MIN_ADX
        and current["macd"] < current["macd_signal"]
        and volume_ok
    )

    if buy:
        return "buy", "BUY conditions passed"

    if sell:
        return "sell", "SELL conditions passed"

    return None, "No complete signal"


# ==================================================
# FEE ESTIMATION
# ==================================================

def estimated_round_trip_cost(symbol, price, size):

    multiplier = CONTRACT_MULTIPLIER[symbol]

    notional = abs(
        float(price) * float(size) * multiplier
    )

    fee_rate = FEE_RATE_PER_SIDE[symbol]
    fees = notional * fee_rate * 2

    slippage = (
        notional * ESTIMATED_SLIPPAGE_PER_SIDE * 2
    )

    return fees + slippage, fees, slippage, notional


def fee_aware_target_gap(symbol, entry, size, atr):

    multiplier = CONTRACT_MULTIPLIER[symbol]

    notional = abs(entry * size * multiplier)

    round_trip_rate = 2 * (
        FEE_RATE_PER_SIDE[symbol]
        + ESTIMATED_SLIPPAGE_PER_SIDE
    )

    required_move = (
        notional * round_trip_rate + MIN_NET_TARGET_USD
    ) / max(abs(size) * multiplier, 1e-12)

    return max(
        atr * TAKE_PROFIT_ATR,
        required_move
    )


def fee_filter_passes(symbol, price, size, atr):

    estimated_cost, fees, slippage, notional = (
        estimated_round_trip_cost(symbol, price, size)
    )

    expected_gross = (
        atr
        * TAKE_PROFIT_ATR
        * abs(size)
        * CONTRACT_MULTIPLIER[symbol]
    )

    required_gross = (
        estimated_cost
        / max(1 - MAX_ESTIMATED_COST_TO_ATR_PROFIT, 1e-9)
    )

    log(
        f"[FEE CHECK] {symbol} "
        f"notional=${notional:.4f} "
        f"fees=${fees:.4f} "
        f"slippage=${slippage:.4f} "
        f"estimated_cost=${estimated_cost:.4f} "
        f"expected_gross=${expected_gross:.4f}"
    )

    return expected_gross > required_gross


# ==================================================
# POSITIONS
# ==================================================

def get_positions():

    result, code = api_req(
        "GET",
        "/v2/positions/margined"
    )

    if code != 200:
        log(f"[POSITIONS ERROR] HTTP={code} {result}")
        return None

    if not isinstance(result, dict):
        return None

    if not result.get("success"):
        log(f"[POSITIONS ERROR] {result}")
        return None

    data = result.get("result", [])

    return data if isinstance(data, list) else []


def get_open_positions():

    positions = get_positions()

    if positions is None:
        return None

    active = []

    for position in positions:

        symbol = position.get("product_symbol")

        if symbol not in COINS:
            continue

        try:
            size = float(position.get("size", 0))
        except (TypeError, ValueError):
            size = 0

        if size != 0:
            active.append(position)

    return active


def position_details(position):

    symbol = position.get("product_symbol")
    size = float(position.get("size", 0))

    entry = float(
        position.get(
            "entry_price",
            position.get("average_entry_price", 0)
        ) or 0
    )

    side = "buy" if size > 0 else "sell"

    return symbol, side, abs(size), entry


# ==================================================
# OPEN ORDERS
# ==================================================

def get_open_orders(symbol=None):

    params = {"states": "open,pending"}

    if symbol:
        params["product_symbol"] = symbol

    result, code = api_req(
        "GET",
        "/v2/orders",
        params=params
    )

    if code != 200:
        log(f"[ORDERS ERROR] HTTP={code} {result}")
        return None

    if not isinstance(result, dict):
        return None

    if not result.get("success"):
        log(f"[ORDERS ERROR] {result}")
        return None

    orders = result.get("result", [])

    return orders if isinstance(orders, list) else []


# ==================================================
# ORDER HELPERS
# ==================================================

def place_market_order(symbol, side, size, reduce_only=False):

    body = {
        "product_symbol": symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "reduce_only": bool(reduce_only)
    }

    result, code = api_req(
        "POST",
        "/v2/orders",
        body=body
    )

    if code not in (200, 201):
        log(
            f"[MARKET ORDER FAILED] {symbol} "
            f"side={side} HTTP={code} {result}"
        )
        return None

    if not isinstance(result, dict) or not result.get("success"):
        log(f"[MARKET ORDER REJECTED] {symbol} {result}")
        return None

    order = result.get("result", {})

    log(
        f"[MARKET ORDER SENT] {symbol} "
        f"side={side} size={size} "
        f"reduce_only={reduce_only}"
    )

    return order


def place_stop_order(symbol, side, size, stop_price):

    body = {
        "product_symbol": symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(round(stop_price, 8)),
        "stop_trigger_method": STOP_TRIGGER_METHOD,
        "reduce_only": True
    }

    result, code = api_req(
        "POST",
        "/v2/orders",
        body=body
    )

    if code not in (200, 201):
        log(
            f"[STOP ORDER FAILED] {symbol} "
            f"HTTP={code} {result}"
        )
        return None

    if not isinstance(result, dict) or not result.get("success"):
        log(f"[STOP ORDER REJECTED] {symbol} {result}")
        return None

    order = result.get("result", {})

    log(
        f"[STOP PLACED] {symbol} "
        f"side={side} stop={stop_price}"
    )

    return order


def cancel_order(order_id):

    if not order_id:
        return False

    result, code = api_req(
        "DELETE",
        f"/v2/orders/{order_id}"
    )

    if code not in (200, 201):
        log(f"[CANCEL FAILED] {order_id} {result}")
        return False

    if isinstance(result, dict) and result.get("success"):
        log(f"[ORDER CANCELLED] {order_id}")
        return True

    return False


# ==================================================
# POSITION STOP / TARGET HELPERS
# ==================================================

def initial_stop_price(side, entry, atr):

    gap = max(
        atr * INITIAL_STOP_ATR,
        entry * MIN_STOP_GAP_PCT
    )

    if side == "buy":
        return entry - gap

    return entry + gap


def initial_target_price(symbol, side, entry, size, atr):

    gap = fee_aware_target_gap(
        symbol,
        entry,
        size,
        atr
    )

    if side == "buy":
        return entry + gap

    return entry - gap


def emergency_close(symbol, side, size):

    close_side = "sell" if side == "buy" else "buy"

    log(
        f"[EMERGENCY CLOSE ATTEMPT] "
        f"{symbol} side={close_side} size={size}"
    )

    return place_market_order(
        symbol,
        close_side,
        size,
        reduce_only=True
    )


# ==================================================
# INITIALIZE POSITION AFTER ENTRY
# ==================================================

def initialize_position(symbol, side, size, entry, atr):

    stop_price = initial_stop_price(
        side,
        entry,
        atr
    )

    target_price = initial_target_price(
        symbol,
        side,
        entry,
        size,
        atr
    )

    state["peaks"][symbol] = entry
    state["stops"][symbol] = stop_price
    state["targets"][symbol] = target_price
    state["initial_gaps"][symbol] = abs(
        entry - stop_price
    )
    state["profit_locked"][symbol] = False

    save_state()

    stop_side = "sell" if side == "buy" else "buy"

    stop_order = place_stop_order(
        symbol,
        stop_side,
        size,
        stop_price
    )

    if not stop_order:

        log(
            f"[CRITICAL] {symbol} initial stop failed. "
            "Attempting emergency reduce-only close."
        )

        emergency_close(symbol, side, size)

        return False

    stop_id = stop_order.get("id")

    if stop_id:
        state["stop_order_ids"][symbol] = stop_id

    save_state()

    log(
        f"[POSITION INITIALIZED] {symbol} "
        f"side={side} entry={entry} "
        f"SL={stop_price} TP={target_price}"
    )

    return True
  # ==================================================
# POSITION MANAGEMENT
# ==================================================

def clear_position_state(symbol):

    for key in [
        "peaks",
        "stops",
        "targets",
        "initial_gaps",
        "profit_locked",
        "stop_order_ids"
    ]:
        state.get(key, {}).pop(symbol, None)

    save_state()


def manage_positions():

    positions = get_open_positions()

    # If exchange position query fails, do not change orders.
    if positions is None:
        return

    active_symbols = set()

    for position in positions:

        try:
            symbol, side, size, entry = position_details(position)

            if symbol not in COINS or size <= 0 or entry <= 0:
                continue

            active_symbols.add(symbol)

            live_price = get_live_price(symbol)

            if live_price is None:
                log(f"[MANAGE SKIP] {symbol}: live price unavailable")
                continue

            candles = get_candles(symbol)
            df = candles_df(candles)

            if df is None:
                log(f"[MANAGE SKIP] {symbol}: candle data unavailable")
                continue

            df = calculate_indicators(df)
            atr = float(df.iloc[-1]["atr"])

            if pd.isna(atr) or atr <= 0:
                continue

            target = state["targets"].get(symbol)

            if not target:
                target = initial_target_price(
                    symbol, side, entry, size, atr
                )
                state["targets"][symbol] = target

            old_stop = float(
                state["stops"].get(
                    symbol,
                    initial_stop_price(side, entry, atr)
                )
            )

            peak = float(
                state["peaks"].get(symbol, entry)
            )

            if side == "buy":

                peak = max(peak, live_price)

                profit_distance = live_price - entry

                target_hit = live_price >= float(target)

            else:

                peak = min(peak, live_price)

                profit_distance = entry - live_price

                target_hit = live_price <= float(target)

            state["peaks"][symbol] = peak

            # Close when the fee-aware estimated target is reached.
            if target_hit:

                log(
                    f"[TARGET HIT] {symbol} "
                    f"price={live_price} target={target}"
                )

                emergency_close(symbol, side, size)

                time.sleep(2)

                refreshed = get_open_positions()

                if refreshed is not None:
                    still_open = any(
                        p.get("product_symbol") == symbol
                        and float(p.get("size", 0) or 0) != 0
                        for p in refreshed
                    )

                    if not still_open:
                        clear_position_state(symbol)

                continue

            # Profit lock and trailing stop.
            candidate = old_stop

            if profit_distance >= atr * PROFIT_LOCK_TRIGGER_ATR:

                state["profit_locked"][symbol] = True

                if side == "buy":

                    lock_stop = entry + (
                        atr * 0.05
                    )

                    trail_stop = peak - (
                        atr * TRAIL_GAP_ATR
                    )

                    candidate = max(
                        old_stop,
                        lock_stop,
                        trail_stop
                    )

                else:

                    lock_stop = entry - (
                        atr * 0.05
                    )

                    trail_stop = peak + (
                        atr * TRAIL_GAP_ATR
                    )

                    candidate = min(
                        old_stop,
                        lock_stop,
                        trail_stop
                    )

            # Do not place a stop too close to current price.
            min_gap = max(
                live_price * MIN_STOP_GAP_PCT,
                0.00000001
            )

            if side == "buy":
                candidate = min(
                    candidate,
                    live_price - min_gap
                )
                improves = candidate > old_stop

            else:
                candidate = max(
                    candidate,
                    live_price + min_gap
                )
                improves = candidate < old_stop

            if improves:

                stop_side = (
                    "sell" if side == "buy" else "buy"
                )

                # Place new stop first. Cancel old stop only
                # after the exchange accepts the new stop.
                new_order = place_stop_order(
                    symbol,
                    stop_side,
                    size,
                    candidate
                )

                if new_order:

                    new_id = new_order.get("id")

                    old_id = state["stop_order_ids"].get(symbol)

                    state["stops"][symbol] = candidate

                    if new_id:
                        state["stop_order_ids"][symbol] = new_id

                    save_state()

                    if old_id and old_id != new_id:
                        cancel_order(old_id)

                    log(
                        f"[TRAIL UPDATED] {symbol} "
                        f"new_stop={candidate}"
                    )

            save_state()

            log(
                f"[POSITION] {symbol} "
                f"side={side} entry={entry} "
                f"live={live_price} "
                f"stop={state['stops'].get(symbol)} "
                f"target={target}"
            )

        except Exception as e:
            log(f"[POSITION MANAGER ERROR] {e}")

    # Clear saved state only after exchange confirms the position is flat.
    for symbol in COINS:

        if symbol not in active_symbols:

            if symbol in state["stops"]:

                log(
                    f"[POSITION CLOSED/FLAT] {symbol} "
                    "clearing saved position state"
                )

                clear_position_state(symbol)


# ==================================================
# ENTRY MANAGEMENT
# ==================================================

def can_open_new_trade(symbol, active_positions):

    if not ENABLE_TRADING:
        return False

    if symbol in active_positions:
        return False

    if len(active_positions) >= MAX_POSITIONS:
        return False

    last_trade = float(
        state["last_trade"].get(symbol, 0)
    )

    if time.time() - last_trade < COOLDOWN_SECONDS:
        return False

    return True


def process_symbol(symbol, active_positions):

    try:

        if not can_open_new_trade(symbol, active_positions):
            return

        candles = get_candles(symbol)
        df = candles_df(candles)

        if df is None:
            log(f"[NO DATA] {symbol}: candle history unavailable")
            return

        latest_candle = int(df.iloc[-1]["time"])

        previous_candle = state["last_candle"].get(symbol)

        if previous_candle == latest_candle:
            return

        # Record candle so the same candle is not repeatedly traded.
        state["last_candle"][symbol] = latest_candle
        save_state()

        signal, reason = get_signal(df)

        if signal is None:
            log(f"[NO SIGNAL] {symbol}: {reason}")
            return

        live_price = get_live_price(symbol)

        if live_price is None:
            log(f"[ENTRY BLOCKED] {symbol}: no fresh live price")
            return

        candle_close = float(df.iloc[-1]["close"])

        price_gap = abs(
            live_price - candle_close
        ) / candle_close

        if price_gap > MAX_CANDLE_LIVE_GAP_PCT:
            log(
                f"[ENTRY BLOCKED] {symbol}: "
                f"live/candle price gap {price_gap:.4%}"
            )
            return

        df = calculate_indicators(df)
        atr = float(df.iloc[-1]["atr"])

        if pd.isna(atr) or atr <= 0:
            return

        if not fee_filter_passes(
            symbol,
            live_price,
            ORDER_SIZES[symbol],
            atr
        ):
            log(
                f"[FEE FILTER BLOCK] {symbol}: "
                "estimated costs too high for ATR target"
            )
            return

        side = signal
        size = ORDER_SIZES[symbol]

        log(
            f"[ENTRY SIGNAL] {symbol} "
            f"side={side} price={live_price} "
            f"ATR={atr} reason={reason}"
        )

        if not ENABLE_TRADING:
            log("[SAFE MODE] Trading disabled; no order sent")
            return

        order = place_market_order(
            symbol,
            side,
            size,
            reduce_only=False
        )

        if not order:
            return

        # Wait briefly for the exchange to register the position.
        time.sleep(2)

        positions = get_open_positions()

        if positions is None:
            log(
                f"[CRITICAL] {symbol}: cannot verify position "
                "after entry. Check Testnet manually."
            )
            return

        actual_position = next(
            (
                p for p in positions
                if p.get("product_symbol") == symbol
                and float(p.get("size", 0) or 0) != 0
            ),
            None
        )

        if actual_position is None:
            log(
                f"[ENTRY NOT CONFIRMED] {symbol}. "
                "Check exchange orders and positions."
            )
            return

        actual_symbol, actual_side, actual_size, actual_entry = (
            position_details(actual_position)
        )

        if actual_side != side:
            log(
                f"[CRITICAL] {symbol}: exchange position side "
                f"{actual_side} differs from signal {side}. "
                "Check position manually."
            )
            return

        state["last_trade"][symbol] = time.time()
        save_state()

        initialize_position(
            actual_symbol,
            actual_side,
            actual_size,
            actual_entry,
            atr
        )

    except Exception as e:
        log(f"[SYMBOL ERROR] {symbol}: {e}")


# ==================================================
# MAIN BOT LOOP
# ==================================================

def main_loop():

    log("==============================================")
    log("DELTA EXCHANGE INDIA TESTNET BOT STARTED")
    log("==============================================")

    log(f"AUTO TRADING: {ENABLE_TRADING}")
    log(f"LEVERAGE SETTING: {LEVERAGE}x (not set by script)")
    log(f"COINS: {COINS}")
    log(f"MAX POSITIONS: {MAX_POSITIONS}")
    log("TIMEFRAME: 5 MINUTES")
    log("ENTRY: EMA + RSI + ADX + MACD + VOLUME")
    log("EXIT: ATR STOP + FEE-AWARE TARGET + TRAILING")
    log(f"ESTIMATED NET TARGET: ${MIN_NET_TARGET_USD:.2f}")
    log("==============================================")

    while True:

        try:

            if not API_KEY or not API_SECRET:
                log("[WAITING] API credentials missing")
                time.sleep(30)
                continue

            # Manage existing positions before looking for new entries.
            manage_positions()

            active_positions = get_open_positions()

            if active_positions is None:
                log("[WAITING] Position data unavailable; entries paused")
                time.sleep(CHECK_INTERVAL)
                continue

            active_symbols = set()

            for position in active_positions:
                symbol = position.get("product_symbol")

                if symbol in COINS:
                    active_symbols.add(symbol)

            log(
                f"[SCAN] Active positions: "
                f"{len(active_symbols)}/{MAX_POSITIONS}"
            )

            for symbol in COINS:

                # Refresh positions between symbols so the bot
                # does not exceed the configured position limit.
                latest_positions = get_open_positions()

                if latest_positions is None:
                    log("[SCAN PAUSED] Cannot refresh positions")
                    break

                latest_symbols = {
                    p.get("product_symbol")
                    for p in latest_positions
                    if p.get("product_symbol") in COINS
                    and float(p.get("size", 0) or 0) != 0
                }

                process_symbol(
                    symbol,
                    latest_symbols
                )

            time.sleep(CHECK_INTERVAL)

        except Exception as e:

            log(f"[MAIN LOOP ERROR] {e}")

            time.sleep(CHECK_INTERVAL)


# ==================================================
# STARTUP
# ==================================================

if __name__ == "__main__":

    log("=== BOT STARTING ===")

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    log("=== FLASK STARTED ===")

    main_loop()
