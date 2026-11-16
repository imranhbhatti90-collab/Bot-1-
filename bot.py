
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

# ============================================================
# CAPITAL / RISK
# ============================================================
MARGIN_PER_TRADE_USD = 20.0
MAX_POSITIONS = 10
MAX_ACCOUNT_MARGIN_USE_USD = 200.0
BALANCE_BUFFER_USD = 5.0

# ============================================================
# STRATEGY
# ============================================================
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

RSI_BUY_MIN = 50
RSI_BUY_MAX = 68
RSI_SELL_MIN = 32
RSI_SELL_MAX = 50

# False = EMA alignment; fresh crossover not required.
REQUIRE_FRESH_CROSS = False

# ============================================================
# RISK MANAGEMENT
# ============================================================
ATR_STOP_MULTIPLIER = 1.0
ATR_TRAIL_MULTIPLIER = 1.0
ATR_PARTIAL_MULTIPLIER = 1.0
PARTIAL_CLOSE_PERCENT = 0.50

FIXED_PARTIAL_TARGETS = {
    "BTCUSD": 200.0,
    "ETHUSD": 15.0,
    "PAXGUSD": 10.0,
    "SILVERUSD": 0.20,
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
POSITION_META_FILE = "position_meta.json"

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
    "User-Agent": "Delta-India-Testnet-MultiAgent-Bot/4.0",
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
    "account_margin_used": 0.0,
    "available_balance": 0.0,
}

PRODUCTS = {}
PRODUCT_ID_TO_SYMBOL = {}

LAST_TRADE_TIME = {}
POSITION_META = {}

DISCOVERED_AT = 0
TICKER_CACHE = {}
TICKER_CACHE_TIME = 0

# ============================================================
# GENERAL HELPERS
# ============================================================
def number(value, default=0.0):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except Exception:
        pass
    return default


def asset_symbol(asset):
    if isinstance(asset, dict):
        return str(asset.get("symbol", "")).upper()
    return str(asset or "").upper()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_state():
    try:
        with lock:
            data = json.dumps(state, indent=2, default=str)
        with open(STATE_FILE, "w") as f:
            f.write(data)
    except Exception as e:
        log.error("STATE SAVE ERROR: %s", e)


def save_position_meta():
    try:
        with lock:
            data = json.dumps(POSITION_META, indent=2, default=str)
        with open(POSITION_META_FILE, "w") as f:
            f.write(data)
    except Exception as e:
        log.error("POSITION META SAVE ERROR: %s", e)


def load_position_meta():
    global POSITION_META
    try:
        if os.path.exists(POSITION_META_FILE):
            with open(POSITION_META_FILE, "r") as f:
                data = json.load(f)

            if isinstance(data, dict):
                POSITION_META = data
                log.info(
                    "POSITION META RESTORED: %s symbols",
                    len(POSITION_META)
                )
    except Exception as e:
        log.error("POSITION META LOAD ERROR: %s", e)


def event(message):
    log.info(message)

    with lock:
        state["events"].append({
            "time": utc_now(),
            "message": str(message)
        })
        state["events"] = state["events"][-100:]

    save_state()


def agent(name, status, detail=""):
    with lock:
        state["agent_status"][name] = {
            "status": status,
            "detail": str(detail),
            "time": utc_now()
        }


