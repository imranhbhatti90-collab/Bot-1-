import os, json, time, hmac, hashlib, threading, logging, traceback, math
from datetime import datetime, timezone
from urllib.parse import urlencode
import requests
import pandas as pd
from flask import Flask, jsonify, Response

# DELTA INDIA TESTNET SCANNER - ORDER EXECUTION HARD DISABLED
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
AUTO_TRADING_REQUESTED = os.getenv("AUTO_TRADING", "false").lower() == "true"
# Safety lock: order execution is intentionally disabled in this build.
AUTO_TRADING = True
EXECUTION_LOCK_REASON = "ORDER EXECUTION DISABLED: API signing, product sizing, and protective-order workflow require separate validation."
MARGIN_USD = 20.0
MAX_POSITIONS = 10
TREND_RESOLUTION, ENTRY_RESOLUTION = "15m", "5m"
SCAN_INTERVAL, DISCOVERY_INTERVAL = 30, 900
TREND_EMA_FAST, TREND_EMA_SLOW = 50, 200
ENTRY_EMA_FAST, ENTRY_EMA_SLOW = 10, 20
RSI_PERIOD, ATR_PERIOD, VOLUME_MA_PERIOD = 14, 14, 20
VOLUME_MULTIPLIER = 1.00
MAX_ATR_PERCENT, MAX_CANDLE_RANGE_PERCENT, MAX_CANDLE_BODY_PERCENT, MAX_CLOSE_JUMP_PERCENT = 5.0, 6.0, 5.0, 5.0
ZERO_VOLUME_QUARANTINE_COUNT, QUALITY_RECENT_CANDLES = 4, 5
BUY_RSI_MIN, BUY_RSI_MAX, SELL_RSI_MIN, SELL_RSI_MAX = 50, 70, 30, 50
TREND_CANDLE_COUNT, ENTRY_CANDLE_COUNT = 1000, 1000
MIN_TREND_CANDLES, MIN_ENTRY_CANDLES = 220, 50
SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER = 1.2, 2.0
SIGNAL_HISTORY_LIMIT, STATE_FILE = 200, "state.json"
API_KEY, API_SECRET = os.getenv("DELTA_API_KEY", "").strip(), os.getenv("DELTA_API_SECRET", "").strip()
FALLBACK_SYMBOLS = ["1000SHIBUSD","ADAUSD","BTCUSD","DOGEUSD","DOGSUSD","ETHUSD","NVDAXUSD","PAXGUSD","QQQXUSD","SILVERUSD","SLVONUSD","SNDKBUSD","SOLUSD","SPYXUSD","USOONUSD","XAUTUSD","XRPUSD"]
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger=logging.getLogger("delta_testnet_scanner")
session=requests.Session(); session.headers.update({"Accept":"application/json","User-Agent":"Delta-India-Testnet-Scanner/5.0"})
app=Flask(__name__); lock=threading.RLock()
symbols=FALLBACK_SYMBOLS.copy(); products={}; active_signals={}; signal_history=[]; last_diagnostics={}; data_quarantine={}; errors=[]
scan_count=0; last_scan_time=None; last_scan_duration=None; last_discovery_time=0; trade_history=[]; execution_status={}

def utc_now(): return datetime.now(timezone.utc).isoformat()
def safe_float(v, default=None):
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception: return default
def add_error(msg):
    errors.append({"time":utc_now(),"message":str(msg)}); del errors[:-50]
def result_list(payload):
    r=payload.get("result",[]) if isinstance(payload,dict) else []
    return r if isinstance(r,list) else []
def result_obj(payload): return payload.get("result") if isinstance(payload,dict) else None

# Signing uses the exact query string and body text sent. GET only is used in this scanner build.
def make_query(params):
    if not params: return ""
    return urlencode(params, doseq=True)
