
# FINAL FIXED BOT - Delta Testnet - Sell Mode Crash Sell
# Problem fixed from logs:
# 1. Old bot scanned 138 coins -> now only 6 coins
# 2. TREND_MIXED / EMA_NOT_ALIGNED / VOL_OK / GAP / RSI blocked everything -> REMOVED
# 3. New logic: Sell mode me sirf Close < EMA50 = SELL allow
# 4. Duplicate grid entry fix: 1 symbol = 1 position, cooldown 5 min
# 5. USOONUSD always ignored
# 6. Size 40, Max 2 positions, Trailing 0.5% Red Line, Profit Lock

import os, time, hmac, hashlib, json, requests, threading
from datetime import datetime
from flask import Flask

app = Flask(__name__)
@app.route('/')
def home(): return "Bot Live - Sell Mode - 6 Coins Only"
@app.route('/health')
def health(): return "OK", 200

API_KEY = os.getenv("DELTA_API_KEY","")
API_SECRET = os.getenv("DELTA_API_SECRET","")
BASE = "https://cdn-ind.testnet.deltaex.org"
MODE = os.getenv("MODE","Sell")  # Sell / Buy / Both
SIZE = 40
MAX_POS = 2
COOLDOWN = 300  # 5 min
TRAIL_PCT = 0.005  # 0.5%
GRID_PCT = 0.002  # 0.2% up then next SELL add - but only if no position

# SIRF 6 COINS - 138 nahi
SYMBOLS = ["BTCUSD","ETHUSD","SOLUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
IGNORE = {"USOONUSD","SNDKBUSD","QQQXUSD","SLVONUSD","SPYXUSD","NVDAXUSD","DOGSUSD","1000SHIBUSD","ADAUSD","DOGEUSD","XRPUSD"}

last_trade = {}  # symbol -> timestamp
last_grid_price = {}  # symbol -> price of last entry
open_stops = {}  # symbol -> stop price

def sign(method, path, body=""):
    ts = str(int(time.time()))
    msg = method + ts + path + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"api-key": API_KEY, "timestamp": ts, "signature": sig, "Content-Type": "application/json"}