def write_trade_log(data):
    try:
        with open(TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(data, default=str) + "\n")
    except Exception as e:
        log.error("TRADE LOG ERROR: %s", e)


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

    query = urlencode(sorted(params.items()), doseq=True)
    request_path = path + (("?" + query) if query else "")

    body_text = ""
    if body is not None:
        body_text = json.dumps(body, separators=(",", ":"))

    headers = {}

    if auth:
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "DELTA_API_KEY / DELTA_API_SECRET missing"
            )

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

    url = BASE_URL + request_path

    response = session.request(
        method=method,
        url=url,
        data=body_text if body is not None else None,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    return api_response(response)


def public_get(path, params=None):
    return api_request(
        "GET",
        path,
        params=params,
        auth=False
    )


# ============================================================
# PRODUCT DISCOVERY
# ============================================================
def is_eligible_product(p):
    if str(p.get("contract_type", "")).lower() != "perpetual_futures":
        return False

    if str(p.get("state", "")).lower() != "live":
        return False

    status = str(p.get("trading_status", "")).lower()

    if status not in ("operational", "live", "trading"):
        return False

    if asset_symbol(p.get("quoting_asset")) != "USD":
        return False

    if asset_symbol(p.get("settling_asset")) != "USD":
        return False

    if not p.get("id"):
        return False

    if number(p.get("tick_size")) <= 0:
        return False

    if number(p.get("contract_value")) <= 0:
        return False

    return True


def discover_futures(force=False):
    global PRODUCTS, PRODUCT_ID_TO_SYMBOL, DISCOVERED_AT

    if (
        not force
        and PRODUCTS
        and time.time() - DISCOVERED_AT < DISCOVERY_INTERVAL
    ):
        return list(PRODUCTS.values())

    agent("Scanner", "RUNNING", "Refreshing Delta product list")

    found = []
    after = None

    for _ in range(20):
        params = {
            "contract_types": "perpetual_futures",
            "states": "live",
            "page_size": 50,
        }

        if after:
            params["after"] = after

        data = public_get("/v2/products", params)
        rows = data.get("result", [])

        if isinstance(rows, dict):
            rows = [rows]

        found.extend(rows)

        after = (data.get("meta") or {}).get("after")

        if not after or not rows:
            break

    new_products = {}

    for p in found:
        symbol = str(p.get("symbol", "")).upper()

        if symbol and is_eligible_product(p):
            new_products[symbol] = p

    if new_products:
        PRODUCTS = new_products
        PRODUCT_ID_TO_SYMBOL = {
            str(p.get("id")): symbol
            for symbol, p in PRODUCTS.items()
        }

    DISCOVERED_AT = time.time()

    with lock:
        state["discovered_markets"] = [
            p.get("symbol")
            for p in found
            if p.get("symbol")
        ]
        state["eligible_markets"] = list(PRODUCTS.keys())

    event(
        f"DISCOVERY COMPLETE total={len(found)} "
        f"eligible={len(PRODUCTS)}"
    )

    agent(
        "Scanner",
        "OK" if PRODUCTS else "BLOCKED",
        f"{len(PRODUCTS)} eligible perpetual markets"
    )

    save_state()
    return list(PRODUCTS.values())


# ============================================================
# MARKET DATA
# ============================================================
def get_all_tickers(force=False):
    global TICKER_CACHE, TICKER_CACHE_TIME

    if (
        TICKER_CACHE
        and not force
        and time.time() - TICKER_CACHE_TIME < 5
    ):
        return TICKER_CACHE

    data = public_get(
        "/v2/tickers",
        {"contract_types": "perpetual_futures"}
    )

    rows = data.get("result", [])

    cache = {}

    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if symbol:
            cache[symbol] = row

    TICKER_CACHE = cache
    TICKER_CACHE_TIME = time.time()

    return TICKER_CACHE


def get_ticker(symbol):
    tickers = get_all_tickers()
    return tickers.get(symbol, {})


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

    data = public_get(
        "/v2/history/candles",
        {
            "resolution": resolution,
            "symbol": symbol,
            "start": str(start),
            "end": str(end),
        }
    )

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
        df["volume"] = 0.0
    else:
        df["volume"] = pd.to_numeric(
            df["volume"], errors="coerce"
        ).fillna(0)

    if "time" in df.columns:
        df = df.sort_values("time")

    df = df.dropna(subset=required).reset_index(drop=True)

    # Exclude currently forming candle.
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    return df.reset_index(drop=True)


# ============================================================
# INDICATORS
# ============================================================
def add_indicators(df):
    df = df.copy()

    df["ema10"] = df["close"].ewm(
        span=EMA_ENTRY_FAST,
        adjust=False
    ).mean()

    df["ema20"] = df["close"].ewm(
        span=EMA_ENTRY_SLOW,
        adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=EMA_TREND_FAST,
        adjust=False
    ).mean()

    df["ema200"] = df["close"].ewm(
        span=EMA_TREND_SLOW,
        adjust=False
    ).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-12)
    df["rsi"] = 100 - (100 / (1 + rs))

    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False
    ).mean()

    df["volume_avg"] = df["volume"].rolling(
        VOLUME_PERIOD
    ).mean()

    return df


