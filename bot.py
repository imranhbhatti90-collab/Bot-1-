port os
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
# DELTA EXCHANGE INDIA TESTNET / LIVE BOT CONFIG
# ==================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

DEFAULT_COINS = ["BTCUSD", "ETHUSD", "SOLUSD", "PAXGUSD", "SILVERUSD"]
GOLD_COINS = {"PAXGUSD"}

DEFAULT_ORDER_SIZE = 40
MAX_POSITIONS = 4
MAX_GOLD_POSITIONS = 4
ENABLE_TRADING = True

TIMEFRAME = "15m"
COOLDOWN_SECONDS = 300
STATE_FILE = "state.json"

EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200
RSI_PERIOD = 14
VOLUME_PERIOD = 20
VOL_MULT = 1.2
PULLBACK_MAX_PCT = 0.0035  # EMA 21 ke 0.35% ke andar pullback touch/zone

ATR_PERIOD = 14
ATR_SL_MULT = 2.0
ATR_TRAIL_MULT = 2.0
STOP_TRIGGER_METHOD = "last_traded_price"

# ==================================================
# FLASK SERVER (For Render Health Check)
# ==================================================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def log(msg):
    print(msg, flush=True)


@app.route("/")
def home():
    return "V3 MULTI-COIN BOT WITH TREND PULLBACK + ATR - ACTIVE"


@app.route("/health")
def health():
    return jsonify(status="ok", automatic_trading=ENABLE_TRADING), 200


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# ==================================================
# STATE MANAGEMENT
# ==================================================

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            for k in ("peaks", "stops", "atr", "last_trade", "last_candle"):
                data.setdefault(k, {})
            return data
    except Exception:
        return {"peaks": {}, "stops": {}, "atr": {}, "last_trade": {}, "last_candle": {}}


def save_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


state = load_state()


# ==================================================
# API REQUEST & SYMBOL SELECTION
# ==================================================

def api_req(method, path, params=None, body=None):
    ts = str(int(time.time()))
    qs = "?" + urlencode(params, quote_via=quote) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
    msg = method.upper() + ts + path + qs + payload
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

    headers = {
        "api-key": API_KEY,
        "timestamp": ts,
        "signature": sig,
        "Content-Type": "application/json",
    }
    try:
        r = requests.request(
            method,
            BASE_URL + path + qs,
            headers=headers,
            data=payload or None,
            timeout=15,
        )
        raw_text = r.text.strip()
        if not raw_text:
            parsed = {"success": 200 <= r.status_code < 300, "status_code": r.status_code}
        else:
            try:
                parsed = r.json()
            except ValueError:
                parsed = {"success": 200 <= r.status_code < 300, "raw": raw_text}
        return parsed, r.status_code
    except Exception as e:
        return {"error": str(e)}, 500


def get_active_symbols():
    return DEFAULT_COINS


# ==================================================
# CANDLES & INDICATORS (EMA + RSI + VOL + ATR)
# ==================================================

def get_candles(symbol):
    end = int(time.time())
    start = end - (6500 * 60)
    params = {"resolution": TIMEFRAME, "symbol": symbol, "start": str(start), "end": str(end)}
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and isinstance(res, dict) and res.get("success") and res.get("result"):
        return res["result"]
    return []


def get_latest_candle_ts(candles):
    if not candles:
        return None
    last_c = candles[-1]
    if isinstance(last_c, dict):
        ts = last_c.get("time") or last_c.get("timestamp") or last_c.get("start_time")
        return int(ts) if ts is not None else None
    if isinstance(last_c, (list, tuple)) and len(last_c) > 0:
        return int(last_c[0])
    return None


