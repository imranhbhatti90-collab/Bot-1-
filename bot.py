import os
import json
import time
import hmac
import hashlib
import threading
import logging
import math
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import pandas as pd
from flask import Flask, jsonify, request, Response

# ============================================================
# DELTA EXCHANGE INDIA - TESTNET ONLY
# ============================================================
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

APP_PORT = int(os.getenv("PORT", "10000"))
VIEW_USER = os.getenv("STATE_VIEW_USER", "admin")
VIEW_PASSWORD = os.getenv("STATE_VIEW_PASSWORD", "")

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "true").lower() == "true"

# Capital and risk
MARGIN_PER_TRADE_USD = 20.0
MAX_POSITIONS = 10
BALANCE_BUFFER_USD = 5.0
MAX_ACCOUNT_MARGIN_USE_USD = 200.0

# Strategy
TREND_TIMEFRAME = "15m"
ENTRY_TIMEFRAME = "5m"
EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200
EMA_ENTRY_FAST = 10
EMA_ENTRY_SLOW = 20
RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_PERIOD = 20
VOLUME_MULTIPLIER = 1.10

# Risk management
ATR_STOP_MULTIPLIER = 1.0
ATR_TRAIL_MULTIPLIER = 1.0
ATR_PARTIAL_MULTIPLIER = 1.0
PARTIAL_CLOSE_PERCENT = 0.50

# Existing partial target points
FIXED_PARTIAL_TARGETS = {
    "BTCUSD": 200.0,
    "ETHUSD": 15.0,
    "PAXGUSD": 10.0,
}

CHECK_INTERVAL = 30
COOLDOWN_SECONDS = 300
DISCOVERY_INTERVAL = 900
CANDLE_LOOKBACK_DAYS = 10
MIN_CLOSED_CANDLES = 250
MAX_CANDLE_LIVE_GAP_PCT = 0.005
MAX_MARKET_SCAN_PER_CYCLE = 100
REQUEST_TIMEOUT = 15

STATE_FILE = "state.json"
TRADE_LOG_FILE = "trade_log.jsonl"

# ============================================================
# LOGGING / APP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("DELTA_TESTNET_BOT")

app = Flask(__name__)
session = requests.Session()
session.headers.update({
    "User-Agent": "Delta-India-Testnet-MultiAgent-Bot/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

lock = threading.RLock()

state = {
    "status": "STARTING",
    "mode": "DELTA INDIA TESTNET ONLY",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_scan": None,
    "last_error": None,
    "discovered_markets": [],
    "eligible_markets": [],
    "positions": [],
    "active_orders": [],
    "last_signals": {},
    "agent_status": {},
    "trade_count": 0,
    "wins": 0,
    "losses": 0,
    "realized_pnl_estimate": 0.0,
    "events": [],
}

PRODUCTS = {}
LAST_TRADE_TIME = {}
POSITION_META = {}
DISCOVERED_AT = 0


# ============================================================
# GENERAL HELPERS
# ============================================================
def save_state():
    try:
        with lock:
            data = json.dumps(state, indent=2, default=str)
        with open(STATE_FILE, "w") as f:
            f.write(data)
    except Exception as e:
        log.error("STATE SAVE ERROR: %s", e)


def event(message):
    log.info(message)
    with lock:
        state["events"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "message": str(message)
        })
        state["events"] = state["events"][-100:]


def agent(name, status, detail=""):
    with lock:
        state["agent_status"][name] = {
            "status": status,
            "detail": str(detail),
            "time": datetime.now(timezone.utc).isoformat()
        }


def write_trade_log(data):
    try:
        with open(TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(data, default=str) + "\n")
    except Exception as e:
        log.error("TRADE LOG ERROR: %s", e)


def number(value, default=0.0):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except Exception:
        pass
    return default


def api_response(response):
    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"API returned non-JSON response: {response.status_code}"
        )

    if response.status_code >= 400 or data.get("success") is False:
        raise RuntimeError(
            f"Delta API error {response.status_code}: {data}"
        )

    return data


