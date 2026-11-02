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
# DELTA EXCHANGE INDIA - TESTNET ONLY
# =====================================================
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINS = ["BTCUSD", "ETHUSD", "SOLUSD", "XAUTUSD", "PAXGUSD", "SILVERUSD"]
TIMEFRAME = "5m"
ORDER_SIZE = 40
ORDER_SIZES = {
    "BTCUSD": 40, "ETHUSD": 40, "SOLUSD": 40,
    "XAUTUSD": 200, "PAXGUSD": 200, "SILVERUSD": 200,
}
MAX_POSITIONS = 6
ENABLE_TRADING = True
LEVERAGE = 50  # Informational setting; this script does not change account leverage.
TRAIL_PCT = 0.003
STOP_TRIGGER_METHOD = "last_traded_price"  # Matches the active SILVERUSD stop
TRAIL_ACTIVATE_PROFIT_USD = 5.0  # NET estimated PnL after entry + projected exit fees

# Observed fee ratios from the user's uploaded Delta Testnet order-history CSV.
# These are conservative estimates, not exchange-guaranteed fee schedules.
# Re-check if Delta changes fees or the CSV's fee calculation changes.
ESTIMATED_FEE_RATE = {
    "BTCUSD": 0.05015, "ETHUSD": 0.05015, "SOLUSD": 0.05015,
    "XAUTUSD": 0.01003, "PAXGUSD": 0.01003, "SILVERUSD": 0.01003,
}
# Contract multipliers inferred from the same CSV's Order Value / (Qty * Exec.Price).
# Used only to estimate USD notional and fee drag for net-profit protection.
CONTRACT_MULTIPLIER = {
    "BTCUSD": 0.001, "ETHUSD": 0.01, "SOLUSD": 1.0,
    "XAUTUSD": 0.001, "PAXGUSD": 0.001, "SILVERUSD": 0.1,
}
COOLDOWN_SECONDS = 300
CHECK_INTERVAL = 30
STATE_FILE = "state.json"
MIN_STOP_GAP_PCT = 0.0005
MAX_CANDLE_LIVE_GAP_PCT = 0.005
MAX_TRADE_AGE_SECONDS = 30
ADX_PERIOD = 14
MIN_ADX = 25.0
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 100
RSI_PERIOD = 14
VOLUME_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
CANDLE_LOOKBACK_SECONDS = 900 * 60

# =====================================================
# APP / LOGGING
# =====================================================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

def log(message):
    print(message, flush=True)

@app.route("/")
def home():
    return ("DELTA TESTNET AUTO BOT | $5 PROFIT TRAIL ACTIVATION | "
            "EMA + RSI + ADX + MACD + VOLUME")

@app.route("/health")
def health():
    return jsonify({
        "status": "ok", "bot": "running", "environment": "TESTNET",
        "automatic_trading": ENABLE_TRADING, "max_positions": MAX_POSITIONS,
        "trail_activation_profit_usd": TRAIL_ACTIVATE_PROFIT_USD,
        "trail_distance_pct": TRAIL_PCT * 100
    }), 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

