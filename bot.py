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
TIMEFRAME = "15m"
TREND_TIMEFRAME = None  # No separate trend filter; follow the TradingView 15m EMA cross
ORDER_SIZE = 40
ORDER_SIZES = {
    "BTCUSD": 40, "ETHUSD": 40, "SOLUSD": 40,
    "XAUTUSD": 200, "PAXGUSD": 200, "SILVERUSD": 200,
}
MAX_POSITIONS = 6
ENABLE_TRADING = True
LEVERAGE = 50  # Informational setting; this script does not change account leverage.
ATR_STOP_MULTIPLIER = 1.5
ATR_PERIOD = 14
STOP_TRIGGER_METHOD = "last_traded_price"  # Matches the active SILVERUSD stop
COOLDOWN_SECONDS = 300
CHECK_INTERVAL = 30
STATE_FILE = "state.json"
MIN_STOP_GAP_PCT = 0.0005
MAX_CANDLE_LIVE_GAP_PCT = 0.005
MAX_TRADE_AGE_SECONDS = 30
ADX_PERIOD = 14
MIN_ADX = 20.0
EMA_FAST = 10
EMA_SLOW = 20
RSI_PERIOD = 14
VOLUME_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
CANDLE_LOOKBACK_SECONDS = 2 * 24 * 60 * 60

# =====================================================
# APP / LOGGING
# =====================================================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

def log(message):
    print(message, flush=True)

@app.route("/")
def home():
    return ("DELTA TESTNET AUTO BOT | 15m EMA10/EMA20 CROSSOVER | ATR TRAILING STOP")

@app.route("/health")
def health():
    return jsonify({
        "status": "ok", "bot": "running", "environment": "TESTNET",
        "automatic_trading": ENABLE_TRADING, "max_positions": MAX_POSITIONS,
        "strategy": "15m closed-candle EMA10/EMA20 crossover",
        "trailing_method": "ATR-based breakeven + chandelier trail",
        "atr_multiplier": ATR_STOP_MULTIPLIER
    }), 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

