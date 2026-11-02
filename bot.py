 import os, json, time, hmac, hashlib, threading, logging
from urllib.parse import urlencode, quote
import requests
import pandas as pd
from flask import Flask, jsonify

# DELTA EXCHANGE INDIA TESTNET ONLY
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")
COINS = ["BTCUSD", "ETHUSD", "SOLUSD", "XAUTUSD", "PAXGUSD", "SILVERUSD"]
ORDER_SIZES = {"BTCUSD": 40, "ETHUSD": 40, "SOLUSD": 40,
               "XAUTUSD": 200, "PAXGUSD": 200, "SILVERUSD": 200}
MAX_POSITIONS = 6
ENABLE_TRADING = True
LEVERAGE = 50  # Informational only; this script does not set leverage.

# 5-minute scalping
TIMEFRAME = "5m"
CHECK_INTERVAL = 15
COOLDOWN_SECONDS = 300
STATE_FILE = "state.json"
EMA_FAST, EMA_SLOW, EMA_TREND = 9, 21, 100
RSI_PERIOD, ADX_PERIOD, ATR_PERIOD = 14, 14, 14
MIN_ADX = 18.0
VOLUME_PERIOD, MIN_VOLUME_MULTIPLIER = 20, 1.0
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# Exit parameters: test these on Testnet first.
INITIAL_STOP_ATR = 1.0
TAKE_PROFIT_ATR = 1.0
PROFIT_LOCK_TRIGGER_ATR = 0.5
TRAIL_GAP_ATR = 0.5
STOP_TRIGGER_METHOD = "last_traded_price"
MIN_STOP_GAP_PCT = 0.0005
MAX_CANDLE_LIVE_GAP_PCT = 0.005
MAX_TRADE_AGE_SECONDS = 30
LOOKBACK_SECONDS = 3 * 24 * 60 * 60

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
def log(msg): print(msg, flush=True)

@app.route("/")
def home():
    return "DELTA TESTNET AUTO BOT | 5M SCALPING | EMA 9/21/100 | ATR PROFIT LOCK"

@app.route("/health")
def health():
    return jsonify(status="ok", environment="TESTNET", automatic_trading=ENABLE_TRADING,
                   coins=COINS, max_positions=MAX_POSITIONS, strategy="5m EMA 9/21/100 + RSI + ADX + MACD + volume",
                   stop_atr=INITIAL_STOP_ATR, target_atr=TAKE_PROFIT_ATR,
                   profit_lock_atr=PROFIT_LOCK_TRIGGER_ATR, trail_atr=TRAIL_GAP_ATR), 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

DEFAULT_STATE = {k: {} for k in ("last_trade", "last_candle", "peaks", "initial_gaps",
                                  "targets", "stops", "profit_locked", "stop_order_ids")}
def load_state():
    try:
        with open(STATE_FILE) as f: saved = json.load(f)
        if not isinstance(saved, dict): return DEFAULT_STATE.copy()
        for k in DEFAULT_STATE:
            if not isinstance(saved.get(k), dict): saved[k] = {}
        return saved
    except Exception: return {k: {} for k in DEFAULT_STATE}
state = load_state()
state_lock = threading.Lock()
def save_state():
    try:
        with state_lock:
            with open(STATE_FILE + ".tmp", "w") as f: json.dump(state, f)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e: log(f"[STATE SAVE ERROR] {e}")