def auth_headers(method, path, query_string="", body_text=""):
    if not API_KEY or not API_SECRET: raise RuntimeError("Missing DELTA_API_KEY or DELTA_API_SECRET")
    timestamp=str(int(time.time()))
    request_path=path + ("?" + query_string if query_string else "")
    prehash=method.upper()+timestamp+request_path+body_text
    signature=hmac.new(API_SECRET.encode(),prehash.encode(),hashlib.sha256).hexdigest()
    return {"api-key":API_KEY,"signature":signature,"timestamp":timestamp,"User-Agent":"Delta-India-Testnet-Scanner/5.0","Accept":"application/json","Content-Type":"application/json"}
def api_request(method,path,params=None,body=None,auth=False):
    if BASE_URL != "https://cdn-ind.testnet.deltaex.org": raise RuntimeError("TESTNET URL LOCK FAILED")
    method=method.upper(); query=make_query(params); body_text=json.dumps(body,separators=(",",":"),ensure_ascii=False) if body is not None else ""
    headers=auth_headers(method,path,query,body_text) if auth else {}
    url=BASE_URL+path+("?"+query if query else "")
    response=session.request(method,url,data=body_text if body is not None else None,headers=headers,timeout=20)
    if not response.ok:
        # Include API response text for diagnosis, but do not print credentials/signature.
        raise requests.HTTPError(f"HTTP {response.status_code} {path}: {response.text[:500]}",response=response)
    payload=response.json()
    if isinstance(payload,dict) and payload.get("success") is False: raise RuntimeError(f"Delta API error: {payload.get('error')}")
    return payload
def api_get(path,params=None,auth=False): return api_request("GET",path,params=params,auth=auth)

def discover_symbols(force=False):
    global symbols,products,last_discovery_time
    now=time.time()
    if not force and products and now-last_discovery_time<DISCOVERY_INTERVAL: return symbols
    found={}; cursor=None
    try:
        for _ in range(10):
            params={"page_size":100}
            if cursor: params["after"]=cursor
            payload=api_get("/v2/products",params=params); rows=result_list(payload)
            for p in rows:
                if not isinstance(p,dict): continue
                sym=str(p.get("symbol","")).upper().strip()
                if not sym.endswith("USD") or sym.startswith(("C-","P-")): continue
                ctype=str(p.get("contract_type",p.get("product_type",""))).lower(); desc=str(p.get("description","")).lower(); type_text=ctype or desc
                if type_text and "perpetual" not in type_text: continue
                status=str(p.get("state",p.get("status",""))).lower()
                if status and status not in ("live","active","trading"): continue
                found[sym]=p
            meta=payload.get("meta",{}); next_cursor=meta.get("after")
            if not rows or not next_cursor or next_cursor==cursor: break
            cursor=next_cursor
        if found: products=found; symbols=sorted(found)
        else: logger.warning("[DISCOVERY] using fallback symbols")
        last_discovery_time=now; logger.info("[DISCOVERY] symbols=%s",len(symbols)); return symbols
    except Exception as e:
        logger.exception("[DISCOVERY ERROR] %s",e); add_error(str(e)); return symbols or FALLBACK_SYMBOLS.copy()