# ============================================================
# AUTHENTICATION
# ============================================================
def api_request(method, path, params=None, body=None, auth=True):
    method = method.upper()
    params = params or {}

    query = urlencode(sorted(params.items()))
    request_path = path + (("?" + query) if query else "")

    body_text = ""
    if body is not None:
        body_text = json.dumps(body, separators=(",", ":"))

    headers = {}

    if auth:
        if not API_KEY or not API_SECRET:
            raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET missing")

        timestamp = str(int(time.time()))
        signature_data = (
            method + timestamp + request_path + body_text
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
        }

    url = BASE_URL + path

    response = session.request(
        method=method,
        url=url,
        params=params if params else None,
        data=body_text if body is not None else None,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    return api_response(response)


# ============================================================
# PUBLIC MARKET DATA
# ============================================================

def public_get(path, params=None):
    return api_request(
        "GET", path, params=params, auth=False
    )


def discover_futures(force=False):
    global PRODUCTS, DISCOVERED_AT

    if not force and time.time() - DISCOVERED_AT < DISCOVERY_INTERVAL:
        return list(PRODUCTS.values())

    agent("Scanner", "RUNNING", "Refreshing Delta product list")

    found = []
    after = None

    for _ in range(20):
        params = {
            "contract_types": "perpetual_futures",
            "states": "live",
            "page_size": 100,
        }

        if after:
            params["after"] = after

        data = public_get("/v2/products", params)
        rows = data.get("result", [])

        if isinstance(rows, dict):
            rows = [rows]

        for p in rows[:5]:
            log.info(
                "[PRODUCT DIAGNOSTIC] %s",
                json.dumps({
                    "symbol": p.get("symbol"),
                    "contract_type": p.get("contract_type"),
                    "state": p.get("state"),
                    "trading_status": p.get("trading_status"),
                    "quoting_asset": p.get("quoting_asset"),
                    "settling_asset": p.get("settling_asset"),
                    "contract_value": p.get("contract_value"),
                    "contract_unit_currency": p.get("contract_unit_currency"),
                    "tick_size": p.get("tick_size"),
                })
            )

        found.extend(rows)
        after = (data.get("meta") or {}).get("after")

        if not after or not rows:
            break

    PRODUCTS = {}

    for p in found:
        symbol = str(p.get("symbol", "")).upper()

        if not symbol:
            continue

        if is_eligible_product(p):
            PRODUCTS[symbol] = p

    DISCOVERED_AT = time.time()

    with lock:
        state["discovered_markets"] = [
            p.get("symbol") for p in found if p.get("symbol")
        ]
        state["eligible_markets"] = list(PRODUCTS.keys())

    event(
        f"DISCOVERY COMPLETE total={len(found)} "
        f"eligible={len(PRODUCTS)}"
    )

    agent("Scanner", "OK", f"{len(PRODUCTS)} eligible perpetual markets")
    save_state()

    return list(PRODUCTS.values())


def is_eligible_product(p):
    """
    Conservative eligibility:
    - Live perpetual futures only
    - Operational trading status
    - USD quoting and settlement
    - Valid product ID, tick size and contract value
    """
    if str(p.get("contract_type", "")).lower() != "perpetual_futures":
        return False

    if str(p.get("state", "")).lower() != "live":
        return False

    trading_status = str(
        p.get("trading_status", "operational")
    ).lower()

    if trading_status not in ("operational", "live", "trading"):
        return False

    quoting = str(p.get("quoting_asset", "")).upper()
    settling = str(p.get("settling_asset", "")).upper()

    if quoting != "USD" or settling != "USD":
        return False

    if not p.get("id"):
        return False

    if number(p.get("tick_size")) <= 0:
        return False

    if number(p.get("contract_value")) <= 0:
        return False

    return True


def get_ticker(symbol):
    data = public_get("/v2/tickers", {
        "contract_types": "perpetual_futures"
    })

    rows = data.get("result", [])
    for row in rows:
        if row.get("symbol") == symbol:
            return row

    return {}


def get_live_price(symbol):
    ticker = get_ticker(symbol)

    price = number(ticker.get("close"))
    if price <= 0:
        price = number(ticker.get("mark_price"))
    if price <= 0:
        price = number(ticker.get("spot_price"))

    return price


def get_candles(symbol, resolution):
    end = int(time.time())
    start = end - CANDLE_LOOKBACK_DAYS * 86400

    data = public_get("/v2/history/candles", {
        "resolution": resolution,
        "symbol": symbol,
        "start": str(start),
        "end": str(end),
    })

    rows = data.get("result", [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    required = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in required):
        return pd.DataFrame()

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 0
    else:
        df["volume"] = pd.to_numeric(
            df["volume"], errors="coerce"
        ).fillna(0)

    if "time" in df.columns:
        df = df.sort_values("time")

    df = df.dropna(subset=required).reset_index(drop=True)

    # Exclude the currently forming candle.
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    return df.reset_index(drop=True)


# ============================================================
# INDICATORS / STRATEGY
# ============================================================
def add_indicators(df):
    df = df.copy()

    df["ema10"] = df["close"].ewm(
        span=10, adjust=False
    ).mean()
    df["ema20"] = df["close"].ewm(
        span=20, adjust=False
    ).mean()
    df["ema50"] = df["close"].ewm(
        span=EMA_TREND_FAST, adjust=False
    ).mean()
    df["ema200"] = df["close"].ewm(
        span=EMA_TREND_SLOW, adjust=False
    ).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD, adjust=False
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-12)
    df["rsi"] = 100 - (100 / (1 + rs))

    prev_close = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / ATR_PERIOD, adjust=False
    ).mean()

    df["volume_avg"] = df["volume"].rolling(
        VOLUME_PERIOD
    ).mean()

    return df


