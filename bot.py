import os
import json
import time
import hmac
import hashlib
import threading
import logging
import traceback
import math
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import pandas as pd
from flask import Flask, jsonify, Response

# ============================================================
# DELTA INDIA TESTNET - AUTO TRADER
# HARD LOCK: TESTNET URL ONLY
# ============================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

AUTO_TRADING = os.getenv("AUTO_TRADING", "true").lower() == "true"
MARGIN_USD = 20.0
MAX_POSITIONS = 10

TREND_RESOLUTION = "15m"
ENTRY_RESOLUTION = "5m"
SCAN_INTERVAL = 30
DISCOVERY_INTERVAL = 900

TREND_EMA_FAST = 50
TREND_EMA_SLOW = 200
ENTRY_EMA_FAST = 10
ENTRY_EMA_SLOW = 20

RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_MA_PERIOD = 20
VOLUME_MULTIPLIER = 1.00

MAX_ATR_PERCENT = 5.0
MAX_CANDLE_RANGE_PERCENT = 6.0
MAX_CANDLE_BODY_PERCENT = 5.0
MAX_CLOSE_JUMP_PERCENT = 5.0
ZERO_VOLUME_QUARANTINE_COUNT = 4
QUALITY_RECENT_CANDLES = 5

BUY_RSI_MIN = 50
BUY_RSI_MAX = 70
SELL_RSI_MIN = 30
SELL_RSI_MAX = 50

TREND_CANDLE_COUNT = 1000
ENTRY_CANDLE_COUNT = 1000
MIN_TREND_CANDLES = 220
MIN_ENTRY_CANDLES = 50

SL_ATR_MULTIPLIER = 1.2
TP_ATR_MULTIPLIER = 2.0

SIGNAL_HISTORY_LIMIT = 200
STATE_FILE = "state.json"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

FALLBACK_SYMBOLS = [
    "1000SHIBUSD", "ADAUSD", "BTCUSD", "DOGEUSD", "DOGSUSD",
    "ETHUSD", "NVDAXUSD", "PAXGUSD", "QQQXUSD", "SILVERUSD",
    "SLVONUSD", "SNDKBUSD", "SOLUSD", "SPYXUSD", "USOONUSD",
    "XAUTUSD", "XRPUSD"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("delta_auto_testnet")

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Delta-India-Testnet-AutoBot/4.0"
})

app = Flask(__name__)
lock = threading.RLock()

symbols = FALLBACK_SYMBOLS.copy()
products = {}
active_signals = {}
signal_history = []
last_diagnostics = {}
data_quarantine = {}
errors = []
scan_count = 0
last_scan_time = None
last_scan_duration = None
last_discovery_time = 0
last_entry_candle = {}
trade_history = []
execution_status = {}

# ------------------------------------------------------------
# BASIC HELPERS
# ------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(v, default=None):
    try:
        x = float(v)
        if not math.isfinite(x):
            return default
        return x
    except Exception:
        return default


def add_error(msg):
    errors.append({"time": utc_now(), "message": str(msg)})
    del errors[:-50]


def result_list(payload):
    if not isinstance(payload, dict):
        return []
    r = payload.get("result", [])
    return r if isinstance(r, list) else []


def result_obj(payload):
    if isinstance(payload, dict):
        return payload.get("result")
    return None


# ------------------------------------------------------------
# API AUTHENTICATION
# ------------------------------------------------------------

