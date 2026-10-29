import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote
import pandas as pd
from flask import Flask
import threading

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot FINAL - POS CACHE FIX - Size 40 - No Instant Cut"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")
COINS = ["BTCUSD","ETHUSD","SOLUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
TRAIL_PCT = 0.005
STATE_FILE = "state.json"

# POS CACHE - API khali bheje to cache use karo
_last_pos_cache = []
_last_pos_time = 0

def load_state():
    try:
        with open(STATE_FILE,'r') as f:
            return json.load(f)
    except:
        return {"peaks":{}, "stops":{}, "last_trade":{}}

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
        elif method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=15)
            return {"success": True}, r.status_code
        else:
            r = requests.post(url, headers=headers, json=body, timeout=15)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def get_candles(symbol):
    end = int(time.time())
    start = end - (600 * 60)
    params = {"resolution": "5m", "symbol": symbol, "start": str(start), "end": str(end)}
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and res.get("result") and len(res["result"]) > 20:
        return res["result"]
    return []

def get_pos():
    global _last_pos_cache, _last_pos_time
    res, code = api_req("GET", "/v2/positions/margined")
    if code == 200 and res.get("result"):
        r = res["result"]
        lst = r if isinstance(r, list) else [r]
        # Non-zero positions filter karo
        filtered = [p for p in lst if float(p.get("size",0)) != 0]
        if filtered:
            _last_pos_cache = filtered
            _last_pos_time = time.time()
            return filtered
        # Agar API ne khali bheja par cache me hai to cache use karo
        if _last_pos_cache and (time.time() - _last_pos_time < 120):
            print(f"[POS CACHE] API empty, using cached {len(_last_pos_cache)} pos", flush=True)
            return _last_pos_cache
        return []
    # API fail hua to cache use karo
    if _last_pos_cache and (time.time() - _last_pos_time < 120):
        print(f"[POS CACHE] API fail {code}, using cached {len(_last_pos_cache)} pos", flush=True)
        return _last_pos_cache
    return []

def get_orders():
    res, code = api_req("GET", "/v2/orders", params={"states":"open"})
    if code == 200 and res.get("result"):
        return res["result"]
    return []

def cancel_all_stops(symbol):
    orders = get_orders()
    for o in orders:
        if o.get("product_symbol") == symbol and o.get("reduce_only"):
            oid = o.get("id")
            if oid:
                api_req("DELETE", f"/v2/orders/{oid}")

def place_stop(symbol, side, size, stop_price):
    sz = float(abs(float(size)))
    stop_side = "sell" if side == "buy" else "buy"
    price_str = f"{float(stop_price):.2f}"
    body = {
        "product_symbol": symbol,
        "size": sz,
        "side": stop_side,
        "order_type": "market_order",
        "stop_price": price_str,
        "reduce_only": True,
        "stop_trigger_method": "last_traded_price"
    }
    res, code = api_req("POST", "/v2/orders", body=body)
    print(f"[TRAIL STOP] {symbol} {stop_side} stop={price_str} -> {code}", flush=True)
    if code == 200:
        state["stops"][symbol] = float(stop_price)
        save_state(state)
    return res, code

def check_signal(candles, symbol):
    if len(candles) < 50:
        return None
    df = pd.DataFrame(candles)
