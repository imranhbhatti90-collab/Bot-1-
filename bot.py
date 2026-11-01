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
    "BTCUSD": 40,
    "ETHUSD": 40,
    "SOLUSD": 40,
    "XAUTUSD": 200,
    "PAXGUSD": 200,
    "SILVERUSD": 200,
}
MAX_POSITIONS = 6
ENABLE_TRADING = True                 # User requested automatic entries ON
TRAIL_PCT = 0.003                     # 0.3% trailing distance
COOLDOWN_SECONDS = 300
CHECK_INTERVAL = 30
STATE_FILE = "state.json"
MIN_STOP_GAP_PCT = 0.0005
MAX_CANDLE_LIVE_GAP_PCT = 0.005       # Skip fresh entry if mismatch exceeds 0.5%
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
    return ("DELTA TESTNET AUTO BOT | EMA 9/21/100 + RSI + ADX + MACD + VOLUME "
            "| PRICE MISMATCH GUARD | TRAILING STOP")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "running", "environment": "TESTNET",
                    "automatic_trading": ENABLE_TRADING, "max_positions": MAX_POSITIONS}), 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

# =====================================================
# STATE
# =====================================================
def load_state():
    default = {"peaks": {}, "stops": {}, "last_trade": {}, "last_candle": {}}
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
        response = session.request(method, BASE_URL + path + query_string,
                                   headers=headers, data=payload or None, timeout=(5, 15))
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
def get_live_price(symbol):
    try:
        r = session.get(BASE_URL + f"/v2/trades/{symbol}",
                        headers={"Accept": "application/json"}, timeout=(5, 10))
        if r.status_code != 200:
            log(f"[TRADE PRICE ERROR] {symbol} HTTP={r.status_code}")
            return None
        data = r.json()
        if data.get("success") is not True:
            log(f"[TRADE PRICE ERROR] {symbol}: {data}")
            return None
        result = data.get("result", {})
        trades = result.get("trades", []) if isinstance(result, dict) else result if isinstance(result, list) else []
        if not trades:
            log(f"[NO RECENT TRADES] {symbol}")
            return None
        latest = max(trades, key=lambda x: int(x.get("timestamp", 0)))
        price = float(latest.get("price", 0))
        if price <= 0:
            return None
        log(f"[LIVE TRADE PRICE] {symbol} PRICE={price} TIMESTAMP={latest.get('timestamp')}")
        return price
    except Exception as e:
        log(f"[LIVE PRICE ERROR] {symbol}: {e}")
        return None

