import os, time, json, hmac, hashlib, logging, threading
from urllib.parse import urlencode, quote
import requests, pandas as pd
from flask import Flask, jsonify

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
TIMEFRAME_MINUTES = 5
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
ORDER_SIZE = 40
LEVERAGE = 50
TRAILING_STOP_PERCENT = 0.5
ENABLE_TRADING = True
POLL_SECONDS = 15  # 25 se 15 kiya taaki fast scan kare
STATE_FILE = "state.json"
REQUEST_TIMEOUT = 15

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
app = Flask(__name__)
session = requests.Session()
bot_status = {"running": False, "last_error": None, "last_signal": None}
state = {}
try:
    with open(STATE_FILE, "r") as f: state = json.load(f)
except: state = {}
def save_state():
    try:
        with open(STATE_FILE + ".tmp", "w") as f: json.dump(state, f, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except: pass

def api_request(method, path, params=None, payload=None, auth=True):
    method=method.upper(); params=params or {}
    qs=urlencode(sorted(params.items()), doseq=True, quote_via=quote)
    req_path=path+(("?"+qs) if qs else "")
    body=json.dumps(payload, separators=(",",":"), ensure_ascii=False) if payload else ""
    headers={"Content-Type":"application/json"}
    if auth:
        if not API_KEY or not API_SECRET: raise RuntimeError("KEY MISSING")
        ts=str(int(time.time()))
        sig=hmac.new(API_SECRET.encode(), (method+ts+req_path+body).encode(), hashlib.sha256).hexdigest()
        headers.update({"api-key":API_KEY,"timestamp":ts,"signature":sig})
    r=session.request(method=method, url=BASE_URL+path, params=params or None, data=body or None, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code>=400: raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text[:300]}")
    j=r.json()
    if isinstance(j,dict) and j.get("success") is False: raise RuntimeError(f"API reject {j}")
    return j
def result_data(r): return r.get("result",r) if isinstance(r,dict) else r

def get_all_active_products():
    result=api_request("GET","/v2/products",auth=False)
    prods=result_data(result)
    if isinstance(prods,dict): prods=prods.get("products",[])
    if not isinstance(prods,list): prods=[]
    active=[]
    for p in prods:
        sym=str(p.get("symbol","")).upper()
        if "USD" not in sym: continue
        if "_USDT" in sym: continue
        if "1000" in sym: continue
        if p.get("id") is None: continue
        active.append(p)
    active.sort(key=lambda x: 0 if "BTC" in x.get("symbol","") else 1 if "ETH" in x.get("symbol","") else 2)
    logging.info(f"Found {len(active)} USD products - sample: {[p.get('symbol') for p in active[:5]]}")
    return active

def fetch_market_candles(symbol):
    result=api_request("GET","/v2/history/candles", params={"symbol":symbol,"resolution":f"{TIMEFRAME_MINUTES}m","start":int(time.time())- (60*TIMEFRAME_MINUTES*300),"end":int(time.time())}, auth=False)
    candles=result_data(result)
    if isinstance(candles,dict): candles=candles.get("candles",[])
    if len(candles)<100: raise RuntimeError(f"{symbol}: low candles {len(candles)}")
    df=pd.DataFrame(candles)
    df=df.rename(columns={"c":"close"})
    df["close"]=pd.to_numeric(df["close"],errors="coerce")
    df=df.sort_values("time").dropna(subset=["close"]).reset_index(drop=True)
    if len(df)>2:
        try:
            lt=float(df["time"].iloc[-1])
            if lt>1e12: lt/=1000
            if time.time()-lt < TIMEFRAME_MINUTES*60: df=df.iloc[:-1]
        except: pass
    df["ema_fast"]=df["close"].ewm(span=EMA_FAST,adjust=False).mean()
    df["ema_slow"]=df["close"].ewm(span=EMA_SLOW,adjust=False).mean()
    delta=df["close"].diff(); gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    avg_gain=gain.ewm(alpha=1/RSI_PERIOD,min_periods=RSI_PERIOD,adjust=False).mean()
    avg_loss=loss.ewm(alpha=1/RSI_PERIOD,min_periods=RSI_PERIOD,adjust=False).mean()
    df["rsi"]=100-(100/(1+(avg_gain/avg_loss.replace(0,float("nan")))))
    return df

def check_market_filters(df, side):
    rsi=df.iloc[-1].get("rsi")
    if pd.isna(rsi): return False, f"RSI NA"
    if side=="buy" and rsi < 35: return False, f"BUY reject RSI {rsi:.2f} <35"
    if side=="sell" and rsi > 35: return False, f"SELL reject RSI {rsi:.2f} >35"
    return True, f"ok RSI {rsi:.2f}"

def scan_symbol(symbol):
    df=fetch_market_candles(symbol)
    prev,curr=df.iloc[-2],df.iloc[-1]
    logging.info(f"[MARKET] {symbol} price={curr['close']:.1f} EMA9={curr['ema_fast']:.1f} EMA21={curr['ema_slow']:.1f} RSI={curr['rsi']:.2f}")
    if prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]: side="buy"
    elif prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]: side="sell"
    else: return None, df, f"No EMA cross (9={curr['ema_fast']:.1f} 21={curr['ema_slow']:.1f})"
    ok,reason=check_market_filters(df,side)
    if not ok: return None, df, reason
    return {"symbol":symbol,"side":side,"price":float(curr["close"]),"candle_id":str(curr["time"]),"rsi":float(curr["rsi"])}, df, reason

