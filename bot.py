import os
import json
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode, quote
from datetime import datetime
import pandas as pd

# ========= CONFIG =========
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

# Sirf ye 7 coins - BTC, ETH, SOL, GOLD, SILVER, CRUDE OIL, BNB
ALLOWED_COINS = [
    "BTCUSD",      # Bitcoin
    "ETHUSD",      # Ethereum
    "SOLUSD",      # Solana
    "BNBUSD",      # BNB
    "XAUTUSD",     # Gold
    "PAXGUSD",     # Gold
    "SILVERUSD",   # Silver
    "USOONUSD",    # Crude Oil
]

TIMEFRAME = "15m"
POLL_SECONDS = 60
ORDER_SIZE = 40
TRAILING_STOP_PERCENT = 0.5  # 0.2% se 0.5% kiya

PROFIT_LOCK_ENABLE = True
PROFIT_ACTIVATE = 20.0
PROFIT_FLOOR = 10.0
COOLDOWN_MINUTES = 30
STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"positions": {}, "cooldown": {}}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except:
        pass

state = load_state()

def api_request(method, path, params=None, body=None):
    timestamp = str(int(time.time()))
    query_string = ""
    if params:
        query_string = urlencode(params, quote_via=quote)
        path_with_query = f"{path}?{query_string}"
    else:
        path_with_query = path

    if body:
        message = method + timestamp + path_with_query + json.dumps(body)
    else:
        message = method + timestamp + path_with_query

    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json"
    }
    url = BASE_URL + path_with_query
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        else:
            r = requests.post(url, headers=headers, json=body, timeout=10)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def get_all_active_products():
    res, code = api_request("GET", "/v2/products")
    if code != 200 or not res.get("result"):
        return [{"symbol": s, "id": s} for s in ALLOWED_COINS]
    products = res["result"]
    active = []
    for p in products:
        symbol = p.get("symbol", "")
        if symbol not in ALLOWED_COINS:
            continue
        if not p.get("is_active", True):
            continue
        if "_USDT" in symbol or "1000" in symbol:
            continue
        active.append(p)
    print(f"[INFO] Found {len(active)} ALLOWED: {[p['symbol'] for p in active]}")
    return active

def get_candles(product_id, resolution="15m", count=100):
    params = {"resolution": resolution, "symbol": product_id, "count": count}
    res, code = api_request("GET", "/v2/history/candles", params=params)
    if code == 200 and res.get("result"):
        return res["result"]
    return []

def get_all_positions():
    all_pos = []
    for coin in ALLOWED_COINS:
        underlying = coin.replace("USD", "")
        res, code = api_request("GET", "/v2/positions", params={"underlying_asset_symbol": underlying})
        if code == 200 and res.get("result"):
            result = res["result"]
            if isinstance(result, list):
                all_pos.extend(result)
            elif isinstance(result, dict):
                all_pos.append(result)
    if not all_pos:
        res, code = api_request("GET", "/v2/positions/margined")
        if code == 200 and res.get("result"):
            all_pos = res["result"] if isinstance(res["result"], list) else [res["result"]]
    return all_pos

def get_current_pnl(symbol, positions):
    for p in positions:
        if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
            return float(p.get("unrealized_pnl", 0) or p.get("pnl", 0) or 0)
    return 0

def check_signal(candles):
    if len(candles) < 50:
        return None
    try:
        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles)
            df['close'] = pd.to_numeric(df['close'])
        else:
            df = pd.DataFrame(candles, columns=['time','open','high','low','close','volume'])
            df['close'] = pd.to_numeric(df['close'])
    except:
        return None
    df['ema9'] = df['close'].ewm(span=9).mean()
    df['ema21'] = df['close'].ewm(span=21).mean()
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    last = df.iloc[-1]
    prev = df.iloc[-2]
    ema9, ema21 = last['ema9'], last['ema21']
    ema9_prev, ema21_prev = prev['ema9'], prev['ema21']
    rsi = last['rsi']
    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21
    if buy_cross and rsi > 35:
        return "buy"
    if sell_cross and rsi < 65:
        return "sell"
    return None

def place_order(symbol, side, size):
    if size < 1:
        size = ORDER_SIZE
    body = {"product_symbol": symbol, "size": size, "side": side, "order_type": "market_order"}
    res, code = api_request("POST", "/v2/orders", body=body)
    print(f"[ORDER] {symbol} {side} {size} -> {code} {res}")
    return res, code

def close_position(symbol, size, side):
    close_side = "sell" if side == "buy" else "buy"
    if size < 1:
        size = ORDER_SIZE
    return place_order(symbol, close_side, size)

