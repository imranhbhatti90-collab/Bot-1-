# FINAL FREE BOT - BINA TRADINGVIEW KE - DELTA TESTNET ONLY
# Crash pe SELL only + TP Chart se + 138 Grid + EMA 50/200
# Render FREE plan pe chalega, koi paid plan nahi chahiye

import os, json, time, hmac, hashlib, threading, requests, pandas as pd
from urllib.parse import urlencode, quote
from flask import Flask, request, jsonify

# =====================================================
# FINAL COMBINED BOT - EMA TREND + KABOOM GRID
# Dono video ka mix: Crash pe SELL only + 138 Grid positions
# DELTA TESTNET ONLY
# =====================================================
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

MODE = os.getenv("KABOOM_MODE", "Sell")  # Sell = crash pe SELL only
COINS = ["BTCUSD","ETHUSD","PAXGUSD","SILVERUSD","BCHUSD","SOLUSD"]
ORDER_SIZES = {"BTCUSD":40,"ETHUSD":40,"PAXGUSD":40,"SILVERUSD":40,"BCHUSD":40,"SOLUSD":40}
MAX_POSITIONS = 138
POINTS = {
    "BTCUSD": {"sl": 400.0, "tp": 300.0, "grid": 200.0, "decimals": 2},
    "ETHUSD": {"sl": 30.0, "tp": 20.0, "grid": 15.0, "decimals": 2},
    "PAXGUSD": {"sl": 20.0, "tp": 15.0, "grid": 10.0, "decimals": 2},
    "SILVERUSD": {"sl": 0.50, "tp": 0.30, "grid": 0.20, "decimals": 3},
    "BCHUSD": {"sl": 5.0, "tp": 3.0, "grid": 2.0, "decimals": 2},
    "SOLUSD": {"sl": 3.0, "tp": 2.0, "grid": 1.5, "decimals": 2},
}
STATE_FILE = "state.json"
CHECK_INTERVAL = 30

app = Flask(__name__)
def log(m): print(m, flush=True)
try:
    with open(STATE_FILE) as f: state=json.load(f)
except: state={"last_grid_price":{},"start_price":{},"last_trade":{}}
for k in ["last_grid_price","start_price","last_trade"]:
    if k not in state: state[k]={}
def save_state():
    try:
        with open(STATE_FILE,"w") as f: json.dump(state,f)
    except: pass

def api_req(method, path, params=None, body=None):
    ts=str(int(time.time()))
    qs="?" + urlencode(params, quote_via=quote) if params else ""
    payload=json.dumps(body) if body else ""
    msg=method+ts+path+qs+payload
    sig=hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    headers={"api-key":API_KEY,"timestamp":ts,"signature":sig,"Content-Type":"application/json"}
    try:
        r=requests.request(method, BASE_URL+path+qs, headers=headers, data=payload or None, timeout=15)
        return r.json(), r.status_code
    except Exception as e: return {"error":str(e)},500

def get_live(symbol):
    try:
        r=requests.get(BASE_URL+f"/v2/tickers/{symbol}", timeout=10)
        res=r.json().get("result",{})
        if isinstance(res,list): res=res[0] if res else {}
        return float(res.get("close",0))
    except: return 0

def get_candles(symbol):
    end=int(time.time())
    res,code=api_req("GET","/v2/history/candles", params={"resolution":"15m","symbol":symbol,"start":str(end-10*24*3600),"end":str(end)})
    if code==200 and res.get("success"): return res.get("result",[])
    return []

def check_trend(symbol):
    # EMA wala logic - dono video ka pehla part
    candles=get_candles(symbol)
    if len(candles)<210: return "MIXED",0
    df=pd.DataFrame(candles)
    if 'close' not in df.columns: df=pd.DataFrame(candles, columns=["time","open","high","low","close","volume"])
    df['close']=pd.to_numeric(df['close'])
    df['ema50']=df['close'].ewm(span=50, adjust=False).mean()
    df['ema200']=df['close'].ewm(span=200, adjust=False).mean()
    df['ema10']=df['close'].ewm(span=10, adjust=False).mean()
    df['ema20']=df['close'].ewm(span=20, adjust=False).mean()
    last=df.iloc[-1]; prev=df.iloc[-2]
    close=float(last['close']); ema50=float(last['ema50']); ema200=float(last['ema200'])
    if ema50>ema200 and close>ema50: trend="BULLISH"
    elif ema50<ema200 and close<ema50: trend="BEARISH"
    else: trend="MIXED"
    log(f"[TREND] {symbol} Close {close:.2f} EMA50 {ema50:.2f} EMA200 {ema200:.2f} -> {trend} MODE={MODE}")
    return trend, close

def get_pos():
    res,code=api_req("GET","/v2/positions/margined")
    if code!=200: return []
    lst=res.get("result",[])
    if isinstance(lst,dict): lst=[lst]
    return [p for p in lst if float(p.get("size",0) or 0)!=0]
