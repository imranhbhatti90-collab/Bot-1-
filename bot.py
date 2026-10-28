import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote
import pandas as pd
from flask import Flask
import threading

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot Running FINAL FIXED"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINS = ["BTCUSD","ETHUSD","SOLUSD","BNBUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
TIMEFRAME = "5m"
POLL = 30
ORDER_SIZE = 40
RSI_BUY = 40
RSI_SELL = 60
COOLDOWN = 5

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

def get_candles(symbol):
    end = int(time.time())
    start = end - (500 * 60)
    params = {"resolution": "5m", "symbol": symbol, "start": str(start), "end": str(end)}
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and res.get("result") and len(res["result"]) > 0:
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

def check_signal(candles, symbol):
    if len(candles) < 50:
        return None
    try:
        df = pd.DataFrame(candles)
        if 'close' not in df.columns:
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
    ema9 = last['ema9']
    ema21 = last['ema21']
    ema9_prev = prev['ema9']
    ema21_prev = prev['ema21']
    rsi = last['rsi']
    print(f"[CHECK] {symbol} EMA9 {ema9:.1f} EMA21 {ema21:.1f} RSI {rsi:.1f}", flush=True)
    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21
    if buy_cross and rsi > RSI_BUY:
        print(f"[BUY SIGNAL] {symbol} RSI {rsi:.1f}", flush=True)
        return "buy"
    if sell_cross and rsi < RSI_SELL:
        print(f"[SELL SIGNAL] {symbol} RSI {rsi:.1f}", flush=True)
        return "sell"
    return None

def place_order(sym, side, size):
    sz = abs(float(size))
    if sz < 1:
        sz = ORDER_SIZE
    body = {"product_symbol": sym, "size": sz, "side": side, "order_type": "market_order", "reduce_only": False}
    res, code = api_req("POST", "/v2/orders", body=body)
    print(f"[ORDER] {sym} {side} {sz} -> {code}", flush=True)
    return res, code

def main_loop():
    print("=== BOT START FINAL - ONLY SYMBOL - NO ID ===", flush=True)
    while True:
        try:
            positions = get_pos()
            if positions:
                print(f"[POS] {len(positions)}", flush=True)
            for symbol in COINS:
                candles = get_candles(symbol)
                if not candles:
                    print(f"[SKIP] {symbol} no candles", flush=True)
                    continue
                sig = check_signal(candles, symbol)
                if sig:
                    has = False
                    for p in positions:
                        if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
                            has = True
                            break
                    if has:
                        print(f"[SKIP] {symbol} has pos", flush=True)
                        continue
                    print(f"[SIGNAL] {symbol} {sig}", flush=True)
                    place_order(symbol, sig, ORDER_SIZE)
                else:
                    print(f"[NO TRADE] {symbol}", flush=True)
            time.sleep(POLL)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            time.sleep(10)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    main_loop()
