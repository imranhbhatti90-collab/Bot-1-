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
# DELTA EXCHANGE INDIA TESTNET / LIVE BOT CONFIG
# ==================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"  # Live ke liye https://api.india.delta.exchange par badal sakte hain

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

# Fallback coins agar dynamic fetch mein issue ho
DEFAULT_COINS = ["BTCUSD", "ETHUSD", "SOLUSD", "XAUTUSD", "PAXGUSD", "SILVERUSD"]

# Har coin ke liye default order size (jaise video mein choti quantity se multiple trades hote hain)
DEFAULT_ORDER_SIZE = 40
MAX_POSITIONS = 10  # Ek sath maximum kitni positions khuli rahengi
ENABLE_TRADING = True

TIMEFRAME = "5m"
CHECK_INTERVAL = 15
COOLDOWN_SECONDS = 180  # Cooldown taaki same coin par baar-baar overtrade na ho
STATE_FILE = "state.json"

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
VOLUME_PERIOD = 20
TRAIL_PCT = 0.005  # 0.5% Trailing stop-loss

# ==================================================
# FLASK SERVER (For Render Health Check)
# ==================================================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def log(msg):
    print(msg, flush=True)


@app.route("/")
def home():
    return "V3 MULTI-COIN SCALPING BOT - ACTIVE & RUNNING"


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
            return json.load(f)
    except:
        return {"peaks": {}, "stops": {}, "last_trade": {}, "last_candle": {}}


def save_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except:
        pass


state = load_state()


# ==================================================
# API REQUEST & DYNAMIC SYMBOL FETCHER
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
        r = requests.request(method, BASE_URL + path + qs, headers=headers, data=payload or None, timeout=15)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500


def get_active_symbols():
    """Delta Exchange se saare active USD linear/perpetual contracts automatically fetch karta hai"""
    res, code = api_req("GET", "/v2/products")
    if code == 200 and isinstance(res, dict) and res.get("success"):
        products = res.get("result", [])
        symbols = []
        for p in products:
            # Sirf active USD margined perpetual contracts lo
            if p.get("contract_type") == "perpetual_futures" and p.get("state") == "live":
                sym = p.get("symbol")
                if sym and (sym.endswith("USD") or sym.endswith("USDT")):
                    symbols.append(sym)
        if symbols:
            log(f"[SYMBOL FETCHER] Fetched {len(symbols)} active symbols from Delta Exchange.")
            return symbols
    log("[SYMBOL FETCHER] Fallback to default COINS list.")
    return DEFAULT_COINS


# ==================================================
# CANDLES & INDICATORS
# ==================================================

def get_candles(symbol):
    end = int(time.time())
    start = end - (300 * 60)  # Last 300 candles
    params = {"resolution": TIMEFRAME, "symbol": symbol, "start": str(start), "end": str(end)}
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and isinstance(res, dict) and res.get("success") and res.get("result"):
        return res["result"]
    return []


def check_signal(candles, symbol):
    if len(candles) < 50:
        return None
    df = pd.DataFrame(candles)
    if 'close' not in df.columns:
        df = pd.DataFrame(candles, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    
    df['close'] = pd.to_numeric(df['close'])
    df['volume'] = pd.to_numeric(df['volume'])
    
    # EMA Calculation
    df['ema9'] = df['close'].ewm(span=EMA_FAST).mean()
    df['ema21'] = df['close'].ewm(span=EMA_SLOW).mean()
    
    # RSI Calculation
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=RSI_PERIOD).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['vol_avg'] = df['volume'].rolling(window=VOLUME_PERIOD).mean()
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    ema9, ema21 = float(last['ema9']), float(last['ema21'])
    ema9_prev, ema21_prev = float(prev['ema9']), float(prev['ema21'])
    rsi = float(last['rsi'])
    vol = float(last['volume'])
    vol_avg = float(last['vol_avg']) if not pd.isna(last['vol_avg']) else vol
    
    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21
    
    # Buy & Sell Filtering (Quality signals with RSI & Volume check)
    if buy_cross and 45 <= rsi <= 70 and vol >= (vol_avg * 0.8):
        return "buy"
    if sell_cross and 30 <= rsi <= 55 and vol >= (vol_avg * 0.8):
        return "sell"
        
    return None


