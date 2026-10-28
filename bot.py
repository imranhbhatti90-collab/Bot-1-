import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote
import pandas as pd
from flask import Flask
import threading

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot DEBUG - Checking API Keys"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")
COINS = ["BTCUSD","ETHUSD","SOLUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
STATE_FILE = "state.json"

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
            print(f"[DELETE RESP] {r.status_code} {r.text[:500]}", flush=True)
            return {"success": True}, r.status_code
        else:
            r = requests.post(url, headers=headers, json=body, timeout=15)
        # DEBUG LINE - exact error dikhega
        print(f"[API DEBUG] {method} {path} -> {r.status_code} | {r.text[:1000]}", flush=True)
        try:
            return r.json(), r.status_code
        except:
            return {"raw": r.text}, r.status_code
    except Exception as e:
        print(f"[API ERROR] {e}", flush=True)
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
    ema9 = float(last['ema9'])
    ema21 = float(last['ema21'])
    rsi = float(last['rsi'])
    print(f"[CHECK] {symbol} EMA9 {ema9:.1f} EMA21 {ema21:.1f} RSI {rsi:.1f}", flush=True)
    if ema9 > ema21 and rsi > 40:
        return "buy"
    if ema9 < ema21 and rsi < 60:
        return "sell"
    return None

def place_entry(symbol, side):
    body = {"product_symbol": symbol, "size": 40, "side": side, "order_type": "market_order"}
    res, code = api_req("POST", "/v2/orders", body=body)
    print(f"[ENTRY] {symbol} {side} -> {code}", flush=True)
    return res, code

def main_loop():
    print(f"=== DEBUG BOT START - KEY:{API_KEY[:5]}... SECRET:{'SET' if API_SECRET else 'EMPTY'} ===", flush=True)
    while True:
        try:
            positions = get_pos()
            held = []
            for p in positions:
                sz = float(p.get("size", 0))
                if sz == 0:
                    continue
                ps = p.get("product_symbol") or p.get("product",{}).get("symbol") or ""
                if ps and ps in COINS:
                    held.append(ps)
            held = list(set(held))
            print(f"[POS HELD] {held if held else '[] - Fresh start'}", flush=True)
            for symbol in COINS:
                candles = get_candles(symbol)
                if not candles:
                    continue
                sig = check_signal(candles, symbol)
                if sig:
                    if symbol in held:
                        print(f"[SKIP] {symbol} has pos", flush=True)
                        continue
                    last_t = state["last_trade"].get(symbol, 0)
                    if time.time() - last_t < 300:
                        print(f"[SKIP] {symbol} cooldown {int(300 - (time.time()-last_t))}s", flush=True)
                        continue
                    print(f"[SIGNAL] {symbol} {sig}", flush=True)
                    place_entry(symbol, sig)
                    curr = float(candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4])
                    state["peaks"][symbol] = curr
                    state["stops"].pop(symbol, None)
                    state["last_trade"][symbol] = time.time()
                    save_state(state)
                    held.append(symbol)
                else:
                    print(f"[NO TRADE] {symbol}", flush=True)
            print("--- SLEEP 30s ---", flush=True)
            time.sleep(30)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    main_loop()