def check_signal(symbol):
    try:
        trend_df = get_candles(symbol, TREND_TIMEFRAME)
        entry_df = get_candles(symbol, ENTRY_TIMEFRAME)

        if len(trend_df) < MIN_CLOSED_CANDLES:
            return None, "NOT_ENOUGH_15M_CANDLES"

        if len(entry_df) < 50:
            return None, "NOT_ENOUGH_5M_CANDLES"

        trend_df = add_indicators(trend_df)
        entry_df = add_indicators(entry_df)

        t = trend_df.iloc[-1]
        e = entry_df.iloc[-1]
        prev = entry_df.iloc[-2]

        if pd.isna(t["ema200"]) or pd.isna(e["atr"]):
            return None, "INDICATORS_NOT_READY"

        trend = "MIXED"
        if t["ema50"] > t["ema200"] and t["close"] > t["ema50"]:
            trend = "BULLISH"
        elif t["ema50"] < t["ema200"] and t["close"] < t["ema50"]:
            trend = "BEARISH"

        crossed_up = (
            prev["ema10"] <= prev["ema20"]
            and e["ema10"] > e["ema20"]
        )
        crossed_down = (
            prev["ema10"] >= prev["ema20"]
            and e["ema10"] < e["ema20"]
        )

        volume_ok = (
            e["volume_avg"] > 0
            and e["volume"] > e["volume_avg"] * VOLUME_MULTIPLIER
        )

        signal = None

        if (
            trend == "BULLISH"
            and crossed_up
            and 50 <= e["rsi"] <= 68
            and volume_ok
        ):
            signal = "buy"

        elif (
            trend == "BEARISH"
            and crossed_down
            and 32 <= e["rsi"] <= 50
            and volume_ok
        ):
            signal = "sell"

        live_price = get_live_price(symbol)
        candle_close = number(e["close"])

        if live_price <= 0 or candle_close <= 0:
            return None, "NO_LIVE_PRICE"

        gap = abs(live_price - candle_close) / candle_close
        if gap > MAX_CANDLE_LIVE_GAP_PCT:
            return None, "LIVE_PRICE_TOO_FAR_FROM_CANDLE"

        atr = number(t["atr"])
        if atr <= 0:
            return None, "INVALID_ATR"

        result = {
            "symbol": symbol,
            "signal": signal,
            "trend": trend,
            "price": live_price,
            "atr": atr,
            "rsi": number(e["rsi"]),
            "volume": number(e["volume"]),
            "volume_avg": number(e["volume_avg"]),
            "time": datetime.now(timezone.utc).isoformat(),
        }

        with lock:
            state["last_signals"][symbol] = result

        return result, "OK" if signal else "NO_ALIGNED_SIGNAL"

    except Exception as e:
        log.exception("SIGNAL ERROR %s", symbol)
        return None, str(e)


