import os
import json
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode, quote
import pandas as pd
from flask import Flask, jsonify
import threading

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot FINAL - CROSS + RSI45 + VOL + NO OVERTRADE + RED LINE - 5min"
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "bot": "running"}, 200
    
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")
COINS = ["BTCUSD","ETHUSD","SOLUSD","XAUTUSD","PAXGUSD","SILVERUSD"]
TRAIL_PCT = 0.005
STATE_FILE = "state.json"

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
        filtered = [p for p in lst if float(p.get("size",0)) != 0]
        if filtered:
            _last_pos_cache = filtered
            _last_pos_time = time.time()
            return filtered
        if _last_pos_cache and (time.time() - _last_pos_time < 120):
            print(f"[POS CACHE] API empty, using cached {len(_last_pos_cache)} pos", flush=True)
            return _last_pos_cache
        return []
    if _last_pos_cache and (time.time() - _last_pos_time < 120):
        print(f"[POS CACHE] API fail {code}, using cached {len(_last_pos_cache)} pos", flush=True)
        return _last_pos_cache
    return []

def get_orders():
    res, code = api_req(
        "GET",
        "/v2/orders",
        params={"states": "open,pending"}
    )
    if code == 200 and res.get("success"):
        return res.get("result", [])
    print(f"[ORDER ERROR] {code} | {res}", flush=True)
    return []
def cancel_all_stops(symbol):
    orders = get_orders()

    for o in orders:
        if (
            o.get("product_symbol") == symbol
            and o.get("reduce_only") is True
            and o.get("stop_order_type") == "stop_loss_order"
            and o.get("state") in ("open", "pending")
        ):
            oid = o.get("id")
            if oid:
                res, code = api_req("DELETE", f"/v2/orders/{oid}")
                print(
                    f"[CANCEL STOP] {symbol} ID={oid} HTTP={code}",
                    flush=True
                )

def place_stop(symbol, side, size, stop_price):
    sz = int(abs(float(size)))

    if sz <= 0:
        print("[STOP ERROR] Invalid size", flush=True)
        return None, 400

    stop_side = "sell" if side == "buy" else "buy"

    body = {
        "product_symbol": symbol,
        "size": sz,
        "side": stop_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": f"{float(stop_price):.2f}",
        "reduce_only": True,
        "stop_trigger_method": "last_traded_price"
    }

    res, code = api_req("POST", "/v2/orders", body=body)

    result = res.get("result", {}) if isinstance(res, dict) else {}

    if (
        code == 200
        and res.get("success") is True
        and result.get("stop_order_type") == "stop_loss_order"
        and result.get("state") in ("pending", "open")
    ):
        state["stops"][symbol] = float(stop_price)
        save_state(state)

        print(
            f"[STOP ACTIVE] {symbol} {stop_side} "
            f"STOP={stop_price} ID={result.get('id')} "
            f"STATE={result.get('state')}",
            flush=True
        )
    else:
        print(
            f"[STOP NOT VERIFIED] {symbol} HTTP={code} RESPONSE={res}",
            flush=True
        )

    return res, code

def check_signal(candles, symbol):
    if len(candles) < 50:
        print(f"[SCAN] {symbol} not enough candles", flush=True)
        return None
    df = pd.DataFrame(candles)
    if 'close' not in df.columns:
        df = pd.DataFrame(candles, columns=['time','open','high','low','close','volume'])
    df['close'] = pd.to_numeric(df['close'])
    df['volume'] = pd.to_numeric(df['volume'])
    df['ema9'] = df['close'].ewm(span=9).mean()
    df['ema21'] = df['close'].ewm(span=21).mean()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['vol_avg'] = df['volume'].rolling(window=20).mean()
    last = df.iloc[-1]
    prev = df.iloc[-2]
    ema9 = float(last['ema9'])
    ema21 = float(last['ema21'])
    ema9_prev = float(prev['ema9'])
    ema21_prev = float(prev['ema21'])
    rsi = float(last['rsi'])
    vol = float(last['volume'])
    vol_avg = float(last['vol_avg']) if not pd.isna(last['vol_avg']) else vol
    close_px = float(last['close'])
    buy_cross = ema9_prev <= ema21_prev and ema9 > ema21
    sell_cross = ema9_prev >= ema21_prev and ema9 < ema21
    print(f"[SCAN] {symbol} Close:{close_px:.2f} EMA9:{ema9:.1f} EMA21:{ema21:.1f} RSI:{rsi:.1f} VOL:{vol:.0f}/{vol_avg:.0f} CROSS: BUY={buy_cross} SELL={sell_cross}", flush=True)
    if buy_cross and rsi >= 45 and rsi <= 70 and vol > (vol_avg * 0.8):
        print(f"[SIGNAL BUY] {symbol} CROSS UP | RSI {rsi:.1f} >=45 | VOL OK", flush=True)
        return "buy"
    if sell_cross and rsi <= 55 and rsi >= 30 and vol > (vol_avg * 0.8):
        print(f"[SIGNAL SELL] {symbol} CROSS DOWN | RSI {rsi:.1f} <=55 | VOL OK", flush=True)
        return "sell"
    if not (buy_cross or sell_cross):
        print(f"[NO CROSS] {symbol} cross nahi hua", flush=True)
    elif rsi < 45:
        print(f"[NO RSI] {symbol} RSI {rsi:.1f} <45", flush=True)
    elif vol < (vol_avg * 0.8):
        print(f"[NO VOL] {symbol} VOL kam", flush=True)
    return None