def parse_sym(p): return p.get("product_symbol") or p.get("product",{}).get("symbol") or ""
def place_stop(symbol, side, size, stop_price):
    stop_side="sell" if side=="buy" else "buy"
    body={"product_symbol":symbol,"size":int(abs(float(size))),"side":stop_side,"order_type":"market_order","stop_order_type":"stop_loss_order","stop_price":f"{float(stop_price):.{POINTS[symbol]['decimals']}f}","reduce_only":True,"stop_trigger_method":"last_traded_price"}
    res,code=api_req("POST","/v2/orders", body=body)
    log(f"[STOP] {symbol} RED LINE @ {stop_price:.2f} -> {code}")
    return code==200
def place_entry(symbol, side):
    qty=ORDER_SIZES.get(symbol,40)
    body={"product_symbol":symbol,"size":qty,"side":side,"order_type":"market_order"}
    res,code=api_req("POST","/v2/orders", body=body)
    log(f"[ENTRY] {symbol} {side.upper()} qty {qty} -> {code} MODE={MODE}")
    return code==200

@app.route("/")
def home(): return f"FINAL COMBINED | EMA TREND + KABOOM GRID | MODE={MODE} | MAX 138 | TESTNET"
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data=request.get_json(force=True) or {}
        sym=data.get("symbol","").upper(); side=data.get("side", MODE.lower())
        log(f"[WEBHOOK] {sym} {side}")
        if sym in COINS:
            if MODE.lower()=="sell" and side!="sell": return jsonify({"ok":False,"reason":"MODE Sell only"}),200
            if MODE.lower()=="buy" and side!="buy": return jsonify({"ok":False,"reason":"MODE Buy only"}),200
            place_entry(sym, side)
            return jsonify({"ok":True}),200
    except Exception as e: log(f"[WEBHOOK ERR] {e}")
    return jsonify({"ok":False}),400
@app.route("/health")
def health(): return jsonify({"mode":MODE,"coins":COINS,"max":MAX_POSITIONS})

def run_flask(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)), use_reloader=False)

def main_loop():
    log(f"=== FINAL COMBINED START MODE={MODE} ===")
    log(f"EMA50/200 TREND FILTER + KABOOM GRID 138 POS")
    for s in COINS:
        if s not in state["start_price"]:
            p=get_live(s)
            if p>0: state["start_price"][s]=p; state["last_grid_price"][s]=p
    save_state()
    while True:
        try:
            positions=get_pos()
            held={}
            for p in positions:
                sym=parse_sym(p)
                if sym in COINS: held[sym]=held.get(sym,0)+1
            total=sum(held.values())
            log(f"[POS] Total {total}/138 {held} MODE={MODE}")
            for symbol in COINS:
                trend, live = check_trend(symbol)
                if live==0: live=get_live(symbol)
                if live<=0: continue
                last_grid=state["last_grid_price"].get(symbol, live)
                # --- DONO KA COMBINE LOGIC ---
                # 1) EMA TREND: Crash pe SELL only, Pump pe BUY only
                # 2) KABOOM GRID: Har grid gap pe naya position
                if MODE=="Sell":
                    # Crash = BEARISH trend me hi SELL
                    if trend=="BEARISH":
                        # Grid: price upar gaya to naya SELL (video jaisa)
                        if live > last_grid + POINTS[symbol]["grid"]:
                            if total < MAX_POSITIONS:
                                log(f"[KABOOM+EMA] {symbol} BEARISH + GRID UP {live:.2f} > {last_grid:.2f} -> SELL ADD")
                                if place_entry(symbol,"sell"):
                                    state["last_grid_price"][symbol]=live
                                    place_stop(symbol,"sell",ORDER_SIZES[symbol], live+POINTS[symbol]["sl"])
                                    total+=1; save_state()
                        elif live < last_grid - POINTS[symbol]["tp"]:
                            state["last_grid_price"][symbol]=live; save_state()
                    elif trend=="BULLISH":
                        log(f"[NO SELL] {symbol} UPTREND hai - crash nahi hai, SELL nahi karega")
                    else:
                        log(f"[NO TRADE] {symbol} MIXED trend - wait")
                else: # Buy mode
                    if trend=="BULLISH":
                        if live < last_grid - POINTS[symbol]["grid"]:
                            if total < MAX_POSITIONS:
                                log(f"[KABOOM+EMA] {symbol} BULLISH + GRID DOWN -> BUY ADD")
                                if place_entry(symbol,"buy"):
                                    state["last_grid_price"][symbol]=live
                                    place_stop(symbol,"buy",ORDER_SIZES[symbol], live-POINTS[symbol]["sl"])
                                    total+=1; save_state()
                    else:
                        log(f"[NO BUY] {symbol} Trend {trend} - pump nahi hai, BUY nahi karega")
            log(f"--- SLEEP {CHECK_INTERVAL}s ---")
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            log(f"[ERR] {e}"); time.sleep(10)

if True:
    threading.Thread(target=run_flask, daemon=True).start()
    main_loop()