def get_candles(symbol):
    end = int(time.time())
    params = {"resolution": TIMEFRAME, "symbol": symbol,
              "start": str(end - CANDLE_LOOKBACK_SECONDS), "end": str(end)}
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
                candles,
                columns=["time", "open", "high", "low", "close", "volume"]
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

        # Normalize timestamp: seconds, milliseconds or microseconds
        def normalize_ts(value):
            value = float(value)

            if value > 1e14:
                value = value / 1_000_000
            elif value > 1e11:
                value = value / 1_000

            return int(value)

        df["time"] = df["time"].apply(normalize_ts)
        df = df.sort_values("time").drop_duplicates("time")

        now = int(time.time())
        candle_seconds = 300

        # Keep only fully closed candles
        df = df[df["time"] + candle_seconds <= now]

        df = df.dropna(subset=required).reset_index(drop=True)

        log(
            f"[CANDLE CHECK] rows={len(df)} "
            f"last_time={int(df['time'].iloc[-1]) if len(df) else 'NONE'} "
            f"now={now}"
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
    tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
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
        fields = ["ema9", "ema21", "ema100", "rsi", "vol_avg", "adx", "macd", "macd_signal", "atr"]
        if any(pd.isna(last[x]) for x in fields):
            log(f"[SCAN] {symbol} indicator data not ready")
            return None, None
        close_px = float(last["close"])
        ema9, ema21, ema100 = float(last["ema9"]), float(last["ema21"]), float(last["ema100"])
        rsi, vol, vol_avg, adx = float(last["rsi"]), float(last["volume"]), float(last["vol_avg"]), float(last["adx"])
        macd, macd_sig, atr_val = float(last["macd"]), float(last["macd_signal"]), float(last["atr"])
        candle_id = int(last["time"]) if "time" in df.columns and pd.notna(last["time"]) else int(time.time() // 300) * 300
        if candle_id > 10_000_000_000:
            candle_id //= 1000
        log(f"[SCAN] {symbol} Candle={candle_id} Close={close_px:.4f} EMA9={ema9:.4f} EMA21={ema21:.4f} EMA100={ema100:.4f} RSI={rsi:.1f} VOL={vol:.0f}/{vol_avg:.0f} ADX={adx:.1f} MACD={macd:.5f}/{macd_sig:.5f} ATR={atr_val:.5f}")
        if adx < MIN_ADX:
            log(f"[NO TRADE] {symbol} ADX={adx:.1f} BELOW {MIN_ADX}")
            return None, candle_id
        buy = ema9 > ema21 and close_px > ema100 and 45 <= rsi <= 70 and macd > macd_sig and vol > vol_avg * 0.6
        sell = ema9 < ema21 and close_px < ema100 and 30 <= rsi <= 55 and macd < macd_sig and vol > vol_avg * 0.6
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
    return p.get("product_symbol") or product.get("symbol") or ""

def get_orders():
    res, code = api_req("GET", "/v2/orders", params={"states": "open,pending", "page_size": 100})
    if code == 200 and isinstance(res, dict) and res.get("success") is True:
        result = res.get("result", [])
        if isinstance(result, list):
            return result
    log(f"[ORDER ERROR] HTTP={code} {res}")
    return None

def is_active_stop(order, symbol):
    return (order.get("product_symbol") == symbol and order.get("stop_order_type") == "stop_loss_order" and order.get("state") in ("open", "pending"))

def verify_stop(symbol, order_id):
    orders = get_orders()
    if orders is None:
        return False
    for order in orders:
        if str(order.get("id")) == str(order_id):
            return is_active_stop(order, symbol) and order.get("reduce_only") in (True, 1, "true", "True")
    return False

def cancel_stop(order):
    order_id, product_id = order.get("id"), order.get("product_id")
    if not order_id or not product_id:
        log("[CANCEL ERROR] Order ID/product ID missing")
        return False
    res, code = api_req("DELETE", "/v2/orders", body={"id": int(order_id), "product_id": int(product_id)})
    if code == 200 and isinstance(res, dict) and res.get("success"):
        log(f"[CANCEL SENT] ID={order_id}")
        return True
    log(f"[CANCEL FAILED] ID={order_id} HTTP={code} {res}")
    return False

def cancel_old_stops(symbol, keep_order_id):
    orders = get_orders()
    if orders is None:
        log("[CANCEL BLOCKED] Cannot verify open orders")
        return False
    ok = True
    for order in orders:
        if is_active_stop(order, symbol) and str(order.get("id")) != str(keep_order_id):
            if not cancel_stop(order):
                ok = False
    return ok

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
        log(f"[STOP BLOCKED] {symbol} SIDE={position_side} STOP={stop_price:.4f} LAST={live} wrong-side/too-close")
        return False
    stop_side = "sell" if position_side == "buy" else "buy"
    body = {"product_symbol": symbol, "size": sz, "side": stop_side,
            "order_type": "market_order", "stop_order_type": "stop_loss_order",
            "stop_price": f"{float(stop_price):.2f}", "reduce_only": True,
            "stop_trigger_method": "last_traded_price"}
    res, code = api_req("POST", "/v2/orders", body=body)
    result = res.get("result", {}) if isinstance(res, dict) else {}
    if code != 200 or res.get("success") is not True or not result.get("id"):
        log(f"[STOP FAILED] {symbol} HTTP={code} {res}")
        return False
    order_id = result["id"]
    time.sleep(1)
    if not verify_stop(symbol, order_id):
        log(f"[STOP NOT VERIFIED] {symbol} ID={order_id}; old stop retained")
        return False
    state["stops"][symbol] = float(stop_price)
    save_state()
    log(f"[STOP VERIFIED] {symbol} {stop_side.upper()} STOP={stop_price:.4f} ID={order_id}")
    cancel_old_stops(symbol, order_id)
    return True

# =====================================================
# TRAILING STOP MANAGEMENT
# =====================================================
def handle_trailing():
    positions = get_pos()
    if positions is None:
        log("[TRAIL BLOCKED] Position API unavailable")
        return
    active = set()
    for p in positions:
        try:
            size = float(p.get("size", 0) or 0)
            if not size:
                continue
            symbol = parse_pos_symbol(p)
            if symbol not in COINS:
                continue
            active.add(symbol)
            entry = float(p.get("entry_price") or p.get("average_price") or 0)
            if entry <= 0:
                log(f"[TRAIL ERROR] {symbol} entry missing")
                continue
            curr = get_live_price(symbol)
            if curr is None:
                log(f"[TRAIL SKIP] {symbol} live price unavailable")
                continue
            old_peak = float(state["peaks"].get(symbol, entry))
            old_stop = float(state["stops"].get(symbol, 0))
            if size > 0:  # LONG
                new_peak = max(old_peak, entry, curr)
                candidate = new_peak * (1 - TRAIL_PCT)
                if old_stop >= curr:
                    old_stop = 0
                if old_stop > 0:
                    candidate = max(candidate, old_stop)
                if candidate >= curr:
                    candidate = curr * (1 - TRAIL_PCT)
                state["peaks"][symbol] = new_peak
                log(f"[TRAIL] {symbol} LONG LIVE={curr:.4f} ENTRY={entry:.4f} PEAK={new_peak:.4f} STOP={candidate:.4f}")
                if old_stop <= 0 or candidate > old_stop:
                    if place_stop(symbol, "buy", size, candidate):
                        log(f"[TRAIL UPDATED] {symbol} LONG")
            else:  # SHORT
                if old_peak <= 0:
                    old_peak = entry
                new_peak = min(old_peak, entry, curr)
                candidate = new_peak * (1 + TRAIL_PCT)
                if old_stop > 0 and old_stop <= curr:
                    log(f"[STALE STOP IGNORED] {symbol} OLD_STOP={old_stop:.4f} LAST={curr:.4f}")
                    old_stop = 0
                if old_stop > 0:
                    candidate = min(candidate, old_stop)
                if candidate <= curr:
                    candidate = curr * (1 + TRAIL_PCT)
                state["peaks"][symbol] = new_peak
                log(f"[TRAIL] {symbol} SHORT LIVE={curr:.4f} ENTRY={entry:.4f} LOW={new_peak:.4f} STOP={candidate:.4f}")
                if old_stop <= 0 or candidate < old_stop:
                    if place_stop(symbol, "sell", size, candidate):
                        log(f"[TRAIL UPDATED] {symbol} SHORT")
            save_state()
        except Exception as e:
            log(f"[TRAIL ERROR] {symbol if 'symbol' in locals() else '?'}: {e}")

# =====================================================
# INITIAL PROTECTIVE STOP AFTER ENTRY
# =====================================================
def initialize_new_position(symbol):
    # Wait briefly for the exchange position endpoint to reflect the filled order.
    for attempt in range(5):
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
            pos_side = "buy"
        else:
            stop_price = curr * (1 + TRAIL_PCT)
            state["peaks"][symbol] = min(entry, curr)
            pos_side = "sell"
        save_state()
        log(f"[INITIAL STOP] {symbol} ENTRY={entry:.4f} LIVE={curr:.4f} STOP={stop_price:.4f}")
        return place_stop(symbol, pos_side, size, stop_price)
    log(f"[INITIAL STOP FAILED] {symbol} position not confirmed; check Testnet immediately")
    return False

# =====================================================
# ENTRY GUARDS / ORDER
# =====================================================
def price_mismatch_ok(symbol, candle_close):
    live = get_live_price(symbol)
    if live is None or candle_close <= 0:
        log(f"[ENTRY BLOCKED] {symbol} live/candle price unavailable")
        return False
    gap_pct = abs(live - candle_close) / candle_close
    log(f"[PRICE CHECK] {symbol} CANDLE={candle_close:.4f} LIVE={live:.4f} GAP={gap_pct*100:.3f}% LIMIT={MAX_CANDLE_LIVE_GAP_PCT*100:.2f}%")
    if gap_pct > MAX_CANDLE_LIVE_GAP_PCT:
        log(f"[ENTRY BLOCKED] {symbol} candle/live price mismatch {gap_pct*100:.3f}%")
        return False
    return True

def place_entry(symbol, side):
    body = {"product_symbol": symbol, "size": ORDER_SIZES.get(symbol, ORDER_SIZE),
            "side": side, "order_type": "market_order"}
    res, code = api_req("POST", "/v2/orders", body=body)
    result = res.get("result", {}) if isinstance(res, dict) else {}
    if code == 200 and isinstance(res, dict) and res.get("success") is True:
        log(f"[ENTRY ACCEPTED] {symbol} {side.upper()} SIZE={ORDER_SIZES.get(symbol, ORDER_SIZE)} RESULT={result}")
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
    log("EMA 9/21/100 + RSI14 + ADX14 + MACD(12,26,9) + VOLUME MA20")
    log(f"ADX FILTER = {MIN_ADX} | PRICE GAP LIMIT = {MAX_CANDLE_LIVE_GAP_PCT*100:.2f}%")
    log(f"TRAILING STOP = {TRAIL_PCT*100:.2f}% | MAX POSITIONS = {MAX_POSITIONS}")
    log(f"COINS = {COINS}")
    log("==================================================")
    while True:
        try:
            # Always manage existing positions before scanning for new entries.
            handle_trailing()
            positions = get_pos()
            if positions is None:
                log("[SCAN BLOCKED] Position API unavailable")
                time.sleep(CHECK_INTERVAL)
                continue
            held = list({parse_pos_symbol(p) for p in positions if parse_pos_symbol(p) in COINS})
            log(f"[POS HELD] {held} TOTAL={len(held)}")
            if not ENABLE_TRADING:
                log("[SAFE MODE] New entries disabled; trailing management remains active")
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
                # One decision per completed candle, even if it produces no signal.
                previous_candle = int(state["last_candle"].get(symbol, 0) or 0)
                if candle_id <= previous_candle:
                    log(f"[CANDLE ALREADY CHECKED] {symbol} {candle_id}")
                    continue
                state["last_candle"][symbol] = candle_id
                save_state()
                if not signal:
                    continue
                log(f"[ENTRY SIGNAL] {symbol} {signal.upper()}")
                # Verify the market/candle price relationship immediately before entry.
                df = candles_to_df(candles)
                if df is None or not price_mismatch_ok(symbol, float(df.iloc[-1]["close"])):
                    continue
                # Set cooldown before sending to avoid duplicate orders if the API response is uncertain.
                state["last_trade"][symbol] = time.time()
                save_state()
                if not place_entry(symbol, signal):
                    log(f"[ENTRY NOT CONFIRMED] {symbol}")
                    continue
                held.append(symbol)
                stop_ok = initialize_new_position(symbol)
                log(f"[ENTRY COMPLETE] {symbol} STOP_VERIFIED={stop_ok}")
                if not stop_ok:
                    log(f"[URGENT CHECK] {symbol}: entry accepted but stop not verified. Check Testnet position/orders immediately.")
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