session = requests.Session()
def api_req(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET: return {"error": "API credentials missing"}, 401
    method = method.upper()
    qs = "?" + urlencode(params, quote_via=quote) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
    ts = str(int(time.time()))
    msg = method + ts + path + qs + payload
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    headers = {"api-key": API_KEY, "timestamp": ts, "signature": sig,
               "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "python-delta-testnet-scalper"}
    try:
        r = session.request(method, BASE_URL + path + qs, headers=headers,
                            data=payload or None, timeout=(5, 15))
        try: return r.json(), r.status_code
        except Exception: return {"error": r.text[:500]}, r.status_code
    except Exception as e: return {"error": str(e)}, 500

def parse_ts(raw):
    if raw in (None, ""): return 0.0
    try:
        x = float(raw)
        if x > 1e14: x /= 1e6
        elif x > 1e11: x /= 1e3
        return x
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception: return 0.0

def fresh_price(raw, ts, symbol, source):
    try: price = float(raw or 0)
    except (TypeError, ValueError): return None
    stamp = parse_ts(ts)
    age = time.time() - stamp if stamp else 999999
    if price <= 0 or not stamp or age < -10 or age > MAX_TRADE_AGE_SECONDS:
        log(f"[STALE PRICE] {symbol} {source} age={age:.1f}s")
        return None
    return price

def get_live_price(symbol):
    # Actual recent trade first
    try:
        r = session.get(BASE_URL + f"/v2/trades/{symbol}", timeout=(5, 10))
        if r.status_code == 200:
            d = r.json()
            if d.get("success"):
                res = d.get("result", {})
                trades = res.get("trades", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
                if trades:
                    t = max(trades, key=lambda z: parse_ts(z.get("timestamp", z.get("t", z.get("created_at")))))
                    p = fresh_price(t.get("price", t.get("p")),
                                    t.get("timestamp", t.get("t", t.get("created_at"))), symbol, "trade")
                    if p is not None: return p
    except Exception as e: log(f"[TRADE PRICE ERROR] {symbol}: {e}")
    # Ticker close fallback; mark price is intentionally not used.
    try:
        r = session.get(BASE_URL + f"/v2/tickers/{symbol}", timeout=(5, 10))
        if r.status_code != 200: return None
        d = r.json()
        if not d.get("success"): return None
        res = d.get("result", {})
        if isinstance(res, list):
            res = next((x for x in res if x.get("symbol") == symbol), res[0] if res else {})
        if not isinstance(res, dict): return None
        return fresh_price(res.get("close"), res.get("timestamp"), symbol, "ticker_close")
    except Exception as e:
        log(f"[TICKER ERROR] {symbol}: {e}")
        return None

def get_candles(symbol):
    end = int(time.time())
    params = {"resolution": TIMEFRAME, "symbol": symbol,
              "start": str(end - LOOKBACK_SECONDS), "end": str(end)}
    res, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and isinstance(res, dict) and res.get("success") and res.get("result"):
        return res["result"]
    log(f"[CANDLE ERROR] {symbol} HTTP={code} {res}")
    return []

def candles_df(candles):
    try:
        if not candles: return None
        if isinstance(candles[0], dict): df = pd.DataFrame(candles).copy()
        else: df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
        need = ["time", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in need): return None
        for c in need: df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=need).copy()
        def norm(x):
            x = float(x)
            if x > 1e14: x /= 1e6
            elif x > 1e11: x /= 1e3
            return int(x)
        df["time"] = df["time"].apply(norm)
        df = df.sort_values("time").drop_duplicates("time")
        df = df[df["time"] + 300 <= int(time.time())].reset_index(drop=True)
        return df if len(df) >= 120 else None
    except Exception as e:
        log(f"[CANDLE PARSE ERROR] {e}")
        return None

def indicators(df):
    d = df.copy()
    c, h, l = d.close, d.high, d.low
    d["ema9"] = c.ewm(span=EMA_FAST, adjust=False).mean()
    d["ema21"] = c.ewm(span=EMA_SLOW, adjust=False).mean()
    d["ema100"] = c.ewm(span=EMA_TREND, adjust=False).mean()
    delta = c.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    al = loss.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    rs = ag / al.replace(0, float("nan"))
    d["rsi"] = 100 - 100/(1+rs)
    d.loc[(al == 0) & (ag > 0), "rsi"] = 100
    d.loc[(ag == 0) & (al > 0), "rsi"] = 0
    d.loc[(ag == 0) & (al == 0), "rsi"] = 50
    d["vol_avg"] = d.volume.rolling(VOLUME_PERIOD).mean()
    m = c.ewm(span=MACD_FAST, adjust=False).mean() - c.ewm(span=MACD_SLOW, adjust=False).mean()
    d["macd"] = m
    d["macd_signal"] = m.ewm(span=MACD_SIGNAL, adjust=False).mean()
    up, down = h.diff(), -l.diff()
    pdm, mdm = pd.Series(0.0, index=d.index), pd.Series(0.0, index=d.index)
    pdm.loc[(up > down) & (up > 0)] = up
    mdm.loc[(down > up) & (down > 0)] = down
    pc = c.shift(1)
    tr = pd.concat([(h-l), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()
    plus = pdm.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
    minus = mdm.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
    pdi, mdi = 100*plus/atr.replace(0, float("nan")), 100*minus/atr.replace(0, float("nan"))
    dx = 100*(pdi-mdi).abs()/(pdi+mdi).replace(0, float("nan"))
    d["adx"] = dx.ewm(alpha=1/ADX_PERIOD, adjust=False, min_periods=ADX_PERIOD).mean()
    d["atr"] = atr
    return d

def check_signal(candles, symbol):
    df = candles_df(candles)
    if df is None: return None, None
    d = indicators(df)
    x = d.iloc[-1]
    cols = ["ema9", "ema21", "ema100", "rsi", "vol_avg", "macd", "macd_signal", "adx", "atr"]
    if any(pd.isna(x[k]) for k in cols): return None, int(x.time)
    # Pullback/reclaim around EMA9 helps avoid chasing extended candles.
    recent = d.iloc[-4:-1]
    touch_buy = recent.low.min() <= x.ema9 or x.low <= x.ema9
    touch_sell = recent.high.max() >= x.ema9 or x.high >= x.ema9
    vol_ok = x.vol_avg > 0 and x.volume >= x.vol_avg * MIN_VOLUME_MULTIPLIER
    buy = (x.close > x.ema100 and x.ema9 > x.ema21 and x.close > x.ema9
           and touch_buy and 48 <= x.rsi <= 68 and x.adx >= MIN_ADX
           and x.macd > x.macd_signal and vol_ok)
    sell = (x.close < x.ema100 and x.ema9 < x.ema21 and x.close < x.ema9
            and touch_sell and 32 <= x.rsi <= 52 and x.adx >= MIN_ADX
            and x.macd < x.macd_signal and vol_ok)
    log(f"[5M SCAN] {symbol} close={x.close:.5f} EMA9/21/100={x.ema9:.5f}/{x.ema21:.5f}/{x.ema100:.5f} RSI={x.rsi:.1f} ADX={x.adx:.1f} MACD={x.macd:.5f}/{x.macd_signal:.5f} VOL={x.volume:.0f}/{x.vol_avg:.0f} ATR={x.atr:.6f}")
    if buy: log(f"[SCALP SIGNAL] {symbol} BUY"); return "buy", int(x.time)
    if sell: log(f"[SCALP SIGNAL] {symbol} SELL"); return "sell", int(x.time)
    log(f"[NO TRADE] {symbol} conditions not fully confirmed")
    return None, int(x.time)

def get_positions():
    res, code = api_req("GET", "/v2/positions/margined")
    if code != 200 or not isinstance(res, dict) or not res.get("success"):
        log(f"[POSITION ERROR] HTTP={code} {res}")
        return None
    items = res.get("result", [])
    if isinstance(items, dict): items = [items]
    if not isinstance(items, list): return None
    return [p for p in items if float(p.get("size", 0) or 0) != 0]

def pos_symbol(p):
    return p.get("product_symbol") or (p.get("product") or {}).get("symbol") or p.get("symbol") or ""

def get_orders():
    res, code = api_req("GET", "/v2/orders", params={"states": "open,pending", "page_size": 100})
    if code == 200 and isinstance(res, dict) and res.get("success"):
        result = res.get("result", [])
        return result if isinstance(result, list) else []
    log(f"[ORDER LIST ERROR] HTTP={code} {res}")
    return None

def is_stop(o, symbol):
    return (o.get("product_symbol") == symbol and o.get("stop_order_type") == "stop_loss_order"
            and o.get("state") in ("open", "pending")
            and o.get("reduce_only") in (True, 1, "true", "True"))

def cancel_order(o):
    oid, pid = o.get("id"), o.get("product_id")
    if not oid or not pid: return False
    res, code = api_req("DELETE", "/v2/orders", body={"id": int(oid), "product_id": int(pid)})
    if code != 200 or not isinstance(res, dict) or not res.get("success"):
        log(f"[CANCEL FAILED] {oid} HTTP={code} {res}")
        return False
    time.sleep(0.7)
    orders = get_orders()
    ok = orders is not None and not any(str(z.get("id")) == str(oid) for z in orders)
    log(f"[CANCEL {'VERIFIED' if ok else 'NOT VERIFIED'}] ID={oid}")
    return ok

def valid_stop(side, stop, live):
    if live is None or live <= 0 or stop <= 0: return False
    gap = live * MIN_STOP_GAP_PCT
    return stop < live-gap if side == "buy" else stop > live+gap

def place_stop(symbol, side, size, stop):
    live = get_live_price(symbol)
    if not valid_stop(side, stop, live):
        log(f"[STOP BLOCKED] {symbol} stop={stop:.8f} live={live}")
        return False
    close_side = "sell" if side == "buy" else "buy"
    body = {"product_symbol": symbol, "size": int(abs(float(size))), "side": close_side,
            "order_type": "market_order", "stop_order_type": "stop_loss_order",
            "stop_price": f"{stop:.2f}", "reduce_only": True,
            "stop_trigger_method": STOP_TRIGGER_METHOD}
    res, code = api_req("POST", "/v2/orders", body=body)
    result = res.get("result", {}) if isinstance(res, dict) else {}
    if code != 200 or not res.get("success") or not result.get("id"):
        log(f"[STOP FAILED] {symbol} HTTP={code} {res}")
        return False
    oid = result["id"]
    time.sleep(1)
    orders = get_orders()
    if orders is None or not any(str(o.get("id")) == str(oid) and is_stop(o, symbol) for o in orders):
        log(f"[STOP NOT VERIFIED] {symbol} ID={oid}; old stop retained")
        return False
    # Verify new stop first; only then cancel previous stop(s).
    for old in orders:
        if is_stop(old, symbol) and str(old.get("id")) != str(oid):
            cancel_order(old)
    state["stops"][symbol] = float(stop)
    state["stop_order_ids"][symbol] = str(oid)
    save_state()
    log(f"[STOP VERIFIED] {symbol} {close_side.upper()} STOP={stop:.8f} ID={oid}")
    return True

def market_close(symbol, side, size, reason):
    close_side = "sell" if side == "buy" else "buy"
    body = {"product_symbol": symbol, "size": int(abs(float(size))),
            "side": close_side, "order_type": "market_order", "reduce_only": True}
    res, code = api_req("POST", "/v2/orders", body=body)
    if code == 200 and isinstance(res, dict) and res.get("success"):
        log(f"[EXIT SUBMITTED] {symbol} {reason} {close_side.upper()} size={abs(int(size))}")
        return True
    log(f"[EXIT FAILED] {symbol} {reason} HTTP={code} {res}")
    return False

def current_atr(symbol):
    c = get_candles(symbol)
    df = candles_df(c) if c else None
    if df is None: return None
    a = float(indicators(df).iloc[-1].atr)
    return a if a > 0 else None

def initialize_position(symbol):
    for _ in range(6):
        time.sleep(2)
        ps = get_positions()
        if ps is None: continue
        p = next((z for z in ps if pos_symbol(z) == symbol), None)
        if p is None: continue
        size = float(p.get("size", 0) or 0)
        entry = float(p.get("entry_price") or p.get("average_price") or 0)
        if not size or entry <= 0: continue
        atr = current_atr(symbol)
        if atr is None:
            log(f"[INITIAL STOP FAILED] {symbol} ATR unavailable")
            return False
        side = "buy" if size > 0 else "sell"
        gap = atr * INITIAL_STOP_ATR
        target_gap = atr * TAKE_PROFIT_ATR
        stop = entry-gap if side == "buy" else entry+gap
        target = entry+target_gap if side == "buy" else entry-target_gap
        live = get_live_price(symbol)
        if live is None:
            log(f"[INITIAL STOP FAILED] {symbol} last price unavailable")
            return False
        state["initial_gaps"][symbol] = gap
        state["targets"][symbol] = target
        state["peaks"][symbol] = max(entry, live) if side == "buy" else min(entry, live)
        state["profit_locked"][symbol] = False
        save_state()
        log(f"[INITIAL ATR STOP] {symbol} entry={entry:.8f} stop={stop:.8f} target={target:.8f} ATR={atr:.8f}")
        return place_stop(symbol, side, size, stop)
    log(f"[INITIAL STOP FAILED] {symbol} position not confirmed")
    return False

def manage_positions():
    ps = get_positions()
    if ps is None:
        log("[TRAIL BLOCKED] Position API unavailable")
        return
    open_symbols = set()
    for p in ps:
        try:
            symbol = pos_symbol(p)
            if symbol not in COINS: continue
            size = float(p.get("size", 0) or 0)
            if not size: continue
            open_symbols.add(symbol)
            side = "buy" if size > 0 else "sell"
            entry = float(p.get("entry_price") or p.get("average_price") or 0)
            live = get_live_price(symbol)
            if entry <= 0 or live is None:
                log(f"[MANAGE SKIP] {symbol} live/entry unavailable; exchange stop remains")
                continue
            gap = float(state["initial_gaps"].get(symbol, 0) or 0)
            target = float(state["targets"].get(symbol, 0) or 0)
            if gap <= 0:
                # Recover risk gap from active stop after restart.
                orders = get_orders()
                old = [o for o in (orders or []) if is_stop(o, symbol)]
                vals = []
                for o in old:
                    try: vals.append(float(o.get("stop_price")))
                    except (TypeError, ValueError): pass
                if vals:
                    stop = max(vals) if side == "buy" else min(vals)
                    gap = entry-stop if side == "buy" else stop-entry
                if gap <= 0:
                    atr = current_atr(symbol)
                    if atr is None: continue
                    gap = atr * INITIAL_STOP_ATR
                state["initial_gaps"][symbol] = gap
            if target <= 0:
                tg = gap * TAKE_PROFIT_ATR / INITIAL_STOP_ATR
                target = entry+tg if side == "buy" else entry-tg
                state["targets"][symbol] = target

            peak = float(state["peaks"].get(symbol, entry) or entry)
            peak = max(peak, entry, live) if side == "buy" else min(peak, entry, live)
            state["peaks"][symbol] = peak

            # Close at ATR target with reduce_only market order; stop stays until flat confirmed.
            target_hit = live >= target if side == "buy" else live <= target
            if target_hit:
                log(f"[TARGET HIT] {symbol} live={live:.8f} target={target:.8f}")
                market_close(symbol, side, size, "ATR_TAKE_PROFIT")
                continue

            favourable = peak-entry if side == "buy" else entry-peak
            if favourable >= gap * PROFIT_LOCK_TRIGGER_ATR:
                state["profit_locked"][symbol] = True
                lock_pad = max(gap*0.10, entry*0.0002)
                candidate = peak-gap*TRAIL_GAP_ATR if side == "buy" else peak+gap*TRAIL_GAP_ATR
                candidate = max(candidate, entry+lock_pad) if side == "buy" else min(candidate, entry-lock_pad)

                orders = get_orders()
                vals = []
                for o in (orders or []):
                    if is_stop(o, symbol):
                        try: vals.append(float(o.get("stop_price")))
                        except (TypeError, ValueError): pass
                old_stop = (max(vals) if side == "buy" else min(vals)) if vals else 0
                if old_stop > 0:
                    candidate = max(candidate, old_stop) if side == "buy" else min(candidate, old_stop)
                improves = old_stop <= 0 or (candidate > old_stop if side == "buy" else candidate < old_stop)
                if improves and valid_stop(side, candidate, live):
                    log(f"[PROFIT LOCK TRAIL] {symbol} peak={peak:.8f} old={old_stop:.8f} new={candidate:.8f}")
                    place_stop(symbol, side, size, candidate)
                else:
                    log(f"[PROFIT LOCK WAIT] {symbol} candidate={candidate:.8f} live={live:.8f}")
            else:
                log(f"[WAIT PROFIT LOCK] {symbol} move={favourable:.8f} trigger={gap*PROFIT_LOCK_TRIGGER_ATR:.8f}")
            save_state()
        except Exception as e: log(f"[POSITION MANAGER ERROR] {e}")

    # Cancel exchange stops only after positions API confirms the symbol is flat.
    orders = get_orders()
    if orders is not None:
        for symbol in COINS:
            if symbol in open_symbols: continue
            for o in orders:
                if is_stop(o, symbol): cancel_order(o)
            for k in ("peaks", "initial_gaps", "targets", "stops", "profit_locked", "stop_order_ids"):
                state.get(k, {}).pop(symbol, None)
        save_state()

def price_check(symbol, candle_close):
    live = get_live_price(symbol)
    if live is None or candle_close <= 0: return False
    diff = abs(live-candle_close)/candle_close
    log(f"[PRICE CHECK] {symbol} candle={candle_close:.8f} live={live:.8f} gap={diff*100:.3f}%")
    return diff <= MAX_CANDLE_LIVE_GAP_PCT

def place_entry(symbol, side):
    body = {"product_symbol": symbol, "size": ORDER_SIZES[symbol],
            "side": side, "order_type": "market_order"}
    res, code = api_req("POST", "/v2/orders", body=body)
    result = res.get("result", {}) if isinstance(res, dict) else {}
    if code == 200 and isinstance(res, dict) and res.get("success"):
        log(f"[ENTRY ACCEPTED] {symbol} {side.upper()} SIZE={ORDER_SIZES[symbol]} RESULT={result}")
        return True
    log(f"[ENTRY FAILED] {symbol} HTTP={code} {res}")
    return False

def main_loop():
    log("==================================================")
    log("DELTA EXCHANGE INDIA TESTNET 5M SCALPING BOT STARTED")
    log(f"AUTO TRADING={'ON' if ENABLE_TRADING else 'OFF'} | LEVERAGE SETTING={LEVERAGE}x (not set by script)")
    log(f"COINS={COINS} | ORDER_SIZES={ORDER_SIZES} | MAX_POSITIONS={MAX_POSITIONS}")
    log(f"ENTRY=EMA {EMA_FAST}/{EMA_SLOW}/{EMA_TREND} + RSI + ADX + MACD + VOLUME")
    log(f"EXIT=STOP {INITIAL_STOP_ATR} ATR | TARGET {TAKE_PROFIT_ATR} ATR | LOCK {PROFIT_LOCK_TRIGGER_ATR} ATR | TRAIL {TRAIL_GAP_ATR} ATR")
    log("==================================================")
    while True:
        try:
            manage_positions()
            ps = get_positions()
            if ps is None:
                time.sleep(CHECK_INTERVAL)
                continue
            held = {pos_symbol(p) for p in ps if pos_symbol(p) in COINS}
            log(f"[POS HELD] {sorted(held)} TOTAL={len(held)}")
            if ENABLE_TRADING and len(held) < MAX_POSITIONS:
                for symbol in COINS:
                    if symbol in held or len(held) >= MAX_POSITIONS: continue
                    last = float(state["last_trade"].get(symbol, 0) or 0)
                    if time.time()-last < COOLDOWN_SECONDS:
                        log(f"[COOLDOWN] {symbol}")
                        continue
                    candles = get_candles(symbol)
                    if not candles: continue
                    signal, candle_id = check_signal(candles, symbol)
                    if candle_id is None: continue
                    previous = int(state["last_candle"].get(symbol, 0) or 0)
                    if candle_id <= previous:
                        log(f"[CANDLE ALREADY CHECKED] {symbol} {candle_id}")
                        continue
                    state["last_candle"][symbol] = candle_id
                    save_state()
                    if not signal: continue
                    df = candles_df(candles)
                    if df is None or not price_check(symbol, float(df.iloc[-1].close)):
                        log(f"[ENTRY BLOCKED] {symbol} price check failed")
                        continue
                    # Recheck position count just before submitting.
                    latest = get_positions()
                    if latest is None: continue
                    latest_held = {pos_symbol(p) for p in latest if pos_symbol(p) in COINS}
                    if symbol in latest_held or len(latest_held) >= MAX_POSITIONS:
                        held = latest_held
                        continue
                    state["last_trade"][symbol] = time.time()
                    save_state()
                    if not place_entry(symbol, signal): continue
                    held.add(symbol)
                    protected = initialize_position(symbol)
                    log(f"[ENTRY COMPLETE] {symbol} INITIAL_STOP_VERIFIED={protected}")
                    if not protected:
                        log(f"[URGENT] {symbol}: entry accepted but initial stop not verified. Check Testnet immediately.")
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            log(f"[MAIN LOOP ERROR] {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    log("=== BOT STARTING ===")
    threading.Thread(target=run_flask, daemon=True).start()
    log("=== FLASK STARTED ===")
    main_loop()
   