def auth_headers(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing DELTA_API_KEY or DELTA_API_SECRET")

    timestamp = str(int(time.time()))
    query = urlencode(params or {})
    request_path = path + (("?" + query) if query else "")
    body_text = json.dumps(body, separators=(",", ":")) if body else ""

    prehash = method.upper() + timestamp + request_path + body_text
    signature = hmac.new(
        API_SECRET.encode(),
        prehash.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp,
        "User-Agent": "Delta-India-Testnet-AutoBot/4.0",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def api_request(method, path, params=None, body=None, auth=False):
    # Explicit environment lock.
    if BASE_URL != "https://cdn-ind.testnet.deltaex.org":
        raise RuntimeError("TESTNET URL LOCK FAILED")

    headers = {}
    if auth:
        headers = auth_headers(method, path, params, body)

    url = BASE_URL + path

    response = session.request(
        method=method.upper(),
        url=url,
        params=params,
        json=body,
        headers=headers,
        timeout=20
    )
    response.raise_for_status()

    payload = response.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"Delta API error: {payload.get('error')}")

    return payload


def api_get(path, params=None, auth=False):
    return api_request("GET", path, params=params, auth=auth)


def api_post(path, body, auth=True):
    return api_request("POST", path, body=body, auth=auth)


# ------------------------------------------------------------
# PRODUCT DISCOVERY
# ------------------------------------------------------------

def discover_symbols(force=False):
    global symbols, products, last_discovery_time

    now = time.time()
    if not force and products and now - last_discovery_time < DISCOVERY_INTERVAL:
        return symbols

    found = {}
    cursor = None

    try:
        for _ in range(10):
            params = {"page_size": 100}
            if cursor:
                params["after"] = cursor

            payload = api_get("/v2/products", params=params)
            rows = result_list(payload)

            for p in rows:
                if not isinstance(p, dict):
                    continue

                sym = str(p.get("symbol", "")).upper().strip()
                if not sym.endswith("USD") or sym.startswith(("C-", "P-")):
                    continue

                contract_type = str(
                    p.get("contract_type", p.get("product_type", ""))
                ).lower()
                description = str(p.get("description", "")).lower()
                type_text = contract_type or description

                if type_text and "perpetual" not in type_text:
                    continue

                status = str(p.get("state", p.get("status", ""))).lower()
                if status and status not in ("live", "active", "trading"):
                    continue

                found[sym] = p

            meta = payload.get("meta", {})
            next_cursor = meta.get("after")
            if not rows or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        if found:
            products = found
            symbols = sorted(found.keys())
        else:
            logger.warning("[DISCOVERY] Using fallback symbols")

        last_discovery_time = now
        logger.info("[DISCOVERY] symbols=%s", len(symbols))
        return symbols

    except Exception as exc:
        logger.exception("[DISCOVERY ERROR] %s", exc)
        add_error(str(exc))
        if not symbols:
            symbols = FALLBACK_SYMBOLS.copy()
        return symbols


# ------------------------------------------------------------
# CANDLES / DATA QUALITY
# ------------------------------------------------------------

def fetch_candles(symbol, resolution, count):
    now = int(time.time())
    seconds = {"5m": 300, "15m": 900}[resolution]
    start = now - count * seconds

    payload = api_get(
        "/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": symbol,
            "start": start,
            "end": now
        }
    )

    rows = result_list(payload)
    if not rows:
        raise ValueError("NO_CANDLES")

    frame = pd.DataFrame(rows)
    required = ["time", "open", "high", "low", "close", "volume"]

    if any(c not in frame.columns for c in required):
        raise ValueError("MISSING_CANDLE_FIELDS")

    for c in required:
        frame[c] = pd.to_numeric(frame[c], errors="coerce")

    frame = frame.dropna(subset=required)

    if frame["time"].median() > 10_000_000_000:
        frame["time"] /= 1000.0

    frame = frame.sort_values("time").drop_duplicates("time")
    current_open = now - now % seconds
    frame = frame[frame["time"] < current_open].reset_index(drop=True)

    if frame.empty:
        raise ValueError("NO_CLOSED_CANDLES")

    integrity = (
        (frame["open"] > 0) &
        (frame["high"] > 0) &
        (frame["low"] > 0) &
        (frame["close"] > 0) &
        (frame["volume"] >= 0) &
        (frame["high"] >= frame["low"]) &
        (frame["high"] >= frame["open"]) &
        (frame["high"] >= frame["close"]) &
        (frame["low"] <= frame["open"]) &
        (frame["low"] <= frame["close"])
    )

    if not integrity.all():
        raise ValueError("DATA_QUARANTINE:INVALID_OHLC")

    recent = frame.tail(QUALITY_RECENT_CANDLES)
    flags = []

    if len(recent) == QUALITY_RECENT_CANDLES:
        zeros = int((recent["volume"] <= 0).sum())
        if zeros >= ZERO_VOLUME_QUARANTINE_COUNT:
            flags.append(f"ZERO_VOLUME({zeros}/{QUALITY_RECENT_CANDLES})")

    for _, row in recent.iterrows():
        close = row["close"]
        if close <= 0:
            flags.append("INVALID_CLOSE")
            continue

        range_pct = (row["high"] - row["low"]) / close * 100
        body_pct = abs(row["close"] - row["open"]) / close * 100

        if range_pct > MAX_CANDLE_RANGE_PERCENT:
            flags.append(f"CANDLE_RANGE({range_pct:.2f}%)")
        if body_pct > MAX_CANDLE_BODY_PERCENT:
            flags.append(f"CANDLE_BODY({body_pct:.2f}%)")

    if len(frame) >= 2:
        closes = frame.tail(QUALITY_RECENT_CANDLES + 1)["close"].tolist()
        for a, b in zip(closes[:-1], closes[1:]):
            if a <= 0:
                flags.append("INVALID_PREVIOUS_CLOSE")
            elif abs(b - a) / a * 100 > MAX_CLOSE_JUMP_PERCENT:
                flags.append("CLOSE_JUMP")

    key = f"{symbol}:{resolution}"

    if flags:
        data_quarantine[key] = {
            "symbol": symbol,
            "resolution": resolution,
            "flags": list(set(flags)),
            "status": "QUARANTINED",
            "updated_at": utc_now()
        }
        raise ValueError("DATA_QUARANTINE:" + "|".join(flags))

    data_quarantine.pop(key, None)
    return frame