def fetch_candles(symbol,resolution,count):
    now=int(time.time()); seconds={"5m":300,"15m":900}[resolution]; start=now-count*seconds
    payload=api_get("/v2/history/candles",params={"resolution":resolution,"symbol":symbol,"start":start,"end":now})
    rows=result_list(payload)
    if not rows: raise ValueError("NO_CANDLES")
    frame=pd.DataFrame(rows); required=["time","open","high","low","close","volume"]
    if any(c not in frame.columns for c in required): raise ValueError("MISSING_CANDLE_FIELDS")
    for c in required: frame[c]=pd.to_numeric(frame[c],errors="coerce")
    frame=frame.dropna(subset=required)
    if frame["time"].median()>10_000_000_000: frame["time"]/=1000
    frame=frame.sort_values("time").drop_duplicates("time"); current_open=now-now%seconds
    frame=frame[frame["time"]<current_open].reset_index(drop=True)
    if frame.empty: raise ValueError("NO_CLOSED_CANDLES")
    integrity=(frame.open>0)&(frame.high>0)&(frame.low>0)&(frame.close>0)&(frame.volume>=0)&(frame.high>=frame.low)&(frame.high>=frame.open)&(frame.high>=frame.close)&(frame.low<=frame.open)&(frame.low<=frame.close)
    if not integrity.all(): raise ValueError("DATA_QUARANTINE:INVALID_OHLC")
    recent=frame.tail(QUALITY_RECENT_CANDLES); flags=[]
    if len(recent)==QUALITY_RECENT_CANDLES:
        zeros=int((recent.volume<=0).sum())
        if zeros>=ZERO_VOLUME_QUARANTINE_COUNT: flags.append(f"ZERO_VOLUME({zeros}/{QUALITY_RECENT_CANDLES})")
    for _,row in recent.iterrows():
        close=row.close
        if close<=0: flags.append("INVALID_CLOSE"); continue
        range_pct=(row.high-row.low)/close*100; body_pct=abs(row.close-row.open)/close*100
        if range_pct>MAX_CANDLE_RANGE_PERCENT: flags.append(f"CANDLE_RANGE({range_pct:.2f}%)")
        if body_pct>MAX_CANDLE_BODY_PERCENT: flags.append(f"CANDLE_BODY({body_pct:.2f}%)")
    if len(frame)>=2:
        closes=frame.tail(QUALITY_RECENT_CANDLES+1).close.tolist()
        for a,b in zip(closes[:-1],closes[1:]):
            if a<=0: flags.append("INVALID_PREVIOUS_CLOSE")
            elif abs(b-a)/a*100>MAX_CLOSE_JUMP_PERCENT: flags.append("CLOSE_JUMP")
    key=f"{symbol}:{resolution}"
    if flags:
        data_quarantine[key]={"symbol":symbol,"resolution":resolution,"flags":sorted(set(flags)),"status":"QUARANTINED","updated_at":utc_now()}
        raise ValueError("DATA_QUARANTINE:"+"|".join(sorted(set(flags))))
    data_quarantine.pop(key,None); return frame

def calculate_indicators(frame):
    frame=frame.copy()
    for span in (10,20,50,200): frame[f"ema{span}"]=frame.close.ewm(span=span,adjust=False,min_periods=span).mean()
    delta=frame.close.diff(); gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    avg_gain=gain.ewm(alpha=1/RSI_PERIOD,min_periods=RSI_PERIOD,adjust=False).mean(); avg_loss=loss.ewm(alpha=1/RSI_PERIOD,min_periods=RSI_PERIOD,adjust=False).mean()
    rs=avg_gain/loss.replace(0,float("nan")) if False else avg_gain/avg_loss.replace(0,float("nan"))
    frame["rsi"]=100-(100/(1+rs)); frame.loc[(avg_loss==0)&(avg_gain>0),"rsi"]=100; frame.loc[(avg_gain==0)&(avg_loss>0),"rsi"]=0; frame.loc[(avg_gain==0)&(avg_loss==0),"rsi"]=50
    prev=frame.close.shift(1); tr=pd.concat([frame.high-frame.low,(frame.high-prev).abs(),(frame.low-prev).abs()],axis=1).max(axis=1)
    frame["atr"]=tr.ewm(alpha=1/ATR_PERIOD,min_periods=ATR_PERIOD,adjust=False).mean()
    frame["volume_avg"]=frame.volume.shift(1).rolling(VOLUME_MA_PERIOD,min_periods=VOLUME_MA_PERIOD).mean()
    return frame

def analyze_trend(frame):
    if len(frame)<MIN_TREND_CANDLES: return {"trend":"NOT_READY"}
    row=calculate_indicators(frame).iloc[-1]; close=safe_float(row.close); e50=safe_float(row.ema50); e200=safe_float(row.ema200)
    if None in (close,e50,e200): return {"trend":"NOT_READY"}
    trend="BULLISH" if e50>e200 and close>e50 and close>e200 else "BEARISH" if e50<e200 and close<e50 and close<e200 else "MIXED"
    return {"trend":trend,"close":close,"ema50":e50,"ema200":e200,"time":int(row.time)}