_cached_positions = {"data": [], "time": 0}
def get_all_positions():
    global _cached_positions
    if time.time() - _cached_positions["time"] < 10:
        return _cached_positions["data"]
    try:
        result=api_request("GET","/v2/positions/margined",auth=True)
        pos=result_data(result)
        if isinstance(pos,dict): pos=pos.get("positions",[])
        if isinstance(pos,list) and len(pos)>0:
            _cached_positions = {"data": pos, "time": time.time()}
            return pos
    except Exception as e: logging.info(f"margined fail {e}")
    all_pos=[]
    for asset in ["BTC","ETH","SOL","XRP","BNB"]:
        try:
            result=api_request("GET","/v2/positions", params={"underlying_asset_symbol":asset}, auth=True)
            pos=result_data(result)
            if isinstance(pos,dict): pos=pos.get("positions",[])
            if isinstance(pos,list): all_pos.extend(pos)
        except: continue
    _cached_positions = {"data": all_pos, "time": time.time()}
    return all_pos

def get_positions_for_symbol(symbol, all_pos=None):
    if all_pos is None: all_pos=get_all_positions()
    return [p for p in all_pos if str(p.get("product_symbol","")).upper()==symbol.upper() and abs(float(p.get("size",0) or 0))>0]
def position_side(p):
    s=str(p.get("side","")).lower()
    if s in ("buy","long"): return "buy"
    if s in ("sell","short"): return "sell"
    return "buy" if float(p.get("size",0) or 0)>0 else "sell"
def position_size(p): return abs(int(float(p.get("size",0) or 0)))

def place_order(product, side, price, candle_id, rsi):
    sym=product["symbol"]
    if sym in state: return False
    payload={"product_id":int(product["id"]),"size":ORDER_SIZE,"side":side,"order_type":"market_order","leverage":LEVERAGE}
    try:
        res=api_request("POST","/v2/orders",payload=payload,auth=True)
        logging.warning(f"[ORDER SUCCESS] {side.upper()} {sym} price={price} RSI={rsi:.1f}")
        state[sym]={"symbol":sym,"product_id":int(product["id"]),"side":side,"entry_price":float(price),"size":ORDER_SIZE,"highest_price":float(price),"lowest_price":float(price)}
        save_state(); return True
    except Exception as e:
        bot_status["last_error"]=str(e); logging.error(f"Order fail {sym} {e}"); return False