# ============================================================
# ACCOUNT / POSITIONS / ORDERS
# ============================================================
def get_positions():
    all_positions = []

    for symbol, product in PRODUCTS.items():
        try:
            data = api_request(
                "GET",
                "/v2/positions",
                params={"product_id": int(product["id"])}
            )

            result = data.get("result", {})
            if isinstance(result, list):
                rows = result
            else:
                rows = [result] if result else []

            for p in rows:
                size = number(p.get("size"))
                if size != 0:
                    p["symbol"] = symbol
                    p["product_id"] = product["id"]
                    all_positions.append(p)

        except Exception as e:
            log.warning("POSITION FETCH %s: %s", symbol, e)

    return all_positions


def get_active_orders():
    try:
        data = api_request("GET", "/v2/orders")
        rows = data.get("result", [])
        return rows if isinstance(rows, list) else []
    except Exception as e:
        log.error("ACTIVE ORDERS ERROR: %s", e)
        return []


def get_wallet_balances():
    data = api_request("GET", "/v2/wallet/balances")
    rows = data.get("result", [])
    return rows if isinstance(rows, list) else []


def available_usd_balance():
    rows = get_wallet_balances()

    for row in rows:
        asset = str(
            row.get("asset_symbol", row.get("asset", ""))
        ).upper()

        if asset in ("USD", "USDT", "USDC"):
            available = row.get("available_balance")
            if available is None:
                available = row.get("balance")
            return number(available)

    return 0.0


def get_product_leverage(product_id):
    data = api_request(
        "GET",
        f"/v2/products/{int(product_id)}/orders/leverage"
    )

    result = data.get("result", {})
    leverage = number(result.get("leverage"))

    if leverage <= 0:
        raise RuntimeError("Actual leverage unavailable")

    return leverage


# ============================================================
# CONTRACT SIZING
# ============================================================
def contract_notional_usd_per_contract(product, price):
    """
    Only accept contract metadata that can be safely converted
    into USD notional. Unknown units are rejected.
    """
    value = number(product.get("contract_value"))
    unit = str(
        product.get("contract_unit_currency", "")
    ).upper()

    if value <= 0 or price <= 0:
        return 0.0

    if unit == "USD":
        return value

    underlying = str(
        product.get("underlying_asset", "")
    ).upper()

    if unit and unit == underlying:
        return value * price

    return 0.0


def calculate_order_size(product, price, leverage):
    per_contract = contract_notional_usd_per_contract(
        product, price
    )

    if per_contract <= 0:
        raise RuntimeError(
            "Unsupported contract unit; refusing guessed quantity"
        )

    target_notional = MARGIN_PER_TRADE_USD * leverage
    raw_size = target_notional / per_contract

    # Integer contracts only.
    size = int(math.floor(raw_size))

    if size < 1:
        raise RuntimeError(
            "20 USD margin cannot buy minimum one contract"
        )

    actual_notional = size * per_contract
    actual_margin = actual_notional / leverage

    if actual_margin > MARGIN_PER_TRADE_USD * 1.001:
        raise RuntimeError("Calculated margin exceeds budget")

    return size, actual_margin, per_contract


# ============================================================
# PRICE ROUNDING
# ============================================================
def round_to_tick(price, tick_size):
    tick = number(tick_size)
    if tick <= 0:
        return price

    return round(round(price / tick) * tick, 10)


# ============================================================
# ORDER PLACEMENT
# ============================================================
def place_market_order(product_id, side, size, reduce_only=False):
    body = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "reduce_only": bool(reduce_only),
    }

    data = api_request("POST", "/v2/orders", body=body)
    return data.get("result", data)