def analyze_entry(symbol,trend_data,frame):
    if len(frame)<MIN_ENTRY_CANDLES: return {"symbol":symbol,"signal":"NONE","reason":"ENTRY_CANDLES_SHORT"}
    row=calculate_indicators(frame).iloc[-1]
    close=safe_float(row.close); ema10=safe_float(row.ema10); ema20=safe_float(row.ema20); rsi=safe_float(row.rsi); atr=safe_float(row.atr); volume=safe_float(row.volume,0); volume_avg=safe_float(row.volume_avg)
    trend=trend_data.get("trend"); reasons=[]
    if not close or close<=0: reasons.append("INVALID_PRICE")
    if not ema10 or not ema20: reasons.append("EMA_NOT_READY")
    if rsi is None: reasons.append("RSI_NOT_READY")
    if not atr or atr<=0: reasons.append("ATR_NOT_READY")
    atr_pct=atr/close*100 if close and atr else None
    if atr_pct is not None and atr_pct>MAX_ATR_PERCENT: reasons.append("ATR_OUTLIER")
    volume_ok=bool(volume>0 and volume_avg and volume_avg>0 and volume>=volume_avg*VOLUME_MULTIPLIER)
    buy=not reasons and trend=="BULLISH" and ema10>ema20 and BUY_RSI_MIN<=rsi<=BUY_RSI_MAX and volume_ok
    sell=not reasons and trend=="BEARISH" and ema10<ema20 and SELL_RSI_MIN<=rsi<=SELL_RSI_MAX and volume_ok
    signal="BUY" if buy else "SELL" if sell else "NONE"
    return {"symbol":symbol,"time":utc_now(),"trend":trend,"close":close,"ema10":ema10,"ema20":ema20,"ema50":trend_data.get("ema50"),"ema200":trend_data.get("ema200"),"rsi":rsi,"atr":atr,"atr_percent":atr_pct,"volume":volume,"volume_avg":volume_avg,"volume_ratio":volume/volume_avg if volume_avg else None,"volume_ok":volume_ok,"signal":signal,"candle_time":int(row.time),"trend_candle_time":trend_data.get("time"),"reason":"FILTERS_PASSED" if signal!="NONE" else "FILTERS_BLOCKED"}

def record_signal(d):
    sym=d.get("symbol"); sig=d.get("signal","NONE"); prev=active_signals.get(sym); now=utc_now()
    if sig=="NONE":
        if prev: signal_history.append({"event":"END","symbol":sym,"signal":prev["signal"],"ended_at":now,"last_price":d.get("close")}); active_signals.pop(sym,None)
        return
    if not prev or prev.get("signal")!=sig:
        active_signals[sym]={"symbol":sym,"signal":sig,"first_seen":now,"last_seen":now,"scans":1,"last_price":d.get("close"),"rsi":d.get("rsi"),"atr":d.get("atr")}
        signal_history.append({"event":"START","symbol":sym,"signal":sig,"time":now,"price":d.get("close"),"rsi":d.get("rsi"),"atr":d.get("atr")})
    else: prev.update(last_seen=now,scans=prev.get("scans",0)+1,last_price=d.get("close"))
    del signal_history[:-SIGNAL_HISTORY_LIMIT]