def close_position(symbol, position, reason):
    curr_size=position_size(position)
    if curr_size<=0: return False
    side=position_side(position)
    close_side="sell" if side=="buy" else "buy"
    pid=position.get("product_id") or state.get(symbol,{}).get("product_id")
    if not pid: return False
    payload={"product_id":int(pid),"size":curr_size,"side":close_side,"order_type":"market_order","reduce_only":True}
    try:
        api_request("POST","/v2/orders",payload=payload,auth=True)
        logging.warning(f"[CLOSE] {symbol} {reason}")
        if symbol in state: state.pop(symbol,None); save_state()
        return True
    except Exception as e: logging.error(f"Close fail {e}"); return False

def get_mark_price(p):
    for k in ("mark_price","markPrice","last_price"):
        v=p.get(k)
        try:
            if v not in (None,"",0,"0") and float(v)>0: return float(v)
        except: pass
    return None

def bot_loop():
    bot_status["running"]=True
    logging.info(f"BOT STARTED | ALL COINS FIXED | RSI 35 STRICT")
    while True:
        try:
            products=get_all_active_products()
            if len(products)==0:
                logging.error("No products found, retry in 30s")
                time.sleep(30)
                continue
            all_pos=get_all_positions()
            for sym in list(state.keys()):
                ps=get_positions_for_symbol(sym, all_pos)
                if not ps: state.pop(sym,None); save_state()
            for sym in list(state.keys()):
                ps=get_positions_for_symbol(sym, all_pos)
                if not ps or len(ps)!=1: continue
                pos=ps[0]; info=state.get(sym,{}); 
                try: entry=float(pos.get("entry_price") or info.get("entry_price"))
                except: continue
                mp=get_mark_price(pos)
                if mp is None: continue
                highest=float(info.get("highest_price",entry)); lowest=float(info.get("lowest_price",entry))
                if position_side(pos)=="buy":
                    highest=max(highest,mp,entry); info["highest_price"]=highest; save_state()
                    trig=highest*(1-TRAILING_STOP_PERCENT/100)
                    logging.info(f"[TRAIL LONG] {sym} mark={mp:.2f} trig={trig:.2f}")
                    if mp<=trig: close_position(sym,pos,f"Long trail {mp:.2f} <= {trig:.2f}")
                else:
                    lowest=min(lowest,mp,entry); info["lowest_price"]=lowest; save_state()
                    trig=lowest*(1+TRAILING_STOP_PERCENT/100)
                    logging.info(f"[TRAIL SHORT] {sym} mark={mp:.2f} trig={trig:.2f}")
                    if mp>=trig: close_position(sym,pos,f"Short trail {mp:.2f} >= {trig:.2f}")

            for prod in products:
                sym=prod.get("symbol")
                if not sym or sym in state: continue
                try:
                    sig,df,reason=scan_symbol(sym)
                    if sig is None:
                        logging.info(f"[NO TRADE] {sym}: {reason}")
                        continue
                    logging.warning(f"[SIGNAL] {sym} {sig['side'].upper()} price={sig['price']:.1f} RSI={sig['rsi']:.1f}")
                    place_order(prod,sig["side"],sig["price"],sig["candle_id"],sig["rsi"])
                except Exception as e:
                    logging.error(f"Scan {sym} error {e}")
                time.sleep(0.6)
        except Exception as e:
            logging.exception(f"Main loop error {e}")
            bot_status["last_error"]=str(e)
        time.sleep(POLL_SECONDS)

@app.route("/")
def home(): return jsonify({"bot":"ALL COINS FIXED RSI 35","status":"running"})
@app.route("/status")
def status(): return jsonify({**bot_status,"open":list(state.keys())})

if __name__=="__main__":
    threading.Thread(target=bot_loop,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")),debug=False,use_reloader=False)
