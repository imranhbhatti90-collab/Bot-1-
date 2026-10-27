import os
import time
import json
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlencode, quote

import requests
import pandas as pd
from flask import Flask, jsonify

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

TIMEFRAME_MINUTES = 5
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20

ORDER_SIZE = 40
LEVERAGE = 50
TRAILING_STOP_PERCENT = 0.5
ENABLE_TRADING = True
POLL_SECONDS = 20
STATE_FILE = "state.json"
REQUEST_TIMEOUT = 15
CLOSE_RETRY_SECONDS = 30

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
app = Flask(__name__)
session = requests.Session()

bot_status = {"running": False, "trading_enabled": ENABLE_TRADING, "last_scan": None, "last_error": None, "products_scanned": 0, "last_signal": None}

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError: return {}
    except: return {}
state = load_state()
def save_state():
    try:
        with open(STATE_FILE + ".tmp", "w") as f: json.dump(state, f, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as exc: logging.error("State save failed: %s", exc)

def api_request(method, path, params=None, payload=None, auth=True):
    method = method.upper()
    params = params or {}
    query_string = urlencode(sorted(params.items()), doseq=True, quote_via=quote)
    request_path = path + (("?" + query_string) if query_string else "")
    body_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
    headers = {"Content-Type": "application/json"}
    if auth:
        if not API_KEY or not API_SECRET: raise RuntimeError("API KEY/SECRET missing")
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + request_path + body_text
        signature = hmac.new(API_SECRET.encode("utf-8"), signature_data.encode("utf-8"), hashlib.sha256).hexdigest()
        headers.update({"api-key": API_KEY, "timestamp": timestamp, "signature": signature})
    url = BASE_URL + path
    response = session.request(method=method, url=url, params=params if params else None, data=body_text if body_text else None, headers=headers, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400: raise RuntimeError(f"HTTP {response.status_code} {path}: {response.text[:500]}")
    result = response.json()
    if isinstance(result, dict) and result.get("success") is False: raise RuntimeError(f"API rejected {path}: {result}")
    return result

def result_data(result):
    if isinstance(result, dict): return result.get("result", result)
    return result

def get_all_active_products():
    result = api_request("GET", "/v2/products", auth=False)
    products = result_data(result)
    if isinstance(products, dict): products = products.get("products", [])
    active = []
    for p in products:
        if not isinstance(p, dict): continue
        if str(p.get("state","")).lower() != "active": continue
        if "USD" not in str(p.get("symbol","")).upper(): continue
        if p.get("id") is None: continue
        active.append(p)
    return active

def fetch_market_candles(symbol):
    result = api_request("GET", "/v2/history/candles", params={"symbol": symbol, "resolution": f"{TIMEFRAME_MINUTES}m", "start": int(time.time()) - (60 * TIMEFRAME_MINUTES * 350), "end": int(time.time())}, auth=False)
    candles = result_data(result)
    if isinstance(candles, dict): candles = candles.get("candles", [])
    if len(candles) < 150: raise RuntimeError(f"{symbol}: insufficient candle data")
    df = pd.DataFrame(candles)
    rename = {}
    for target, opts in {"time": ["time","timestamp","t"], "close": ["close","c"], "volume": ["volume","v"]}.items():
        for o in opts:
            if o in df.columns: rename[o]=target; break
    df = df.rename(columns=rename)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.sort_values("time").dropna(subset=["close"]).reset_index(drop=True)
    if len(df)>2:
        try:
            lt = float(df["time"].iloc[-1])
            if lt > 1e12: lt/=1000
            if time.time() - lt < TIMEFRAME_MINUTES*60: df = df.iloc[:-1]
        except: pass
    if len(df) < 150: raise RuntimeError(f"{symbol}: not enough candles")
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    delta = df["close"].diff(); gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + (avg_gain/avg_loss.replace(0,float("nan")))))
    df["volume_ma"] = df["volume"].rolling(VOLUME_MA_PERIOD).mean() if "volume" in df.columns else float("nan")
    return df

# ===== OPTION 1: STRICT 35 =====
def check_market_filters(df, side):
    row = df.iloc[-1]
    rsi = row.get("rsi")
    if pd.isna(rsi):
        return False, "RSI not available"
    # Buy > 35, Sell < 35
    if side == "buy" and rsi < 35:
        return False, f"BUY reject RSI {rsi:.2f} < 35"
    if side == "sell" and rsi > 35:
        return False, f"SELL reject RSI {rsi:.2f} > 35"
    return True, "Filters passed RSI 35"