# ------------------------------------------------------------
# INDICATORS
# ------------------------------------------------------------

def calculate_indicators(frame):
    frame = frame.copy()

    for span in [10, 20, 50, 200]:
        frame[f"ema{span}"] = frame["close"].ewm(
            span=span, adjust=False, min_periods=span
        ).mean()

    delta = frame["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD,
        min_periods=RSI_PERIOD,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD,
        min_periods=RSI_PERIOD,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    frame["rsi"] = 100 - (100 / (1 + rs))
    frame.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    frame.loc[(avg_gain == 0) & (avg_loss > 0), "rsi"] = 0
    frame.loc[(avg_gain == 0) & (avg_loss == 0), "rsi"] = 50

    prev_close = frame["close"].shift(1)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev_close).abs(),
        (frame["low"] - prev_close).abs()
    ], axis=1).max(axis=1)

    frame["atr"] = tr.ewm(
        alpha=1 / ATR_PERIOD,
        min_periods=ATR_PERIOD,
        adjust=False
    ).mean()

    frame["volume_avg"] = frame["volume"].shift(1).rolling(
        VOLUME_MA_PERIOD,
        min_periods=VOLUME_MA_PERIOD
    ).mean()

    return frame


# ------------------------------------------------------------
# SIGNAL ANALYSIS
# ------------------------------------------------------------

def analyze_trend(frame):
    if len(frame) < MIN_TREND_CANDLES:
        return {"trend": "NOT_READY"}

    enriched = calculate_indicators(frame)
    row = enriched.iloc[-1]

    close = safe_float(row["close"])
    ema50 = safe_float(row["ema50"])
    ema200 = safe_float(row["ema200"])

    if None in (close, ema50, ema200):
        return {"trend": "NOT_READY"}

    if ema50 > ema200 and close > ema50 and close > ema200:
        trend = "BULLISH"
    elif ema50 < ema200 and close < ema50 and close < ema200:
        trend = "BEARISH"
    else:
        trend = "MIXED"

    return {
        "trend": trend,
        "close": close,
        "ema50": ema50,
        "ema200": ema200,
        "time": int(row["time"])
    }


def analyze_entry(symbol, trend_data, frame):
    if len(frame) < MIN_ENTRY_CANDLES:
        return {"symbol": symbol, "signal": "NONE", "reason": "ENTRY_CANDLES_SHORT"}

    frame = calculate_indicators(frame)
    row = frame.iloc[-1]

    close = safe_float(row["close"])
    ema10 = safe_float(row["ema10"])
    ema20 = safe_float(row["ema20"])
    rsi = safe_float(row["rsi"])
    atr = safe_float(row["atr"])
    volume = safe_float(row["volume"], 0)
    volume_avg = safe_float(row["volume_avg"])

    trend = trend_data.get("trend")
    reasons = []

    if not close or close <= 0:
        reasons.append("INVALID_PRICE")
    if not ema10 or not ema20:
        reasons.append("EMA_NOT_READY")
    if not rsi:
        reasons.append("RSI_NOT_READY")
    if not atr or atr <= 0:
        reasons.append("ATR_NOT_READY")

    atr_pct = atr / close * 100 if close and atr else None
    if atr_pct and atr_pct > MAX_ATR_PERCENT:
        reasons.append("ATR_OUTLIER")

    volume_ok = (
        volume > 0 and volume_avg and volume_avg > 0
        and volume >= volume_avg * VOLUME_MULTIPLIER
    )

    buy = (
        not reasons and trend == "BULLISH" and
        ema10 > ema20 and BUY_RSI_MIN <= rsi <= BUY_RSI_MAX and
        volume_ok
    )

    sell = (
        not reasons and trend == "BEARISH" and
        ema10 < ema20 and SELL_RSI_MIN <= rsi <= SELL_RSI_MAX and
        volume_ok
    )

    signal = "BUY" if buy else "SELL" if sell else "NONE"

    diagnostic = {
        "symbol": symbol,
        "time": utc_now(),
        "trend": trend,
        "close": close,
        "ema10": ema10,
        "ema20": ema20,
        "ema50": trend_data.get("ema50"),
        "ema200": trend_data.get("ema200"),
        "rsi": rsi,
        "atr": atr,
        "atr_percent": atr_pct,
        "volume": volume,
        "volume_avg": volume_avg,
        "volume_ratio": volume / volume_avg if volume_avg else None,
        "volume_ok": bool(volume_ok),
        "signal": signal,
        "candle_time": int(row["time"]),
        "trend_candle_time": trend_data.get("time"),
        "reason": "FILTERS_PASSED" if signal != "NONE" else "FILTERS_BLOCKED"
    }

    return diagnostic