# ============================================================
# SIGNAL DIAGNOSTICS
# ============================================================
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

        required_values = [
            t["ema50"],
            t["ema200"],
            t["atr"],
            e["ema10"],
            e["ema20"],
            e["rsi"],
            e["volume_avg"]
        ]

        if any(pd.isna(v) for v in required_values):
            return None, "INDICATORS_NOT_READY"

        trend = "MIXED"

        if (
            t["ema50"] > t["ema200"]
            and t["close"] > t["ema50"]
        ):
            trend = "BULLISH"

        elif (
            t["ema50"] < t["ema200"]
            and t["close"] < t["ema50"]
        ):
            trend = "BEARISH"

        aligned_buy = e["ema10"] > e["ema20"]
        aligned_sell = e["ema10"] < e["ema20"]

        crossed_up = (
            prev["ema10"] <= prev["ema20"]
            and e["ema10"] > e["ema20"]
        )

        crossed_down = (
            prev["ema10"] >= prev["ema20"]
            and e["ema10"] < e["ema20"]
        )

        rsi = number(e["rsi"])

        buy_rsi_ok = RSI_BUY_MIN <= rsi <= RSI_BUY_MAX
        sell_rsi_ok = RSI_SELL_MIN <= rsi <= RSI_SELL_MAX

        volume = number(e["volume"])
        volume_avg = number(e["volume_avg"])

        volume_ok = (
            volume_avg > 0
            and volume > volume_avg * VOLUME_MULTIPLIER
        )

        live_price = get_live_price(symbol)
        candle_close = number(e["close"])

        if live_price <= 0 or candle_close <= 0:
            return None, "NO_LIVE_PRICE"

        gap = abs(live_price - candle_close) / candle_close
        gap_ok = gap <= MAX_CANDLE_LIVE_GAP_PCT

        atr = number(t["atr"])

        if atr <= 0:
            return None, "INVALID_ATR"

        signal = None
        reason = "NO_ALIGNED_SIGNAL"

        if trend == "MIXED":
            reason = "TREND_MIXED"

        elif trend == "BULLISH":
            if not aligned_buy:
                reason = "EMA_NOT_BULLISH_ALIGNED"

            elif REQUIRE_FRESH_CROSS and not crossed_up:
                reason = "NO_BULLISH_EMA_CROSS"

            elif not buy_rsi_ok:
                reason = "BUY_RSI_OUT_OF_RANGE"

            elif not volume_ok:
                reason = "VOLUME_TOO_LOW"

            else:
                signal = "buy"
                reason = "BUY_SETUP_CONFIRMED"

        elif trend == "BEARISH":
            if not aligned_sell:
                reason = "EMA_NOT_BEARISH_ALIGNED"

            elif REQUIRE_FRESH_CROSS and not crossed_down:
                reason = "NO_BEARISH_EMA_CROSS"

            elif not sell_rsi_ok:
                reason = "SELL_RSI_OUT_OF_RANGE"

            elif not volume_ok:
                reason = "VOLUME_TOO_LOW"

            else:
                signal = "sell"
                reason = "SELL_SETUP_CONFIRMED"

        if not gap_ok:
            signal = None
            reason = "LIVE_PRICE_TOO_FAR_FROM_CANDLE"

        result = {
            "symbol": symbol,
            "signal": signal,
            "reason": reason,
            "trend": trend,
            "price": live_price,
            "candle_close": candle_close,
            "price_gap_pct": gap * 100,
            "ema50_15m": number(t["ema50"]),
            "ema200_15m": number(t["ema200"]),
            "ema10_5m": number(e["ema10"]),
            "ema20_5m": number(e["ema20"]),
            "previous_ema10_5m": number(prev["ema10"]),
            "previous_ema20_5m": number(prev["ema20"]),
            "aligned_buy": bool(aligned_buy),
            "aligned_sell": bool(aligned_sell),
            "crossed_up": bool(crossed_up),
            "crossed_down": bool(crossed_down),
            "rsi": rsi,
            "buy_rsi_ok": bool(buy_rsi_ok),
            "sell_rsi_ok": bool(sell_rsi_ok),
            "volume": volume,
            "volume_avg": volume_avg,
            "volume_ok": bool(volume_ok),
            "atr": atr,
            "time": utc_now(),
        }

        with lock:
            state["last_signals"][symbol] = result

        log.info(
            "[SIGNAL DIAGNOSTIC] %s TREND=%s "
            "EMA50=%.8f EMA200=%.8f "
            "EMA10=%.8f EMA20=%.8f "
            "ALIGNED_BUY=%s ALIGNED_SELL=%s "
            "CROSS_UP=%s CROSS_DOWN=%s "
            "RSI=%.2f BUY_RSI=%s SELL_RSI=%s "
            "VOL=%.4f VOL_AVG=%.4f VOL_OK=%s "
            "GAP=%.4f%% GAP_OK=%s FINAL=%s REASON=%s",
            symbol,
            trend,
            number(t["ema50"]),
            number(t["ema200"]),
            number(e["ema10"]),
            number(e["ema20"]),
            aligned_buy,
            aligned_sell,
            crossed_up,
            crossed_down,
            rsi,
            buy_rsi_ok,
            sell_rsi_ok,
            volume,
            volume_avg,
            volume_ok,
            gap * 100,
            gap_ok,
            signal,
            reason
        )

        return result, reason

    except Exception as e:
        log.exception("SIGNAL ERROR %s", symbol)
        return None, str(e)


# ============================================================
# ACCOUNT / POSITIONS / ORDERS
# ============================================================
def get_positions():
    """
    Fetch account-wide positions in one request.
    If the request fails, the caller blocks new entries.
    """
    data = api_request("GET", "/v2/positions")
    rows = data.get("result", [])

    if isinstance(rows, dict):
        if isinstance(rows.get("positions"), list):
            rows = rows["positions"]
        else:
            rows = [rows]

    if not isinstance(rows, list):
        raise RuntimeError("Unexpected positions API response")

    all_positions = []

    for p in rows:
        if not isinstance(p, dict):
            continue

        size = number(p.get("size"))

        if size == 0:
            continue

        product_id = str(p.get("product_id", ""))
        symbol = PRODUCT_ID_TO_SYMBOL.get(product_id)

        if not symbol:
            symbol = str(p.get("product_symbol", "")).upper()

        if not symbol:
            symbol = f"UNKNOWN_PRODUCT_{product_id}"

        p["symbol"] = symbol
        p["product_id"] = product_id

        all_positions.append(p)

    return all_positions


