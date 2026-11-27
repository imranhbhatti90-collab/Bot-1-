import os, json, time, hmac, hashlib, threading, logging, traceback, math
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import pandas as pd
from flask import Flask, jsonify, Response

# ============================================================
# DELTA EXCHANGE INDIA - TESTNET ONLY
# ============================================================
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

AUTO_TRADING = os.getenv("AUTO_TRADING", "true").lower() == "true"
MARGIN_USD = 20.0
MAX_POSITIONS = 10

FALLBACK_SYMBOLS = [
    "1000SHIBUSD", "ADAUSD", "BTCUSD", "DOGEUSD", "DOGSUSD",
    "ETHUSD", "NVDAXUSD", "PAXGUSD", "QQQXUSD", "SILVERUSD",
    "SLVONUSD", "SNDKBUSD", "SOLUSD", "SPYXUSD", "USOONUSD",
    "XAUTUSD", "XRPUSD"
]

TREND_RESOLUTION = "15m"
ENTRY_RESOLUTION = "5m"
CANDLE_LIMIT = 1000
MIN_TREND_CANDLES = 220
MIN_ENTRY_CANDLES = 50
SCAN_INTERVAL = 30
DISCOVERY_INTERVAL = 900

RSI_BUY_MIN, RSI_BUY_MAX = 50.0, 70.0
RSI_SELL_MIN, RSI_SELL_MAX = 30.0, 50.0
VOLUME_MULTIPLIER = 1.0
MAX_ATR_PCT = 5.0
MAX_CANDLE_RANGE_PCT = 6.0
MAX_CANDLE_BODY_PCT = 5.0
MAX_CLOSE_JUMP_PCT = 5.0
ZERO_VOLUME_QUARANTINE_COUNT = 4
ZERO_VOLUME_WINDOW = 5

ATR_SL_MULTIPLIER = 1.2
ATR_TP_MULTIPLIER = 2.0
STOP_TRIGGER_METHOD = "last_traded_price"

STATE_FILE = "state.json"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
REQUEST_TIMEOUT = 15

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("delta-testnet-bot")

app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "Delta-India-Testnet-Bot/1.0"})

lock = threading.RLock()
state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_scan": None,
    "symbols": FALLBACK_SYMBOLS[:],
    "last_signals": {},
    "execution_status": {},
    "orders": [],
    "errors": [],
    "trade_count": 0,
    "wins": 0,
    "losses": 0,
    "realized_pnl_estimate": 0.0,
    "data_quality": {},
}
products_by_symbol = {}
last_discovery = 0
last_executed_candle = {}

# ----------------------- STATE -------------------------------
def load_state():
    global state, last_executed_candle
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                state.update(saved)
                last_executed_candle = saved.get("last_executed_candle", {}) or {}
    except Exception:
        log.exception("Could not load state file")

def save_state():
    with lock:
        state["last_executed_candle"] = last_executed_candle
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)

def record_error(where, exc):
    msg = f"{where}: {type(exc).__name__}: {exc}"
    log.exception(msg)
    with lock:
        state.setdefault("errors", []).append({
            "time": datetime.now(timezone.utc).isoformat(),
            "message": msg
        })
        state["errors"] = state["errors"][-30:]
    save_state()

# ----------------------- API ---------------------------------
def api_request(method, path, params=None, body=None, auth=False):
    method = method.upper()
    params = params or {}
    query = urlencode(sorted((k, str(v)) for k, v in params.items() if v is not None))
    request_path = path + (("?" + query) if query else "")
    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""

    headers = {}
    if auth:
        if not API_KEY or not API_SECRET:
            raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET missing in Render environment")
        timestamp = str(int(time.time()))
        prehash = method + timestamp + request_path + body_text
        signature = hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        headers.update({
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": signature,
            "Content-Type": "application/json",
        })

    url = BASE_URL + request_path
    response = session.request(
        method, url, headers=headers,
        data=(body_text if body is not None else None),
        timeout=REQUEST_TIMEOUT
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text}

    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} {path}: {payload}")
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"API rejected {path}: {payload}")
    return payload

def result_of(payload):
    if isinstance(payload, dict):
        return payload.get("result", payload)
    return payload