def scan_symbol(symbol):
    df = fetch_market_candles(symbol)
    prev, curr = df.iloc[-2], df.iloc[-1]
    if prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]: side="buy"
    elif prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]: side="sell"
    else: return None, df, "No EMA crossover"
    ok, reason = check_market_filters(df, side)
    if not ok: return None, df, reason
    return {"symbol": symbol, "side": side, "price": float(curr["close"]), "candle_id": str(curr["time"]), "rsi": float(curr["rsi"])}, df, reason

def get_all_positions():
    all_positions = []
    try:
        result = api_request("GET", "/v2/positions/margined", auth=True)
        positions = result_data(result)
        if isinstance(positions, dict): positions = positions.get("positions", [])
        if isinstance(positions, list) and len(positions) > 0: return positions
    except Exception as e: logging.info(f"margined try failed: {e}")
    underlying_assets = set()
    for s in list(state.keys()):
        for asset in ["BTC", "ETH", "SOL", "XRP", "BNB", "AVAX", "MATIC", "DOGE", "LTC"]:
            if asset in s: underlying_assets.add(asset)
    if not underlying_assets: underlying_assets = {"BTC", "ETH", "SOL", "XRP", "BNB"}
    for underlying in underlying_assets:
        try:
            result = api_request("GET", "/v2/positions", params={"underlying_asset_symbol": underlying}, auth=True)
            positions = result_data(result)
            if isinstance(positions, dict): positions = positions.get("positions", [])
            if isinstance(positions, list): all_positions.extend(positions)
        except Exception as exc:
            logging.warning(f"Position fetch for {underlying} failed: {exc}")
            continue
    return all_positions

def get_positions_for_symbol(symbol, all_positions=None):
    if all_positions is None: all_positions = get_all_positions()
    return [p for p in all_positions if isinstance(p, dict) and str(p.get("product_symbol","")).upper() == symbol.upper() and abs(float(p.get("size", 0) or 0)) > 0]

def position_side(p):
    s=str(p.get("side","")).lower()
    if s in ("buy","long"): return "buy"
    if s in ("sell","short"): return "sell"
    return "buy" if float(p.get("size",0) or 0)>0 else "sell" if float(p.get("size",0) or 0)<0 else None
def position_size(p): return abs(int(float(p.get("size", 0) or 0)))

def sync_positions_with_exchange(products):
    try: all_pos = get_all_positions()
    except Exception as exc:
        logging.warning("Sync failed: %s", exc)
        return
    for product in products:
        symbol = product.get("symbol")
        if not symbol: continue
        positions = get_positions_for_symbol(symbol, all_pos)
        if not positions:
            if symbol in state: state.pop(symbol, None); save_state()
            continue
        if len(positions) > 1: continue
        p = positions[0]; side = position_side(p)
        entry_price = p.get("entry_price") or p.get("average_entry_price")
        product_id = p.get("product_id") or product.get("id")
        if side is None or not entry_price or not product_id: continue
        old = state.get(symbol, {})
        state[symbol] = {"symbol": symbol, "product_id": int(product_id), "side": side, "entry_price": float(entry_price), "size": position_size(p), "highest_price": max(float(old.get("highest_price", entry_price)), float(entry_price)), "lowest_price": min(float(old.get("lowest_price", entry_price)), float(entry_price)), "entry_pending": False, "recovered": True}
        save_state()

def place_order(product, side, price, candle_id, rsi):
    symbol = product["symbol"]; product_id = int(product["id"])
    if not ENABLE_TRADING: return False
    if symbol in state: return False
    payload = {"product_id": product_id, "size": ORDER_SIZE, "side": side, "order_type": "market_order", "leverage": LEVERAGE}
    try:
        result = api_request("POST", "/v2/orders", payload=payload, auth=True)
        logging.warning(f"[ORDER] {side.upper()} {symbol} price={price} RSI={rsi:.1f}")
        state[symbol] = {"symbol": symbol, "product_id": product_id, "side": side, "entry_price": float(price), "size": ORDER_SIZE, "highest_price": float(price), "lowest_price": float(price), "entry_pending": True, "rsi_at_entry": float(rsi)}
        save_state(); return True
    except Exception as exc:
        bot_status["last_error"] = str(exc); logging.error("Order failed %s: %s", symbol, exc); return False