def get_active_orders():
    data = api_request("GET", "/v2/orders")
    rows = data.get("result", [])

    if isinstance(rows, dict):
        rows = [rows]

    return rows if isinstance(rows, list) else []


def get_wallet_balances():
    data = api_request("GET", "/v2/wallet/balances")
    rows = data.get("result", [])

    return rows if isinstance(rows, list) else []


def usd_wallet_row():
    rows = get_wallet_balances()

    for row in rows:
        asset = str(row.get("asset_symbol", "")).upper()

        if asset == "USD":
            return row

    raise RuntimeError("USD wallet balance not found")


def available_usd_balance():
    row = usd_wallet_row()
    return number(row.get("available_balance"))


def exchange_margin_used_usd():
    """
    blocked_margin is the exchange-reported aggregate.
    Use component fields only as a fallback if blocked_margin
    is not supplied by the API.
    """
    row = usd_wallet_row()

    if row.get("blocked_margin") not in (None, ""):
        return max(0.0, number(row.get("blocked_margin")))

    if row.get("portfolio_margin") not in (None, ""):
        return max(0.0, number(row.get("portfolio_margin")))

    components = (
        number(row.get("position_margin"))
        + number(row.get("order_margin"))
        + number(row.get("cross_position_margin"))
        + number(row.get("cross_order_margin"))
        + number(row.get("cross_commission"))
        + number(row.get("commission"))
        + number(row.get("cross_locked_collateral"))
    )

    return max(0.0, components)


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
# CONTRACT SIZING - FAIL CLOSED
# ============================================================
def contract_notional_usd_per_contract(product, price):
    value = number(product.get("contract_value"))

    unit = asset_symbol(
        product.get("contract_unit_currency")
    )

    underlying = asset_symbol(
        product.get("underlying_asset")
    )

    settling = asset_symbol(
        product.get("settling_asset")
    )

    if value <= 0 or price <= 0:
        return 0.0

    # Contract value already expressed in USD.
    if unit == "USD" and settling == "USD":
        return value

    # Contract value represents underlying asset units.
    if unit and underlying and unit == underlying:
        return value * price

    log.warning(
        "[SIZING BLOCK] %s unit=%s underlying=%s "
        "settling=%s contract_value=%s",
        product.get("symbol"),
        unit,
        underlying,
        settling,
        value
    )

    return 0.0


def calculate_order_size(product, price, leverage):
    per_contract = contract_notional_usd_per_contract(
        product,
        price
    )

    if per_contract <= 0:
        raise RuntimeError(
            "Unsupported contract unit; refusing guessed quantity"
        )

    if leverage <= 0:
        raise RuntimeError("Invalid leverage")

    target_notional = MARGIN_PER_TRADE_USD * leverage

    raw_size = target_notional / per_contract
    size = int(math.floor(raw_size))

    if size < 1:
        raise RuntimeError(
            "Configured margin cannot buy minimum one contract"
        )

    actual_notional = size * per_contract
    actual_margin = actual_notional / leverage

    if actual_margin > MARGIN_PER_TRADE_USD * 1.001:
        raise RuntimeError(
            "Calculated margin exceeds per-trade budget"
        )

    return size, actual_margin, per_contract


# ============================================================
# ACCOUNT MARGIN GUARD
# ============================================================
def account_margin_guard(required_margin=MARGIN_PER_TRADE_USD):
    balance = available_usd_balance()
    used_margin = exchange_margin_used_usd()

    with lock:
        state["available_balance"] = balance
        state["account_margin_used"] = used_margin

    if balance <= 0:
        raise RuntimeError(
            "Could not verify available USD balance"
        )

    if balance < required_margin + BALANCE_BUFFER_USD:
        raise RuntimeError(
            f"Insufficient available balance: {balance:.2f} USD"
        )

    projected_margin = used_margin + required_margin

    if projected_margin > MAX_ACCOUNT_MARGIN_USE_USD + 0.000001:
        raise RuntimeError(
            f"Account margin cap would be exceeded: "
            f"used={used_margin:.2f}, "
            f"new={required_margin:.2f}, "
            f"cap={MAX_ACCOUNT_MARGIN_USE_USD:.2f}"
        )

    if balance - BALANCE_BUFFER_USD < required_margin:
        raise RuntimeError(
            "Available balance after buffer below trade margin"
        )

    return balance


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
    if side not in ("buy", "sell"):
        raise RuntimeError("Invalid order side")

    if int(size) <= 0:
        raise RuntimeError("Invalid order size")

    body = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "reduce_only": bool(reduce_only),
    }

    data = api_request(
        "POST",
        "/v2/orders",
        body=body
    )

    return data.get("result", data)