def get_products():
    payload = api_request("GET", "/v2/products")
    result = result_of(payload)
    if isinstance(result, dict):
        result = result.get("products", [])
    return result if isinstance(result, list) else []

def discover_symbols(force=False):
    global last_discovery, products_by_symbol
    if not force and time.time() - last_discovery < DISCOVERY_INTERVAL:
        return
    try:
        products = get_products()
        found = {}
        for p in products:
            if not isinstance(p, dict):
                continue
            sym = p.get("symbol")
            if sym in FALLBACK_SYMBOLS and p.get("state", "live") == "live":
                # This bot intentionally supports vanilla linear products only.
                if p.get("notional_type") == "vanilla" and not p.get("is_quanto", False):
                    found[sym] = p
        products_by_symbol = found
        active = [s for s in FALLBACK_SYMBOLS if s in found]
        state["symbols"] = active if active else FALLBACK_SYMBOLS[:]
        last_discovery = time.time()
        log.info("[DISCOVERY] eligible=%s / requested=%s", len(active), len(FALLBACK_SYMBOLS))
        save_state()
    except Exception as e:
        record_error("product discovery", e)
        if not products_by_symbol:
            state["symbols"] = FALLBACK_SYMBOLS[:]

def fetch_candles(symbol, resolution):
    now = int(time.time())
    start = now - (CANDLE_LIMIT * (900 if resolution == "15m" else 300) * 2)
    payload = api_request(
        "GET", "/v2/history/candles",
        params={
            "symbol": symbol, "resolution": resolution,
            "start": start, "end": now
        }
    )
    rows = result_of(payload)
    if isinstance(rows, dict):
        rows = rows.get("candles", [])
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"No candle data for {symbol} {resolution}")

    df = pd.DataFrame(rows)
    rename = {
        "time": "time", "timestamp": "time",
        "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume"
    }
    df = df.rename(columns=rename)
    required = ["time", "open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in required):
        raise ValueError(f"Unexpected candle fields: {list(df.columns)}")
    for c in ["time", "open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=required).sort_values("time").drop_duplicates("time")
    # Ignore the currently forming candle.
    interval = 900 if resolution == "15m" else 300
    df = df[df["time"] + interval <= now]
    return df.tail(CANDLE_LIMIT).reset_index(drop=True)

# ----------------------- INDICATORS ---------------------------
def add_indicators(df):
    d = df.copy()
    close = d["close"]
    d["ema10"] = close.ewm(span=10, adjust=False).mean()
    d["ema20"] = close.ewm(span=20, adjust=False).mean()
    d["ema50"] = close.ewm(span=50, adjust=False).mean()
    d["ema200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    d["rsi"] = 100 - (100 / (1 + rs))
    d["rsi"] = d["rsi"].fillna(50)

    prev_close = close.shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    d["volume_ma20"] = d["volume"].rolling(20).mean()
    return d

def validate_data(df, symbol, resolution):
    if len(df) < (MIN_TREND_CANDLES if resolution == TREND_RESOLUTION else MIN_ENTRY_CANDLES):
        raise ValueError(f"INSUFFICIENT_CLOSED_CANDLES({len(df)})")
    recent = df.tail(ZERO_VOLUME_WINDOW)
    zeros = int((recent["volume"] <= 0).sum())
    if zeros >= ZERO_VOLUME_QUARANTINE_COUNT:
        raise ValueError(f"DATA_QUARANTINE:{resolution}:ZERO_VOLUME_RECENT({zeros}/{ZERO_VOLUME_WINDOW})")

    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last["close"])
    if close <= 0:
        raise ValueError("INVALID_CLOSE")
    atr_pct = float(last.get("atr", 0)) / close * 100
    candle_range_pct = (float(last["high"]) - float(last["low"])) / close * 100
    candle_body_pct = abs(float(last["close"]) - float(last["open"])) / close * 100
    jump_pct = abs(float(last["close"]) - float(prev["close"])) / float(prev["close"]) * 100 if prev["close"] else 999

    if atr_pct > MAX_ATR_PCT:
        raise ValueError(f"ATR_TOO_HIGH({atr_pct:.2f}%)")
    if candle_range_pct > MAX_CANDLE_RANGE_PCT:
        raise ValueError(f"CANDLE_RANGE_TOO_HIGH({candle_range_pct:.2f}%)")
    if candle_body_pct > MAX_CANDLE_BODY_PCT:
        raise ValueError(f"CANDLE_BODY_TOO_HIGH({candle_body_pct:.2f}%)")
    if jump_pct > MAX_CLOSE_JUMP_PCT:
        raise ValueError(f"CLOSE_JUMP_TOO_HIGH({jump_pct:.2f}%)")

    state.setdefault("data_quality", {})[symbol + "_" + resolution] = {
        "status": "OK", "time": int(last["time"]),
        "atr_pct": round(atr_pct, 4)
    }

def analyze_symbol(symbol):
    trend_raw = fetch_candles(symbol, TREND_RESOLUTION)
    entry_raw = fetch_candles(symbol, ENTRY_RESOLUTION)
    trend = add_indicators(trend_raw)
    entry = add_indicators(entry_raw)
    validate_data(trend, symbol, TREND_RESOLUTION)
    validate_data(entry, symbol, ENTRY_RESOLUTION)

    t = trend.iloc[-1]
    e = entry.iloc[-1]
    trend_name = "MIXED"
    if t["ema50"] > t["ema200"] and t["close"] > t["ema50"] and t["close"] > t["ema200"]:
        trend_name = "BULLISH"
    elif t["ema50"] < t["ema200"] and t["close"] < t["ema50"] and t["close"] < t["ema200"]:
        trend_name = "BEARISH"

    signal = "HOLD"
    if (trend_name == "BULLISH" and e["ema10"] > e["ema20"]
            and RSI_BUY_MIN <= e["rsi"] <= RSI_BUY_MAX
            and e["volume"] >= e["volume_ma20"] * VOLUME_MULTIPLIER):
        signal = "BUY"
    elif (trend_name == "BEARISH" and e["ema10"] < e["ema20"]
            and RSI_SELL_MIN <= e["rsi"] <= RSI_SELL_MAX
            and e["volume"] >= e["volume_ma20"] * VOLUME_MULTIPLIER):
        signal = "SELL"

    return {
        "symbol": symbol, "signal": signal, "trend": trend_name,
        "price": float(e["close"]), "atr": float(e["atr"]),
        "rsi": float(e["rsi"]), "ema10": float(e["ema10"]),
        "ema20": float(e["ema20"]), "ema50": float(t["ema50"]),
        "ema200": float(t["ema200"]), "candle_time": int(e["time"]),
        "volume": float(e["volume"]), "volume_ma20": float(e["volume_ma20"])
    }

# ----------------------- ACCOUNT / EXECUTION ------------------
def get_positions():
    payload = api_request("GET", "/v2/positions/margined", auth=True)
    result = result_of(payload)
    if isinstance(result, dict):
        result = result.get("positions", [])
    return result if isinstance(result, list) else []

def open_positions():
    return [
        p for p in get_positions()
        if isinstance(p, dict) and int(float(p.get("size", 0) or 0)) != 0
    ]

def get_leverage(product_id):
    payload = api_request(
        "GET", f"/v2/products/{int(product_id)}/orders/leverage", auth=True
    )
    result = result_of(payload)
    if isinstance(result, dict) and "leverage" in result:
        return float(result["leverage"])
    raise RuntimeError(f"Could not read current leverage: {payload}")

def round_to_tick(value, tick, direction):
    tick = float(tick or 0)
    if tick <= 0:
        return round(float(value), 8)
    units = float(value) / tick
    if direction == "up":
        return round(math.ceil(units) * tick, 8)
    return round(math.floor(units) * tick, 8)

def calculate_order_size(product, price, leverage):
    contract_value = float(product.get("contract_value") or 0)
    if contract_value <= 0 or price <= 0 or leverage <= 0:
        raise ValueError("Invalid price, leverage, or contract_value")
    # Delta vanilla linear notional: price * size * contract_value.
    raw_size = (MARGIN_USD * leverage) / (price * contract_value)
    size = int(math.floor(raw_size))
    limit = product.get("position_size_limit")
    if limit is not None:
        size = min(size, int(float(limit)))
    if size < 1:
        raise ValueError(
            f"$20 margin produces size below 1 contract for {product.get('symbol')}; no order sent"
        )
    return size

def submit_entry(signal_data):
    symbol = signal_data["symbol"]
    product = products_by_symbol.get(symbol)
    if not product:
        raise RuntimeError(f"{symbol}: product metadata unavailable / unsupported contract")
    if not API_KEY or not API_SECRET:
        raise RuntimeError("API credentials missing")

    positions = open_positions()
    if len(positions) >= MAX_POSITIONS:
        state["execution_status"][symbol] = f"BLOCKED_MAX_POSITIONS({len(positions)}/{MAX_POSITIONS})"
        return

    # Do not open a second position on a symbol that already has one.
    for p in positions:
        if p.get("product_symbol") == symbol or p.get("symbol") == symbol:
            state["execution_status"][symbol] = "BLOCKED_POSITION_ALREADY_OPEN"
            return

    candle_key = str(signal_data["candle_time"])
    if last_executed_candle.get(symbol) == candle_key:
        state["execution_status"][symbol] = "BLOCKED_DUPLICATE_CANDLE"
        return

    product_id = product.get("id")
    if not product_id:
        raise RuntimeError(f"{symbol}: product id missing")

    leverage = get_leverage(product_id)  # READ ONLY: never changes leverage.
    price = float(signal_data["price"])
    size = calculate_order_size(product, price, leverage)
    atr = float(signal_data["atr"])
    if atr <= 0:
        raise ValueError("ATR invalid; refusing entry")

    side = "buy" if signal_data["signal"] == "BUY" else "sell"
    tick = float(product.get("tick_size") or 0.01)
    if side == "buy":
        sl = round_to_tick(price - ATR_SL_MULTIPLIER * atr, tick, "down")
        tp = round_to_tick(price + ATR_TP_MULTIPLIER * atr, tick, "up")
    else:
        sl = round_to_tick(price + ATR_SL_MULTIPLIER * atr, tick, "up")
        tp = round_to_tick(price - ATR_TP_MULTIPLIER * atr, tick, "down")

    if sl <= 0 or tp <= 0:
        raise ValueError("Calculated bracket price invalid")

    client_order_id = f"tst-{symbol.lower()}-{candle_key}"[:32]
    order_body = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "reduce_only": False,
        "bracket_stop_loss_price": str(sl),
        "bracket_take_profit_price": str(tp),
        "bracket_stop_trigger_method": STOP_TRIGGER_METHOD,
        "client_order_id": client_order_id
    }

    log.info(
        "[ORDER REQUEST] %s %s size=%s margin_target=$%.2f leverage=%sx "
        "price~%s SL=%s TP=%s",
        symbol, side.upper(), size, MARGIN_USD, leverage, price, sl, tp
    )
    response = api_request("POST", "/v2/orders", body=order_body, auth=True)
    result = result_of(response)
    if isinstance(result, list):
        order_result = result[0] if result else {}
    elif isinstance(result, dict):
        order_result = result
    else:
        order_result = {}

    order_id = order_result.get("id") or order_result.get("order_id")
    if not order_id and not response.get("success", False):
        raise RuntimeError(f"Order response did not confirm acceptance: {response}")

    # Save immediately to reduce duplicate submissions after a restart.
    last_executed_candle[symbol] = candle_key
    state["trade_count"] = int(state.get("trade_count", 0)) + 1
    state["execution_status"][symbol] = "ORDER_ACCEPTED"
    state.setdefault("orders", []).append({
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol, "side": side, "size": size,
        "margin_target_usd": MARGIN_USD, "leverage_read": leverage,
        "reference_price": price, "stop_loss": sl,
        "take_profit": tp, "order_id": order_id,
        "client_order_id": client_order_id,
        "response": response
    })
    state["orders"] = state["orders"][-100:]
    save_state()
    log.info("[ORDER ACCEPTED] %s %s id=%s", symbol, side.upper(), order_id)

# ----------------------- SCANNER ------------------------------
def scan_symbol(symbol):
    try:
        data = analyze_symbol(symbol)
        state.setdefault("last_signals", {})[symbol] = data
        log.info(
            "[SIGNAL] %s trend=%s signal=%s price=%.8f RSI=%.2f ATR=%.8f",
            symbol, data["trend"], data["signal"], data["price"], data["rsi"], data["atr"]
        )
        if data["signal"] in ("BUY", "SELL"):
            if AUTO_TRADING:
                submit_entry(data)
            else:
                state["execution_status"][symbol] = "AUTO_TRADING_DISABLED"
        else:
            state["execution_status"][symbol] = "WAITING_FOR_ALIGNED_SIGNAL"
    except Exception as e:
        state.setdefault("execution_status", {})[symbol] = f"BLOCKED: {e}"
        log.warning("[SCAN BLOCKED] %s: %s", symbol, e)

def scanner_loop():
    load_state()
    log.info("DELTA INDIA TESTNET BOT STARTING | AUTO_TRADING=%s", AUTO_TRADING)
    log.info("Margin target=$%.2f | max positions=%s | leverage will NOT be changed",
             MARGIN_USD, MAX_POSITIONS)
    while True:
        cycle_start = time.time()
        try:
            discover_symbols()
            symbols = state.get("symbols") or FALLBACK_SYMBOLS
            for symbol in symbols:
                scan_symbol(symbol)
                time.sleep(0.15)
            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            save_state()
        except Exception as e:
            record_error("scanner loop", e)
        elapsed = time.time() - cycle_start
        time.sleep(max(1, SCAN_INTERVAL - elapsed))

# ----------------------- WEB DASHBOARD ------------------------
@app.route("/")
def home():
    html = """
    <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Delta Testnet Bot</title><style>
    body{font-family:Arial;background:#101827;color:#eef2ff;margin:20px}
    .card{background:#1d293b;padding:14px;border-radius:10px;margin:10px 0}
    pre{white-space:pre-wrap;word-break:break-word}
    </style></head><body>
    <h2>DELTA EXCHANGE INDIA · TESTNET BOT</h2>
    <div class="card">AUTO TRADING: <b id="auto"></b> | Margin target: $20 | Max positions: 10</div>
    <div class="card"><h3>Scanner status</h3><pre id="data">Loading...</pre></div>
    <script>
    async function refresh(){try{let r=await fetch('/dashboard-data');let d=await r.json();
    document.getElementById('auto').textContent=d.auto_trading?'ON':'OFF';
    document.getElementById('data').textContent=JSON.stringify(d,null,2);}catch(e){}}
    refresh();setInterval(refresh,5000);
    </script></body></html>
    """
    return Response(html, mimetype="text/html")

@app.route("/health")
def health():
    return jsonify({
        "ok": True, "service": "delta-india-testnet",
        "auto_trading": AUTO_TRADING,
        "api_credentials_present": bool(API_KEY and API_SECRET),
        "last_scan": state.get("last_scan")
    })

@app.route("/dashboard-data")
def dashboard_data():
    positions = []
    position_error = None
    if API_KEY and API_SECRET:
        try:
            positions = open_positions()
        except Exception as e:
            position_error = str(e)
    return jsonify({
        "auto_trading": AUTO_TRADING,
        "testnet": True,
        "margin_target_usd": MARGIN_USD,
        "max_positions": MAX_POSITIONS,
        "open_positions": positions,
        "position_error": position_error,
        "state": state
    })

@app.route("/positions")
def positions_route():
    try:
        return jsonify({"success": True, "positions": open_positions()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/diagnostics")
def diagnostics():
    return jsonify({
        "base_url": BASE_URL,
        "testnet_only": True,
        "auto_trading": AUTO_TRADING,
        "api_key_present": bool(API_KEY),
        "api_secret_present": bool(API_SECRET),
        "requested_symbols": FALLBACK_SYMBOLS,
        "eligible_symbols": list(products_by_symbol.keys()),
        "margin_usd": MARGIN_USD,
        "max_positions": MAX_POSITIONS,
        "last_scan": state.get("last_scan"),
        "execution_status": state.get("execution_status", {})
    })

# ----------------------- STARTUP ------------------------------
if __name__ == "__main__":
    threading.Thread(target=scanner_loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
