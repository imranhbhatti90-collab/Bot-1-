import os, json, time, hmac, hashlib, threading, logging, math
from urllib.parse import urlencode
import requests
import pandas as pd
from flask import Flask, jsonify

# ============================================================
# DELTA INDIA TESTNET ONLY
# ============================================================
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

ENABLE_TRADING = True
MAX_POSITIONS = 10
MARGIN_PER_TRADE = 20.0
MAX_TOTAL_MARGIN = 200.0
BALANCE_BUFFER = 5.0

TREND_TF = "15m"
ENTRY_TF = "5m"
SCAN_INTERVAL = 30
DISCOVERY_INTERVAL = 900
COOLDOWN = 300

EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200
EMA_ENTRY_FAST = 10
EMA_ENTRY_SLOW = 20
RSI_PERIOD = 14
VOLUME_PERIOD = 20
VOL_MULT = 1.10
ATR_PERIOD = 14
ATR_SL_MULT = 1.0
ATR_TP_MULT = 1.0
ATR_TRAIL_MULT = 1.0
PARTIAL_PERCENT = 0.50

STOP_TRIGGER = "last_traded_price"
STATE_FILE = "state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.info

app = Flask(__name__)
session = requests.Session()

# ============================================================
# STATE
# ============================================================
def fresh_state():
    return {
        "positions": {},
        "last_trade": {},
        "last_candle": {},
        "products": {},
        "events": []
    }

def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        for k, v in fresh_state().items():
            d.setdefault(k, v)
        return d
    except Exception:
        return fresh_state()

state = load_state()
state_lock = threading.Lock()

def save_state():
    with state_lock:
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log("[STATE SAVE ERROR] %s", e)

def event(msg):
    state["events"].append({"time": int(time.time()), "message": msg})
    state["events"] = state["events"][-100:]
    log(msg)
    save_state()