# =====================================================
# STATE
# =====================================================
def load_state():
    default = {
        "peaks": {}, "stops": {}, "last_trade": {}, "last_candle": {},
        "trail_active": {}
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
# The previous bot activated trailing on gross PnL. Do not carry that activation
# into the new fee-aware NET-PnL rule after an upgrade/restart.
FEE_MODEL_VERSION = "csv_fee_estimate_v1"
if state.get("fee_model_version") != FEE_MODEL_VERSION:
    state["trail_active"] = {}
    state["fee_model_version"] = FEE_MODEL_VERSION
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
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {
        "api-key": API_KEY, "timestamp": timestamp, "signature": signature,
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "python-delta-testnet-bot",
    }
    try:
        response = session.request(
            method, BASE_URL + path + query_string,
            headers=headers, data=payload or None, timeout=(5, 15)
        )
        try:
            result = response.json()
        except Exception:
            result = {"error": response.text[:500]}
        return result, response.status_code
    except Exception as e:
        return {"error": str(e)}, 500

# =====================================================
# MARKET PRICE / CANDLES
# =====================================================
def _parse_market_timestamp(raw_ts):
    """Convert Delta seconds/ms/us or ISO timestamp to Unix seconds."""
    if raw_ts in (None, ""):
        return 0.0
    try:
        ts = float(raw_ts)
        if ts > 1e14:
            ts /= 1_000_000
        elif ts > 1e11:
            ts /= 1_000
        return ts
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            return datetime.fromisoformat(
                str(raw_ts).replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return 0.0


def _fresh_price(price_raw, raw_ts, symbol, source):
    try:
        price = float(price_raw or 0)
    except (TypeError, ValueError):
        return None
    ts = _parse_market_timestamp(raw_ts)
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
    """Return a fresh LAST traded price, never mark_price.

    The exchange stop shown in the user's account is triggered by Last Price.
    If no fresh trade/ticker close is available, return None so trailing and
    new entries pause while the exchange-side protective stop remains intact.
    """
    # First choice: latest actual execution from the public trades endpoint.
    try:
        r = session.get(
            BASE_URL + f"/v2/trades/{symbol}",
            headers={"Accept": "application/json"}, timeout=(5, 10)
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("success") is True:
                result = data.get("result", {})
                trades = result.get("trades", []) if isinstance(result, dict) else (
                    result if isinstance(result, list) else []
                )
                if trades:
                    latest = max(
                        trades,
                        key=lambda t: _parse_market_timestamp(
                            t.get("timestamp", t.get("t", t.get("created_at")))
                        )
                    )
                    price = _fresh_price(
                        latest.get("price", latest.get("p")),
                        latest.get("timestamp", latest.get("t", latest.get("created_at"))),
                        symbol, "recent_trade"
                    )
                    if price is not None:
                        return price
        else:
            log(f"[TRADE PRICE HTTP] {symbol} HTTP={r.status_code}")
    except Exception as e:
        log(f"[TRADE PRICE ERROR] {symbol}: {e}")

    # Fallback: ticker close is the last traded/closing price. Do not use
    # mark_price here because the active exchange stop is set to Last Price.
    try:
        r = session.get(
            BASE_URL + f"/v2/tickers/{symbol}",
            headers={"Accept": "application/json"}, timeout=(5, 10)
        )
        if r.status_code != 200:
            log(f"[TICKER PRICE ERROR] {symbol} HTTP={r.status_code}")
            return None
        data = r.json()
        if data.get("success") is not True:
            log(f"[TICKER PRICE ERROR] {symbol}: {data}")
            return None
        result = data.get("result", {})
        if isinstance(result, list):
            result = next(
                (item for item in result if item.get("symbol") == symbol),
                result[0] if result else {}
            )
        if not isinstance(result, dict) or not result:
            log(f"[TICKER EMPTY] {symbol}: {data}")
            return None
        close_price = result.get("close")
        if close_price in (None, "", 0, "0"):
            log(f"[NO FRESH LAST PRICE] {symbol}; mark_price intentionally ignored")
            return None
        return _fresh_price(
            close_price, result.get("timestamp"), symbol, "ticker_close"
        )
    except Exception as e:
        log(f"[TICKER PRICE ERROR] {symbol}: {e}")
        return None

def get_candles(symbol):
    end = int(time.time())
    params = {
        "resolution": TIMEFRAME, "symbol": symbol,
        "start": str(end - CANDLE_LOOKBACK_SECONDS), "end": str(end)
    }
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and isinstance(res, dict) and res.get("success") and res.get("result"):
        return res["result"]
    log(f"[CANDLE ERROR] {symbol} HTTP={code} {res}")
    return []

def candles_to_df(candles):
    try:
        if not candles:
            return None
        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles).copy()
        else:
            df = pd.DataFrame(
                candles, columns=["time", "open", "high", "low", "close", "volume"]
            )
        required = ["high", "low", "close", "volume"]
        if not all(c in df.columns for c in required):
            log(f"[CANDLE INVALID] Missing columns: {list(df.columns)}")
            return None
        for c in required:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        if "time" not in df.columns:
            log("[CANDLE INVALID] Timestamp column missing")
            return None
        df["time"] = pd.to_numeric(df["time"], errors="coerce")
        df = df.dropna(subset=["time"]).copy()
        if df.empty:
            return None

        def normalize_ts(value):
            value = float(value)
            if value > 1e14:
                value /= 1_000_000
            elif value > 1e11:
                value /= 1_000
            return int(value)

        df["time"] = df["time"].apply(normalize_ts)
        df = df.sort_values("time").drop_duplicates("time")
        now = int(time.time())
        df = df[df["time"] + 300 <= now]
        df = df.dropna(subset=required).reset_index(drop=True)
        log(
            f"[CANDLE CHECK] rows={len(df)} "
            f"last_time={int(df['time'].iloc[-1]) if len(df) else 'NONE'} now={now}"
        )
        if len(df) < 120:
            log(f"[CANDLE INSUFFICIENT] Only {len(df)} closed candles")
            return None
        return df
    except Exception as e:
        log(f"[CANDLE PARSE ERROR] {e}")
        return None

# =====================================================
# INDICATORS / SIGNAL
# =====================================================
def add_indicators(df):
    df = df.copy()
    close, high, low = df["close"], df["high"], df["low"]
    df["ema9"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema21"] = close.ewm(span=EMA_SLOW, adjust=False).mean()
    df["ema100"] = close.ewm(span=EMA_TREND, adjust=False).mean()

    delta = close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    df.loc[(avg_gain == 0) & (avg_loss > 0), "rsi"] = 0
    df.loc[(avg_gain == 0) & (avg_loss == 0), "rsi"] = 50

    df["vol_avg"] = df["volume"].rolling(VOLUME_PERIOD).mean()
    ema12 = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = close.ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()

    up_move, down_move = high.diff(), -low.diff()
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm.loc[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm.loc[(down_move > up_move) & (down_move > 0)] = down_move
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
    plus_s = plus_dm.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
    minus_s = minus_dm.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
    plus_di = 100 * plus_s / atr.replace(0, float("nan"))
    minus_di = 100 * minus_s / atr.replace(0, float("nan"))
    di_sum = (plus_di + minus_di).replace(0, float("nan"))
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    df["adx"] = dx.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
    df["atr"] = atr
    return df

def check_signal(candles, symbol):
    df = candles_to_df(candles)
    if df is None:
        log(f"[SCAN] {symbol} insufficient/invalid/unfinished candle data")
        return None, None
    try:
        df = add_indicators(df)
        last = df.iloc[-1]
        fields = [
            "ema9", "ema21", "ema100", "rsi", "vol_avg",
            "adx", "macd", "macd_signal", "atr"
        ]
        if any(pd.isna(last[x]) for x in fields):
            log(f"[SCAN] {symbol} indicator data not ready")
            return None, None
        close_px = float(last["close"])
        ema9, ema21, ema100 = (
            float(last["ema9"]), float(last["ema21"]), float(last["ema100"])
        )
        rsi = float(last["rsi"])
        vol, vol_avg, adx = float(last["volume"]), float(last["vol_avg"]), float(last["adx"])
        macd, macd_sig = float(last["macd"]), float(last["macd_signal"])
        candle_id = int(last["time"])
        log(
            f"[SCAN] {symbol} Candle={candle_id} Close={close_px:.4f} "
            f"EMA9={ema9:.4f} EMA21={ema21:.4f} EMA100={ema100:.4f} "
            f"RSI={rsi:.1f} VOL={vol:.0f}/{vol_avg:.0f} ADX={adx:.1f} "
            f"MACD={macd:.5f}/{macd_sig:.5f}"
        )
        if adx < MIN_ADX:
            log(f"[NO TRADE] {symbol} ADX={adx:.1f} BELOW {MIN_ADX}")
            return None, candle_id
        buy = (
            ema9 > ema21 and close_px > ema100 and 45 <= rsi <= 70
            and macd > macd_sig and vol > vol_avg * 0.6
        )
        sell = (
            ema9 < ema21 and close_px < ema100 and 30 <= rsi <= 55
            and macd < macd_sig and vol > vol_avg * 0.6
        )
        if buy:
            log(f"[SIGNAL BUY] {symbol} ADX={adx:.1f}")
            return "buy", candle_id
        if sell:
            log(f"[SIGNAL SELL] {symbol} ADX={adx:.1f}")
            return "sell", candle_id
        log(f"[NO TRADE] {symbol} filters not satisfied")
        return None, candle_id
    except Exception as e:
        log(f"[SIGNAL ERROR] {symbol}: {e}")
        return None, None

# =====================================================
# POSITIONS / ORDERS
# =====================================================
def get_pos():
    res, code = api_req("GET", "/v2/positions/margined")
    if code != 200 or not isinstance(res, dict) or res.get("success") is not True:
        log(f"[POSITION ERROR] HTTP={code} {res}")
        return None
    result = res.get("result", [])
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return None
    return [p for p in result if float(p.get("size", 0) or 0) != 0]

def parse_pos_symbol(p):
    product = p.get("product") or {}
    return p.get("product_symbol") or product.get("symbol") or p.get("symbol") or ""

def get_orders():
    res, code = api_req(
        "GET", "/v2/orders",
        params={"states": "open,pending", "page_size": 100}
    )
    if code == 200 and isinstance(res, dict) and res.get("success") is True:
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
        and order.get("reduce_only") in (True, 1, "true", "True")
    )

def active_stop_orders(symbol, orders=None):
    if orders is None:
        orders = get_orders()
    if orders is None:
        return None
    return [o for o in orders if is_active_stop(o, symbol)]

def verify_stop(symbol, order_id):
    orders = get_orders()
    if orders is None:
        return False
    return any(
        str(o.get("id")) == str(order_id) and is_active_stop(o, symbol)
        for o in orders
    )

def cancel_stop(order):
    order_id, product_id = order.get("id"), order.get("product_id")
    if not order_id or not product_id:
        log("[CANCEL ERROR] Order ID/product ID missing")
        return False
    res, code = api_req(
        "DELETE", "/v2/orders",
        body={"id": int(order_id), "product_id": int(product_id)}
    )
    if code != 200 or not isinstance(res, dict) or not res.get("success"):
        log(f"[CANCEL FAILED] ID={order_id} HTTP={code} {res}")
        return False

    # Verify cancellation by checking open/pending orders again.
    time.sleep(0.7)
    orders = get_orders()
    if orders is None:
        log(f"[CANCEL UNVERIFIED] ID={order_id}")
        return False
    still_open = any(str(o.get("id")) == str(order_id) for o in orders)
    if still_open:
        log(f"[CANCEL NOT CONFIRMED] ID={order_id}")
        return False
    log(f"[CANCEL VERIFIED] ID={order_id}")
    return True

def valid_stop_price(position_side, stop_price, live_price):
    if live_price is None or live_price <= 0 or stop_price is None or stop_price <= 0:
        return False
    gap = live_price * MIN_STOP_GAP_PCT
    if position_side == "buy":
        return stop_price < live_price - gap
    if position_side == "sell":
        return stop_price > live_price + gap
    return False

def place_stop(symbol, position_side, size, stop_price):
    sz = int(abs(float(size)))
    if sz <= 0 or stop_price <= 0:
        log("[STOP ERROR] Invalid size or price")
        return False
    live = get_live_price(symbol)
    if live is None or not valid_stop_price(position_side, stop_price, live):
        log(
            f"[STOP BLOCKED] {symbol} SIDE={position_side} "
            f"STOP={stop_price:.4f} LAST={live} wrong-side/too-close"
        )
        return False

    stop_side = "sell" if position_side == "buy" else "buy"
    body = {
        "product_symbol": symbol, "size": sz, "side": stop_side,
        "order_type": "market_order", "stop_order_type": "stop_loss_order",
        "stop_price": f"{float(stop_price):.2f}", "reduce_only": True,
        "stop_trigger_method": STOP_TRIGGER_METHOD
    }
    res, code = api_req("POST", "/v2/orders", body=body)
    result = res.get("result", {}) if isinstance(res, dict) else {}
    if code != 200 or res.get("success") is not True or not result.get("id"):
        log(f"[STOP FAILED] {symbol} HTTP={code} {res}")
        return False

    order_id = result["id"]
    time.sleep(1)
    if not verify_stop(symbol, order_id):
        log(f"[STOP NOT VERIFIED] {symbol} ID={order_id}; existing stop retained")
        return False

    # Record the new verified stop before attempting to cancel older stops.
    state["stops"][symbol] = float(stop_price)
    state["stop_order_ids"] = state.get("stop_order_ids", {})
    state["stop_order_ids"][symbol] = str(order_id)
    save_state()
    log(f"[STOP VERIFIED] {symbol} {stop_side.upper()} STOP={stop_price:.4f} ID={order_id}")

    orders = get_orders()
    if orders is None:
        log(f"[OLD STOP RETAINED] {symbol}: cannot verify order list")
        return True

    for old in orders:
        if is_active_stop(old, symbol) and str(old.get("id")) != str(order_id):
            if not cancel_stop(old):
                log(f"[OLD STOP NOT CANCELLED] {symbol} ID={old.get('id')}")
    return True

# =====================================================
# PROFIT-TRIGGERED TRAILING STOP
# =====================================================
def position_unrealized_pnl(position):
    """Gross unrealized PnL in USD as reported by Delta's position API."""
    for key in ("unrealized_pnl", "unrealizedPnl"):
        value = position.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def estimate_round_trip_fees(symbol, size, entry_price, exit_price):
    """Estimate entry + exit fees using observed CSV fee rates and contract multipliers."""
    rate = ESTIMATED_FEE_RATE.get(symbol)
    multiplier = CONTRACT_MULTIPLIER.get(symbol)
    if rate is None or multiplier is None or entry_price <= 0 or exit_price <= 0:
        return None
    qty = abs(float(size))
    entry_notional = qty * entry_price * multiplier
    exit_notional = qty * exit_price * multiplier
    return (entry_notional + exit_notional) * rate


def stop_price_for_min_net_profit(symbol, side, size, entry, min_net_profit):
    """Stop level estimated to retain at least min_net_profit after both-side fees."""
    rate = ESTIMATED_FEE_RATE.get(symbol)
    multiplier = CONTRACT_MULTIPLIER.get(symbol)
    qty = abs(float(size))
    if not rate or not multiplier or qty <= 0 or entry <= 0:
        return None
    exposure = qty * multiplier
    entry_fee = entry * exposure * rate
    if side == "buy":
        # (stop-entry)*exposure - entry fee - stop*exposure*fee >= target
        denom = exposure * (1.0 - rate)
        if denom <= 0:
            return None
        return (entry * exposure + entry_fee + min_net_profit) / denom
    # (entry-stop)*exposure - entry fee - stop*exposure*fee >= target
    denom = exposure * (1.0 + rate)
    return (entry * exposure - entry_fee - min_net_profit) / denom

def recover_stop_state(symbol, position_side, orders):
    """Recover the exchange's active stop after a Render restart."""
    stops = [o for o in (orders or []) if is_active_stop(o, symbol)]
    if not stops:
        return float(state["stops"].get(symbol, 0) or 0), False

    parsed = []
    for order in stops:
        try:
            parsed.append((float(order.get("stop_price")), order))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return float(state["stops"].get(symbol, 0) or 0), False

    # For LONG keep the highest protective stop; for SHORT the lowest.
    chosen_price, chosen_order = (
        max(parsed, key=lambda x: x[0]) if position_side == "buy"
        else min(parsed, key=lambda x: x[0])
    )
    state["stops"][symbol] = chosen_price
    state.setdefault("stop_order_ids", {})[symbol] = str(chosen_order.get("id", ""))
    # An exchange-side protective stop indicates trailing/stop management
    # was already initialized before a process restart.
    state["trail_active"][symbol] = bool(state["trail_active"].get(symbol, False))
    return chosen_price, True

def handle_trailing():
    positions = get_pos()
    if positions is None:
        log("[TRAIL BLOCKED] Position API unavailable")
        return

    orders = get_orders()
    if orders is None:
        log("[TRAIL BLOCKED] Cannot read open orders; leaving stops untouched")
        return

    active_symbols = set()
    for p in positions:
        try:
            size = float(p.get("size", 0) or 0)
            if not size:
                continue
            symbol = parse_pos_symbol(p)
            if symbol not in COINS:
                continue
            active_symbols.add(symbol)

            entry = float(p.get("entry_price") or p.get("average_price") or 0)
            if entry <= 0:
                log(f"[TRAIL ERROR] {symbol} entry missing")
                continue

            curr = get_live_price(symbol)
            if curr is None:
                log(f"[TRAIL SKIP] {symbol} stale/unavailable live price")
                continue

            gross_pnl = position_unrealized_pnl(p)
            if gross_pnl is None:
                log(f"[PROFIT DATA MISSING] {symbol}; cannot activate net $5 trail safely")
                continue

            estimated_fees = estimate_round_trip_fees(symbol, size, entry, curr)
            if estimated_fees is None:
                log(f"[FEE ESTIMATE MISSING] {symbol}; trailing activation blocked")
                continue
            net_pnl = gross_pnl - estimated_fees
            side = "buy" if size > 0 else "sell"
            log(
                f"[NET PNL CHECK] {symbol} GROSS=${gross_pnl:.2f} "
                f"EST_ROUND_TRIP_FEES=${estimated_fees:.2f} NET_EST=${net_pnl:.2f}"
            )
            old_stop, exchange_stop_found = recover_stop_state(symbol, side, orders)

            # Rebuild peak conservatively after restart. Never discard an
            # exchange-side active stop. Do not lower a previously stored peak.
            old_peak = float(state["peaks"].get(symbol, entry) or entry)
            if side == "buy":
                peak = max(old_peak, entry, curr)
            else:
                peak = min(old_peak, entry, curr)

            was_active = bool(state["trail_active"].get(symbol, False))
            if not was_active and net_pnl >= TRAIL_ACTIVATE_PROFIT_USD:
                state["trail_active"][symbol] = True
                was_active = True
                log(
                    f"[TRAIL ACTIVATED] {symbol} NET_EST=${net_pnl:.2f} "
                    f">= ${TRAIL_ACTIVATE_PROFIT_USD:.2f}"
                )

            state["peaks"][symbol] = peak

            # Before +$5, retain the initial protective stop; do not trail.
            if not was_active:
                log(
                    f"[WAIT NET $5] {symbol} NET_EST=${net_pnl:.2f} "
                    f"TARGET=${TRAIL_ACTIVATE_PROFIT_USD:.2f} "
                    f"STOP={old_stop or 'not recorded'}"
                )
                save_state()
                continue

            candidate = (
                peak * (1 - TRAIL_PCT) if side == "buy"
                else peak * (1 + TRAIL_PCT)
            )

            # Once net +$5 is reached, combine the 0.30% trailing stop with
            # a fee-aware minimum-profit stop. This is an estimate; slippage,
            # gaps and fee changes can still reduce the realised result.
            profit_floor = stop_price_for_min_net_profit(
                symbol, side, size, entry, TRAIL_ACTIVATE_PROFIT_USD
            )
            if profit_floor is None:
                log(f"[NET LOCK BLOCKED] {symbol}; fee/contract model unavailable")
                continue
            if side == "buy":
                candidate = max(candidate, profit_floor)
            else:
                candidate = min(candidate, profit_floor)

            # Enforce monotonic protection: a LONG stop can only rise;
            # a SHORT stop can only fall. Never reset an old stop to zero
            # just because price has crossed it; let exchange order state decide.
            if old_stop > 0:
                candidate = max(candidate, old_stop) if side == "buy" else min(candidate, old_stop)

            if not valid_stop_price(side, candidate, curr):
                log(
                    f"[TRAIL WAIT] {symbol} candidate={candidate:.4f} "
                    f"live={curr:.4f}; existing stop retained"
                )
                save_state()
                continue

            improvement = (
                old_stop <= 0 or candidate > old_stop
                if side == "buy"
                else old_stop <= 0 or candidate < old_stop
            )
            log(
                f"[TRAIL] {symbol} SIDE={side.upper()} NET_EST=${net_pnl:.2f} "
                f"PEAK={peak:.4f} OLD_STOP={old_stop:.4f} "
                f"NEW_STOP={candidate:.4f}"
            )
            if improvement:
                place_stop(symbol, side, size, candidate)
            save_state()

        except Exception as e:
            log(f"[TRAIL ERROR] {p.get('product_symbol', '?')}: {e}")

    # Clear only local state for symbols no longer in open positions.
    # Exchange orders are not cancelled here; manual/exchange review is safer.
    for symbol in list(state["peaks"].keys()):
        if symbol in COINS and symbol not in active_symbols:
            state["peaks"].pop(symbol, None)
            state["stops"].pop(symbol, None)
            state.get("trail_active", {}).pop(symbol, None)
            state.get("stop_order_ids", {}).pop(symbol, None)
    save_state()

# =====================================================
# INITIAL PROTECTIVE STOP AFTER ENTRY
# =====================================================
def initialize_new_position(symbol):
    for attempt in range(6):
        time.sleep(2)
        positions = get_pos()
        if positions is None:
            log(f"[INITIAL STOP BLOCKED] {symbol} position API error")
            continue
        position = next((p for p in positions if parse_pos_symbol(p) == symbol), None)
        if position is None:
            continue
        size = float(position.get("size", 0) or 0)
        entry = float(position.get("entry_price") or position.get("average_price") or 0)
        curr = get_live_price(symbol)
        if not size or entry <= 0 or curr is None:
            continue

        if size > 0:
            stop_price = curr * (1 - TRAIL_PCT)
            state["peaks"][symbol] = max(entry, curr)
            side = "buy"
        else:
            stop_price = curr * (1 + TRAIL_PCT)
            state["peaks"][symbol] = min(entry, curr)
            side = "sell"

        state["trail_active"][symbol] = False
        save_state()
        log(
            f"[INITIAL PROTECTIVE STOP] {symbol} ENTRY={entry:.4f} "
            f"LIVE={curr:.4f} STOP={stop_price:.4f}; trail waits for +$5"
        )
        return place_stop(symbol, side, size, stop_price)

    log(f"[INITIAL STOP FAILED] {symbol} position not confirmed; check Testnet immediately")
    return False

# =====================================================
# ENTRY GUARDS / ORDER
# =====================================================
def price_mismatch_ok(symbol, candle_close):
    live = get_live_price(symbol)
    if live is None or candle_close <= 0:
        log(f"[ENTRY BLOCKED] {symbol} live/candle price unavailable or stale")
        return False
    gap_pct = abs(live - candle_close) / candle_close
    log(
        f"[PRICE CHECK] {symbol} CANDLE={candle_close:.4f} LIVE={live:.4f} "
        f"GAP={gap_pct*100:.3f}% LIMIT={MAX_CANDLE_LIVE_GAP_PCT*100:.2f}%"
    )
    if gap_pct > MAX_CANDLE_LIVE_GAP_PCT:
        log(f"[ENTRY BLOCKED] {symbol} candle/live mismatch {gap_pct*100:.3f}%")
        return False
    return True

def place_entry(symbol, side):
    body = {
        "product_symbol": symbol,
        "size": ORDER_SIZES.get(symbol, ORDER_SIZE),
        "side": side,
        "order_type": "market_order"
    }
    res, code = api_req("POST", "/v2/orders", body=body)
    result = res.get("result", {}) if isinstance(res, dict) else {}
    if code == 200 and isinstance(res, dict) and res.get("success") is True:
        log(
            f"[ENTRY ACCEPTED] {symbol} {side.upper()} "
            f"SIZE={ORDER_SIZES.get(symbol, ORDER_SIZE)} RESULT={result}"
        )
        return True
    log(f"[ENTRY FAILED] {symbol} HTTP={code} {res}")
    return False

# =====================================================
# MAIN LOOP
# =====================================================
def main_loop():
    log("==================================================")
    log("DELTA EXCHANGE INDIA TESTNET AUTO BOT STARTED")
    log(f"AUTO TRADING = {'ON' if ENABLE_TRADING else 'OFF'}")
    log(f"LEVERAGE SETTING = {LEVERAGE}x (not changed by this script)")
    log("EMA 9/21/100 + RSI14 + ADX14 + MACD(12,26,9) + VOLUME MA20")
    log(f"TRAIL PRICE SOURCE = LAST TRADED PRICE | STOP TRIGGER = {STOP_TRIGGER_METHOD}")
    log("NET PROFIT MODEL = observed CSV fee rates + inferred contract multipliers (estimate)")
    log(f"ESTIMATED FEE RATES = {ESTIMATED_FEE_RATE}")
    log(f"ADX FILTER = {MIN_ADX} | PRICE GAP LIMIT = {MAX_CANDLE_LIVE_GAP_PCT*100:.2f}%")
    log(
        f"INITIAL STOP DISTANCE = {TRAIL_PCT*100:.2f}% | "
        f"TRAIL ACTIVATION = ${TRAIL_ACTIVATE_PROFIT_USD:.2f} "
        f"| TRAIL DISTANCE = {TRAIL_PCT*100:.2f}%"
    )
    log(f"MAX POSITIONS = {MAX_POSITIONS} | COINS = {COINS}")
    log("==================================================")

    while True:
        try:
            handle_trailing()
            positions = get_pos()
            if positions is None:
                log("[SCAN BLOCKED] Position API unavailable")
                time.sleep(CHECK_INTERVAL)
                continue

            held = list({
                parse_pos_symbol(p) for p in positions
                if parse_pos_symbol(p) in COINS
            })
            log(f"[POS HELD] {held} TOTAL={len(held)}")

            if not ENABLE_TRADING:
                log("[SAFE MODE] New entries disabled; protective stop management remains active")
                time.sleep(CHECK_INTERVAL)
                continue

            if len(held) >= MAX_POSITIONS:
                log("[MAX POSITIONS] No new entry")
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol in COINS:
                if len(held) >= MAX_POSITIONS:
                    break
                if symbol in held:
                    continue

                last_trade = float(state["last_trade"].get(symbol, 0) or 0)
                remaining = COOLDOWN_SECONDS - (time.time() - last_trade)
                if remaining > 0:
                    log(f"[COOLDOWN] {symbol} {int(remaining)}s")
                    continue

                candles = get_candles(symbol)
                if not candles:
                    continue
                signal, candle_id = check_signal(candles, symbol)
                if candle_id is None:
                    continue

                previous_candle = int(state["last_candle"].get(symbol, 0) or 0)
                if candle_id <= previous_candle:
                    log(f"[CANDLE ALREADY CHECKED] {symbol} {candle_id}")
                    continue

                state["last_candle"][symbol] = candle_id
                save_state()
                if not signal:
                    continue

                log(f"[ENTRY SIGNAL] {symbol} {signal.upper()}")
                df = candles_to_df(candles)
                if df is None or not price_mismatch_ok(symbol, float(df.iloc[-1]["close"])):
                    continue

                # Set cooldown before submission to reduce duplicate orders
                # if the exchange response is delayed/ambiguous.
                state["last_trade"][symbol] = time.time()
                save_state()

                if not place_entry(symbol, signal):
                    log(f"[ENTRY NOT CONFIRMED] {symbol}")
                    continue

                held.append(symbol)
                stop_ok = initialize_new_position(symbol)
                log(f"[ENTRY COMPLETE] {symbol} INITIAL_STOP_VERIFIED={stop_ok}")
                if not stop_ok:
                    log(
                        f"[URGENT CHECK] {symbol}: entry accepted but initial stop "
                        "not verified. Check Testnet position/orders immediately."
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
  
