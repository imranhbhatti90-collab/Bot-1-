
# VIDEO STRATEGY - ALL 138 COINS ENTRY - SL + TARGET SAB ME
# User bola: 2 nahi, sab me entry lo, sab me SL + Target ho
# Isliye MAX_POS = 138, har coin me entry, har coin me SL + TP
# SELL: SL = Entry + 0.5% (Red Line), TP = Entry - 1.0% (Green Target)
# BUY:  SL = Entry - 0.5%, TP = Entry + 1.0%
# WARNING: 138 ek saath khulenge to margin zyada lagega - testnet me okay hai

import os, time, hmac, hashlib, json, requests, threading
from flask import Flask

app = Flask(__name__)
@app.route('/')
def home(): return "Bot Live - ALL 138 - SL + TP Sab Me"
@app.route('/health')
def health(): return "OK", 200

API_KEY = os.getenv("DELTA_API_KEY","")
API_SECRET = os.getenv("DELTA_API_SECRET","")
BASE = "https://cdn-ind.testnet.deltaex.org"
MODE = os.getenv("MODE","Sell")
SIZE = 40
MAX_POS = 138  # SAB ME ENTRY - 2 ka limit hataya
COOLDOWN = 300
SL_PCT = 0.005  # 0.5% Stop Loss - Red Line
TP_PCT = 0.01   # 1.0% Target - Green Line

IGNORE = {"USOONUSD"}
last_trade = {}

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

def get_all_products():
    r = req("GET","/v2/products")
    prods=[]
    for p in r.get("result",[]):
        sym=p.get("symbol","")
        if sym in IGNORE: continue
        if not sym.endswith("USD"): continue
        prods.append((sym, p.get("id")))
    print(f"[PRODUCTS] Total {len(prods)} coins - SAB ME ENTRY", flush=True)
    return prods

def get_candles(pid):
    r = req("GET", f"/v2/history/candles?product_id={pid}&resolution=5m&start={int(time.time())-220*300}&end={int(time.time())}")
    return r.get("result",[])

def ema(values, period):
    if len(values)<period: return None
    k=2/(period+1)
    e=sum(values[:period])/period
    for v in values[period:]:
        e=v*k+e*(1-k)
    return e

def get_positions():
    r = req("GET","/v2/positions/margined")
    pos={}
    for p in r.get("result",[]):
        sym=p.get("product_symbol") or p.get("symbol")
        size=float(p.get("size",0) or 0)
        if sym in IGNORE: continue
        if size!=0: pos[sym]=size
    return pos

def place_market(pid, side, qty):
    body={"product_id":pid,"size":qty,"side":side,"order_type":"market_order","time_in_force":"gtc"}
    r=req("POST","/v2/orders", body)
    print(f"[ENTRY] {side} qty {qty} -> {r.get('success')}", flush=True)
    return r

def place_stop(pid, side, qty, stop_price):
    # SL - Red Line
    stop_side="buy" if side=="sell" else "sell"
    body={"product_id":pid,"size":qty,"side":stop_side,"order_type":"stop_market_order","stop_price":str(round(stop_price,4)),"time_in_force":"gtc","reduce_only":"true"}
    r=req("POST","/v2/orders", body)
    print(f"[STOP] RED SL @ {stop_price} -> {r.get('success')}", flush=True)
    return r

def place_target(pid, side, qty, tp_price):
    # TP - Green Target - Limit Order
    tp_side="buy" if side=="sell" else "sell"
    body={"product_id":pid,"size":qty,"side":tp_side,"order_type":"limit_order","limit_price":str(round(tp_price,4)),"time_in_force":"gtc","reduce_only":"true"}
    r=req("POST","/v2/orders", body)
    print(f"[TARGET] GREEN TP @ {tp_price} -> {r.get('success')}", flush=True)
    return r

def get_live_price(pid):
    r=req("GET", f"/v2/tickers?product_id={pid}")
    res=r.get("result",[])
    if res: return float(res[0].get("mark_price") or res[0].get("close") or 0)
    return 0

def trade_loop():
    print(f"=== VIDEO STRATEGY ALL 138 MODE={MODE} SL 0.5% TP 1% ===", flush=True)
    products=get_all_products()
    while True:
        try:
            pos=get_positions()
            print(f"[POS] Total {len(pos)}/{MAX_POS} MODE={MODE} SCAN={len(products)}", flush=True)
            for sym,pid in products:
                if sym in pos:
                    continue
                if sym in last_trade and time.time()-last_trade[sym]<COOLDOWN:
                    continue
                candles=get_candles(pid)
                if len(candles)<60: continue
                closes=[float(c.get("close")) for c in candles]
                live=get_live_price(pid) or closes[-1]
                ema50=ema(closes,50)
                ema200=ema(closes,200) if len(closes)>=200 else ema50
                is_below=live<ema50
                is_above=live>ema50
                trend="BEARISH" if is_below else "BULLISH"
                print(f"[TREND] {sym} Close {live:.4f} EMA50 {ema50:.4f} -> {trend}", flush=True)
                should_sell=False
                should_buy=False
                if MODE=="Sell" and is_below: should_sell=True
                elif MODE=="Buy" and is_above: should_buy=True
                elif MODE=="Both":
                    if is_below: should_sell=True
                    if is_above: should_buy=True
                if should_sell:
                    print(f"[SIGNAL] {sym} SELL - Entry {live}", flush=True)
                    r=place_market(pid,"sell",SIZE)
                    if r.get("success"):
                        last_trade[sym]=time.time()
                        sl=live*(1+SL_PCT)
                        tp=live*(1-TP_PCT)
                        print(f"[LEVELS] {sym} Entry {live:.4f} SL {sl:.4f} TP {tp:.4f}", flush=True)
                        place_stop(pid,"sell",SIZE,sl)
                        place_target(pid,"sell",SIZE,tp)
                elif should_buy:
                    print(f"[SIGNAL] {sym} BUY - Entry {live}", flush=True)
                    r=place_market(pid,"buy",SIZE)
                    if r.get("success"):
                        last_trade[sym]=time.time()
                        sl=live*(1-SL_PCT)
                        tp=live*(1+TP_PCT)
                        print(f"[LEVELS] {sym} Entry {live:.4f} SL {sl:.4f} TP {tp:.4f}", flush=True)
                        place_stop(pid,"buy",SIZE,sl)
                        place_target(pid,"buy",SIZE,tp)
                else:
                    print(f"[NO TRADE] {sym} wait - Price EMA50 ke upar/niche nahi", flush=True)
            print("--- SLEEP 30s ---", flush=True)
            time.sleep(30)
        except Exception as e:
            print(f"[LOOP ERR] {e}", flush=True)
            time.sleep(10)

if __name__=="__main__":
    threading.Thread(target=trade_loop, daemon=True).start()
    port=int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0", port=port)