def close_position_market(symbol, position_size):
    product = PRODUCTS.get(symbol)
    if not product:
        return False

    size = abs(int(position_size))
    if size <= 0:
        return False

    side = "sell" if position_size > 0 else "buy"

    result = place_market_order(
        product["id"], side, size, reduce_only=True
    )

    event(f"CLOSE ORDER {symbol} side={side} size={size}")
    write_trade_log({
        "event": "CLOSE",
        "symbol": symbol,
        "side": side,
        "size": size,
        "result": result,
        "time": datetime.now(timezone.utc).isoformat()
    })

    return True


def partial_close_position(symbol, position_size):
    total = abs(int(position_size))

    # Integer contracts: do not partially close a one-contract position.
    if total < 2:
        event(f"PARTIAL SKIPPED {symbol}: position below 2 contracts")
        return False

    close_size = max(1, int(math.floor(total * PARTIAL_CLOSE_PERCENT)))
    close_size = min(close_size, total - 1)

    side = "sell" if position_size > 0 else "buy"

    product = PRODUCTS[symbol]
    result = place_market_order(
        product["id"], side, close_size, reduce_only=True
    )

    event(
        f"PARTIAL CLOSE {symbol} size={close_size} "
        f"remaining_est={total-close_size}"
    )

    write_trade_log({
        "event": "PARTIAL_CLOSE",
        "symbol": symbol,
        "size": close_size,
        "result": result,
        "time": datetime.now(timezone.utc).isoformat()
    })

    return True


# ============================================================
# POSITION MANAGEMENT
# ============================================================
def initialize_position_metadata(position):
    symbol = position["symbol"]
    size = number(position.get("size"))
    entry = number(position.get("entry_price"))

    if not entry or not size:
        return

    if symbol in POSITION_META:
        return

    product = PRODUCTS.get(symbol)
    if not product:
        return

    try:
        candles = get_candles(symbol, TREND_TIMEFRAME)
        if len(candles) < 30:
            return

        candles = add_indicators(candles)
        atr = number(candles.iloc[-1]["atr"])

        if atr <= 0:
            return

        direction = 1 if size > 0 else -1
        stop_distance = atr * ATR_STOP_MULTIPLIER

        POSITION_META[symbol] = {
            "entry": entry,
            "direction": direction,
            "initial_size": abs(int(size)),
            "stop": entry - direction * stop_distance,
            "best_price": entry,
            "atr": atr,
            "partial_done": False,
            "created_at": time.time(),
        }

        event(
            f"POSITION INIT {symbol} entry={entry} "
            f"ATR={atr} stop={POSITION_META[symbol]['stop']}"
        )

    except Exception as e:
        log.error("POSITION INIT ERROR %s: %s", symbol, e)


def partial_target_distance(symbol, atr):
    if symbol in FIXED_PARTIAL_TARGETS:
        return FIXED_PARTIAL_TARGETS[symbol]
    return atr * ATR_PARTIAL_MULTIPLIER