# ------------------------------------------------------------
# AUTH ACCOUNT INFORMATION
# ------------------------------------------------------------

def get_positions():
    payload = api_get("/v2/positions/margined", auth=True)
    return result_list(payload)


def get_open_orders():
    payload = api_get("/v2/orders", auth=True)
    return result_list(payload)


def open_position_for_symbol(symbol, positions=None):
    if positions is None:
        positions = get_positions()

    for p in positions:
        if str(p.get("product_symbol", p.get("symbol", ""))).upper() == symbol:
            size = safe_float(p.get("size"), 0)
            if size and size != 0:
                return p
    return None


def active_orders_for_symbol(symbol, orders=None):
    if orders is None:
        orders = get_open_orders()

    result = []
    for o in orders:
        if str(o.get("product_symbol", o.get("symbol", ""))).upper() != symbol:
            continue
        status = str(o.get("state", "")).lower()
        if status in ("open", "pending"):
            result.append(o)
    return result


def count_open_positions(positions=None):
    if positions is None:
        positions = get_positions()

    count = 0
    for p in positions:
        if safe_float(p.get("size"), 0) != 0:
            count += 1
    return count


# ------------------------------------------------------------
# POSITION SIZING
# ------------------------------------------------------------

def get_product(symbol):
    if symbol not in products:
        discover_symbols(force=True)
    return products.get(symbol)


def get_leverage(product_id):
    payload = api_get(
        f"/v2/products/{product_id}/orders/leverage",
        auth=True
    )
    data = result_obj(payload)
    if not isinstance(data, dict):
        raise RuntimeError("LEVERAGE API returned unexpected data")

    lev = safe_float(data.get("leverage"))
    if not lev or lev <= 0:
        raise RuntimeError("Exchange leverage unavailable")
    return lev


def calculate_contract_size(product, leverage, price):
    contract_value = safe_float(product.get("contract_value"))
    minimum_size = safe_float(product.get("minimum_order_size"), 1)
    maximum_size = safe_float(product.get("maximum_order_size"))

    if not contract_value or contract_value <= 0:
        raise RuntimeError("Product contract_value missing/invalid")

    if not price or price <= 0:
        raise RuntimeError("Invalid price for sizing")

    # Approximation for USD-quoted contract metadata.
    # Refuse if product metadata does not support calculation.
    target_notional = MARGIN_USD * leverage
    raw_size = target_notional / contract_value
    size = int(math.floor(raw_size))

    if size < minimum_size:
        raise RuntimeError(
            f"Calculated size {size} below exchange minimum {minimum_size}"
        )

    if maximum_size and size > maximum_size:
        size = int(maximum_size)

    if size <= 0:
        raise RuntimeError("Calculated order size invalid")

    estimated_notional = size * contract_value
    estimated_margin = estimated_notional / leverage

    if estimated_margin > MARGIN_USD * 1.10:
        raise RuntimeError("Sizing would exceed margin allowance")

    return size, estimated_margin, estimated_notional


# ------------------------------------------------------------
# ORDER PLACEMENT
# ------------------------------------------------------------

def place_market_entry(symbol, side, size, product):
    body = {
        "product_id": int(product["id"]),
        "product_symbol": symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order"
    }

    logger.warning("[ENTRY ORDER] %s %s size=%s", symbol, side, size)
    return api_post("/v2/orders", body, auth=True)


def place_protective_order(symbol, product, side, size, stop_price, order_type):
    body = {
        "product_id": int(product["id"]),
        "product_symbol": symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": order_type,
        "stop_price": str(stop_price),
        "stop_trigger_method": "last_traded_price",
        "reduce_only": True
    }

    logger.warning(
        "[PROTECTIVE ORDER] %s %s size=%s trigger=%s type=%s",
        symbol, side, size, stop_price, order_type
    )
    return api_post("/v2/orders", body, auth=True)