def close_position_market(symbol, position_size):
    product = PRODUCTS.get(symbol)

    if not product:
        raise RuntimeError(
            f"Product unavailable for close: {symbol}"
        )

    size = abs(int(position_size))

    if size <= 0:
        return False

    side = "sell" if position_size > 0 else "buy"

    result = place_market_order(
        product["id"],
        side,
        size,
        reduce_only=True
    )

    event(
        f"CLOSE ORDER SENT {symbol} side={side} size={size}"
    )

    write_trade_log({
        "event": "CLOSE_ORDER",
        "symbol": symbol,
        "side": side,
        "size": size,
        "result": result,
        "time": utc_now()
    })

    return True


def partial_close_position(symbol, position_size):
    total = abs(int(position_size))

    if total < 2:
        event(
            f"PARTIAL SKIPPED {symbol}: below 2 contracts"
        )
        return False

    close_size = max(
        1,
        int(math.floor(total * PARTIAL_CLOSE_PERCENT))
    )

    close_size = min(close_size, total - 1)

    product = PRODUCTS.get(symbol)

    if not product:
        raise RuntimeError(
            f"Product unavailable for partial close: {symbol}"
        )

    side = "sell" if position_size > 0 else "buy"

    result = place_market_order(
        product["id"],
        side,
        close_size,
        reduce_only=True
    )

    event(
        f"PARTIAL CLOSE ORDER SENT {symbol} "
        f"size={close_size}; awaiting position confirmation"
    )

    write_trade_log({
        "event": "PARTIAL_CLOSE_ORDER",
        "symbol": symbol,
        "size": close_size,
        "result": result,
        "time": utc_now()
    })

    return close_size


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

    try:
        candles = get_candles(symbol, TREND_TIMEFRAME)

        if len(candles) < 30:
            return

        atr = number(
            add_indicators(candles).iloc[-1]["atr"]
        )

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
            "partial_pending": False,
            "partial_before_size": 0,
            "partial_sent_at": 0,
            "exit_pending": False,
            "created_at": time.time(),
        }

        event(
            f"POSITION INIT {symbol} entry={entry} "
            f"ATR={atr:.8f} "
            f"stop={POSITION_META[symbol]['stop']:.8f}"
        )

        save_position_meta()

    except Exception as e:
        log.error(
            "POSITION INIT ERROR %s: %s",
            symbol,
            e
        )


def partial_target_distance(symbol, atr):
    if symbol in FIXED_PARTIAL_TARGETS:
        return FIXED_PARTIAL_TARGETS[symbol]

    return atr * ATR_PARTIAL_MULTIPLIER


