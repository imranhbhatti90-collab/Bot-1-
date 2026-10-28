import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote
import pandas as pd
from flask import Flask
import threading

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot Running 6 Coins 5m RSI40 0.5% FIXED"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

ALLOWED = ["BTCUSD","ETHUSD","SOLUSD","BNBUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
TIMEFRAME = "5m"
POLL = 30
ORDER_SIZE = 40
TRAIL = 0.5
RSI_BUY = 40
RSI_SELL = 60
COOLDOWN = 5
STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"positions": {}, "cooldown": {}, "stops": {}}

def save_state(s):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f)
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
            r = requests.get(url, headers=headers, timeout=10)
        else:
            r = requests.post(url, headers=headers, json=body, timeout=10)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def get_products():
    res, code = api_req("GET", "/v2/products")
    if code != 200 or not res.get("result"):
        print(f"[PRODUCTS FAIL] code={code}, using fallback", flush=True)
        out = []
        for s in ALLOWED:
            out.append({"symbol": s, "id": s})
        print(f"[INFO] Fallback {len(out)} coins", flush=True)
        return out
    active = []
    for p in res["result"]:
        sym = p.get("symbol", "")
        if sym not in ALLOWED:
            continue
        if not p.get("is_active", True):
            continue
        active.append(p)
    print(f"[INFO] {len(active)} coins", flush=True)
    return active

def get_candles(pid):
    import time as t
    end = int(t.time())
    start = end - (100 * 5 * 60)
    params = {"resolution": TIMEFRAME, "product_id": str(pid), "start": str(start), "end": str(end)}
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and res.get("result") and len(res["result"]) > 0:
        print(f"[CANDLES OK] {pid} {len(res['result'])} candles", flush=True)
        return res["result"]
    params2 = {"resolution": TIMEFRAME, "symbol": str(pid), "start": str(start), "end": str(end)}
    res2, code2 = api_req("GET", "/v2/history/candles", params=params2)
    if code2 == 200 and res2.get("result") and len(res2["result"]) > 0:
        print(f"[CANDLES OK2] {pid} {len(res2['result'])} candles", flush=True)
        return res2["result"]
    print(f"[CANDLES FAIL] {pid} code={code}", flush=True)
    return []

def get_pos():
    res, code = api_req("GET", "/v2/positions/margined")
    if code == 200 and res.get("result"):
        r = res["result"]
        return r if isinstance(r, list) else [r]
    return []

def check_signal(candles):
    if len(candles) < 50:
        print(f"[CHECK FAIL] {len(candles)} candles", flush=True)
        return None
    try:
        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles)
            df['close'] = pd.to_numeric(df['close'])
        else:
            df = pd.DataFrame(candles, columns=['time','open','high','low','close','volume'])
            df['close'] = pd.to_numeric(df['close'])
    except Exception as e:
        print(f"[CHECK ERROR] {e}", flush=True)
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
    ema9 = last['ema9']
    ema21 = last['ema21']
    ema9_prev = prev['ema9']
    ema21_prev = prev['ema21']
    rsi = last['rsi']
    print(f"[CHECK] EMA9 {ema9:.1f} EMA21 {ema21:.1f} RSI {rsi:.1f}", flush=True)
    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21
    if buy_cross and rsi > RSI_BUY:
        print(f"[BUY SIGNAL] RSI {rsi:.1f}", flush=True)
        return "buy"
    if sell_cross and rsi < RSI_SELL:
        print(f"[SELL SIGNAL] RSI {rsi:.1f}", flush=True)
        return "sell"
    return None

def place_order(sym, side, size, close=False, stop=None):
    sz = abs(float(size))
    if sz < 1:
        sz = ORDER_SIZE
    if stop:
        body = {"product_symbol": sym, "size": sz, "side": side, "order_type": "stop_market_order", "stop_price": str(stop), "reduce_only": True}
    else:
        body = {"product_symbol": sym, "size": sz, "side": side, "order_type": "market_order"}
        if close:
            body["reduce_only"] = True
    res, code = api_req("POST", "/v2/orders", body=body)
    print(f"[ORDER] {sym} {side} {sz} -> {code}", flush=True)
    return res, code

def main_loop():
    print("=== BOT START 6 COINS 5m RSI40 0.5% FIXED ===", flush=True)
    prods = get_products()
    while True:
        try:
            positions = get_pos()
            if positions:
                print(f"[POS] {len(positions)}", flush=True)
            for prod in prods:
                sym = prod["symbol"]
                pid = prod.get("id", sym)
                lt = state.get("cooldown", {}).get(sym, 0)
                if time.time() - lt < COOLDOWN * 60:
                    continue
                candles = get_candles(pid)
                if not candles:
                    print(f"[SKIP] {sym} no candles", flush=True)
                    continue
                sig = check_signal(candles)
                if sig:
                    has = False
                    for p in positions:
                        if p.get("product_symbol") == sym or p.get("symbol") == sym:
                            has = True
                            break
                    if has:
                        print(f"[SKIP] {sym} has pos", flush=True)
                        continue
                    lc = candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4]
                    print(f"[SIGNAL] {sym} {sig} at {lc}", flush=True)
                    res, code = place_order(sym, sig, ORDER_SIZE, close=False)
                    if code == 200:
                        state["positions"][sym] = {"side": sig, "entry": float(lc)}
                        state["cooldown"][sym] = time.time()
                        save_state(state)
                else:
                    print(f"[NO TRADE] {sym}", flush=True)
            time.sleep(POLL)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            time.sleep(10)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    main_loop()