def cancel_order(order_id):
    # DELETE order endpoint.
    return api_request(
        "DELETE",
        f"/v2/orders/{order_id}",
        auth=True
    )


def cancel_symbol_orders(symbol):
    orders = active_orders_for_symbol(symbol)
    for o in orders:
        oid = o.get("id")
        if oid:
            try:
                cancel_order(oid)
                logger.info("[CANCEL RESIDUAL] %s order=%s", symbol, oid)
            except Exception as exc:
                logger.error("[CANCEL ERROR] %s %s", symbol, exc)


# ------------------------------------------------------------
# EXECUTION SAFETY
# ------------------------------------------------------------

def execute_signal(diagnostic):
    symbol = diagnostic.get("symbol")
    signal = diagnostic.get("signal")
    price = safe_float(diagnostic.get("close"))
    atr = safe_float(diagnostic.get("atr"))
    candle_time = diagnostic.get("candle_time")

    if not AUTO_TRADING:
        execution_status[symbol] = "AUTO_TRADING_DISABLED"
        return

    if signal not in ("BUY", "SELL"):
        return

    if not price or not atr or atr <= 0:
        execution_status[symbol] = "INVALID_PRICE_OR_ATR"
        return

    # Prevent repeat entries on same 5m candle.
    if last_entry_candle.get(symbol) == candle_time:
        execution_status[symbol] = "SAME_CANDLE_ENTRY_BLOCK"
        return

    try:
        positions = get_positions()
        orders = get_open_orders()

        if open_position_for_symbol(symbol, positions):
            execution_status[symbol] = "POSITION_ALREADY_OPEN"
            return

        if active_orders_for_symbol(symbol, orders):
            execution_status[symbol] = "ACTIVE_ORDER_EXISTS"
            return

        if count_open_positions(positions) >= MAX_POSITIONS:
            execution_status[symbol] = "MAX_POSITION_LIMIT"
            return

        product = get_product(symbol)
        if not product:
            execution_status[symbol] = "PRODUCT_NOT_FOUND"
            return

        product_id = product.get("id")
        if not product_id:
            execution_status[symbol] = "PRODUCT_ID_MISSING"
            return

        leverage = get_leverage(product_id)
        size, estimated_margin, notional = calculate_contract_size(
            product, leverage, price
        )

        entry_side = "buy" if signal == "BUY" else "sell"
        exit_side = "sell" if signal == "BUY" else "buy"

        # ATR stop and target levels.
        if signal == "BUY":
            stop_price = price - atr * SL_ATR_MULTIPLIER
            target_price = price + atr * TP_ATR_MULTIPLIER
        else:
            stop_price = price + atr * SL_ATR_MULTIPLIER
            target_price = price - atr * TP_ATR_MULTIPLIER

        if stop_price <= 0 or target_price <= 0:
            execution_status[symbol] = "INVALID_STOP_TARGET"
            return

        # ENTRY
        entry_response = place_market_entry(
            symbol, entry_side, size, product
        )

        # Wait for exchange position to appear.
        position = None
        for _ in range(8):
            time.sleep(1)
            position = open_position_for_symbol(symbol)
            if position:
                break

        if not position:
            execution_status[symbol] = (
                "ENTRY_SENT_POSITION_NOT_CONFIRMED"
            )
            logger.error(
                "[ENTRY UNCERTAIN] %s: check exchange manually before restart",
                symbol
            )
            return

        actual_size = abs(int(safe_float(position.get("size"), size)))
        entry_price = safe_float(position.get("entry_price"), price)

        # Protective SL first.
        try:
            sl_response = place_protective_order(
                symbol, product, exit_side, actual_size,
                stop_price, "stop_loss_order"
            )
        except Exception as sl_error:
            logger.critical(
                "[SL FAILED] %s; HALTING NEW ENTRIES. Error=%s",
                symbol, sl_error
            )
            execution_status[symbol] = "SL_FAILED_EMERGENCY"

            # Attempt emergency reduce-only market close.
            try:
                emergency_body = {
                    "product_id": int(product_id),
                    "product_symbol": symbol,
                    "size": actual_size,
                    "side": exit_side,
                    "order_type": "market_order",
                    "reduce_only": True
                }
                api_post("/v2/orders", emergency_body, auth=True)
                execution_status[symbol] = "SL_FAILED_EMERGENCY_CLOSE_SENT"
            except Exception as close_error:
                logger.critical(
                    "[EMERGENCY CLOSE FAILED] %s %s",
                    symbol, close_error
                )
                execution_status[symbol] = "CRITICAL_MANUAL_ACTION_REQUIRED"
            return

        # Verify protective stop appears in active orders.
        verified_sl = False
        for _ in range(5):
            time.sleep(1)
            active = active_orders_for_symbol(symbol)
            for o in active:
                stop_type = str(o.get("stop_order_type", "")).lower()
                reduce_only = o.get("reduce_only")
                if "stop_loss" in stop_type and reduce_only:
                    verified_sl = True
                    break
            if verified_sl:
                break

        if not verified_sl:
            logger.critical("[SL NOT VERIFIED] %s", symbol)
            execution_status[symbol] = "SL_NOT_VERIFIED"
            return

        # Place reduce-only take-profit.
        tp_response = None
        try:
            tp_response = place_protective_order(
                symbol, product, exit_side, actual_size,
                target_price, "take_profit_order"
            )
        except Exception as tp_error:
            logger.error("[TP ERROR] %s %s", symbol, tp_error)
            execution_status[symbol] = "SL_VERIFIED_TP_FAILED"

        trade = {
            "time": utc_now(),
            "symbol": symbol,
            "signal": signal,
            "entry_price": entry_price,
            "reference_price": price,
            "size": actual_size,
            "margin_estimate": estimated_margin,
            "notional_estimate": notional,
            "leverage_read_from_exchange": leverage,
            "atr": atr,
            "stop_loss": stop_price,
            "take_profit": target_price,
            "entry_order": entry_response,
            "sl_order": sl_response,
            "tp_order": tp_response,
            "status": "ENTRY_AND_SL_VERIFIED"
        }

        trade_history.append(trade)
        del trade_history[:-100]

        last_entry_candle[symbol] = candle_time
        execution_status[symbol] = "TRADE_OPEN_SL_VERIFIED"

        logger.warning(
            "[TRADE OPEN] %s %s entry=%s size=%s SL=%s TP=%s "
            "leverage=%sx margin_est=%s",
            symbol, signal, entry_price, actual_size,
            stop_price, target_price, leverage, estimated_margin
        )

    except Exception as exc:
        execution_status[symbol] = "EXECUTION_ERROR"
        logger.error("[EXECUTION ERROR] %s %s", symbol, exc)
        logger.debug(traceback.format_exc())
        add_error(f"Execution {symbol}: {exc}")