def handle_trailing(positions):
    agent(
        "Risk Check",
        "RUNNING",
        "Managing open positions"
    )

    active_symbols = set()

    for p in positions:
        symbol = p.get("symbol")
        size = number(p.get("size"))
        entry = number(p.get("entry_price"))

        if not symbol or size == 0 or entry <= 0:
            continue

        active_symbols.add(symbol)

        if symbol not in PRODUCTS:
            log.warning(
                "[POSITION WARNING] %s is open but not in eligible products",
                symbol
            )
            continue

        initialize_position_metadata(p)

        meta = POSITION_META.get(symbol)

        if not meta:
            continue

        try:
            if meta.get("exit_pending"):
                log.warning(
                    "[EXIT PENDING] %s waiting for exchange update",
                    symbol
                )
                continue

            price = get_live_price(symbol)

            if price <= 0:
                continue

            product = PRODUCTS[symbol]
            tick = number(product.get("tick_size"))

            direction = 1 if size > 0 else -1
            current_abs_size = abs(int(size))

            # Confirm partial close by checking exchange position size.
            if meta.get("partial_pending"):
                before_size = int(
                    meta.get("partial_before_size", 0)
                )

                if (
                    before_size > 0
                    and current_abs_size < before_size
                ):
                    meta["partial_pending"] = False
                    meta["partial_done"] = True

                    if direction == 1:
                        meta["stop"] = max(
                            number(meta["stop"]),
                            entry
                        )
                    else:
                        meta["stop"] = min(
                            number(meta["stop"]),
                            entry
                        )

                    event(
                        f"PARTIAL CONFIRMED {symbol} "
                        f"remaining={current_abs_size}"
                    )

                    save_position_meta()

                else:
                    log.info(
                        "[PARTIAL WAIT] %s current=%s expected_below=%s",
                        symbol,
                        current_abs_size,
                        before_size
                    )
                    continue

            atr = number(meta.get("atr"))

            candles = get_candles(
                symbol,
                TREND_TIMEFRAME
            )

            if len(candles) >= 30:
                latest_atr = number(
                    add_indicators(candles).iloc[-1]["atr"]
                )

                if latest_atr > 0:
                    atr = latest_atr
                    meta["atr"] = atr

            if atr <= 0:
                continue

            if direction == 1:
                meta["best_price"] = max(
                    number(meta["best_price"]),
                    price
                )

                profit_move = price - entry

                trail_stop = (
                    meta["best_price"]
                    - atr * ATR_TRAIL_MULTIPLIER
                )

                meta["stop"] = max(
                    number(meta["stop"]),
                    trail_stop
                )

            else:
                meta["best_price"] = min(
                    number(meta["best_price"]),
                    price
                )

                profit_move = entry - price

                trail_stop = (
                    meta["best_price"]
                    + atr * ATR_TRAIL_MULTIPLIER
                )

                meta["stop"] = min(
                    number(meta["stop"]),
                    trail_stop
                )

            partial_distance = partial_target_distance(
                symbol,
                atr
            )

            if (
                not meta.get("partial_done")
                and not meta.get("partial_pending")
                and profit_move >= partial_distance
            ):
                close_size = partial_close_position(
                    symbol,
                    size
                )

                if close_size:
                    meta["partial_pending"] = True
                    meta["partial_before_size"] = current_abs_size
                    meta["partial_sent_at"] = time.time()
                    save_position_meta()

            meta["stop"] = round_to_tick(
                meta["stop"],
                tick
            )

            stop_hit = (
                price <= meta["stop"]
                if direction == 1
                else price >= meta["stop"]
            )

            if stop_hit:
                event(
                    f"BOT STOP HIT {symbol} "
                    f"price={price:.8f} "
                    f"stop={meta['stop']:.8f}"
                )

                meta["exit_pending"] = True
                save_position_meta()

                try:
                    close_position_market(symbol, size)
                except Exception:
                    meta["exit_pending"] = False
                    save_position_meta()
                    raise

                continue

            log.info(
                "[TRAIL] %s side=%s price=%.8f entry=%.8f "
                "move=%.8f partial=%.8f stop=%.8f "
                "partial_done=%s",
                symbol,
                "LONG" if direction == 1 else "SHORT",
                price,
                entry,
                profit_move,
                partial_distance,
                meta["stop"],
                meta.get("partial_done")
            )

            save_position_meta()

        except Exception as e:
            log.exception(
                "TRAIL ERROR %s: %s",
                symbol,
                e
            )

    for symbol in list(POSITION_META.keys()):
        if symbol not in active_symbols:
            POSITION_META.pop(symbol, None)

    save_position_meta()

    agent(
        "Risk Check",
        "OK",
        "Position management cycle complete"
    )


# ============================================================
# ENTRY FILTERS
# ============================================================
def has_symbol_position(symbol, positions):
    return any(
        p.get("symbol") == symbol
        and number(p.get("size")) != 0
        for p in positions
    )


def terminal_order_state(status):
    return str(status or "").lower() in (
        "closed",
        "cancelled",
        "canceled",
        "rejected",
        "filled",
        "expired"
    )


def has_symbol_active_order(symbol, orders):
    product = PRODUCTS.get(symbol)

    if not product:
        return True

    product_id = str(product.get("id"))

    for order in orders:
        if str(order.get("product_id")) != product_id:
            continue

        status = str(
            order.get("state", order.get("status", ""))
        ).lower()

        if not terminal_order_state(status):
            return True

    return False


def occupied_symbol_count(positions, orders):
    occupied = set()

    for p in positions:
        if number(p.get("size")) == 0:
            continue

        symbol = p.get("symbol")

        if symbol:
            occupied.add(symbol)

    for order in orders:
        status = str(
            order.get("state", order.get("status", ""))
        ).lower()

        if terminal_order_state(status):
            continue

        product_id = str(order.get("product_id"))
        symbol = PRODUCT_ID_TO_SYMBOL.get(product_id)

        if symbol:
            occupied.add(symbol)

    return len(occupied)


def cooldown_active(symbol):
    last = LAST_TRADE_TIME.get(symbol, 0)

    return (
        time.time() - last
        < COOLDOWN_SECONDS
    )