def handle_trailing(positions):
    agent("Risk Check", "RUNNING", "Checking open positions")

    for p in positions:
        symbol = p.get("symbol")
        size = number(p.get("size"))
        entry = number(p.get("entry_price"))

        if not symbol or size == 0 or entry <= 0:
            continue

        if symbol not in PRODUCTS:
            continue

        initialize_position_metadata(p)

        meta = POSITION_META.get(symbol)
        if not meta:
            continue

        try:
            price = get_live_price(symbol)
            if price <= 0:
                continue

            product = PRODUCTS[symbol]
            tick = number(product.get("tick_size"))

            direction = 1 if size > 0 else -1
            atr = number(meta["atr"])

            # Refresh ATR from latest closed 15m candle.
            candles = get_candles(symbol, TREND_TIMEFRAME)
            if len(candles) >= 30:
                latest_atr = number(
                    add_indicators(candles).iloc[-1]["atr"]
                )
                if latest_atr > 0:
                    atr = latest_atr
                    meta["atr"] = atr

            if direction == 1:
                meta["best_price"] = max(
                    meta["best_price"], price
                )
                profit_move = price - entry
                trail_stop = (
                    meta["best_price"] -
                    atr * ATR_TRAIL_MULTIPLIER
                )
                meta["stop"] = max(meta["stop"], trail_stop)

                stop_hit = price <= meta["stop"]

            else:
                meta["best_price"] = min(
                    meta["best_price"], price
                )
                profit_move = entry - price
                trail_stop = (
                    meta["best_price"] +
                    atr * ATR_TRAIL_MULTIPLIER
                )
                meta["stop"] = min(meta["stop"], trail_stop)

                stop_hit = price >= meta["stop"]

            partial_distance = partial_target_distance(
                symbol, atr
            )

            if (
                not meta["partial_done"]
                and profit_move >= partial_distance
            ):
                completed = partial_close_position(symbol, size)

                if completed:
                    meta["partial_done"] = True
                    # Move remaining position's stop to entry.
                    if direction == 1:
                        meta["stop"] = max(meta["stop"], entry)
                    else:
                        meta["stop"] = min(meta["stop"], entry)

                    event(
                        f"PARTIAL TARGET HIT {symbol} "
                        f"move={profit_move:.6f} "
                        f"target={partial_distance:.6f}"
                    )

            meta["stop"] = round_to_tick(
                meta["stop"], tick
            )

            if stop_hit:
                event(
                    f"BOT STOP HIT {symbol} price={price} "
                    f"stop={meta['stop']}"
                )
                close_position_market(symbol, size)
                POSITION_META.pop(symbol, None)
                continue

            log.info(
                "[TRAIL] %s side=%s price=%.8f entry=%.8f "
                "move=%.8f partial=%.8f stop=%.8f",
                symbol,
                "LONG" if direction == 1 else "SHORT",
                price,
                entry,
                profit_move,
                partial_distance,
                meta["stop"]
            )

        except Exception as e:
            log.exception("TRAIL ERROR %s: %s", symbol, e)

    agent("Risk Check", "OK", "Position management cycle complete")


# ============================================================
# ENTRY FILTERS / POSITION LIMIT
# ============================================================
def has_symbol_position(symbol, positions):
    return any(
        p.get("symbol") == symbol and number(p.get("size")) != 0
        for p in positions
    )


def has_symbol_active_order(symbol, orders):
    product = PRODUCTS.get(symbol)
    if not product:
        return True

    product_id = str(product.get("id"))

    for order in orders:
        if str(order.get("product_id")) == product_id:
            status = str(order.get("state", "")).lower()
            if status not in ("closed", "cancelled", "rejected"):
                return True

    return False


def cooldown_active(symbol):
    last = LAST_TRADE_TIME.get(symbol, 0)
    return time.time() - last < COOLDOWN_SECONDS


def account_margin_guard():
    balance = available_usd_balance()

    if balance <= 0:
        raise RuntimeError("Could not verify available USD balance")

    if balance < MARGIN_PER_TRADE_USD + BALANCE_BUFFER_USD:
        raise RuntimeError(
            f"Insufficient available balance: {balance:.2f} USD"
        )

    return balance


def enter_trade(signal):
    symbol = signal["symbol"]
    product = PRODUCTS[symbol]
    side = signal["signal"]
    price = number(signal["price"])

    if side not in ("buy", "sell"):
        return

    agent("Position Sizer", "RUNNING", symbol)

    balance = account_margin_guard()
    leverage = get_product_leverage(product["id"])

    size, margin, per_contract = calculate_order_size(
        product, price, leverage
    )

    event(
        f"SIZE {symbol} contracts={size} "
        f"margin={margin:.4f} leverage={leverage} "
        f"contract_notional={per_contract:.8f}"
    )

    agent(
        "Position Sizer",
        "OK",
        f"{symbol}: {size} contracts / {margin:.2f} USD margin"
    )

    agent("Executor", "RUNNING", f"Submitting {side} {symbol}")

    result = place_market_order(
        product["id"], side, size, reduce_only=False
    )

    LAST_TRADE_TIME[symbol] = time.time()

    with lock:
        state["trade_count"] += 1

    event(
        f"ENTRY ORDER SENT {symbol} side={side} "
        f"size={size} margin={margin:.2f} "
        f"available_balance={balance:.2f}"
    )

    write_trade_log({
        "event": "ENTRY",
        "symbol": symbol,
        "side": side,
        "size": size,
        "margin_estimate": margin,
        "leverage": leverage,
        "price_reference": price,
        "order_result": result,
        "time": datetime.now(timezone.utc).isoformat()
    })

    agent("Executor", "OK", f"{symbol} order submitted")