# ------------------------------------------------------------
# SIGNAL TRACKING
# ------------------------------------------------------------

def record_signal(diagnostic):
    symbol = diagnostic.get("symbol")
    signal = diagnostic.get("signal", "NONE")
    previous = active_signals.get(symbol)
    now = utc_now()

    if signal == "NONE":
        if previous:
            signal_history.append({
                "event": "END",
                "symbol": symbol,
                "signal": previous["signal"],
                "ended_at": now,
                "last_price": diagnostic.get("close")
            })
            active_signals.pop(symbol, None)
        return

    if not previous or previous.get("signal") != signal:
        active_signals[symbol] = {
            "symbol": symbol,
            "signal": signal,
            "first_seen": now,
            "last_seen": now,
            "scans": 1,
            "last_price": diagnostic.get("close"),
            "rsi": diagnostic.get("rsi"),
            "atr": diagnostic.get("atr")
        }
        signal_history.append({
            "event": "START",
            "symbol": symbol,
            "signal": signal,
            "time": now,
            "price": diagnostic.get("close"),
            "rsi": diagnostic.get("rsi"),
            "atr": diagnostic.get("atr")
        })
    else:
        previous["last_seen"] = now
        previous["scans"] = previous.get("scans", 0) + 1
        previous["last_price"] = diagnostic.get("close")

    del signal_history[:-SIGNAL_HISTORY_LIMIT]


# ------------------------------------------------------------
# PER SYMBOL SCANNER
# ------------------------------------------------------------