# ============================================================
# ENTRY EXECUTOR
# ============================================================
def enter_trade(signal):
    symbol = signal["symbol"]
    product = PRODUCTS.get(symbol)

    if not product:
        raise RuntimeError("Product not found")

    side = signal["signal"]
    price = number(signal["price"])

    if side not in ("buy", "sell") or price <= 0:
        return

    agent(
        "Position Sizer",
        "RUNNING",
        symbol
    )

    leverage = get_product_leverage(product["id"])

    size, margin, per_contract = calculate_order_size(
        product,
        price,
        leverage
    )

    # Verify account margin using the actual calculated margin.
    balance = account_margin_guard(margin)

    event(
        f"SIZE {symbol} contracts={size} "
        f"margin={margin:.4f} "
        f"leverage={leverage} "
        f"per_contract_notional={per_contract:.8f}"
    )

    agent(
        "Position Sizer",
        "OK",
        f"{symbol}: {size} contracts / {margin:.2f} USD"
    )

    agent(
        "Executor",
        "RUNNING",
        f"Submitting {side} {symbol}"
    )

    result = place_market_order(
        product["id"],
        side,
        size,
        reduce_only=False
    )

    LAST_TRADE_TIME[symbol] = time.time()

    with lock:
        state["trade_count"] += 1

    event(
        f"ENTRY ORDER SUBMITTED {symbol} "
        f"side={side} size={size} "
        f"margin_est={margin:.2f} "
        f"available_balance={balance:.2f}"
    )

    write_trade_log({
        "event": "ENTRY_ORDER",
        "symbol": symbol,
        "side": side,
        "size": size,
        "margin_estimate": margin,
        "leverage": leverage,
        "price_reference": price,
        "order_result": result,
        "time": utc_now()
    })

    agent(
        "Executor",
        "OK",
        f"{symbol} order submitted"
    )


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

                agent(
                    "Executor",
                    "BLOCKED",
                    "API credentials missing"
                )

                time.sleep(30)
                continue

            discover_futures()

            if not PRODUCTS:
                raise RuntimeError(
                    "No eligible markets discovered"
                )

            # Account-wide positions. Any API failure blocks entries.
            positions = get_positions()
            orders = get_active_orders()

            with lock:
                state["positions"] = positions
                state["active_orders"] = orders

            handle_trailing(positions)

            # Refresh after position-management orders.
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

            occupied = occupied_symbol_count(
                positions,
                orders
            )

            if occupied >= MAX_POSITIONS:
                event(
                    f"MAX POSITIONS / ORDERS REACHED "
                    f"{occupied}/{MAX_POSITIONS}"
                )

                time.sleep(CHECK_INTERVAL)
                continue

            try:
                balance = account_margin_guard(
                    MARGIN_PER_TRADE_USD
                )

            except Exception as e:
                agent(
                    "Equity",
                    "BLOCKED",
                    str(e)
                )

                log.warning(
                    "[EQUITY BLOCK] %s",
                    e
                )

                time.sleep(CHECK_INTERVAL)
                continue

            agent(
                "Equity",
                "OK",
                f"Available balance {balance:.2f} USD"
            )

            open_symbols = {
                p.get("symbol")
                for p in positions
                if number(p.get("size")) != 0
            }

            scanned = 0

            for symbol in list(PRODUCTS.keys()):
                if scanned >= MAX_MARKET_SCAN_PER_CYCLE:
                    break

                if occupied_symbol_count(
                    positions,
                    orders
                ) >= MAX_POSITIONS:
                    break

                scanned += 1

                if symbol in open_symbols:
                    log.info(
                        "[SCAN SKIP] %s POSITION_ALREADY_OPEN",
                        symbol
                    )
                    continue

                if has_symbol_active_order(symbol, orders):
                    log.info(
                        "[SCAN SKIP] %s ACTIVE_ORDER",
                        symbol
                    )
                    continue

                if cooldown_active(symbol):
                    log.info(
                        "[SCAN SKIP] %s COOLDOWN",
                        symbol
                    )
                    continue

                signal, reason = check_signal(symbol)

                if not signal or not signal.get("signal"):
                    log.info(
                        "[SCAN] %s SIGNAL=NONE REASON=%s",
                        symbol,
                        reason
                    )
                    continue

                log.info(
                    "[SIGNAL] %s %s TREND=%s "
                    "PRICE=%.8f RSI=%.2f ATR=%.8f",
                    symbol,
                    signal["signal"].upper(),
                    signal["trend"],
                    signal["price"],
                    signal["rsi"],
                    signal["atr"]
                )

                try:
                    # Recheck account positions and orders immediately.
                    fresh_positions = get_positions()
                    fresh_orders = get_active_orders()

                    if occupied_symbol_count(
                        fresh_positions,
                        fresh_orders
                    ) >= MAX_POSITIONS:
                        break

                    if has_symbol_position(
                        symbol,
                        fresh_positions
                    ):
                        continue

                    if has_symbol_active_order(
                        symbol,
                        fresh_orders
                    ):
                        continue

                    # Check margin cap again before order placement.
                    account_margin_guard(
                        MARGIN_PER_TRADE_USD
                    )

                    enter_trade(signal)

                    positions = get_positions()
                    orders = get_active_orders()

                    with lock:
                        state["positions"] = positions
                        state["active_orders"] = orders

                    open_symbols = {
                        p.get("symbol")
                        for p in positions
                        if number(p.get("size")) != 0
                    }

                except Exception as e:
                    log.exception(
                        "ENTRY BLOCKED %s: %s",
                        symbol,
                        e
                    )

                    agent(
                        "Executor",
                        "BLOCKED",
                        f"{symbol}: {e}"
                    )

                time.sleep(0.2)

            with lock:
                state["last_scan"] = utc_now()
                state["status"] = "TESTNET LIVE"
                state["last_error"] = None

            save_state()
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log.exception("MAIN LOOP ERROR")

            with lock:
                state["last_error"] = str(e)
                state["status"] = "ERROR - RETRYING"

            agent(
                "Scanner",
                "ERROR",
                str(e)
            )

            save_state()
            time.sleep(10)