def compute_indicators(candles):
    if not candles or len(candles) < (EMA_TREND_SLOW + 5):
        return None

    df = pd.DataFrame(candles)
    if "close" not in df.columns:
        df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["high", "low", "close", "volume"]).reset_index(drop=True)

    if len(df) < (EMA_TREND_SLOW + 5):
        return None

    df["ema9"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=EMA_TREND_FAST, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=EMA_TREND_SLOW, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss.replace(0, pd.NA)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["vol_avg"] = df["volume"].rolling(window=VOLUME_PERIOD).mean()

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(window=ATR_PERIOD).mean()

    return df


def check_signal(candles, symbol):
    df = compute_indicators(candles)
    if df is None:
        return None, None, ""

    last = df.iloc[-1]
    prev = df.iloc[-2]

    open_px = float(last["open"])
    close_px = float(last["close"])
    low_px = float(last["low"])
    high_px = float(last["high"])
    prev_low = float(prev["low"])
    prev_high = float(prev["high"])

    ema9, ema21 = float(last["ema9"]), float(last["ema21"])
    ema9_prev, ema21_prev = float(prev["ema9"]), float(prev["ema21"])
    ema50 = float(last["ema50"])
    ema200 = float(last["ema200"])
    rsi = float(last["rsi"]) if not pd.isna(last["rsi"]) else 50.0
    vol = float(last["volume"])
    vol_avg = float(last["vol_avg"]) if not pd.isna(last["vol_avg"]) else vol
    atr = float(last["atr"]) if not pd.isna(last["atr"]) else 0.0

    if atr <= 0 or vol_avg <= 0 or ema21 <= 0:
        return None, None, ""

    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21

    uptrend = (ema50 > ema200) and (close_px > ema200)
    downtrend = (ema50 < ema200) and (close_px < ema200)
    vol_ok = vol >= (vol_avg * VOL_MULT)

    pullback_dist_buy = min(abs(low_px - ema21), abs(prev_low - ema21)) / ema21
    buy_pullback = (
        uptrend
        and (pullback_dist_buy <= PULLBACK_MAX_PCT)
        and (close_px > open_px)
        and (close_px > ema21)
        and (ema9 >= ema21 * 0.999)
    )

    pullback_dist_sell = min(abs(high_px - ema21), abs(prev_high - ema21)) / ema21
    sell_pullback = (
        downtrend
        and (pullback_dist_sell <= PULLBACK_MAX_PCT)
        and (close_px < open_px)
        and (close_px < ema21)
        and (ema9 <= ema21 * 1.001)
    )

    if vol_ok and (50 <= rsi <= 70):
        if buy_cross and uptrend:
            return "buy", atr, "CROSS_9_21"
        if buy_pullback:
            return "buy", atr, "PULLBACK_EMA21"

    if vol_ok and (30 <= rsi <= 50):
        if sell_cross and downtrend:
            return "sell", atr, "CROSS_9_21"
        if sell_pullback:
            return "sell", atr, "PULLBACK_EMA21"

    return None, atr, ""


# ==================================================
# ORDER EXECUTION & STOP-LOSS MANAGEMENT
# ==================================================

def format_stop_price(symbol, price):
    if symbol in ("BTCUSD", "XAUTUSD", "PAXGUSD"):
        return round(float(price), 1)
    if symbol == "ETHUSD":
        return round(float(price), 2)
    return round(float(price), 4)


def get_positions():
    res, code = api_req("GET", "/v2/positions/margined")
    if code == 200 and isinstance(res, dict) and res.get("success"):
        data = res.get("result", [])
        return [p for p in (data if isinstance(data, list) else [data]) if float(p.get("size", 0)) != 0]
    return []


def get_open_orders(symbol=None):
    params = {"states": "open,pending"}
    if symbol:
        params["product_symbol"] = symbol
    res, code = api_req("GET", "/v2/orders", params=params)
    if code == 200 and isinstance(res, dict) and res.get("success"):
        orders = res.get("result", [])
        if isinstance(orders, list):
            return [
                o for o in orders
                if o.get("state") in ("open", "pending")
            ]
    return []


def cancel_symbol_stops(symbol):
    orders = get_open_orders(symbol)
    for o in orders:
        if o.get("reduce_only") or o.get("stop_order_type"):
            oid = o.get("id")
            if oid:
                res, code = api_req("DELETE", f"/v2/orders/{oid}")
                if code == 404:
                    log(f"[CANCEL STOP] {symbol} oid={oid} already gone (404)")
                else:
                    log(f"[CANCEL STOP] sym={symbol} oid={oid} status={code} res={res}")
                time.sleep(0.2)


def place_order(symbol, side, size):
    body = {
        "product_symbol": symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
    }
    res, code = api_req("POST", "/v2/orders", body=body)
    if code in (200, 201) and isinstance(res, dict) and res.get("success"):
        log(f"[ORDER PLACED] {symbol} {side} size {size}")
        return res.get("result")
    log(f"[ORDER FAILED] {symbol} {side} -> {res}")
    return None


def place_stop_order(symbol, entry_side, size, stop_price, curr_px=None):
    stop_side = "sell" if entry_side == "buy" else "buy"
    px = float(stop_price)

    if curr_px and float(curr_px) > 0:
        cpx = float(curr_px)
        if entry_side == "buy" and px >= cpx:
            px = cpx * 0.995
        elif entry_side == "sell" and px <= cpx:
            px = cpx * 1.005

    clean_stop = format_stop_price(symbol, px)
    body = {
        "product_symbol": symbol,
        "size": int(size),
        "side": stop_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(clean_stop),
        "stop_trigger_method": STOP_TRIGGER_METHOD,
        "reduce_only": True,
    }

    log(f"[STOP REQUEST] Placing stop for {symbol} @ {clean_stop}")
    res, code = api_req("POST", "/v2/orders", body=body)
    log(f"[STOP RESPONSE] {symbol} HTTP={code} | Result: {res}")

    if code in (200, 201) and isinstance(res, dict) and res.get("success"):
        log(f"[STOP SUCCESS] Stop-loss active for {symbol} at {clean_stop}")
        return res.get("result")
    else:
        log(f"[STOP FAILED] {symbol} error: {res}")
        return None


def manage_trailing_stops(coins_list):
    positions = get_positions()
    active_symbols = {p.get("product_symbol") for p in positions}

    all_open_orders = get_open_orders()
    if all_open_orders:
        for o in all_open_orders:
            if o.get("reduce_only") or o.get("stop_order_type"):
                sym = o.get("product_symbol")
                oid = o.get("id")
                if sym not in active_symbols and oid:
                    res, code = api_req("DELETE", f"/v2/orders/{oid}")
                    if code == 404:
                        log(f"[STRICT CLEANUP] {sym} oid={oid} already gone (404)")
                    else:
                        log(f"[STRICT CLEANUP] sym={sym} oid={oid} status={code} res={res}")
                    time.sleep(0.2)

    for p in positions:
        psym = p.get("product_symbol")
        if psym not in coins_list:
            continue
        size = float(p.get("size", 0))
        if size == 0:
            continue

        entry = float(p.get("entry_price") or p.get("average_price") or 0)
        if entry == 0:
            continue

        candles = get_candles(psym)
        df = compute_indicators(candles)
        if df is None:
            continue

        curr = float(df.iloc[-1]["close"])
        atr_val = float(df.iloc[-1]["atr"]) if not pd.isna(df.iloc[-1]["atr"]) else 0.0
        if atr_val <= 0:
            atr_val = float(state["atr"].get(psym, 0))
        if atr_val <= 0:
            continue

        state["atr"][psym] = atr_val
        trail_dist = atr_val * ATR_TRAIL_MULT

        old_peak = state["peaks"].get(psym, entry)
        old_stop = state["stops"].get(psym, 0)

        if size > 0:  # LONG POSITION
            new_peak = max(old_peak, curr)
            state["peaks"][psym] = new_peak
            trail_stop = new_peak - trail_dist
            if trail_stop > old_stop or old_stop == 0:
                cancel_symbol_stops(psym)
                res = place_stop_order(psym, "buy", abs(size), trail_stop, curr_px=curr)
                if res:
                    state["stops"][psym] = format_stop_price(psym, trail_stop)
        else:  # SHORT POSITION
            new_peak = min(old_peak, curr) if old_peak != 0 else curr
            state["peaks"][psym] = new_peak
            trail_stop = new_peak + trail_dist
            if trail_stop < old_stop or old_stop == 0:
                cancel_symbol_stops(psym)
                res = place_stop_order(psym, "sell", abs(size), trail_stop, curr_px=curr)
                if res:
                    state["stops"][psym] = format_stop_price(psym, trail_stop)

        save_state(state)

    for sym in list(state["stops"].keys()):
        if sym not in active_symbols:
            state["stops"].pop(sym, None)
            state["peaks"].pop(sym, None)
            state["atr"].pop(sym, None)
            save_state(state)


# ==================================================
# MAIN BOT LOOP
# ==================================================

def main_loop():
    log("=== V3 MULTI-COIN BOT (EMA 50/200 + PULLBACK + ATR) STARTED ===")

    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    while True:
        try:
            active_coins = get_active_symbols()
            manage_trailing_stops(active_coins)

            positions = get_positions()
            held_symbols = {p.get("product_symbol") for p in positions}

            if len(held_symbols) >= MAX_POSITIONS:
                log(f"[MAX POSITIONS REACHED] {len(held_symbols)}/{MAX_POSITIONS}. Waiting...")
                time.sleep(30)
                continue

            for symbol in active_coins:
                if symbol in held_symbols:
                    continue

                if symbol in GOLD_COINS:
                    active_gold = sum(1 for s in held_symbols if s in GOLD_COINS)
                    if active_gold >= MAX_GOLD_POSITIONS:
                        continue

                last_t = state["last_trade"].get(symbol, 0)
                if time.time() - last_t < COOLDOWN_SECONDS:
                    continue

                candles = get_candles(symbol)
                if not candles:
                    continue

                candle_ts = get_latest_candle_ts(candles)
                if candle_ts is not None and state["last_candle"].get(symbol) == candle_ts:
                    continue

                signal, atr_val, reason = check_signal(candles, symbol)
                if signal and atr_val:
                    log(f"[SIGNAL DETECTED] {symbol} -> {signal.upper()} ({reason}) | ATR={atr_val:.4f}")
                    if candle_ts is not None:
                        state["last_candle"][symbol] = candle_ts
                        save_state(state)

                    if ENABLE_TRADING:
                        live_positions = get_positions()
                        live_held = {p.get("product_symbol") for p in live_positions}
                        if len(live_held) >= MAX_POSITIONS:
                            log(f"[SKIP ENTRY] {symbol} -> live positions already {len(live_held)}/{MAX_POSITIONS}")
                            continue
                        if symbol in GOLD_COINS:
                            live_gold = sum(1 for s in live_held if s in GOLD_COINS)
                            if live_gold >= MAX_GOLD_POSITIONS:
                                log(f"[SKIP ENTRY] {symbol} -> live gold positions already {live_gold}/{MAX_GOLD_POSITIONS}")
                                continue

                        order = place_order(symbol, signal, DEFAULT_ORDER_SIZE)
                        if order:
                            time.sleep(2)
                            curr_px = float(candles[-1]["close"] if isinstance(candles[-1], dict) else candles[-1][4])
                            sl_dist = atr_val * ATR_SL_MULT

                            if signal == "buy":
                                initial_stop = curr_px - sl_dist
                            else:
                                initial_stop = curr_px + sl_dist

                            place_stop_order(symbol, signal, DEFAULT_ORDER_SIZE, initial_stop, curr_px=curr_px)

                            state["last_trade"][symbol] = time.time()
                            state["peaks"][symbol] = curr_px
                            state["stops"][symbol] = format_stop_price(symbol, initial_stop)
                            state["atr"][symbol] = atr_val
                            save_state(state)
                    time.sleep(1)

            log("--- Scan completed. Sleeping for 30s ---")
            time.sleep(30)

        except Exception as e:
            log(f"[MAIN LOOP ERROR] {e}")
            time.sleep(10)


if __name__ == "__main__":
    main_loop()