def scan_symbol(symbol):
    try:
        trend_frame = fetch_candles(
            symbol, TREND_RESOLUTION, TREND_CANDLE_COUNT
        )
        trend_data = analyze_trend(trend_frame)

        if trend_data.get("trend") == "NOT_READY":
            return {
                "symbol": symbol,
                "signal": "NONE",
                "trend": "NOT_READY",
                "reason": "TREND_CANDLES_INSUFFICIENT"
            }

        entry_frame = fetch_candles(
            symbol, ENTRY_RESOLUTION, ENTRY_CANDLE_COUNT
        )
        diagnostic = analyze_entry(symbol, trend_data, entry_frame)
        last_diagnostics[symbol] = diagnostic

        logger.info(
            "[SIGNAL] %s trend=%s signal=%s close=%s RSI=%s ATR=%s",
            symbol, diagnostic.get("trend"), diagnostic.get("signal"),
            diagnostic.get("close"), diagnostic.get("rsi"),
            diagnostic.get("atr")
        )

        record_signal(diagnostic)

        if diagnostic.get("signal") in ("BUY", "SELL"):
            execute_signal(diagnostic)

        return diagnostic

    except Exception as exc:
        error_text = f"{symbol}: {type(exc).__name__}: {exc}"
        logger.error("[SCAN BLOCKED] %s", error_text)
        add_error(error_text)

        diagnostic = {
            "symbol": symbol,
            "time": utc_now(),
            "signal": "NONE",
            "trend": "DATA_INVALID",
            "reason": error_text
        }
        last_diagnostics[symbol] = diagnostic
        record_signal(diagnostic)
        return diagnostic


# ------------------------------------------------------------
# POSITION / ORDER MONITOR
# ------------------------------------------------------------

def monitor_positions():
    try:
        positions = get_positions()
        orders = get_open_orders()

        open_symbols = set()
        for p in positions:
            size = safe_float(p.get("size"), 0)
            if size:
                open_symbols.add(
                    str(p.get("product_symbol", p.get("symbol", ""))).upper()
                )

        # Cancel residual protective orders only when position is closed.
        for symbol in list(products.keys()):
            active = active_orders_for_symbol(symbol, orders)
            if active and symbol not in open_symbols:
                logger.info("[POSITION CLOSED] %s - cancelling residual orders", symbol)
                for o in active:
                    oid = o.get("id")
                    if oid:
                        try:
                            cancel_order(oid)
                        except Exception as exc:
                            logger.error("[RESIDUAL CANCEL ERROR] %s %s", symbol, exc)

        return positions, orders

    except Exception as exc:
        logger.error("[MONITOR ERROR] %s", exc)
        add_error(f"Monitor: {exc}")
        return [], []


# ------------------------------------------------------------
# STATE SAVE / LOAD
# ------------------------------------------------------------

def save_state():
    try:
        data = {
            "active_signals": active_signals,
            "signal_history": signal_history[-SIGNAL_HISTORY_LIMIT:],
            "scan_count": scan_count,
            "symbols": symbols,
            "last_entry_candle": last_entry_candle,
            "trade_history": trade_history[-100:]
        }
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as exc:
        logger.error("[SAVE STATE ERROR] %s", exc)


def load_state():
    global active_signals, signal_history, scan_count, last_entry_candle, trade_history

    try:
        if not os.path.exists(STATE_FILE):
            return

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        active_signals = data.get("active_signals", {})
        signal_history = data.get("signal_history", [])[-SIGNAL_HISTORY_LIMIT:]
        scan_count = data.get("scan_count", 0)
        last_entry_candle = data.get("last_entry_candle", {})
        trade_history = data.get("trade_history", [])[-100:]

        logger.info("[STATE RESTORED] scan_count=%s", scan_count)

    except Exception as exc:
        logger.error("[LOAD STATE ERROR] %s", exc)


# ------------------------------------------------------------
# MAIN SCANNER LOOP
# ------------------------------------------------------------

def scanner_loop():
    global scan_count, last_scan_time, last_scan_duration

    logger.warning("TESTNET AUTO BOT STARTED")
    logger.warning("AUTO_TRADING=%s MARGIN=$%s MAX_POSITIONS=%s",
                   AUTO_TRADING, MARGIN_USD, MAX_POSITIONS)
    logger.warning("BASE URL=%s", BASE_URL)
    logger.warning("Leverage is read from exchange; bot will not modify it.")

    while True:
        started = time.time()

        try:
            current_symbols = discover_symbols()

            buys = sells = 0

            for symbol in current_symbols:
                d = scan_symbol(symbol)
                if d.get("signal") == "BUY":
                    buys += 1
                elif d.get("signal") == "SELL":
                    sells += 1

            monitor_positions()

            scan_count += 1
            last_scan_time = utc_now()
            last_scan_duration = round(time.time() - started, 2)

            logger.info(
                "[SCAN COMPLETE] symbols=%s BUY=%s SELL=%s "
                "active_signals=%s duration=%ss",
                len(current_symbols), buys, sells,
                len(active_signals), last_scan_duration
            )

            save_state()

        except Exception as exc:
            logger.exception("[SCANNER LOOP ERROR] %s", exc)
            add_error(str(exc))

        time.sleep(max(1, SCAN_INTERVAL - (time.time() - started)))


# ------------------------------------------------------------
# DASHBOARD
# ------------------------------------------------------------