# ============================================================
# MAIN TRADING LOOP
# ============================================================
def trading_loop():
    time.sleep(5)

    while True:
        try:
            if not API_KEY or not API_SECRET:
                with lock:
                    state["status"] = "API KEYS MISSING"
                agent("Executor", "BLOCKED", "API credentials missing")
                time.sleep(30)
                continue

            discover_futures()

            positions = get_positions()
            orders = get_active_orders()

            with lock:
                state["positions"] = positions
                state["active_orders"] = orders

            # Always manage existing positions before new entries.
            handle_trailing(positions)

            # Refresh positions after any partial/full exits.
            positions = get_positions()
            orders = get_active_orders()

            with lock:
                state["positions"] = positions
                state["active_orders"] = orders

            if not ENABLE_TRADING:
                with lock:
                    state["status"] = "SCANNER ONLY"
                time.sleep(CHECK_INTERVAL)
                continue

            if len(positions) >= MAX_POSITIONS:
                event(
                    f"MAX POSITIONS REACHED "
                    f"{len(positions)}/{MAX_POSITIONS}"
                )
                time.sleep(CHECK_INTERVAL)
                continue

            try:
                balance = account_margin_guard()
            except Exception as e:
                agent("Equity", "BLOCKED", str(e))
                time.sleep(CHECK_INTERVAL)
                continue

            agent("Equity", "OK", f"Available balance {balance:.2f} USD")

            open_symbols = {
                p.get("symbol")
                for p in positions
                if number(p.get("size")) != 0
            }

            scanned = 0

            for symbol in list(PRODUCTS.keys()):
                if scanned >= MAX_MARKET_SCAN_PER_CYCLE:
                    break

                if len(positions) >= MAX_POSITIONS:
                    break

                scanned += 1

                if symbol in open_symbols:
                    continue

                if has_symbol_active_order(symbol, orders):
                    continue

                if cooldown_active(symbol):
                    continue

                signal, reason = check_signal(symbol)

                if not signal or not signal.get("signal"):
                    log.info(
                        "[SCAN] %s SIGNAL=NONE REASON=%s",
                        symbol, reason
                    )
                    continue

                log.info(
                    "[SIGNAL] %s %s TREND=%s PRICE=%.8f "
                    "RSI=%.2f ATR=%.8f",
                    symbol,
                    signal["signal"].upper(),
                    signal["trend"],
                    signal["price"],
                    signal["rsi"],
                    signal["atr"]
                )

                try:
                    # Re-check account positions and orders before entry.
                    fresh_positions = get_positions()
                    fresh_orders = get_active_orders()

                    if len(fresh_positions) >= MAX_POSITIONS:
                        break

                    if has_symbol_position(symbol, fresh_positions):
                        continue

                    if has_symbol_active_order(symbol, fresh_orders):
                        continue

                    enter_trade(signal)

                    # Refresh to avoid multiple entries during same scan.
                    positions = get_positions()
                    open_symbols = {
                        p.get("symbol")
                        for p in positions
                        if number(p.get("size")) != 0
                    }

                except Exception as e:
                    log.exception("ENTRY BLOCKED %s: %s", symbol, e)
                    agent("Executor", "BLOCKED", f"{symbol}: {e}")

                time.sleep(0.2)

            with lock:
                state["last_scan"] = datetime.now(
                    timezone.utc
                ).isoformat()
                state["status"] = "TESTNET LIVE"

            save_state()
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log.exception("MAIN LOOP ERROR")
            with lock:
                state["last_error"] = str(e)
                state["status"] = "ERROR - RETRYING"

            agent("Scanner", "ERROR", str(e))
            save_state()
            time.sleep(10)