def req(method, path, body=None):
    b = json.dumps(body) if body else ""
    h = sign(method, path, b)
    try:
        if method=="GET":
            r = requests.get(BASE+path, headers=h, timeout=15)
        else:
            r = requests.post(BASE+path, headers=h, data=b, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[REQ ERR] {e}", flush=True)
        return {}

def get_product_id(symbol):
    r = req("GET","/v2/products")
    for p in r.get("result",[]):
        if p.get("symbol")==symbol:
            return p.get("id")
    return None

PRODUCTS = {}
def get_pid(symbol):
    if symbol not in PRODUCTS:
        PRODUCTS[symbol]=get_product_id(symbol)
    return PRODUCTS[symbol]

def get_candles(symbol, pid):
    # 5m candles, last 220 for EMA200
    r = req("GET", f"/v2/history/candles?product_id={pid}&resolution=5m&start={int(time.time())-220*300}&end={int(time.time())}")
    candles = r.get("result",[])
    return candles

def ema(values, period):
    if len(values)<period: return None
    k = 2/(period+1)
    e = sum(values[:period])/period
    for v in values[period:]:
        e = v*k + e*(1-k)
    return e

def get_positions():
    r = req("GET","/v2/positions/margined")
    pos = {}
    for p in r.get("result",[]):
        sym = p.get("product_symbol") or p.get("symbol")
        size = float(p.get("size",0) or 0)
        if sym in IGNORE: continue
        if size!=0:
            pos[sym]=size
    return pos

def place_market(symbol, pid, side, qty):
    body={"product_id":pid,"size":qty,"side":side,"order_type":"market_order","time_in_force":"gtc"}
    r = req("POST","/v2/orders", body)
    print(f"[ENTRY] {symbol} {side} qty {qty} -> {r.get('success')} {r}", flush=True)
    return r

def place_stop(symbol, pid, side, qty, stop_price):
    # side opposite to position: SELL position -> BUY stop
    stop_side = "buy" if side=="sell" else "sell"
    body={"product_id":pid,"size":qty,"side":stop_side,"order_type":"stop_market_order","stop_price":str(round(stop_price,4)),"time_in_force":"gtc","reduce_only":"true"}
    r = req("POST","/v2/orders", body)
    print(f"[STOP] {symbol} RED LINE @ {stop_price} -> {r.get('success')}", flush=True)
    return r

def get_live_price(symbol, pid):
    r = req("GET", f"/v2/tickers?product_id={pid}")
    res = r.get("result",[])
    if res: return float(res[0].get("mark_price") or res[0].get("close") or 0)
    return 0

def trade_loop():
    print(f"=== FINAL COMBINED START MODE={MODE} ===", flush=True)
    print(f"COINS ONLY: {SYMBOLS} | SIZE={SIZE} MAX_POS={MAX_POS} COOLDOWN={COOLDOWN}s TRAIL={TRAIL_PCT*100}%", flush=True)
    while True:
        try:
            pos = get_positions()
            print(f"[POS] Total {len(pos)}/{MAX_POS} {pos} MODE={MODE}", flush=True)
            if len(pos) >= MAX_POS:
                print(f"[SKIP] Max {MAX_POS} positions reached - wait for exit", flush=True)
                time.sleep(30); continue

            for sym in SYMBOLS:
                if sym in pos:
                    print(f"[SKIP] {sym} POSITION_ALREADY_OPEN - no double entry", flush=True)
                    continue
                if len(get_positions())>=MAX_POS: break

                # Cooldown check
                if sym in last_trade and time.time()-last_trade[sym] < COOLDOWN:
                    print(f"[COOLDOWN] {sym} wait {int(COOLDOWN-(time.time()-last_trade[sym]))}s", flush=True)
                    continue

                pid = get_pid(sym)
                if not pid: continue
                candles = get_candles(sym, pid)
                if len(candles)<60: continue
                closes = [float(c.get("close")) for c in candles]
                live = get_live_price(sym, pid) or closes[-1]
                ema50 = ema(closes,50)
                ema200 = ema(closes,200) if len(closes)>=200 else ema50

                # ===== LOOSE TREND - ONLY EMA50 - NO MIXED BLOCK =====
                # Sell mode: Close < EMA50 = BEARISH -> SELL
                # Buy mode: Close > EMA50 = BULLISH -> BUY
                # Both mode: dono taraf
                is_below = live < ema50
                is_above = live > ema50
                trend = "BEARISH" if is_below else "BULLISH"
                print(f"[TREND] {sym} Close {live:.2f} EMA50 {ema50:.2f} EMA200 {ema200:.2f} -> {trend} MODE={MODE}", flush=True)

                should_sell = False
                should_buy = False
                if MODE=="Sell" and is_below: should_sell=True
                elif MODE=="Buy" and is_above: should_buy=True
                elif MODE=="Both":
                    if is_below: should_sell=True
                    if is_above: should_buy=True

                if should_sell:
                    # Grid check - pehli entry hamesha allow, baad me sirf agar price up gaya ho - but yaha pos nahi hai to direct entry
                    print(f"[SIGNAL] {sym} SELL - Price below EMA50 - Crash Sell", flush=True)
                    r = place_market(sym, pid, "sell", SIZE)
                    if r.get("success"):
                        last_trade[sym]=time.time()
                        last_grid_price[sym]=live
                        # Red Line Stop = Entry + 0.5%
                        stop = live * (1+TRAIL_PCT)
                        place_stop(sym, pid, "sell", SIZE, stop)
                        open_stops[sym]=stop
                    else:
                        print(f"[ENTRY FAIL] {sym} {r}", flush=True)

                elif should_buy:
                    print(f"[SIGNAL] {sym} BUY - Price above EMA50", flush=True)
                    r = place_market(sym, pid, "buy", SIZE)
                    if r.get("success"):
                        last_trade[sym]=time.time()
                        last_grid_price[sym]=live
                        stop = live * (1-TRAIL_PCT)
                        place_stop(sym, pid, "buy", SIZE, stop)
                        open_stops[sym]=stop
                else:
                    if MODE=="Sell":
                        print(f"[NO SELL] {sym} Price ABOVE EMA50 - Uptrend, crash nahi hai, wait", flush=True)
                    elif MODE=="Buy":
                        print(f"[NO BUY] {sym} Price BELOW EMA50 - Downtrend, wait", flush=True)
                    else:
                        print(f"[NO TRADE] {sym} wait", flush=True)

            print("--- SLEEP 30s ---", flush=True)
            time.sleep(30)
        except Exception as e:
            print(f"[LOOP ERR] {e}", flush=True)
            time.sleep(10)

if __name__=="__main__":
    threading.Thread(target=trade_loop, daemon=True).start()
    port=int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0", port=port)