def place_entry(symbol, side):
    body = {"product_symbol": symbol, "size": 40, "side": side, "order_type": "market_order"}
    res, code = api_req("POST", "/v2/orders", body=body)
    print(f"[ENTRY] {symbol} {side} size 40 -> {code}", flush=True)
    return res, code

def parse_pos_symbol(p):
    return p.get("product_symbol") or p.get("product",{}).get("symbol") or ""

def handle_trailing():
    positions = get_pos()
    for p in positions:
        size = float(p.get("size", 0))
        if size == 0:
            continue
        psym = parse_pos_symbol(p)
        if not psym or psym not in COINS:
            continue
        entry = float(p.get("entry_price") or p.get("average_price") or 0)
        if entry == 0:
            continue
        candles = get_candles(psym)
        if not candles:
            continue
        curr = float(candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4])
        pnl = (curr - entry) * size
        old_peak = state["peaks"].get(psym, entry)
        if size > 0:
            new_peak = max(old_peak, curr)
            state["peaks"][psym] = new_peak
            trail_stop = entry + 10 if pnl > 20 else new_peak * (1 - TRAIL_PCT)
            if trail_stop >= curr:
                continue
            print(f"[TRAIL] {psym} LONG CURR {curr:.2f} ENTRY {entry:.2f} BLUE | STOP {trail_stop:.2f} RED DOTTED | PEAK {new_peak:.2f} | PNL {pnl:.2f} | TP1 {curr*1.005:.2f} GREEN", flush=True)
            if trail_stop > state["stops"].get(psym, 0) or pnl > 20:
                cancel_all_stops(psym)
                place_stop(psym, "buy", size, trail_stop)
        else:
            new_peak = min(old_peak, curr) if old_peak != 0 else curr
            state["peaks"][psym] = new_peak
            trail_stop = entry - 10 if pnl > 20 else new_peak * (1 + TRAIL_PCT)
            if trail_stop <= curr:
                continue
            print(f"[TRAIL] {psym} SHORT CURR {curr:.2f} ENTRY {entry:.2f} BLUE | STOP {trail_stop:.2f} RED DOTTED | PEAK {new_peak:.2f} | PNL {pnl:.2f} | TP1 {curr*0.995:.2f} GREEN", flush=True)
            if trail_stop < state["stops"].get(psym, 9999999) or pnl > 20:
                cancel_all_stops(psym)
                place_stop(psym, "sell", size, trail_stop)
        save_state(state)

def main_loop():
    print("=== FINAL - CROSS + RSI45 + VOL + 5min + NO OVERTRADE + RED LINE ===", flush=True)
    while True:
        try:
            handle_trailing()
            positions = get_pos()
            held = []
            for p in positions:
                sz = float(p.get("size", 0))
                if sz == 0:
                    continue
                ps = parse_pos_symbol(p)
                if ps and ps in COINS:
                    held.append(ps)
            held = list(set(held))
            print(f"[POS HELD] {held if held else '[] - Fresh start'} | Total {len(held)}", flush=True)
            if len(held) >= 2:
                print(f"[MAX POS REACHED] {len(held)} positions, no new trade", flush=True)
                print("--- SLEEP 30s ---", flush=True)
                time.sleep(30)
                continue
            for symbol in COINS:
                candles = get_candles(symbol)
                if not candles:
                    print(f"[SCAN] {symbol} no candles", flush=True)
                    continue
                sig = check_signal(candles, symbol)
                if sig:
                    if symbol in held:
                        print(f"[SKIP] {symbol} already has position", flush=True)
                        continue
                    last_t = state["last_trade"].get(symbol, 0)
                    remaining = 300 - (time.time() - last_t)
                    if remaining > 0:
                        print(f"[SKIP] {symbol} cooldown {int(remaining)}s left", flush=True)
                        continue
                    print(f"[SIGNAL] {symbol} {sig} - CONDITION MATCH", flush=True)
                    res, code = place_entry(symbol, sig)
                    if code == 200:
                        curr = float(candles[-1]['close'] if isinstance(candles[-1], dict) else candles[-1][4])
                        state["peaks"][symbol] = curr
                        state["stops"].pop(symbol, None)
                        state["last_trade"][symbol] = time.time()
                        if sig == "buy":
                            stop_px = curr * (1 - TRAIL_PCT)
                            print(f"[CHART] NEW {symbol} BUY @ {curr:.2f} BLUE | STOP {stop_px:.2f} RED DOTTED | TP1 {curr*1.005:.2f} GREEN", flush=True)
                            cancel_all_stops(symbol)
                            place_stop(symbol, "buy", 40, stop_px)
                        else:
                            stop_px = curr * (1 + TRAIL_PCT)
                            print(f"[CHART] NEW {symbol} SELL @ {curr:.2f} BLUE | STOP {stop_px:.2f} RED DOTTED | TP1 {curr*0.995:.2f} GREEN", flush=True)
                            cancel_all_stops(symbol)
                            place_stop(symbol, "sell", 40, stop_px)
                        save_state(state)
                        held.append(symbol)
                        print(f"[HELD UPDATED] {held}", flush=True)
                    else:
                        print(f"[FAIL COOLDOWN] {symbol} 5min", flush=True)
                        state["last_trade"][symbol] = time.time()
                        save_state(state)
                else:
                    print(f"[NO TRADE] {symbol} - Condition match nahi hua", flush=True)
            print("--- SLEEP 30s ---", flush=True)
            time.sleep(30)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(10)
if __name__ == "__main__":
    print("=== BOT STARTING ===", flush=True)

    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    print("=== FLASK STARTED ===", flush=True)

    main_loop()