# ============================================================
# FLASK DASHBOARD
# ============================================================
def dashboard_auth():
    if not VIEW_PASSWORD:
        return True

    auth = request.authorization
    return bool(
        auth
        and auth.username == VIEW_USER
        and auth.password == VIEW_PASSWORD
    )


@app.route("/")
def home():
    return Response(
        "<h2>DELTA EXCHANGE INDIA - TESTNET BOT</h2>"
        "<p><a href='/health'>Health</a></p>"
        "<p><a href='/dashboard'>Dashboard</a></p>"
        "<p><a href='/dashboard-data'>Dashboard JSON</a></p>",
        mimetype="text/html"
    )


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "status": state.get("status"),
        "mode": "TESTNET ONLY",
        "time": datetime.now(timezone.utc).isoformat()
    })


@app.route("/dashboard")
def dashboard():
    if not dashboard_auth():
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="Dashboard"'}
        )

    html = """
    <!doctype html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Delta Testnet Bot</title>
      <style>
        body { background:#101722; color:#e8eef8;
               font-family:Arial; padding:16px; }
        h2 { color:#55d6a4; }
        .card { background:#1b2736; padding:14px;
                margin:10px 0; border-radius:10px; }
        pre { white-space:pre-wrap; overflow-wrap:anywhere; }
      </style>
    </head>
    <body>
      <h2>DELTA EXCHANGE INDIA - TESTNET</h2>
      <div class="card" id="status">Loading...</div>
      <div class="card"><h3>Positions</h3><pre id="positions"></pre></div>
      <div class="card"><h3>Active Orders</h3><pre id="orders"></pre></div>
      <div class="card"><h3>Eligible Markets</h3><pre id="markets"></pre></div>
      <div class="card"><h3>Agent Status</h3><pre id="agents"></pre></div>
      <div class="card"><h3>Last Signals</h3><pre id="signals"></pre></div>
      <div class="card"><h3>Recent Events</h3><pre id="events"></pre></div>
      <script>
        async function refresh() {
          try {
            const r = await fetch('/dashboard-data');
            const d = await r.json();
            document.getElementById('status').innerText =
              'STATUS: ' + d.status + '\\nMODE: ' + d.mode +
              '\\nPOSITIONS: ' + d.positions.length +
              '\\nTRADE COUNT: ' + d.trade_count +
              '\\nLAST SCAN: ' + d.last_scan +
              '\\nERROR: ' + d.last_error;
            document.getElementById('positions').innerText =
              JSON.stringify(d.positions, null, 2);
            document.getElementById('orders').innerText =
              JSON.stringify(d.active_orders, null, 2);
            document.getElementById('markets').innerText =
              JSON.stringify(d.eligible_markets, null, 2);
            document.getElementById('agents').innerText =
              JSON.stringify(d.agent_status, null, 2);
            document.getElementById('signals').innerText =
              JSON.stringify(d.last_signals, null, 2);
            document.getElementById('events').innerText =
              JSON.stringify(d.events.slice(-20), null, 2);
          } catch(e) {
            document.getElementById('status').innerText = String(e);
          }
        }
        refresh();
        setInterval(refresh, 10000);
      </script>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")


@app.route("/dashboard-data")
def dashboard_data():
    if not dashboard_auth():
        return Response("Unauthorized", 401)

    with lock:
        snapshot = json.loads(json.dumps(state, default=str))

    return jsonify(snapshot)


# ============================================================
# STARTUP
# ============================================================
def start_bot():
    thread = threading.Thread(
        target=trading_loop,
        daemon=True,
        name="delta-testnet-trading-loop"
    )
    thread.start()


if __name__ == "__main__":
    start_bot()
    app.run(
        host="0.0.0.0",
        port=APP_PORT,
        threaded=True
