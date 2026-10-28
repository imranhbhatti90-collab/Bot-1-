import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote
import pandas as pd
from flask import Flask
import threading

app = Flask(__name__)
@app.route('/')
def home():
    return "Delta Bot Running - 6 Coins - 5m - RSI 40 - 0.5% Trail Visible"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

ALLOWED_COINS_NEW = ["BTCUSD","ETHUSD","SOLUSD","BNBUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
ALLOWED_COINS_ALL = ALLOWED_COINS_NEW + ["USOONUSD"]

TIMEFRAME = "5m"  # Aapki demand - 5m wapas
POLL_SECONDS = 30  # 5m ke liye 30 sec
ORDER_SIZE = 40
TRAILING_STOP_PERCENT = 0.5
RSI_BUY = 40  # Aapki demand - RSI 40 both side
RSI_SELL = 60  # Sell ke liye 60 taaki trades milen, strict 40 karna ho to 40 kar do
PROFIT_LOCK_ENABLE = True
PROFIT_ACTIVATE = 20.0
PROFIT_FLOOR = 10.0
COOLDOWN_MINUTES = 5  # 5m ke liye 5 min cooldown
STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"positions": {}, "cooldown": {}, "stop_orders": {}}

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
    headers = {"api-key": API_KEY, "timestamp": timestamp, "signature": signature, "Content-Type": "application/json"}
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
        return [{"symbol": s, "id": s} for s in ALLOWED_COINS_NEW]
    products = res["result"]
    active = []
    for p in products:
        symbol = p.get("symbol", "")
        if symbol not in ALLOWED_COINS_NEW:
            continue
        if not p.get("is_active", True):
            continue
        if "_USDT" in symbol or "1000" in symbol:
            continue
        active.append(p)
    print(f"[INFO] Found {len(active)} ALLOWED: {[p['symbol'] for p in active]}", flush=True)
    return active

def get_candles(product_id, resolution="5m", count=100):
    params = {"resolution": resolution, "symbol": product_id, "count": count}
    res, code = api_request("GET", "/v2/history/candles", params=params)
    if code == 200 and res.get("result"):
        return res["result"]
    return []

def get_all_positions():
    all_pos = []
    for coin in ALLOWED_COINS_ALL:
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
    print(f"[CHECK] EMA9 {ema9:.2f} EMA21 {ema21:.2f} RSI {rsi:.2f}", flush=True)
    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21
    if buy_cross and rsi > RSI_BUY:
        print(f"[SIGNAL BUY] RSI {rsi:.2f} > {RSI_BUY}", flush=True)
        return "buy"
    if sell_cross and rsi < RSI_SELL:
        print(f"[SIGNAL SELL] RSI {rsi:.2f} < {RSI_SELL}", flush=True)
        return "sell"
    return None

def place_order(symbol, side, size, is_close=False, stop_price=None):
    actual_size = abs(float(size))
    if actual_size < 1:
        actual_size = ORDER_SIZE
    if stop_price:
        # Stop loss order jo chart par dikhega
        body = {
            "product_symbol": symbol,
            "size": actual_size,
            "side": side,
            "order_type": "stop_market_order",
            "stop_price": str(stop_price),
            "reduce_only": True
        }
    else:
        body = {
            "product_symbol": symbol,
            "size": actual_size,
            "side": side,
            "order_type": "market_order"
        }
        if is_close:
            body["reduce_only"] = True
    res, code = api_request("POST", "/v2/orders", body=body)
    print(f"[ORDER] {symbol} {side} {actual_size} stop={stop_price} -> {code}", flush=True)
    return res, code

def cancel_stop_orders(symbol):
    # Purane stop orders cancel karo
    res, code = api_request("GET", "/v2/orders", params={"product_symbol": symbol, "state": "open"})
    if code == 200 and res.get("result"):
        for order in res["result"]:
            if order.get("order_type") in ["stop_market_order", "stop_limit_order"]:
                oid = order.get("id")
                api_request("DELETE", f"/v2/orders/{oid}")
                print(f"[CANCEL STOP] {symbol} order {oid}", flush=True)

def update_trailing_stop(symbol, side, trigger_price):
    # Chart par dikhne wala trailing stop update karo
    actual_side = "sell" if side == "buy" else "buy"
    cancel_stop_orders(symbol)
    time.sleep(0.5)
    place_order(symbol, actual_side, ORDER_SIZE, is_close=True, stop_price=trigger_price)
    print(f"[TRAIL VISIBLE] {symbol} {side} Stop at {trigger_price} - Chart par dikhega!", flush=True)