# ============================================================
# API AUTH / REQUEST
# ============================================================
def api_request(method, path, params=None, body=None, auth=True):
    params = params or {}
    query = urlencode(params)
    full_path = path + ("?" + query if query else "")
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
    headers = {"Content-Type": "application/json"}

    if auth:
        ts = str(int(time.time()))
        message = method.upper() + ts + full_path + payload
        signature = hmac.new(
            API_SECRET.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        headers.update({
            "api-key": API_KEY,
            "timestamp": ts,
            "signature": signature
        })

    try:
        r = session.request(
            method, BASE_URL + full_path,
            headers=headers,
            data=payload or None,
            timeout=15
        )
        try:
            return r.json(), r.status_code
        except Exception:
            return {"raw": r.text}, r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def public_request(path, params=None):
    try:
        r = session.get(BASE_URL + path, params=params, timeout=15)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

# ============================================================
# DISCOVERY - ACTIVE USD PERPETUALS
# ============================================================
def discover_products():
    res, code = public_request("/v2/products", {"page_size": 200})
    if code != 200 or not isinstance(res, dict) or not res.get("success"):
        event(f"[DISCOVERY ERROR] {res}")
        return []

    raw = res.get("result", [])
    eligible = []

    for p in raw:
        symbol = p.get("symbol") or p.get("product_symbol")
        if not symbol:
            continue

        status = str(p.get("state", p.get("status", ""))).lower()
        contract_type = str(p.get("contract_type", "")).lower()
        settlement = str(p.get("settlement_asset", p.get("settling_asset", ""))).upper()

        if status and status not in ("live", "active", "trading"):
            continue
        if contract_type and "perpetual" not in contract_type:
            continue
        if settlement and settlement not in ("USD", "USDT"):
            continue

        if not symbol.endswith("USD"):
            continue

        # Exclude options and expired/fixed expiry contracts
        if p.get("expiry") or p.get("settlement_time"):
            continue

        eligible.append(p)

    state["products"] = {
        (p.get("symbol") or p.get("product_symbol")): p
        for p in eligible
    }
    save_state()
    event(f"[DISCOVERY] total={len(raw)} eligible={len(eligible)}")
    return list(state["products"].keys())

# ============================================================
# CANDLES / INDICATORS
# ============================================================
def candles(symbol, timeframe, count=600):
    now = int(time.time())
    start = now - count * (15 if timeframe == "15m" else 5) * 60
    params = {
        "resolution": timeframe,
        "symbol": symbol,
        "start": start,
        "end": now
    }
    res, code = public_request("/v2/history/candles", params)
    if code == 200 and isinstance(res, dict) and res.get("success"):
        return res.get("result", [])
    log("[CANDLE ERROR] %s %s %s", symbol, timeframe, res)
    return []

def to_df(raw):
    if not raw:
        return None
    try:
        if isinstance(raw[0], dict):
            df = pd.DataFrame(raw)
        else:
            df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        df = df.sort_values("time" if "time" in df else df.index.name or "close")
        if len(df) < 210:
            return None
        # Remove currently forming candle
        df = df.iloc[:-1].copy()
        df["ema10"] = df.close.ewm(span=10, adjust=False).mean()
        df["ema20"] = df.close.ewm(span=20, adjust=False).mean()
        df["ema50"] = df.close.ewm(span=50, adjust=False).mean()
        df["ema200"] = df.close.ewm(span=200, adjust=False).mean()

        delta = df.close.diff()
        gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
        loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
        rs = gain / loss.replace(0, float("nan"))
        df["rsi"] = 100 - 100 / (1 + rs)
        df["vol_avg"] = df.volume.rolling(VOLUME_PERIOD).mean()

        prev = df.close.shift(1)
        tr = pd.concat([
            df.high - df.low,
            (df.high - prev).abs(),
            (df.low - prev).abs()
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(ATR_PERIOD).mean()
        return df
    except Exception as e:
        log("[INDICATOR ERROR] %s", e)
        return None

# ============================================================
# SIGNAL: 15m TREND + 5m EMA ALIGNMENT
# ============================================================
def signal_for(symbol):
    tdf = to_df(candles(symbol, TREND_TF))
    edf = to_df(candles(symbol, ENTRY_TF))
    if tdf is None or edf is None:
        return None

    t = tdf.iloc[-1]
    e = edf.iloc[-1]
    prev = edf.iloc[-2]

    if any(pd.isna(t[x]) for x in ("ema50", "ema200")):
        return None
    if any(pd.isna(e[x]) for x in ("ema10", "ema20", "rsi", "atr", "vol_avg")):
        return None

    bullish = t.ema50 > t.ema200 and t.close > t.ema200
    bearish = t.ema50 < t.ema200 and t.close < t.ema200
    volume_ok = e.volume >= e.vol_avg * VOL_MULT
    cross_up = prev.ema10 <= prev.ema20 and e.ema10 > e.ema20
    cross_down = prev.ema10 >= prev.ema20 and e.ema10 < e.ema20

    # Alignment entries (cross not mandatory)
    buy_aligned = e.ema10 > e.ema20
    sell_aligned = e.ema10 < e.ema20

    rsi_buy = 50 <= e.rsi <= 68
    rsi_sell = 32 <= e.rsi <= 50

    side = None
    if bullish and buy_aligned and rsi_buy and volume_ok:
        side = "buy"
    elif bearish and sell_aligned and rsi_sell and volume_ok:
        side = "sell"

    log(
        "[SIGNAL DIAG] %s trend=%s EMA10=%.8f EMA20=%.8f RSI=%.2f "
        "VOL=%.2f AVG=%.2f ATR=%.8f CROSS_UP=%s CROSS_DOWN=%s SIGNAL=%s",
        symbol, "BULL" if bullish else "BEAR" if bearish else "MIXED",
        e.ema10, e.ema20, e.rsi, e.volume, e.vol_avg, e.atr,
        cross_up, cross_down, side
    )

    return {
        "side": side,
        "atr": float(e.atr),
        "price": float(e.close),
        "rsi": float(e.rsi),
        "volume": float(e.volume),
        "vol_avg": float(e.vol_avg),
        "candle_ts": int(e.time) if "time" in e else int(time.time())
    }

# ============================================================
# POSITIONS / OPEN ORDERS
# ============================================================
def get_positions():
    res, code = api_request("GET", "/v2/positions/margined")
    if code == 200 and res.get("success"):
        arr = res.get("result", [])
        if isinstance(arr, dict):
            arr = [arr]
        return [p for p in arr if float(p.get("size", 0) or 0) != 0]
    event(f"[POSITIONS ERROR] HTTP={code} {res}")
    return None

def get_orders():
    res, code = api_request("GET", "/v2/orders", {"states": "open,pending"})
    if code == 200 and res.get("success"):
        return res.get("result", [])
    event(f"[ORDERS ERROR] HTTP={code} {res}")
    return None

def orders_for(symbol):
    orders = get_orders()
    if orders is None:
        return None
    return [
        o for o in orders
        if o.get("product_symbol") == symbol
        and o.get("state") in ("open", "pending")
    ]

def cancel_order(order_id):
    res, code = api_request("DELETE", f"/v2/orders/{order_id}")
    return code in (200, 204) or (isinstance(res, dict) and res.get("success"))

def cancel_protection(symbol):
    orders = orders_for(symbol)
    if orders is None:
        return False
    for o in orders:
        if o.get("reduce_only") or o.get("stop_order_type"):
            if not cancel_order(o["id"]):
                event(f"[CANCEL FAILED] {symbol} order={o.get('id')}")
                return False
    return True

def position_map(positions):
    return {p.get("product_symbol"): p for p in positions}

# ============================================================
# PRODUCT / CONTRACT SIZING
# ============================================================
def product_for(symbol):
    return state["products"].get(symbol, {})

def get_leverage(product):
    for key in ("max_leverage", "leverage", "default_leverage"):
        try:
            v = float(product.get(key, 0))
            if v > 0:
                return min(v, 100.0)
        except Exception:
            pass
    # Conservative fallback used for estimates only.
    return 20.0

def contract_value(product):
    try:
        val = float(product.get("contract_value", 1) or 1)
        return val if val > 0 else 1.0
    except Exception:
        return 1.0

def size_for(symbol, price):
    product = product_for(symbol)
    lev = get_leverage(product)
    cval = contract_value(product)

    # Approximate target notional = margin × leverage
    target_notional = MARGIN_PER_TRADE * lev
    size = int(target_notional / max(price * cval, 1e-12))

    # Contract quantity integer; minimum 1
    size = max(1, size)
    notional = size * price * cval
    est_margin = notional / lev

    log("[SIZE] %s size=%s price=%s cval=%s lev-est=%s margin-est=%.2f",
        symbol, size, price, cval, lev, est_margin)
    return size, est_margin, lev

# ============================================================
# ORDER HELPERS
# ============================================================
def place_market(symbol, side, size, reduce_only=False):
    body = {
        "product_symbol": symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "reduce_only": bool(reduce_only)
    }
    res, code = api_request("POST", "/v2/orders", body=body)
    if code in (200, 201) and res.get("success"):
        event(f"[ORDER OK] {symbol} {side} size={size} reduce_only={reduce_only}")
        return res.get("result")
    event(f"[ORDER FAILED] {symbol} {side} size={size} HTTP={code} {res}")
    return None

def place_stop(symbol, side, size, stop_price):
    close_side = "sell" if side == "buy" else "buy"
    product = product_for(symbol)
    tick = float(product.get("tick_size", 0.0001) or 0.0001)
    stop_price = round(round(stop_price / tick) * tick, 8)

    body = {
        "product_symbol": symbol,
        "size": int(size),
        "side": close_side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(stop_price),
        "stop_trigger_method": STOP_TRIGGER,
        "reduce_only": True
    }
    res, code = api_request("POST", "/v2/orders", body=body)
    if code in (200, 201) and res.get("success"):
        event(f"[SL OK] {symbol} stop={stop_price} size={size}")
        return res.get("result")
    event(f"[SL FAILED] {symbol} stop={stop_price} HTTP={code} {res}")
    return None

def place_tp(symbol, side, size, target_price):
    close_side = "sell" if side == "buy" else "buy"
    product = product_for(symbol)
    tick = float(product.get("tick_size", 0.0001) or 0.0001)
    target_price = round(round(target_price / tick) * tick, 8)

    body = {
        "product_symbol": symbol,
        "size": int(size),
        "side": close_side,
        "order_type": "market_order",
        "stop_order_type": "take_profit_order",
        "stop_price": str(target_price),
        "stop_trigger_method": STOP_TRIGGER,
        "reduce_only": True
    }
    res, code = api_request("POST", "/v2/orders", body=body)
    if code in (200, 201) and res.get("success"):
        event(f"[TP OK] {symbol} target={target_price} partial_size={size}")
        return res.get("result")
    event(f"[TP FAILED] {symbol} target={target_price} HTTP={code} {res}")
    return None

# ============================================================
# ORDER / POSITION CHECK HELPERS
# ============================================================
def wait_position(symbol, attempts=6):
    for _ in range(attempts):
        time.sleep(2)
        pos = get_positions()
        if pos is None:
            continue
        for p in pos:
            if p.get("product_symbol") == symbol and float(p.get("size", 0)) != 0:
                return p
    return None

def position_side(size):
    return "buy" if size > 0 else "sell"

def close_side(side):
    return "sell" if side == "buy" else "buy"

def close_position(symbol, size):
    return place_market(symbol, close_side(position_side(size)), abs(int(size)), True)

# ============================================================
# POSITION PROTECTION SETUP
# ============================================================
def setup_protection(symbol, position, atr):
    size = int(float(position.get("size", 0)))
    if size == 0:
        return False

    side = position_side(size)
    entry = float(position.get("entry_price", 0) or 0)
    if entry <= 0 or atr <= 0:
        event(f"[PROTECTION ERROR] {symbol} invalid entry/ATR")
        return False

    sl_dist = atr * ATR_SL_MULT
    tp_dist = atr * ATR_TP_MULT

    if side == "buy":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist

    partial_size = abs(size) // 2
    # One contract cannot be split; no partial TP is placed for size < 2
    if partial_size < 1:
        partial_size = 0

    # Persist management info first
    state["positions"][symbol] = {
        "side": side,
        "entry": entry,
        "atr": atr,
        "initial_sl": sl,
        "target": tp,
        "partial_size": partial_size,
        "partial_done": partial_size == 0,
        "trail_stop": sl,
        "last_protect_update": 0,
        "created": time.time()
    }
    save_state()

    # Place SL before TP; if SL fails, do not place TP and mark unsafe
    sl_order = place_stop(symbol, side, abs(size), sl)
    if not sl_order:
        state["positions"][symbol]["unsafe"] = True
        save_state()
        event(f"[UNPROTECTED POSITION] {symbol} SL failed; entries will pause")
        return False

    if partial_size > 0:
        tp_order = place_tp(symbol, side, partial_size, tp)
        if not tp_order:
            state["positions"][symbol]["unsafe"] = True
            save_state()
            event(f"[TP FAILED / SAFETY] {symbol}; entries paused pending review")
            return False

    state["positions"][symbol]["unsafe"] = False
    state["positions"][symbol]["sl_order_id"] = sl_order.get("id") if isinstance(sl_order, dict) else None
    save_state()
    event(f"[PROTECTION READY] {symbol} SL={sl} TP={tp} partial={partial_size}")
    return True

# ============================================================
# POSITION MANAGEMENT
# ============================================================
def get_symbol_candles(symbol):
    raw = candles(symbol, ENTRY_TF)
    df = to_df(raw)
    if df is None:
        return None
    return df

def update_protective_stop(symbol, side, size, new_stop, meta):
    orders = orders_for(symbol)
    if orders is None:
        return False

    old_stops = [
        o for o in orders
        if o.get("stop_order_type") == "stop_loss_order"
    ]

    # Place replacement first, then cancel old stop to reduce unprotected time
    new_order = place_stop(symbol, side, size, new_stop)
    if not new_order:
        return False

    for o in old_stops:
        if o.get("id") != (new_order.get("id") if isinstance(new_order, dict) else None):
            cancel_order(o.get("id"))

    meta["sl_order_id"] = new_order.get("id") if isinstance(new_order, dict) else None
    meta["trail_stop"] = new_stop
    meta["last_protect_update"] = time.time()
    save_state()
    return True

def manage_positions():
    positions = get_positions()
    if positions is None:
        event("[MANAGEMENT FAIL] Cannot fetch positions; new entries blocked")
        return False

    pmap = position_map(positions)

    for symbol, position in pmap.items():
        size = int(float(position.get("size", 0)))
        if size == 0:
            continue

        side = position_side(size)
        entry = float(position.get("entry_price", 0) or 0)
        if entry <= 0:
            continue

        meta = state["positions"].get(symbol)

        # Existing position not created by bot: do not overwrite its protection.
        if not meta:
            event(f"[EXTERNAL/UNKNOWN POSITION] {symbol}; entries blocked until reviewed")
            return False

        df = get_symbol_candles(symbol)
        if df is None:
            event(f"[MANAGEMENT SKIP] {symbol} candles unavailable")
            return False

        last = df.iloc[-1]
        price = float(last.close)
        atr = float(last.atr)
        if atr <= 0:
            atr = float(meta.get("atr", 0))
        if atr <= 0:
            continue

        # Position size reduction means partial TP likely filled.
        original_size = int(meta.get("original_size", abs(size)))
        if abs(size) < original_size and not meta.get("partial_done", False):
            meta["partial_done"] = True
            meta["partial_filled_at"] = time.time()
            event(f"[PARTIAL CONFIRMED] {symbol} remaining={abs(size)}")
            save_state()

            # Cancel TP remainder if any; now only remaining position managed.
            orders = orders_for(symbol)
            if orders:
                for o in orders:
                    if o.get("stop_order_type") == "take_profit_order":
                        cancel_order(o.get("id"))

            # Move stop to breakeven after confirmed partial fill
            breakeven = entry
            old_stop = float(meta.get("trail_stop", meta.get("initial_sl", 0)))
            if side == "buy":
                candidate = max(old_stop, breakeven)
            else:
                candidate = min(old_stop, breakeven) if old_stop else breakeven

            if update_protective_stop(symbol, side, abs(size), candidate, meta):
                event(f"[BREAKEVEN SET] {symbol} stop={candidate}")

        # Track best favorable price
        peak = float(meta.get("peak_price", entry))
        if side == "buy":
            peak = max(peak, price)
            proposed = peak - atr * ATR_TRAIL_MULT
            old_stop = float(meta.get("trail_stop", meta.get("initial_sl", 0)))
            # Never lower stop on a long
            proposed = max(proposed, old_stop)
            improve = proposed > old_stop + (atr * 0.02)
        else:
            peak = min(peak, price) if peak else price
            proposed = peak + atr * ATR_TRAIL_MULT
            old_stop = float(meta.get("trail_stop", meta.get("initial_sl", 0)))
            # Never raise stop on a short
            proposed = min(proposed, old_stop) if old_stop else proposed
            improve = proposed < old_stop - (atr * 0.02) if old_stop else True

        meta["peak_price"] = peak
        meta["atr"] = atr
        meta["last_price"] = price

        # After partial TP, trail remaining from favorable peak.
        # Before partial, initial SL remains active.
        if meta.get("partial_done") and improve:
            if update_protective_stop(symbol, side, abs(size), proposed, meta):
                event(f"[TRAIL UPDATED] {symbol} side={side} price={price} stop={proposed}")

        event(
            f"[MANAGE] {symbol} side={side} entry={entry:.8f} "
            f"price={price:.8f} ATR={atr:.8f} TP={meta.get('target')} "
            f"SL={meta.get('trail_stop')} partial={meta.get('partial_done')}"
        )

    # Clean metadata only after exchange confirms position is gone
    for symbol in list(state["positions"].keys()):
        if symbol not in pmap:
            event(f"[POSITION CLOSED] {symbol}; clearing metadata")
            state["positions"].pop(symbol, None)

    save_state()
    return True

# ============================================================
# ENTRY FILTERS / RISK GUARDS
# ============================================================
def estimate_used_margin(positions):
    total = 0.0
    for p in positions:
        try:
            symbol = p.get("product_symbol")
            size = abs(float(p.get("size", 0)))
            entry = float(p.get("entry_price", 0))
            product = product_for(symbol)
            lev = get_leverage(product)
            cval = contract_value(product)
            total += size * entry * cval / lev
        except Exception:
            # Unknown sizing => fail-safe, treat as full margin
            total += MARGIN_PER_TRADE
    return total

def entry_allowed(symbol, positions):
    held = {p.get("product_symbol") for p in positions}
    if symbol in held:
        return False, "ALREADY_OPEN"
    if len(held) >= MAX_POSITIONS:
        return False, "MAX_POSITIONS"
    used = estimate_used_margin(positions)
    if used + MARGIN_PER_TRADE > MAX_TOTAL_MARGIN:
        return False, "MAX_MARGIN_CAP"
    if symbol in state["last_trade"]:
        if time.time() - state["last_trade"][symbol] < COOLDOWN:
            return False, "COOLDOWN"
    return True, "OK"

def submit_entry(symbol, signal, atr, price):
    size, margin_est, leverage_est = size_for(symbol, price)

    if size <= 0:
        event(f"[ENTRY SKIP] {symbol} calculated size zero")
        return False

    positions = get_positions()
    if positions is None:
        event("[ENTRY BLOCK] Position query failed")
        return False

    allowed, reason = entry_allowed(symbol, positions)
    if not allowed:
        event(f"[ENTRY BLOCK] {symbol} reason={reason}")
        return False

    if estimate_used_margin(positions) + margin_est > MAX_TOTAL_MARGIN:
        event(f"[ENTRY BLOCK] {symbol} estimated account margin cap")
        return False

    order = place_market(symbol, signal, size, reduce_only=False)
    if not order:
        return False

    # Wait for exchange-confirmed position
    position = wait_position(symbol)
    if not position:
        event(f"[ENTRY WARNING] {symbol} order sent but position not confirmed; NO further entries")
        return False

    actual_size = abs(int(float(position.get("size", 0))))
    if actual_size <= 0:
        return False

    actual_entry = float(position.get("entry_price", price) or price)
    event(f"[ENTRY CONFIRMED] {symbol} side={signal} size={actual_size} entry={actual_entry}")

    # Save original size for partial-fill tracking
    state["positions"][symbol] = {
        "side": signal,
        "entry": actual_entry,
        "atr": atr,
        "original_size": actual_size,
        "partial_size": actual_size // 2,
        "partial_done": actual_size < 2,
        "peak_price": actual_entry,
        "created": time.time(),
        "unsafe": True
    }
    save_state()

    # Protection is based on confirmed actual size/entry
    if not setup_protection(symbol, position, atr):
        event(f"[ENTRY SAFETY] {symbol} protection setup failed")
        return False

    state["positions"][symbol]["original_size"] = actual_size
    state["last_trade"][symbol] = time.time()
    save_state()
    return True

# ============================================================
# FLASK STATUS / DASHBOARD DATA
# ============================================================
@app.route("/")
def home():
    return "DELTA TESTNET 5M SCALPING BOT - RUNNING"

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "testnet": True,
        "trading_enabled": ENABLE_TRADING,
        "symbols": len(state.get("products", {})),
        "positions_managed": len(state.get("positions", {}))
    })

@app.route("/dashboard-data")
def dashboard_data():
    positions = get_positions()
    orders = get_orders()
    return jsonify({
        "testnet": True,
        "trading": ENABLE_TRADING,
        "symbols": list(state["products"].keys()),
        "positions": positions,
        "orders": orders,
        "managed": state["positions"],
        "events": state["events"][-30:]
    })

def run_server():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), use_reloader=False)

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    if not API_KEY or not API_SECRET:
        log("[FATAL] API credentials missing")
        return

    log("=" * 65)
    log("DELTA INDIA TESTNET 5M SCALPING BOT STARTING")
    log("TESTNET URL: %s", BASE_URL)
    log("TRADING ENABLED: %s", ENABLE_TRADING)
    log("MAX POSITIONS: %s", MAX_POSITIONS)
    log("MARGIN PER TRADE: %.2f", MARGIN_PER_TRADE)
    log("MAX ACCOUNT MARGIN: %.2f", MAX_TOTAL_MARGIN)
    log("ENTRY TF: %s | TREND TF: %s", ENTRY_TF, TREND_TF)
    log("SL ATR=%.2f | TP ATR=%.2f | TRAIL ATR=%.2f",
        ATR_SL_MULT, ATR_TP_MULT, ATR_TRAIL_MULT)
    log("=" * 65)

    threading.Thread(target=run_server, daemon=True).start()

    symbols = []
    last_discovery = 0

    while True:
        try:
            if time.time() - last_discovery >= DISCOVERY_INTERVAL or not symbols:
                discovered = discover_products()
                if discovered:
                    symbols = discovered
                last_discovery = time.time()

            # Manage all positions before considering new entries
            safe = manage_positions()

            positions = get_positions()
            if positions is None:
                event("[SCAN HALTED] Cannot verify positions")
                time.sleep(SCAN_INTERVAL)
                continue

            held = {p.get("product_symbol") for p in positions}
            # Safety: if an exchange position is unknown to bot metadata,
            # don't create additional positions until manually reviewed.
            unknown = held - set(state["positions"].keys())
            if unknown:
                event(f"[UNKNOWN OPEN POSITIONS] {sorted(unknown)}")
                time.sleep(SCAN_INTERVAL)
                continue

            # If any position has failed protection, block new entries
            unsafe = any(m.get("unsafe") for m in state["positions"].values())
            if not safe or unsafe:
                event("[NEW ENTRIES BLOCKED] Position management/protection not safe")
                time.sleep(SCAN_INTERVAL)
                continue

            if len(held) >= MAX_POSITIONS:
                log("[MAX POSITIONS] %s/%s", len(held), MAX_POSITIONS)
                time.sleep(SCAN_INTERVAL)
                continue

            for symbol in symbols:
                if symbol in held:
                    continue

                positions = get_positions()
                if positions is None:
                    break

                held_now = {p.get("product_symbol") for p in positions}
                if len(held_now) >= MAX_POSITIONS:
                    break

                allowed, reason = entry_allowed(symbol, positions)
                if not allowed:
                    log("[SKIP] %s %s", symbol, reason)
                    continue

                result = signal_for(symbol)
                if not result:
                    continue

                side = result["side"]
                atr = result["atr"]
                price = result["price"]
                candle_ts = result["candle_ts"]

                if not side or atr <= 0 or price <= 0:
                    continue

                last_candle = state["last_candle"].get(symbol)
                if last_candle == candle_ts:
                    continue

                # Mark candle consumed before order attempt, prevent duplicate entry
                state["last_candle"][symbol] = candle_ts
                save_state()

                log("[SIGNAL] %s %s price=%.8f ATR=%.8f RSI=%.2f",
                    symbol, side.upper(), price, atr, result["rsi"])

                if ENABLE_TRADING:
                    ok = submit_entry(symbol, side, atr, price)
                    if ok:
                        held.add(symbol)
                    # Do not re-enter same symbol on same scan cycle
                    time.sleep(1)

            log("[SCAN DONE] sleeping %ss", SCAN_INTERVAL)
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("Bot stopped by user")
            break
        except Exception as e:
            log("[MAIN ERROR] %s", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