# ============================================================
# DASHBOARD AUTH
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


# ============================================================
# HOME
# ============================================================
@app.route("/")
def home():
    return Response(
        "<h2>DELTA EXCHANGE INDIA - TESTNET BOT</h2>"
        "<p><a href='/health'>Health</a></p>"
        "<p><a href='/dashboard'>Dashboard</a></p>"
        "<p><a href='/dashboard-data'>Dashboard JSON</a></p>",
        mimetype="text/html"
    )


# ============================================================
# HEALTH
# ============================================================
@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "status": state.get("status"),
        "mode": "TESTNET ONLY",
        "time": utc_now()
    })


# ============================================================
# DASHBOARD
# ============================================================
@app.route("/dashboard")
def dashboard():
    if not dashboard_auth():
        return Response(
            "Authentication required",
            401,
            {
                "WWW-Authenticate": 'Basic realm="Dashboard"'
            }
        )

    html = """
    <!doctype html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Delta Testnet Bot</title>
      <style>
        body {
          background:#101722;
          color:#e8eef8;
          font-family:Arial;
          padding:16px;
        }
        h2 { color:#55d6a4; }
        .card {
          background:#1b2736;
          padding:14px;
          margin:10px 0;
          border-radius:10px;
        }
        pre {
          white-space:pre-wrap;
          overflow-wrap:anywhere;
        }
      </style>
    </head>
    <body>
      <h2>DELTA EXCHANGE INDIA - TESTNET</h2>
      <div class="card" id="status">Loading...</div>

      <div class="card">
        <h3>Positions</h3>
        <pre id="positions"></pre>
      </div>

      <div class="card">
        <h3>Active Orders</h3>
        <pre id="orders"></pre>
      </div>

      <div class="card">
        <h3>Eligible Markets</h3>
        <pre id="markets"></pre>
      </div>

      <div class="card">
        <h3>Agent Status</h3>
        <pre id="agents"></pre>
      </div>

      <div class="card">
        <h3>Last Signals / Diagnostics</h3>
        <pre id="signals"></pre>
      </div>

      <div class="card">
        <h3>Recent Events</h3>
        <pre id="events"></pre>
      </div>

      <script>
        async function refresh() {
          try {
            const r = await fetch('/dashboard-data');

            if (!r.ok) {
              throw new Error('Dashboard data HTTP ' + r.status);
            }

            const d = await r.json();

            document.getElementById('status').innerText =
              'STATUS: ' + d.status +
              '\\nMODE: ' + d.mode +
              '\\nPOSITIONS: ' + d.positions.length +
              '\\nTRADE COUNT: ' + d.trade_count +
              '\\nAVAILABLE BALANCE: ' + d.available_balance +
              '\\nACCOUNT MARGIN USED: ' + d.account_margin_used +
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
            document.getElementById('status').innerText =
              String(e);
          }
        }

        refresh();
        setInterval(refresh, 10000);
      </script>
    </body>
    </html>
    """

    return Response(
        html,
        mimetype="text/html"
    )


# ============================================================
# DASHBOARD JSON
# ============================================================
@app.route("/dashboard-data")
def dashboard_data():
    if not dashboard_auth():
        return Response(
            "Unauthorized",
            401
        )

    with lock:
        snapshot = json.loads(
            json.dumps(state, default=str)
        )

    return jsonify(snapshot)


# ============================================================
# STARTUP
# ============================================================
def start_bot():
    load_position_meta()

    thread = threading.Thread(
        target=trading_loop,
        daemon=True,
        name="delta-testnet-trading-loop"
    )

    thread.start()

    log.info("TRADING ENGINE THREAD STARTED")


if __name__ == "__main__":
    log.info("DELTA INDIA TESTNET BOT STARTING")
    log.info("TESTNET BASE URL: %s", BASE_URL)
    log.info("API KEY PRESENT: %s", bool(API_KEY))
    log.info("API SECRET PRESENT: %s", bool(API_SECRET))
    log.info("ENABLE TRADING: %s", ENABLE_TRADING)
    log.info("MAX POSITIONS: %s", MAX_POSITIONS)
    log.info("MARGIN PER TRADE: %.2f USD", MARGIN_PER_TRADE_USD)
    log.info(
        "MAX ACCOUNT MARGIN: %.2f USD",
        MAX_ACCOUNT_MARGIN_USE_USD
    )
    log.info(
        "EMA ALIGNMENT MODE: %s",
        not REQUIRE_FRESH_CROSS
    )

    start_bot()

    app.run(
        host="0.0.0.0",
        port=APP_PORT,
        debug=False,
        use_reloader=False
    )