def close_position(symbol, position, reason):
    current_size = position_size(position)
    if current_size <= 0: return False
    side = position_side(position)
    if side not in ("buy", "sell"): return False
    close_side = "sell" if side == "buy" else "buy"
    product_id = position.get("product_id") or state.get(symbol, {}).get("product_id")
    if not product_id: return False
    payload = {"product_id": int(product_id), "size": current_size, "side": close_side, "order_type": "market_order", "reduce_only": True}
    try:
        result = api_request("POST", "/v2/orders", payload=payload, auth=True)
        logging.warning(f"[CLOSE] {symbol} {reason}")
        if symbol in state: state[symbol]["close_pending_at"] = time.time(); save_state()
        return True
    except Exception as exc:
        bot_status["last_error"] = str(exc); logging.error("Close failed %s: %s", symbol, exc); return False

def get_mark_price(position):
    for key in ("mark_price", "markPrice", "last_price"):
        v = position.get(key)
        try:
            if v not in (None, "", 0, "0") and float(v) > 0: return float(v)
        except: pass
    return None

def check_and_manage_positions(products):
    try: all_pos = get_all_positions()
    except Exception as exc:
        logging.warning("Position check failed: %s", exc)
        return
    for symbol in list(state.keys()):
        positions = get_positions_for_symbol(symbol, all_pos)
        if not positions:
            state.pop(symbol, None); save_state(); continue
        if len(positions) > 1: continue
        position = positions[0]; info = state.get(symbol, {})
        try: entry = float(position.get("entry_price") or info.get("entry_price"))
        except: continue
        mark_price = get_mark_price(position)
        if mark_price is None: continue
        highest = float(info.get("highest_price", entry)); lowest = float(info.get("lowest_price", entry))
        if position_side(position) == "buy":
            highest = max(highest, mark_price, entry); info["highest_price"] = highest
            trigger = highest * (1 - TRAILING_STOP_PERCENT / 100)
            logging.info(f"[TRAIL LONG] {symbol} mark={mark_price:.2f} trig={trigger:.2f}")
            save_state()
            if mark_price <= trigger: close_position(symbol, position, f"Long trail {mark_price:.2f} <= {trigger:.2f}")
        else:
            lowest = min(lowest, mark_price, entry); info["lowest_price"] = lowest
            trigger = lowest * (1 + TRAILING_STOP_PERCENT / 100)
            logging.info(f"[TRAIL SHORT] {symbol} mark={mark_price:.2f} trig={trigger:.2f}")
            save_state()
            if mark_price >= trigger: close_position(symbol, position, f"Short trail {mark_price:.2f} >= {trigger:.2f}")

def bot_loop():
    bot_status["running"] = True
    logging.info(f"BOT STARTED | RSI 35 STRICT | trading={ENABLE_TRADING}")
    while True:
        try:
            products = get_all_active_products()
            bot_status["products_scanned"] = len(products)
            sync_positions_with_exchange(products)
            check_and_manage_positions(products)
            for product in products:
                symbol = product.get("symbol")
                if not symbol or symbol in state: continue
                try:
                    signal, df, reason = scan_symbol(symbol)
                    if signal is None: continue
                    bot_status["last_signal"] = {"symbol": symbol, "side": signal["side"], "price": signal["price"], "time": time.time()}
                    logging.warning(f"[SIGNAL] {symbol} {signal['side'].upper()} price={signal['price']:.2f} RSI={signal['rsi']:.2f}")
                    place_order(product, signal["side"], signal["price"], signal["candle_id"], signal["rsi"])
                except Exception as exc:
                    logging.error(f"Scan failed {symbol}: {exc}")
                    bot_status["last_error"] = str(exc)
                time.sleep(0.5)
            bot_status["last_scan"] = time.time()
        except Exception as exc:
            bot_status["last_error"] = str(exc)
            logging.exception("Main loop error")
        time.sleep(POLL_SECONDS)

@app.route("/")
def home(): return jsonify({"bot": "Delta EMA RSI 35 STRICT", "trading": ENABLE_TRADING, "rsi": "Buy>35 Sell<35"})
@app.route("/status")
def status(): return jsonify({**bot_status, "open": list(state.keys())})
@app.route("/check")
def check(): return jsonify({"api_key": bool(API_KEY), "api_secret": bool(API_SECRET), "base": BASE_URL})

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=False, use_reloader=False)