def main_loop():
    print(f"=== Delta Bot Started - 6 Coins - 5m - RSI {RSI_BUY}/{RSI_SELL} - 0.5% Trail Visible ===", flush=True)
    products = get_all_active_products()
    while True:
        try:
            positions = get_all_positions()
            if positions:
                print(f"[POS] {len(positions)} open", flush=True)
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
                    # Find position
                    pos_side = "buy"
                    mark_price = 0
                    pos_size = ORDER_SIZE
                    for p in positions:
                        if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
                            mark_price = float(p.get("mark_price", 0) or p.get("entry_price", 0) or 0)
                            pos_side = p.get("side", "buy")
                            pos_size = float(p.get("size", ORDER_SIZE))
                            break
                    if mark_price == 0 and candles:
                        try:
                            mark_price = float(candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4])
                        except:
                            mark_price = 0
                    if mark_price > 0:
                        pos_data = state["positions"].get(symbol, {})
                        entry = pos_data.get("entry_price", mark_price)
                        highest = pos_data.get("highest_price", entry)
                        lowest = pos_data.get("lowest_price", entry)
                        if pos_side == "buy":
                            highest = max(highest, mark_price)
                            trigger = highest * (1 - TRAILING_STOP_PERCENT / 100)
                            # Chart par visible stop update karo
                            last_stop = state.get("stop_orders", {}).get(symbol, 0)
                            if abs(trigger - last_stop) > mark_price * 0.001:  # 0.1% se zyada change
                                update_trailing_stop(symbol, pos_side, trigger)
                                if "stop_orders" not in state:
                                    state["stop_orders"] = {}
                                state["stop_orders"][symbol] = trigger
                                save_state(state)
                            if mark_price <= trigger:
                                print(f"[TRAIL LONG {TRAILING_STOP_PERCENT}%] {symbol} Highest {highest:.2f} Trigger {trigger:.2f} Mark {mark_price:.2f} => CLOSE", flush=True)
                                cancel_stop_orders(symbol)
                                place_order(symbol, "sell", pos_size, is_close=True)
                                state["cooldown"][symbol] = time.time()
                                if symbol in state["positions"]:
                                    del state["positions"][symbol]
                                save_state(state)
                                continue
                            else:
                                pos_data["highest_price"] = highest
                                pos_data["entry_price"] = entry
                                pos_data["side"] = pos_side
                                state["positions"][symbol] = pos_data
                                save_state(state)
                        else:  # short
                            lowest = min(lowest, mark_price) if lowest else mark_price
                            trigger = lowest * (1 + TRAILING_STOP_PERCENT / 100)
                            last_stop = state.get("stop_orders", {}).get(symbol, 0)
                            if abs(trigger - last_stop) > mark_price * 0.001:
                                update_trailing_stop(symbol, pos_side, trigger)
                                if "stop_orders" not in state:
                                    state["stop_orders"] = {}
                                state["stop_orders"][symbol] = trigger
                                save_state(state)
                            if mark_price >= trigger:
                                print(f"[TRAIL SHORT {TRAILING_STOP_PERCENT}%] {symbol} Lowest {lowest:.2f} Trigger {trigger:.2f} Mark {mark_price:.2f} => CLOSE", flush=True)
                                cancel_stop_orders(symbol)
                                place_order(symbol, "buy", pos_size, is_close=True)
                                state["cooldown"][symbol] = time.time()
                                if symbol in state["positions"]:
                                    del state["positions"][symbol]
                                save_state(state)
                                continue
                            else:
                                pos_data["lowest_price"] = lowest
                                pos_data["entry_price"] = entry
                                pos_data["side"] = pos_side
                                state["positions"][symbol] = pos_data
                                save_state(state)
                signal = check_signal(candles)
                if signal:
                    has_pos = any((p.get("product_symbol") == symbol or p.get("symbol") == symbol) for p in positions)
                    if has_pos:
                        print(f"[SKIP] {symbol} already has position", flush=True)
                        continue
                    last_close = candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4]
                    print(f"[SIGNAL] {symbol} {signal} at {last_close}", flush=True)
                    res, code = place_order(symbol, signal, ORDER_SIZE, is_close=False)
                    if code == 200:
                        state["positions"][symbol] = {
                            "side": signal,
                            "entry_price": float(last_close),
                            "highest_price": float(last_close),
                            "lowest_price": float(last_close),
                            "peak_pnl": 0
                        }
                        state["cooldown"][symbol] = time.time()
                        # Initial stop loss lagao jo chart par dikhega
                        initial_trigger = float(last_close) * (0.995 if signal == "buy" else 1.005)
                        update_trailing_stop(symbol, signal, initial_trigger)
                        save_state(state)
                else:
                    print(f"[NO TRADE] {symbol} No EMA cross", flush=True)
            time.sleep(POLL_SECONDS)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    main_loop()