# =====================================================
# STATE
# =====================================================
def load_state():
    default = {
        "peaks": {}, "stops": {}, "last_trade": {}, "last_candle": {},
        "trail_active": {}, "initial_gaps": {}
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

def get_candles(symbol, resolution=TIMEFRAME):
    end = int(time.time())
    # Ten days of 15m candles provides ample history for EMA calculations and ATR.
    lookback = 10 * 24 * 60 * 60
    params = {
        "resolution": resolution, "symbol": symbol,
        "start": str(end - lookback), "end": str(end)
    }
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and isinstance(res, dict) and res.get("success") and res.get("result"):
        return res["result"]
    log(f"[CANDLE ERROR] {symbol} HTTP={code} {res}")
    return []

def candles_to_df(candles, resolution=TIMEFRAME):
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
        interval_seconds = 900 if resolution == "15m" else 300
        df = df[df["time"] + interval_seconds <= now]
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
    """Calculate the chart's EMA10/EMA20 and ATR for protective stops."""
    df = df.copy()
    close, high, low = df["close"], df["high"], df["low"]
    df["ema10"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema20"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    df["atr"] = tr.ewm(
        alpha=1 / ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD
    ).mean()
    return df


def check_signal(candles, symbol):
    """Trade only a confirmed EMA10/EMA20 crossover on a CLOSED 15m candle."""
    df = candles_to_df(candles, resolution=TIMEFRAME)
    if df is None or len(df) < 30:
        log(f"[SCAN] {symbol} 15m candles insufficient for EMA10/EMA20")
        return None, None

    try:
        df = add_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        needed = ["ema10", "ema20", "atr"]
        if any(pd.isna(last[x]) for x in needed) or any(pd.isna(prev[x]) for x in ["ema10", "ema20"]):
            log(f"[SCAN] {symbol} EMA indicators not ready")
            return None, int(last["time"])

        ema10 = float(last["ema10"])
        ema20 = float(last["ema20"])
        prev_ema10 = float(prev["ema10"])
        prev_ema20 = float(prev["ema20"])
        close_px = float(last["close"])
        candle_id = int(last["time"])

        # One crossover is one signal. The user's two chart indicators have
        # identical EMA settings, so they are not counted as two confirmations.
        buy = prev_ema10 <= prev_ema20 and ema10 > ema20
        sell = prev_ema10 >= prev_ema20 and ema10 < ema20

        log(
            f"[EMA CROSS SCAN] {symbol} TF=15m Candle={candle_id} "
            f"Close={close_px:.4f} EMA10={ema10:.6f} EMA20={ema20:.6f} "
            f"PREV_EMA10={prev_ema10:.6f} PREV_EMA20={prev_ema20:.6f}"
        )
        if buy:
            log(f"[EMA CROSS BUY] {symbol} EMA10 crossed ABOVE EMA20 on closed 15m candle")
            return "buy", candle_id
        if sell:
            log(f"[EMA CROSS SELL] {symbol} EMA10 crossed BELOW EMA20 on closed 15m candle")
            return "sell", candle_id

        log(f"[NO TRADE] {symbol} no fresh EMA10/EMA20 crossover on this closed 15m candle")
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
# ATR-BASED BREAKEVEN + TRAILING STOP
# =====================================================
def get_atr_gap(symbol):
    """Read the latest CLOSED 15m ATR(14); return 1.5x ATR as this trade's fixed gap."""
    candles = get_candles(symbol)
    df = candles_to_df(candles) if candles else None
    if df is None:
        return None
    df = add_indicators(df)
    atr = float(df.iloc[-1].get("atr", 0) or 0)
    if atr <= 0:
        log(f"[ATR INVALID] {symbol} ATR={atr}")
        return None
    gap = atr * ATR_STOP_MULTIPLIER
    log(f"[ATR GAP] {symbol} ATR14={atr:.8f} MULT={ATR_STOP_MULTIPLIER} GAP={gap:.8f}")
    return gap


def recover_stop_state(symbol, position_side, orders):
    """Recover the tightest active exchange stop after a Render restart."""
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
    chosen_price, chosen_order = (
        max(parsed, key=lambda x: x[0]) if position_side == "buy"
        else min(parsed, key=lambda x: x[0])
    )
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
                log(f"[TRAIL SKIP] {symbol} stale/unavailable live price; exchange stop retained")
                continue

            side = "buy" if size > 0 else "sell"
            old_stop, exchange_stop_found = recover_stop_state(symbol, side, orders)
            gap = float(state.get("initial_gaps", {}).get(symbol, 0) or 0)

            # For positions carried over from the old bot, derive the original
            # risk gap from its existing exchange stop where possible.
            if gap <= 0 and old_stop > 0:
                gap = (entry - old_stop) if side == "buy" else (old_stop - entry)
                if gap > 0:
                    state.setdefault("initial_gaps", {})[symbol] = gap
                    log(f"[GAP RECOVERED FROM EXCHANGE STOP] {symbol} GAP={gap:.8f}")

            if gap <= 0:
                gap = get_atr_gap(symbol) or 0
                if gap > 0:
                    state.setdefault("initial_gaps", {})[symbol] = gap
                    initial = entry - gap if side == "buy" else entry + gap
                    if old_stop <= 0:
                        log(f"[INITIAL STOP MISSING] {symbol}; placing ATR stop={initial:.8f}")
                        if not place_stop(symbol, side, size, initial):
                            log(f"[URGENT] {symbol} ATR initial stop could not be verified")
                            continue
                        old_stop = initial
                else:
                    log(f"[TRAIL PAUSED] {symbol} ATR unavailable and no recoverable stop gap; existing stop untouched")
                    continue

            old_peak = float(state["peaks"].get(symbol, entry) or entry)
            peak = max(old_peak, entry, curr) if side == "buy" else min(old_peak, entry, curr)
            state["peaks"][symbol] = peak

            # Do not move to breakeven until price has moved favourably by
            # one full initial ATR gap. Then trail the peak by that same gap.
            favourable_move = (peak - entry) if side == "buy" else (entry - peak)
            if favourable_move < gap:
                log(
                    f"[WAIT BREAKEVEN] {symbol} SIDE={side.upper()} ENTRY={entry:.8f} "
                    f"PEAK={peak:.8f} MOVE={favourable_move:.8f} REQUIRED={gap:.8f} "
                    f"STOP={old_stop:.8f}"
                )
                save_state()
                continue

            candidate = peak - gap if side == "buy" else peak + gap
            # At first trigger this is approximately entry; afterwards it
            # follows the best price. A stop is never loosened.
            candidate = max(candidate, entry) if side == "buy" else min(candidate, entry)
            if old_stop > 0:
                candidate = max(candidate, old_stop) if side == "buy" else min(candidate, old_stop)

            if not valid_stop_price(side, candidate, curr):
                log(f"[TRAIL WAIT] {symbol} candidate={candidate:.8f} LAST={curr:.8f}; current exchange stop retained")
                save_state()
                continue

            improvement = (old_stop <= 0 or candidate > old_stop) if side == "buy" else (old_stop <= 0 or candidate < old_stop)
            log(
                f"[ATR TRAIL] {symbol} SIDE={side.upper()} ENTRY={entry:.8f} "
                f"PEAK={peak:.8f} GAP={gap:.8f} OLD_STOP={old_stop:.8f} NEW_STOP={candidate:.8f}"
            )
            if improvement:
                place_stop(symbol, side, size, candidate)
            save_state()
        except Exception as e:
            log(f"[TRAIL ERROR] {p.get('product_symbol', '?')}: {e}")

    # Clear local tracking only. Never cancel exchange-side stops here.
    for symbol in list(state["peaks"].keys()):
        if symbol in COINS and symbol not in active_symbols:
            state["peaks"].pop(symbol, None)
            state["stops"].pop(symbol, None)
            state.get("trail_active", {}).pop(symbol, None)
            state.get("initial_gaps", {}).pop(symbol, None)
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
        if not size or entry <= 0:
            continue

        gap = get_atr_gap(symbol)
        if gap is None or gap <= 0:
            log(f"[INITIAL STOP FAILED] {symbol}: ATR unavailable; check Testnet position and protect it manually")
            return False

        side = "buy" if size > 0 else "sell"
        stop_price = entry - gap if side == "buy" else entry + gap
        curr = get_live_price(symbol)
        if curr is None:
            log(f"[INITIAL STOP FAILED] {symbol}: live last price unavailable")
            return False

        state.setdefault("initial_gaps", {})[symbol] = gap
        state["peaks"][symbol] = max(entry, curr) if side == "buy" else min(entry, curr)
        state["trail_active"][symbol] = False
        save_state()
        log(
            f"[INITIAL ATR STOP] {symbol} ENTRY={entry:.8f} ATR_GAP={gap:.8f} "
            f"STOP={stop_price:.8f}; breakeven triggers after favourable move={gap:.8f}"
        )
        return place_stop(symbol, side, size, stop_price)

    log(f"[INITIAL STOP FAILED] {symbol} position not confirmed; check Testnet immediately")
    return False

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
    log("ENTRY STRATEGY: CLOSED 15m CANDLE EMA10/EMA20 CROSSOVER ONLY")
    log(f"TRAIL PRICE SOURCE = LAST TRADED PRICE | STOP TRIGGER = {STOP_TRIGGER_METHOD}")
    log(f"TRAIL METHOD = ATR({ATR_PERIOD}) x {ATR_STOP_MULTIPLIER}; BE after one initial gap; then trail peak by same gap")
    log("ENTRY FILTERS: no RSI/ADX/MACD/volume/breakout filters; crossover only")
    log(f"RETEST TOLERANCE = {RETEST_ATR_TOLERANCE} x ATR | PRICE GAP LIMIT = {MAX_CANDLE_LIVE_GAP_PCT*100:.2f}%")
    log(
        f"INITIAL/TRAIL GAP = {ATR_STOP_MULTIPLIER} x ATR({ATR_PERIOD}) on 15m candles | "
        "BREAKEVEN TRIGGER = one favourable initial gap"
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
                df = candles_to_df(candles, resolution=TIMEFRAME)
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