def scan_symbol(symbol):
    try:
        tf=fetch_candles(symbol,TREND_RESOLUTION,TREND_CANDLE_COUNT); td=analyze_trend(tf)
        if td.get("trend")=="NOT_READY": return {"symbol":symbol,"signal":"NONE","trend":"NOT_READY","reason":"TREND_CANDLES_INSUFFICIENT"}
        ef=fetch_candles(symbol,ENTRY_RESOLUTION,ENTRY_CANDLE_COUNT); d=analyze_entry(symbol,td,ef); last_diagnostics[symbol]=d
        logger.info("[SIGNAL] %s trend=%s signal=%s close=%s RSI=%s ATR=%s",symbol,d.get("trend"),d.get("signal"),d.get("close"),d.get("rsi"),d.get("atr"))
        record_signal(d)
        # This build is scanner-only. No order submission function is called.
        execution_status[symbol]="EXECUTION_HARD_DISABLED"
        return d
    except Exception as e:
        msg=f"{symbol}: {type(e).__name__}: {e}"; logger.error("[SCAN BLOCKED] %s",msg); add_error(msg)
        d={"symbol":symbol,"time":utc_now(),"signal":"NONE","trend":"DATA_INVALID","reason":msg}; last_diagnostics[symbol]=d; record_signal(d); return d

def get_positions(): return result_list(api_get("/v2/positions/margined",auth=True))
def get_open_orders(): return result_list(api_get("/v2/orders",auth=True))
def monitor_read_only():
    try: return get_positions(),get_open_orders()
    except Exception as e: logger.error("[READ-ONLY MONITOR ERROR] %s",e); add_error(f"Monitor: {e}"); return [],[]
def save_state():
    try:
        data={"active_signals":active_signals,"signal_history":signal_history[-SIGNAL_HISTORY_LIMIT:],"scan_count":scan_count,"symbols":symbols,"trade_history":trade_history[-100:]}
        with open(STATE_FILE+".tmp","w",encoding="utf-8") as f: json.dump(data,f,indent=2,default=str)
        os.replace(STATE_FILE+".tmp",STATE_FILE)
    except Exception as e: logger.error("[SAVE STATE ERROR] %s",e)
def load_state():
    global active_signals,signal_history,scan_count,trade_history
    try:
        if not os.path.exists(STATE_FILE): return
        with open(STATE_FILE,encoding="utf-8") as f: d=json.load(f)
        active_signals=d.get("active_signals",{}); signal_history=d.get("signal_history",[])[-SIGNAL_HISTORY_LIMIT:]; scan_count=d.get("scan_count",0); trade_history=d.get("trade_history",[])[-100:]
        logger.info("[STATE RESTORED] scans=%s",scan_count)
    except Exception as e: logger.error("[STATE LOAD ERROR] %s",e)
def scanner_loop():
    global scan_count,last_scan_time,last_scan_duration
    logger.warning("TESTNET SCANNER STARTED - ORDER EXECUTION HARD DISABLED")
    logger.warning("AUTO_TRADING requested env=%s; effective=%s; margin target display=$%s; max positions display=%s",AUTO_TRADING_REQUESTED,AUTO_TRADING,MARGIN_USD,MAX_POSITIONS)
    logger.warning("BASE URL=%s",BASE_URL); logger.warning(EXECUTION_LOCK_REASON)
    while True:
        started=time.time()
        try:
            syms=discover_symbols(); buys=sells=0
            for s in syms:
                d=scan_symbol(s); buys+=d.get("signal")=="BUY"; sells+=d.get("signal")=="SELL"
            monitor_read_only(); scan_count+=1; last_scan_time=utc_now(); last_scan_duration=round(time.time()-started,2)
            logger.info("[SCAN COMPLETE] symbols=%s BUY=%s SELL=%s active=%s duration=%ss",len(syms),buys,sells,len(active_signals),last_scan_duration); save_state()
        except Exception as e: logger.exception("[SCANNER LOOP ERROR] %s",e); add_error(str(e))
        time.sleep(max(1,SCAN_INTERVAL-(time.time()-started)))