def check_profit_lock(symbol, current_pnl):
    if not PROFIT_LOCK_ENABLE:
        return False
    pos_data = state["positions"].get(symbol, {})
    peak = pos_data.get("peak_pnl", current_pnl)
    if current_pnl > peak:
        peak = current_pnl
        pos_data["peak_pnl"] = peak
        state["positions"][symbol] = pos_data
        save_state(state)
    if peak >= PROFIT_ACTIVATE and current_pnl <= PROFIT_FLOOR:
        print(f"[PROFIT LOCK] {symbol} Peak {peak} -> Current {current_pnl} => CLOSE")
        return True
    if peak >= PROFIT_ACTIVATE and current_pnl <= peak * 0.5:
        print(f"[PROFIT LOCK 50%] {symbol} Peak {peak} -> Current {current_pnl} => CLOSE")
        return True
    return False

def main_loop():
    print("=== Delta Bot Started - 7 Coins Only - 15m - 0.5% ===")
    products = get_all_active_products()
    while True:
        try:
            positions = get_all_positions()
            if positions:
                print(f"[POS] {len(positions)} open")
            for prod in products:
                symbol = prod["symbol"]
                pid = prod.get("id", symbol)
                last_time = state.get("cooldown", {}).get(symbol, 0)
                if time.time() - last_time < COOLDOWN_MINUTES * 60:
                    continue
                candles = get_candles(pid, resolution=TIMEFRAME, count=100)
                if not candles:
                    continue
                current_pnl = get_current_pnl(symbol, positions)
                if current_pnl != 0:
                    if check_profit_lock(symbol, current_pnl):
                        for p in positions:
                            if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
                                size = float(p.get("size", ORDER_SIZE))
                                side = p.get("side", "buy")
                                close_position(symbol, size, side)
                                state["cooldown"][symbol] = time.time()
                                if symbol in state["positions"]:
                                    del state["positions"][symbol]
                                save_state(state)
                                break
                        continue
                    pos_data = state["positions"].get(symbol, {})
                    mark_price = 0
                    for p in positions:
                        if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
                            mark_price = float(p.get("mark_price", 0) or p.get("entry_price", 0) or 0)
                            break
                    if mark_price == 0 and candles:
                        try:
                            mark_price = float(candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4])
                        except:
                            mark_price = 0
                    if mark_price > 0:
                        entry = pos_data.get("entry_price", mark_price)
                        highest = pos_data.get("highest_price", entry)
                        lowest = pos_data.get("lowest_price", entry)
                        if pos_data.get("side") == "buy":
                            highest = max(highest, mark_price)
                            trigger = highest * (1 - TRAILING_STOP_PERCENT / 100)
                            if mark_price <= trigger:
                                print(f"[TRAIL LONG {TRAILING_STOP_PERCENT}%] {symbol} Highest {highest} Trigger {trigger} => CLOSE")
                                for p in positions:
                                    if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
                                        size = float(p.get("size", ORDER_SIZE))
                                        side = p.get("side", "buy")
                                        close_position(symbol, size, side)
                                        state["cooldown"][symbol] = time.time()
                                        if symbol in state["positions"]:
                                            del state["positions"][symbol]
                                        save_state(state)
                                        break
                            else:
                                pos_data["highest_price"] = highest
                                state["positions"][symbol] = pos_data
                                save_state(state)
                        else:
                            lowest = min(lowest, mark_price) if lowest else mark_price
                            trigger = lowest * (1 + TRAILING_STOP_PERCENT / 100)
                            if mark_price >= trigger:
                                print(f"[TRAIL SHORT {TRAILING_STOP_PERCENT}%] {symbol} Lowest {lowest} Trigger {trigger} => CLOSE")
                                for p in positions:
                                    if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
                                        size = float(p.get("size", ORDER_SIZE))
                                        side = p.get("side", "sell")
                                        close_position(symbol, size, side)
                                        state["cooldown"][symbol] = time.time()
                                        if symbol in state["positions"]:
                                            del state["positions"][symbol]
                                        save_state(state)
                                        break
                            else:
                                pos_data["lowest_price"] = lowest
                                state["positions"][symbol] = pos_data
                                save_state(state)
                signal = check_signal(candles)
                if signal:
                    has_pos = any((p.get("product_symbol") == symbol or p.get("symbol") == symbol) for p in positions)
                    if has_pos:
                        continue
                    last_close = candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4]
                    print(f"[SIGNAL] {symbol} {signal}")
                    res, code = place_order(symbol, signal, ORDER_SIZE)
                    if code == 200:
                        state["positions"][symbol] = {
                            "side": signal,
                            "entry_price": float(last_close),
                            "highest_price": float(last_close),
                            "lowest_price": float(last_close),
                            "peak_pnl": 0
                        }
                        state["cooldown"][symbol] = time.time()
                        save_state(state)
            time.sleep(POLL_SECONDS)
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_loop()
