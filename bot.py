import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote
import pandas as pd
from flask import Flask
import threading

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot Running TRAIL 0.5% FIXED"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINS = ["BTCUSD","ETHUSD","SOLUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
TRAIL_PCT = 0.005
STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE,'r') as f:
            return json.load(f)
    except:
        return {"peaks":{}, "stops":{}}

def save_state(s):
    try:
        with open(STATE_FILE,'w') as f:
            json.dump(s,f)
    except:
        pass

state = load_state()

def api_req(method, path, params=None, body=None):
    ts = str(int(time.time()))
    if params:
        qs = urlencode(params, quote_via=quote)
        pqs = f"{path}?{qs}"
    else:
        pqs = path
    if body:
        msg = method + ts + pqs + json.dumps(body)
    else:
        msg = method + ts + pqs
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    headers = {"api-key": API_KEY, "timestamp": ts, "signature": sig, "Content-Type": "application/json"}
    url = BASE_URL + pqs
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=15)
        else:
            r = requests.post(url, headers=headers, json=body, timeout=15)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def get_candles(symbol):
    end = int(time.time())
    start = end - (500 * 60)
    params = {"resolution": "5m", "symbol": symbol, "start": str(start), "end": str(end)}
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and res.get("result") and len(res["result"]) > 10:
        print(f"[CANDLES OK] {symbol} {len(res['result'])}", flush=True)
        return res["result"]
    print(f"[CANDLES FAIL] {symbol} code={code}", flush=True)
    return []

def get_pos():
    res, code = api_req("GET", "/v2/positions/margined")
    if code == 200 and res.get("result"):
        r = res["result"]
        return r if isinstance(r, list) else [r]
    return []

def get_orders():
    res, code = api_req("GET", "/v2/orders", params={"states":"open"})
    if code == 200 and res.get("result"):
        return res["result"]
    return []

def cancel_all_stops(symbol):
    orders = get_orders()
    for o in orders:
        if o.get("product_symbol") == symbol:
            oid = o.get("id")
            if oid:
                api_req("DELETE", f"/v2/orders/{oid}")

def place_stop(symbol, side, size, stop_price):
    sz = int(abs(float(size)))
    if sz < 1:
        sz = 40
    stop_side = "sell" if side == "buy" else "buy"
    body = {
        "product_symbol": symbol,
        "size": sz,
        "side": stop_side,
        "order_type": "market_order",
        "stop_price": str(round(stop_price,1)),
        "reduce_only": True,
        "stop_trigger_method": "last_traded_price"
    }
    res, code = api_req("POST", "/v2/orders", body=body)
    print(f"[TRAIL STOP] {symbol} {stop_side} stop={stop_price:.1f} -> {code}", flush=True)
    if code == 200:
        state["stops"][symbol] = stop_price
        save_state(state)
    return res, code

def check_signal(candles, symbol):
    if len(candles) < 50:
        return None
    df = pd.DataFrame(candles)
    if 'close' not in df.columns:
        df = pd.DataFrame(candles, columns=['time','open','high','low','close','volume'])
    df['close'] = pd.to_numeric(df['close'])
    df['ema9'] = df['close'].ewm(span=9).mean()
    df['ema21'] = df['close'].ewm(span=21).mean()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    last = df.iloc[-1]
    prev = df.iloc[-2]
    ema9 = last['ema9']
    ema21 = last['ema21']
    ema9_prev = prev['ema9']
    ema21_prev = prev['ema21']
    rsi = last['rsi']
    print(f"[CHECK] {symbol} EMA9 {ema9:.1f} EMA21 {ema21:.1f} RSI {rsi:.1f}", flush=True)
    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21
    if buy_cross and rsi > 40:
        return "buy"
    if sell_cross and rsi < 60:
        return "sell"
    return None

def place_entry(symbol, side, size):
    sz = 40
    body = {"product_symbol": symbol, "size": sz, "side": side, "order_type": "market_order"}
    res, code = api_req("POST", "/v2/orders", body=body)
    print(f"[ENTRY] {symbol} {side} -> {code}", flush=True)
    return res, code

def handle_trailing():
    positions = get_pos()
    for p in positions:
        prod = p.get("product", {})
        spot = prod.get("spot_index", {}) if isinstance(prod, dict) else {}
        underlying = spot.get("underlying_asset") or prod.get("underlying_asset")
        symbol = p.get("product_symbol") or p.get("symbol") or (f"{underlying}USD" if underlying else None)
        if not symbol or symbol not in COINS:
            continue
        size = float(p.get("size", 0))
        if size == 0:
            continue
        entry = float(p.get("entry_price") or p.get("average_price") or 0)
        if entry == 0:
            continue
        candles = get_candles(symbol)
        if not candles:
            continue
        last_c = candles[-1]
        curr = float(last_c['close'] if isinstance(last_c, dict) else last_c[4])
        old_peak = state["peaks"].get(symbol, entry)
        if size > 0:
            new_peak = max(old_peak, curr)
            state["peaks"][symbol] = new_peak
            trail_stop = new_peak * (1 - TRAIL_PCT)
            print(f"[TRAIL] {symbol} LONG {curr:.1f} stop {trail_stop:.1f}", flush=True)
            old_stop = state["stops"].get(symbol, 0)
            if trail_stop > old_stop:
                cancel_all_stops(symbol)
                place_stop(symbol, "buy", size, trail_stop)
        else:
            new_peak = min(old_peak, curr) if old_peak != 0 else curr
            state["peaks"][symbol] = new_peak
            trail_stop = new_peak * (1 + TRAIL_PCT)
            print(f"[TRAIL] {symbol} SHORT {curr:.1f} stop {trail_stop:.1f}", flush=True)
            old_stop = state["stops"].get(symbol, 9999999)
            if trail_stop < old_stop or old_stop == 9999999:
                cancel_all_stops(symbol)
                place_stop(symbol, "sell", size, trail_stop)
        save_state(state)

def main_loop():
    print("=== BOT START TRAIL 0.5% ===", flush=True)
    while True:
        try:
            handle_trailing()
            positions = get_pos()
            for symbol in COINS:
                candles = get_candles(symbol)
                if not candles:
                    continue
                sig = check_signal(candles, symbol)
                if sig:
                    has = False
                    for p in positions:
                        prod2 = p.get("product", {})
                        spot2 = prod2.get("spot_index", {}) if isinstance(prod2, dict) else {}
                        und2 = spot2.get("underlying_asset") or prod2.get("underlying_asset")
                        psym = p.get("product_symbol") or p.get("symbol") or (f"{und2}USD" if und2 else "")
                        if psym == symbol:
                            has = True
                            break
                    if has:
                        print(f"[SKIP] {symbol} has pos", flush=True)
                        continue
                    print(f"[SIGNAL] {symbol} {sig}", flush=True)
                    place_entry(symbol, sig, 40)
                    last_c = candles[-1]
                    curr = float(last_c['close'] if isinstance(last_c, dict) else last_c[4])
                    state["peaks"][symbol] = curr
                    state["stops"].pop(symbol, None)
                    save_state(state)
                else:
                    print(f"[NO TRADE] {symbol}", flush=True)
            time.sleep(30)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            time.sleep(10)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    main_loop()