@app.route("/")
def home():
    return Response('''<!DOCTYPE html><html><head><title>Delta Testnet Scanner</title><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{background:#10151c;color:#e7edf5;font-family:Arial;padding:15px}h1{color:#64d8a4}.card{background:#1b2530;padding:15px;margin:10px 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#6ec9ff}</style></head><body><h1>DELTA INDIA TESTNET</h1><h2>Scanner-only mode</h2><div class="card"><b>ORDER EXECUTION HARD DISABLED</b><br>Margin target display: $20 (not used for orders)<br>Max positions display: 10 (not enforced for trading)<br>Leverage: unchanged by this scanner<br>Signal SL reference: ATR × 1.2<br>Signal TP reference: ATR × 2.0</div><div class="card"><a href="/health">Health</a> | <a href="/dashboard-data">Dashboard JSON</a> | <a href="/diagnostics">Diagnostics</a> | <a href="/positions">Read-only Positions</a></div><div class="card"><pre id="data">Loading...</pre></div><script>async function refresh(){try{let r=await fetch('/dashboard-data');let d=await r.json();document.getElementById('data').textContent=JSON.stringify(d,null,2)}catch(e){document.getElementById('data').textContent='Dashboard error'}}refresh();setInterval(refresh,10000)</script></body></html>''',mimetype="text/html")
@app.route("/health")
def health(): return jsonify({"status":"ok","bot_status":"RUNNING","exchange":"DELTA INDIA TESTNET","auto_trading_requested":AUTO_TRADING_REQUESTED,"auto_trading_effective":AUTO_TRADING,"execution_lock":EXECUTION_LOCK_REASON,"scan_count":scan_count,"symbols":len(symbols),"last_scan":last_scan_time,"last_scan_duration":last_scan_duration,"time":utc_now()})
@app.route("/dashboard-data")
def dashboard_data(): return jsonify({"bot_status":"RUNNING","exchange":"DELTA INDIA TESTNET","auto_trading_requested":AUTO_TRADING_REQUESTED,"auto_trading_effective":AUTO_TRADING,"execution_lock":EXECUTION_LOCK_REASON,"margin_target_usd_display_only":MARGIN_USD,"max_positions_display_only":MAX_POSITIONS,"leverage_policy":"NOT MODIFIED","sl_atr_multiplier_reference":SL_ATR_MULTIPLIER,"tp_atr_multiplier_reference":TP_ATR_MULTIPLIER,"symbols":symbols,"total_symbols":len(symbols),"scan_count":scan_count,"last_scan":last_scan_time,"last_scan_duration":last_scan_duration,"active_signals":active_signals,"signal_history":signal_history[-SIGNAL_HISTORY_LIMIT:],"diagnostics":last_diagnostics,"data_quarantine":data_quarantine,"execution_status":execution_status,"trade_history":trade_history[-100:],"errors":errors[-20:]})
@app.route("/diagnostics")
def diagnostics(): return jsonify({"diagnostics":last_diagnostics,"data_quarantine":data_quarantine,"execution_status":execution_status,"errors":errors[-20:],"time":utc_now()})
@app.route("/positions")
def positions_endpoint():
    if not API_KEY or not API_SECRET: return jsonify({"error":"API credentials missing"}),503
    try: return jsonify({"positions":get_positions(),"active_orders":get_open_orders(),"time":utc_now(),"read_only":True})
    except Exception as e: return jsonify({"error":str(e)}),500

def startup_checks():
    if BASE_URL!="https://cdn-ind.testnet.deltaex.org": raise RuntimeError("Refusing to start: non-testnet URL")
    if API_KEY and API_SECRET:
        # Verify read-auth only. This does not place, cancel, or modify orders.
        get_positions(); get_open_orders(); logger.warning("AUTHENTICATED TESTNET READ-ONLY CHECK OK")
    else: logger.warning("API credentials absent; public scanner only. Private account endpoints unavailable.")
    logger.warning("Order execution lock active regardless of AUTO_TRADING env value.")
def start_bot():
    load_state()
    try: startup_checks()
    except Exception as e: logger.critical("[STARTUP CHECK FAILED] %s",e); add_error(f"Startup check failed: {e}")
    threading.Thread(target=scanner_loop,daemon=True,name="delta-scanner").start()
if __name__=="__main__":
    start_bot(); app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),debug=False,use_reloader=False)