# ==================================================
# ORDER EXECUTION & TRAILING MANAGEMENT
# ==================================================

def get_positions():
    res, code = api_req("GET", "/v2/positions/margined")
    if code == 200 and isinstance(res, dict) and res.get("success"):
        data = res.get("result", [])
        return [p for p in (data if isinstance(data, list) else [data]) if float(p.get("size", 0)) != 0]
    return []


def place_order(symbol, side, size):
    body = {
        "product_symbol": symbol,
        "size": size,
        "side": side,
        "order_type": "market_order"
    }
    res, code = api_req("POST", "/v2/orders", body=body)
    if code in (200, 201) and isinstance(res, dict) and res.get("success"):
        log(f"[ORDER PLACED] {symbol} {side} size {size}")
        return res.get("result")
    log(f"[ORDER FAILED] {symbol} {side} -> {res}")
    return None


def manage_trailing_stops(coins_list):
    positions = get_positions()
    for p in positions:
        psym = p.get("product_symbol")
        if psym not in coins_list:
            continue
        size = float(p.get("size", 0))
        entry = float(p.get("entry_price") or p.get("average_price") or 0)
        if entry == 0:
            continue
            
        candles = get_candles(psym)
        if not candles:
            continue
        curr = float(candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4])
        
        old_peak = state["peaks"].get(psym, entry)
        if size > 0:  # LONG POSITION
            new_peak = max(old_peak, curr)
            state["peaks"][psym] = new_peak
            trail_stop = new_peak * (1 - TRAIL_PCT)
            if curr <= trail_stop:
                log(f"[STOP HIT] Closing LONG on {psym} at {curr}")
                place_order(psym, "sell", abs(int(size)))
        else:  # SHORT POSITION
            new_peak = min(old_peak, curr) if old_peak != 0 else curr
            state["peaks"][psym] = new_peak
            trail_stop = new_peak * (1 + TRAIL_PCT)
            if curr >= trail_stop:
                log(f"[STOP HIT] Closing SHORT on {psym} at {curr}")
                place_order(psym, "buy", abs(int(size)))
        save_state(state)


# ==================================================
# MAIN BOT LOOP
# ==================================================

def main_loop():
    log("=== V3 MULTI-COIN AUTOMATED SCALPING BOT STARTED ===")
    
    while True:
        try:
            # 1. Fetch active coins dynamically from exchange
            active_coins = get_active_symbols()
            
            # 2. Manage existing trailing stops
            manage_trailing_stops(active_coins)
            
            # 3. Check current open positions count
            positions = get_positions()
            held_symbols = {p.get("product_symbol") for p in positions}
            
            if len(held_symbols) >= MAX_POSITIONS:
                log(f"[MAX POSITIONS REACHED] {len(held_symbols)}/{MAX_POSITIONS}. Waiting...")
                time.sleep(30)
                continue
                
            # 4. Scan coins for new entries
            for symbol in active_coins:
                if symbol in held_symbols:
                    continue
                    
                # Cooldown check
                last_t = state["last_trade"].get(symbol, 0)
                if time.time() - last_t < COOLDOWN_SECONDS:
                    continue
                    
                candles = get_candles(symbol)
                if not candles:
                    continue
                    
                signal = check_signal(candles, symbol)
                if signal:
                    log(f"[SIGNAL DETECTED] {symbol} -> {signal.upper()}")
                    if ENABLE_TRADING:
                        order = place_order(symbol, signal, DEFAULT_ORDER_SIZE)
                        if order:
                            state["last_trade"][symbol] = time.time()
                            curr_px = float(candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4])
                            state["peaks"][symbol] = curr_px
                            save_state(state)
                    time.sleep(1) # Rate limit gap
                    
            log("--- Scan completed. Sleeping for 30s ---")
            time.sleep(30)
            
        except Exception as e:
            log(f"[MAIN LOOP ERROR] {e}")
            time.sleep(10)


if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    main_loop()