@app.route("/")
def home():
    return Response("""
<!DOCTYPE html>
<html>
<head>
<title>Delta Testnet Auto Bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{background:#10151c;color:#e7edf5;font-family:Arial;padding:15px}
h1{color:#64d8a4}
.card{background:#1b2530;padding:15px;margin:10px 0;border-radius:10px}
pre{white-space:pre-wrap;overflow-wrap:anywhere}
a{color:#6ec9ff}
</style>
</head>
<body>
<h1>DELTA INDIA TESTNET</h1>
<h2>Auto Trading Bot</h2>
<div class="card">
<b>TESTNET ONLY</b><br>
Margin per trade: $20<br>
Max positions: 10<br>
Leverage: Exchange setting (read only)<br>
SL: ATR × 1.2<br>
TP: ATR × 2.0
</div>
<div class="card">
<a href="/health">Health</a> |
<a href="/dashboard-data">Dashboard JSON</a> |
<a href="/diagnostics">Diagnostics</a> |
<a href="/positions">Positions</a>
</div>
<div class="card">
<pre id="data">Loading...</pre>
</div>
<script>
async function refresh(){
 try{
  const r=await fetch('/dashboard-data');
  const d=await r.json();
  document.getElementById('data').textContent=JSON.stringify(d,null,2);
 }catch(e){
  document.getElementById('data').textContent='Dashboard error';
 }
}
refresh();setInterval(refresh,10000);
</script>
</body></html>
""", mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_status": "RUNNING",
        "exchange": "DELTA INDIA TESTNET",
        "auto_trading": AUTO_TRADING,
        "margin_per_trade": MARGIN_USD,
        "max_positions": MAX_POSITIONS,
        "scan_count": scan_count,
        "symbols": len(symbols),
        "last_scan": last_scan_time,
        "last_scan_duration": last_scan_duration,
        "time": utc_now()
    })


@app.route("/dashboard-data")
def dashboard_data():
    return jsonify({
        "bot_status": "RUNNING",
        "exchange": "DELTA INDIA TESTNET",
        "auto_trading": AUTO_TRADING,
        "margin_per_trade_usd": MARGIN_USD,
        "max_positions": MAX_POSITIONS,
        "leverage_policy": "READ FROM EXCHANGE - NEVER CHANGED",
        "sl_atr_multiplier": SL_ATR_MULTIPLIER,
        "tp_atr_multiplier": TP_ATR_MULTIPLIER,
        "symbols": symbols,
        "total_symbols": len(symbols),
        "scan_count": scan_count,
        "last_scan": last_scan_time,
        "last_scan_duration": last_scan_duration,
        "active_signals": active_signals,
        "signal_history": signal_history[-SIGNAL_HISTORY_LIMIT:],
        "diagnostics": last_diagnostics,
        "data_quarantine": data_quarantine,
        "execution_status": execution_status,
        "trade_history": trade_history[-100:],
        "errors": errors[-20:]
    })


@app.route("/diagnostics")
def diagnostics():
    return jsonify({
        "diagnostics": last_diagnostics,
        "data_quarantine": data_quarantine,
        "execution_status": execution_status,
        "errors": errors[-20:],
        "time": utc_now()
    })


@app.route("/positions")
def positions_endpoint():
    if not API_KEY or not API_SECRET:
        return jsonify({"error": "API credentials missing"}), 503

    try:
        return jsonify({
            "positions": get_positions(),
            "active_orders": get_open_orders(),
            "time": utc_now()
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ------------------------------------------------------------
# STARTUP
# ------------------------------------------------------------

def startup_checks():
    if BASE_URL != "https://cdn-ind.testnet.deltaex.org":
        raise RuntimeError("Refusing to start: non-testnet URL")

    if AUTO_TRADING and (not API_KEY or not API_SECRET):
        logger.error(
            "AUTO TRADING BLOCKED: set DELTA_API_KEY and DELTA_API_SECRET"
        )
        return False

    if AUTO_TRADING:
        # Verify authenticated connection before execution starts.
        get_positions()
        get_open_orders()
        logger.warning("AUTHENTICATED TESTNET ACCOUNT CHECK OK")

    return True


def start_bot():
    load_state()

    try:
        startup_checks()
    except Exception as exc:
        logger.critical("[STARTUP CHECK FAILED] %s", exc)
        add_error(f"Startup check failed: {exc}")

    thread = threading.Thread(
        target=scanner_loop,
        daemon=True,
        name="delta-auto-scanner"
    )
    thread.start()


if __name__ == "__main__":
    start_bot()
    port = int(os.getenv("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
