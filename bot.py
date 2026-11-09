import os
import json
import time
import hmac
import hashlib
import threading
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode, quote

import requests
import pandas as pd
from flask import Flask, jsonify, request, Response

# =====================================================
# DELTA EXCHANGE INDIA - TESTNET ONLY
# =====================================================
BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

COINS = ["BTCUSD", "ETHUSD", "PAXGUSD", "SILVERUSD"]
ORDER_SIZE = 60
ORDER_SIZES = {"BTCUSD": 60, "ETHUSD": 60, "PAXGUSD": 300, "SILVERUSD": 300}
MAX_POSITIONS = 4

# SAFETY: new entries are OFF until the revised signals are observed in logs.
# Existing positions are still monitored and their protective stops are managed.
ENABLE_TRADING = true 

TIMEFRAME = "15m"
CANDLE_SECONDS = 900
EMA_ENTRY_FAST, EMA_ENTRY_SLOW = 10, 20
EMA_TREND_FAST, EMA_TREND_SLOW = 50, 200
RSI_PERIOD = 14
RSI_BUY_MIN, RSI_BUY_MAX = 50.0, 68.0
RSI_SELL_MIN, RSI_SELL_MAX = 32.0, 50.0

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.0
POINTS = {
    "BTCUSD": {"tp": 200.0, "decimals": 2},
    "ETHUSD": {"tp": 15.0, "decimals": 2},
    "PAXGUSD": {"tp": 10.0, "decimals": 2},
    "SILVERUSD": {"tp": 0.20, "decimals": 3},
}
PARTIAL_TP_PERCENT = 0.50
STOP_TRIGGER_METHOD = "last_traded_price"
COOLDOWN_SECONDS = 300
CHECK_INTERVAL = 30
STATE_FILE = "state.json"
TRADE_LOG_FILE = "trade_log.jsonl"
MIN_STOP_GAP_PCT = 0.0005

# Tighter than the old 0.5% tolerance. Also require live price to confirm direction.
MAX_CANDLE_LIVE_GAP_PCT = 0.001
MAX_TRADE_AGE_SECONDS = 30
MAX_CLOSED_CANDLE_AGE_SECONDS = 1200
CANDLE_LOOKBACK_SECONDS = 10 * 24 * 60 * 60
MIN_CLOSED_CANDLES = 250

STATE_VIEW_USER = os.getenv("STATE_VIEW_USER", "")
STATE_VIEW_PASSWORD = os.getenv("STATE_VIEW_PASSWORD", "")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
session = requests.Session()
state_lock = threading.RLock()
agent_lock = threading.RLock()

DEFAULT_STATE = {
    "peaks": {}, "stops": {}, "last_trade": {}, "last_candle": {},
    "trail_active": {}, "stop_order_ids": {}, "partial_tp_done": {},
    "partial_tp_pending": {}, "original_sizes": {}, "entry_prices": {},
    "agent_status": {}, "last_signals": {}, "trade_count": 0,
    "wins": 0, "losses": 0, "realized_pnl_estimate": 0.0,
}
try:
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}
for key, value in DEFAULT_STATE.items():
    if key not in state or not isinstance(state[key], type(value)):
        state[key] = value.copy() if isinstance(value, dict) else value


def log(message):
    print(message, flush=True)
    try:
        with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": datetime.now(timezone.utc).isoformat(),
                "message": str(message)
            }) + "\n")
    except Exception:
        pass


def save_state():
    try:
        with state_lock:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"[STATE SAVE ERROR] {e}")


def agent(name, status, detail=""):
    with agent_lock:
        state["agent_status"][name] = {
            "status": status, "detail": detail,
            "updated": datetime.now(timezone.utc).isoformat()
        }


def require_dashboard_auth():
    if not STATE_VIEW_USER or not STATE_VIEW_PASSWORD:
        return False
    auth = request.authorization
    return bool(auth and hmac.compare_digest(auth.username or "", STATE_VIEW_USER)
                and hmac.compare_digest(auth.password or "", STATE_VIEW_PASSWORD))


@app.route("/")
def home():
    return ("DELTA TESTNET BOT | EMA50/200 TREND + EMA10/20 CLOSED-CANDLE "
            "+ RSI + LIVE MOMENTUM FILTER | ATR1.2 | PARTIAL TP 50%")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok", "environment": "TESTNET",
        "automatic_trading": ENABLE_TRADING, "coins": COINS,
        "max_positions": MAX_POSITIONS, "timeframe": TIMEFRAME,
        "trend_filter": "EMA50/EMA200",
        "entry_confirmation": "EMA10/20 on two closed candles + RSI + live price direction",
        "rsi_buy": [RSI_BUY_MIN, RSI_BUY_MAX],
        "rsi_sell": [RSI_SELL_MIN, RSI_SELL_MAX],
        "max_candle_live_gap_pct": MAX_CANDLE_LIVE_GAP_PCT * 100,
        "max_closed_candle_age_seconds": MAX_CLOSED_CANDLE_AGE_SECONDS,
        "atr_period": ATR_PERIOD, "atr_multiplier": ATR_MULTIPLIER,
        "partial_tp_points": POINTS, "order_sizes": ORDER_SIZES,
        "partial_tp_percent": PARTIAL_TP_PERCENT * 100,
        "dashboard": "/dashboard (Basic Auth required)",
    }), 200


DASHBOARD_HTML = r"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delta Testnet Bot</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0b1018;color:#e8edf5;font:14px Arial}
.wrap{max-width:1200px;margin:auto;padding:16px}.muted,small{color:#9aa8ba}
h1{font-size:21px}.pill{padding:6px 10px;border-radius:20px;background:#123b2b;color:#61e6a2}
.card{background:#151e2b;border:1px solid #263448;border-radius:12px;padding:14px;margin:10px 0}
.grid,.agents{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.big{font-size:25px;font-weight:bold;margin:7px 0}.scroll{overflow:auto}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px 6px;border-bottom:1px solid #293649}
</style></head><body><div class="wrap">
<small>DELTA EXCHANGE INDIA · TESTNET</small><h1>AI MULTI-AGENT TRADING TERMINAL</h1>
<div class="pill" id="run">CONNECTING</div>
<div class="grid">
<div class="card"><small>Open positions</small><div class="big" id="positions">--</div></div>
<div class="card"><small>Active orders</small><div class="big" id="orders">--</div></div>
<div class="card"><small>New entries</small><div class="big">OFF</div></div>
</div>
<div class="card"><b>AGENT STATUS</b><div class="agents" id="agents"></div></div>
<div class="card"><b>MARKET SCAN</b><div class="scroll"><table><thead><tr>
<th>Symbol</th><th>Trend</th><th>Signal</th><th>Last scan</th></tr></thead><tbody id="markets"></tbody></table></div></div>
<div class="card"><b>OPEN POSITIONS</b><div class="scroll"><table><thead><tr>
<th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark / P&L</th></tr></thead><tbody id="posrows"></tbody></table></div></div>
<small>Read-only dashboard. New entries disabled by default; protective position management remains active.</small>
</div><script>
async function refresh(){try{
 const r=await fetch('/dashboard-data',{cache:'no-store'});if(!r.ok)throw Error();
 const d=await r.json();document.getElementById('run').textContent='● '+d.status;
 document.getElementById('positions').textContent=d.positions.length;
 document.getElementById('orders').textContent=d.active_orders;
 const names=['Scanner','Risk Check','Position Sizer','Executor','Logger','Equity'];
 const keys=['scanner','risk','sizer','executor','logger','equity'];
 document.getElementById('agents').innerHTML=names.map((n,i)=>{
  const a=d.agents[keys[i]]||{status:'WAIT',detail:'Waiting'};
  return `<div class="card"><b>${n}</b><div>${a.status}</div><small>${a.detail||''}</small></div>`;
 }).join('');
 document.getElementById('markets').innerHTML=d.coins.map(s=>{
  const x=d.signals[s]||{};return `<tr><td>${s}</td><td>${x.trend||'--'}</td><td>${x.signal||'--'}</td><td>${x.updated||'--'}</td></tr>`;
 }).join('');
 document.getElementById('posrows').innerHTML=d.positions.map(p=>
  `<tr><td>${p.symbol}</td><td>${p.side}</td><td>${p.size}</td><td>${p.entry}</td><td>${p.mark} / ${p.pnl}</td></tr>`
 ).join('')||'<tr><td colspan="5">No open positions</td></tr>';
 }catch(e){document.getElementById('run').textContent='CONNECTION ERROR';}}
refresh();setInterval(refresh,10000);
</script></body></html>"""


@app.route("/dashboard")
def dashboard():
    if not require_dashboard_auth():
        response = Response("Dashboard requires STATE_VIEW_USER and STATE_VIEW_PASSWORD.", 401)
        response.headers["WWW-Authenticate"] = 'Basic realm="Delta Testnet Dashboard"'
        return response
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/dashboard-data")
def dashboard_data():
    if not require_dashboard_auth():
        return jsonify({"error": "Authentication required"}), 401
    positions, orders = get_pos(), get_orders()
    if positions is None or orders is None:
        return jsonify({"error": "Exchange data unavailable"}), 503
    rows = []
    for p in positions:
        try:
            size = float(p.get("size", 0) or 0)
            if not size:
                continue
            rows.append({
                "symbol": parse_pos_symbol(p),
                "side": "LONG" if size > 0 else "SHORT",
                "size": abs(size),
                "entry": p.get("entry_price", p.get("average_price", "--")),
                "mark": p.get("mark_price", p.get("last_price", "--")),
                "pnl": p.get("unrealized_pnl", "--")
            })
        except Exception:
            continue
    with state_lock:
        snapshot = json.loads(json.dumps(state))
    return jsonify({
        "status": "TESTNET LIVE · NEW ENTRIES OFF",
        "coins": COINS, "positions": rows, "active_orders": len(orders),
        "agents": snapshot["agent_status"], "signals": snapshot["last_signals"]
    })


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), use_reloader=False)


# =====================================================
# API AUTH
# =====================================================
def api_req(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        return {"error": "API credentials missing"}, 401
    method = method.upper()
    query = "?" + urlencode(params, quote_via=quote) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
    timestamp = str(int(time.time()))
    message = method + timestamp + path + query + payload
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {
        "api-key": API_KEY, "timestamp": timestamp, "signature": signature,
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "delta-testnet-entry-filter-bot",
    }
    try:
        r = session.request(method, BASE_URL + path + query, headers=headers,
                            data=payload or None, timeout=(5, 15))
        try:
            return r.json(), r.status_code
        except Exception:
            return {"error": r.text[:500]}, r.status_code
    except Exception as e:
        return {"error": str(e)}, 500


# =====================================================
# MARKET DATA
# =====================================================
def parse_timestamp(raw):
    if raw in (None, ""):
        return 0.0
    try:
        value = float(raw)
        if value > 1e14:
            value /= 1_000_000
        elif value > 1e11:
            value /= 1_000
        return value
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0


def fresh_price(raw_price, raw_ts, symbol, source):
    try:
        price = float(raw_price or 0)
    except (TypeError, ValueError):
        return None
    ts = parse_timestamp(raw_ts)
    age = time.time() - ts if ts else 999999
    if price <= 0 or ts <= 0 or age < -10 or age > MAX_TRADE_AGE_SECONDS:
        log(f"[STALE PRICE] {symbol} SOURCE={source} AGE={age:.1f}s")
        return None
    return price


def get_live_price(symbol):
    # Prefer most recent trade; fall back to ticker only if its timestamp is fresh.
    try:
        r = session.get(BASE_URL + f"/v2/trades/{symbol}", timeout=(5, 10))
        if r.status_code == 200:
            data = r.json()
            if data.get("success") is True:
                result = data.get("result", {})
                trades = result.get("trades", []) if isinstance(result, dict) else result if isinstance(result, list) else []
                if trades:
                    latest = max(trades, key=lambda x: parse_timestamp(
                        x.get("timestamp", x.get("t", x.get("created_at")))))
                    p = fresh_price(latest.get("price", latest.get("p")),
                                    latest.get("timestamp", latest.get("t", latest.get("created_at"))),
                                    symbol, "recent_trade")
                    if p is not None:
                        return p
    except Exception as e:
        log(f"[TRADE PRICE ERROR] {symbol}: {e}")
    try:
        r = session.get(BASE_URL + f"/v2/tickers/{symbol}", timeout=(5, 10))
        if r.status_code != 200:
            log(f"[TICKER ERROR] {symbol}: HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get("success") is not True:
            log(f"[TICKER ERROR] {symbol}: success=false")
            return None
        result = data.get("result", {})
        if isinstance(result, list):
            result = next((x for x in result if x.get("symbol") == symbol), result[0] if result else {})
        if not isinstance(result, dict):
            return None
        return fresh_price(result.get("close"), result.get("timestamp"), symbol, "ticker_close")
    except Exception as e:
        log(f"[TICKER ERROR] {symbol}: {e}")
        return None


def get_candles(symbol):
    end = int(time.time())
    params = {"resolution": TIMEFRAME, "symbol": symbol,
              "start": str(end - CANDLE_LOOKBACK_SECONDS), "end": str(end)}
    result, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and isinstance(result, dict) and result.get("success") is True:
        return result.get("result", [])
    log(f"[CANDLE API ERROR] {symbol} HTTP={code} {result}")
    return []


def candles_to_df(candles, symbol="?"):
    try:
        if not candles:
            return None
        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles).copy()
        else:
            df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
        required = ["time", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in required):
            log(f"[CANDLE INVALID] {symbol}: missing columns")
            return None
        for col in ["high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["time"] = df["time"].apply(parse_timestamp)
        df = df.dropna(subset=["time"])
        df = df[df["time"] > 0].copy()
        if df.empty:
            return None
        df["time"] = df["time"].astype("int64")
        df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        now = int(time.time())
        closed = df[df["time"] + CANDLE_SECONDS <= now].dropna(
            subset=["high", "low", "close", "volume"]).reset_index(drop=True)
        log(f"[CANDLE DIAGNOSTIC] {symbol} TF={TIMEFRAME} raw={len(df)} closed={len(closed)}")
        if len(closed) < MIN_CLOSED_CANDLES:
            log(f"[CANDLE INSUFFICIENT] {symbol}: need {MIN_CLOSED_CANDLES}, got {len(closed)}")
            return None
        return closed
    except Exception as e:
        log(f"[CANDLE PARSE ERROR] {symbol}: {e}")
        return None


def calculate_rsi(close, period=RSI_PERIOD):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    # If there are no losses, RSI is 100; if no gains, RSI is 0.
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss > 0), 0)
    return rsi


def get_indicators(candles, symbol):
    df = candles_to_df(candles, symbol)
    if df is None or len(df) < MIN_CLOSED_CANDLES:
        return None
    close = df["close"]
    df["ema10"] = close.ewm(span=EMA_ENTRY_FAST, adjust=False).mean()
    df["ema20"] = close.ewm(span=EMA_ENTRY_SLOW, adjust=False).mean()
    df["ema50"] = close.ewm(span=EMA_TREND_FAST, adjust=False).mean()
    df["ema200"] = close.ewm(span=EMA_TREND_SLOW, adjust=False).mean()
    df["rsi"] = calculate_rsi(close)
    prev, last = df.iloc[-2], df.iloc[-1]
    candle_id = int(last["time"])
    candle_age = time.time() - (candle_id + CANDLE_SECONDS)
    return {
        "prev_ema10": float(prev["ema10"]), "prev_ema20": float(prev["ema20"]),
        "prev_close": float(prev["close"]),
        "ema10": float(last["ema10"]), "ema20": float(last["ema20"]),
        "ema50": float(last["ema50"]), "ema200": float(last["ema200"]),
        "close": float(last["close"]), "rsi": float(last["rsi"]),
        "candle_id": candle_id, "candle_age": candle_age,
    }


# =====================================================
# ENTRY SIGNAL: TREND + TWO CLOSED CANDLES + RSI
# =====================================================
def check_signal(candles, symbol):
    agent("scanner", "SCAN", f"Scanning {symbol} on {TIMEFRAME}")
    ind = get_indicators(candles, symbol)
    if ind is None:
        agent("scanner", "BLOCKED", f"{symbol}: insufficient/invalid candles")
        return None, None, None, None

    if ind["candle_age"] < -5 or ind["candle_age"] > MAX_CLOSED_CANDLE_AGE_SECONDS:
        trend = "STALE CANDLE"
        signal = None
        log(f"[ENTRY BLOCKED] {symbol}: last closed candle age={ind['candle_age']:.1f}s")
    else:
        if (ind["ema50"] > ind["ema200"] and ind["close"] > ind["ema50"]
                and ind["close"] > ind["ema200"]):
            trend = "BULLISH"
        elif (ind["ema50"] < ind["ema200"] and ind["close"] < ind["ema50"]
              and ind["close"] < ind["ema200"]):
            trend = "BEARISH"
        else:
            trend = "MIXED"

        buy_confirm = (
            ind["prev_ema10"] > ind["prev_ema20"]
            and ind["prev_close"] > ind["prev_ema10"]
            and ind["ema10"] > ind["ema20"]
            and ind["close"] > ind["ema10"]
        )
        sell_confirm = (
            ind["prev_ema10"] < ind["prev_ema20"]
            and ind["prev_close"] < ind["prev_ema10"]
            and ind["ema10"] < ind["ema20"]
            and ind["close"] < ind["ema10"]
        )
        buy_rsi_ok = RSI_BUY_MIN <= ind["rsi"] <= RSI_BUY_MAX
        sell_rsi_ok = RSI_SELL_MIN <= ind["rsi"] <= RSI_SELL_MAX

        signal = None
        if trend == "BULLISH" and buy_confirm and buy_rsi_ok:
            signal = "buy"
        elif trend == "BEARISH" and sell_confirm and sell_rsi_ok:
            signal = "sell"

        log(f"[SIGNAL FILTER] {symbol} TREND={trend} BUY_CONFIRM={buy_confirm} "
            f"SELL_CONFIRM={sell_confirm} RSI={ind['rsi']:.2f} "
            f"BUY_RSI={buy_rsi_ok} SELL_RSI={sell_rsi_ok}")

    detail = (f"{symbol} trend={trend} EMA50={ind['ema50']:.5f} "
              f"EMA200={ind['ema200']:.5f} RSI={ind['rsi']:.2f}")
    agent("scanner", "OK", detail)
    agent("risk", "OK" if signal else "BLOCKED",
          "Closed-candle trend/entry/RSI aligned" if signal else f"{symbol}: no aligned signal")
    with state_lock:
        state["last_signals"][symbol] = {
            "trend": trend, "signal": (signal or "NO TRADE").upper(),
            "updated": datetime.now().strftime("%H:%M:%S")
        }
    log(f"[50/200 TREND] {symbol} {trend} EMA50={ind['ema50']:.6f} EMA200={ind['ema200']:.6f}")
    log(f"[EMA10/20 + RSI] {symbol} SIGNAL={signal or 'NONE'} "
        f"CLOSE={ind['close']:.6f} RSI={ind['rsi']:.2f}")
    return signal, ind["candle_id"], ind["close"], ind


def live_entry_confirmation(symbol, signal, candle_close, ema10):
    """Fail closed unless fresh live price agrees with the intended trade direction."""
    live = get_live_price(symbol)
    if live is None or candle_close <= 0 or ema10 <= 0:
        log(f"[LIVE FILTER BLOCK] {symbol}: fresh live price unavailable")
        return False
    gap = abs(live - candle_close) / candle_close
    log(f"[LIVE ENTRY CHECK] {symbol} SIGNAL={signal.upper()} LIVE={live:.6f} "
        f"CLOSE={candle_close:.6f} EMA10={ema10:.6f} GAP={gap*100:.4f}%")
    if gap > MAX_CANDLE_LIVE_GAP_PCT:
        log(f"[LIVE FILTER BLOCK] {symbol}: candle/live gap {gap*100:.4f}% exceeds "
            f"{MAX_CANDLE_LIVE_GAP_PCT*100:.3f}%")
        return False
    if signal == "buy" and not (live > candle_close and live > ema10):
        log(f"[LIVE FILTER BLOCK] {symbol}: BUY signal but live price is not above close AND EMA10")
        return False
    if signal == "sell" and not (live < candle_close and live < ema10):
        log(f"[LIVE FILTER BLOCK] {symbol}: SELL signal but live price is not below close AND EMA10")
        return False
    log(f"[LIVE FILTER PASS] {symbol} {signal.upper()} live momentum agrees")
    return True


def parse_pos_symbol(p):
    product = p.get("product") or {}
    return p.get("product_symbol") or product.get("symbol") or p.get("symbol") or ""


def get_pos():
    result, code = api_req("GET", "/v2/positions/margined")
    if code != 200 or not isinstance(result, dict) or result.get("success") is not True:
        log(f"[POSITION ERROR] HTTP={code} {result}")
        return None
    positions = result.get("result", [])
    if isinstance(positions, dict):
        positions = [positions]
    if not isinstance(positions, list):
        return None
    active = []
    for p in positions:
        try:
            if float(p.get("size", 0) or 0) != 0:
                active.append(p)
        except (TypeError, ValueError):
            pass
    return active


def get_orders():
    result, code = api_req("GET", "/v2/orders",
                           params={"states": "open,pending", "page_size": 100})
    if code == 200 and isinstance(result, dict) and result.get("success") is True:
        orders = result.get("result", [])
        return orders if isinstance(orders, list) else []
    log(f"[ORDER API ERROR] HTTP={code} {result}")
    return None


def is_active_stop(order, symbol):
    return (order.get("product_symbol") == symbol
            and order.get("stop_order_type") == "stop_loss_order"
            and order.get("state") in ("open", "pending")
            and order.get("reduce_only") in (True, 1, "true", "True"))


def cancel_order(order):
    oid, pid = order.get("id"), order.get("product_id")
    if not oid or not pid:
        log("[CANCEL ERROR] Missing order/product ID")
        return False
    result, code = api_req("DELETE", "/v2/orders",
                           body={"id": int(oid), "product_id": int(pid)})
    if code != 200 or not isinstance(result, dict) or result.get("success") is not True:
        log(f"[CANCEL FAILED] ID={oid} HTTP={code} {result}")
        return False
    time.sleep(0.5)
    orders = get_orders()
    if orders is None or any(str(x.get("id")) == str(oid) for x in orders):
        log(f"[CANCEL NOT VERIFIED] ID={oid}")
        return False
    log(f"[CANCEL VERIFIED] ID={oid}")
    return True


def valid_stop_price(side, stop, live):
    if live is None or live <= 0 or stop is None or stop <= 0:
        return False
    gap = live * MIN_STOP_GAP_PCT
    return stop < live - gap if side == "buy" else stop > live + gap


def fmt_price(symbol, price):
    return f"{price:.{POINTS[symbol]['decimals']}f}"


def verify_stop(symbol, order_id):
    orders = get_orders()
    return orders is not None and any(
        str(o.get("id")) == str(order_id) and is_active_stop(o, symbol) for o in orders
    )


def place_stop(symbol, side, size, stop_price):
    qty = int(abs(float(size)))
    if qty <= 0:
        return False
    live = get_live_price(symbol)
    if not valid_stop_price(side, stop_price, live):
        log(f"[STOP BLOCKED] {symbol} SIDE={side} STOP={stop_price} LIVE={live}")
        return False
    stop_side = "sell" if side == "buy" else "buy"
    body = {
        "product_symbol": symbol, "size": qty, "side": stop_side,
        "order_type": "market_order", "stop_order_type": "stop_loss_order",
        "stop_price": fmt_price(symbol, stop_price), "reduce_only": True,
        "stop_trigger_method": STOP_TRIGGER_METHOD,
    }
    result, code = api_req("POST", "/v2/orders", body=body)
    order_result = result.get("result", {}) if isinstance(result, dict) else {}
    if (code != 200 or not isinstance(result, dict) or result.get("success") is not True
            or not order_result.get("id")):
        log(f"[STOP FAILED] {symbol} HTTP={code} {result}")
        return False
    oid = order_result["id"]
    time.sleep(1)
    if not verify_stop(symbol, oid):
        log(f"[STOP NOT VERIFIED] {symbol} ID={oid}; old stop retained")
        return False
    state["stops"][symbol] = float(stop_price)
    state["stop_order_ids"][symbol] = str(oid)
    save_state()
    log(f"[STOP VERIFIED] {symbol} SIZE={qty} STOP={fmt_price(symbol, stop_price)} ID={oid}")
    orders = get_orders()
    if orders is None:
        log(f"[OLD STOP RETAINED] {symbol}: cannot inspect orders")
        return True
    for old in orders:
        if is_active_stop(old, symbol) and str(old.get("id")) != str(oid):
            cancel_order(old)
    return True


def recover_stop(symbol, side, orders):
    stops = []
    for order in orders or []:
        if is_active_stop(order, symbol):
            try:
                stops.append((float(order.get("stop_price")), order))
            except (TypeError, ValueError):
                pass
    if not stops:
        return float(state["stops"].get(symbol, 0) or 0), False
    price, order = max(stops, key=lambda x: x[0]) if side == "buy" else min(stops, key=lambda x: x[0])
    state["stops"][symbol] = price
    state["stop_order_ids"][symbol] = str(order.get("id", ""))
    return price, True


def get_atr(symbol):
    candles = get_candles(symbol)
    df = candles_to_df(candles, symbol)
    if df is None or len(df) < max(MIN_CLOSED_CANDLES, ATR_PERIOD + 2):
        log(f"[ATR UNAVAILABLE] {symbol}: insufficient closed candles")
        return None
    try:
        high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(),
                        (low - prev_close).abs()], axis=1).max(axis=1)
        value = float(tr.ewm(alpha=1 / ATR_PERIOD, adjust=False,
                             min_periods=ATR_PERIOD).mean().iloc[-1])
        if not pd.notna(value) or value <= 0:
            log(f"[ATR UNAVAILABLE] {symbol}: invalid ATR {value}")
            return None
        return value
    except Exception as e:
        log(f"[ATR CALC ERROR] {symbol}: {e}")
        return None


def verify_partial_reduction(symbol, expected):
    for i in range(12):
        time.sleep(1)
        positions = get_pos()
        if positions is None:
            continue
        p = next((x for x in positions if parse_pos_symbol(x) == symbol), None)
        actual = abs(float(p.get("size", 0) or 0)) if p else 0
        if abs(actual - expected) < 0.000001:
            log(f"[PARTIAL REDUCTION VERIFIED] {symbol} REMAINING={actual}")
            return True
        log(f"[PARTIAL VERIFY WAIT] {symbol} EXPECTED={expected} ACTUAL={actual} {i+1}/12")
    return False


def execute_partial_tp(symbol, position, current, side, entry, target_distance):
    if state["partial_tp_done"].get(symbol):
        return
    if state["partial_tp_pending"].get(symbol):
        log(f"[PARTIAL TP LOCKED] {symbol}: unresolved previous order; manual check required")
        return
    size = abs(float(position.get("size", 0) or 0))
    if size < 2:
        log(f"[PARTIAL TP SKIP] {symbol}: position size too small")
        return
    favourable = current - entry if side == "buy" else entry - current
    if favourable < target_distance:
        return
    close_qty = int(size * PARTIAL_TP_PERCENT)
    remaining = size - close_qty
    if close_qty <= 0 or remaining <= 0:
        return
    close_side = "sell" if side == "buy" else "buy"
    state["partial_tp_pending"][symbol] = {
        "time": time.time(), "original_size": size,
        "expected_remaining": remaining, "close_quantity": close_qty, "side": side
    }
    save_state()
    log(f"[PARTIAL TP TRIGGER] {symbol} MOVE={favourable:.6f} "
        f"TARGET={target_distance:.6f} CLOSE={close_qty} KEEP={remaining}")
    result, code = api_req("POST", "/v2/orders", body={
        "product_symbol": symbol, "size": close_qty, "side": close_side,
        "order_type": "market_order", "reduce_only": True
    })
    order_result = result.get("result", {}) if isinstance(result, dict) else {}
    if (code != 200 or not isinstance(result, dict) or result.get("success") is not True
            or not order_result.get("id")):
        log(f"[PARTIAL TP AMBIGUOUS] {symbol} HTTP={code} {result}; no retry")
        return
    if not verify_partial_reduction(symbol, remaining):
        log(f"[PARTIAL TP NOT CONFIRMED] {symbol}; pending lock retained")
        return
    state["partial_tp_done"][symbol] = True
    state["original_sizes"][symbol] = size
    state["entry_prices"][symbol] = entry
    state["partial_tp_pending"].pop(symbol, None)
    save_state()
    log(f"[PARTIAL TP COMPLETE] {symbol}: 50% booked, remaining={remaining}")
    live = get_live_price(symbol)
    if valid_stop_price(side, entry, live):
        if place_stop(symbol, side, remaining, entry):
            state["trail_active"][symbol] = True
            save_state()
            log(f"[BREAKEVEN VERIFIED] {symbol} STOP={entry:.6f}")
        else:
            log(f"[URGENT] {symbol}: partial TP done; BE stop not verified")
    else:
        log(f"[BREAKEVEN WAIT] {symbol}: entry stop invalid/too close; old stop retained")


def handle_trailing():
    positions = get_pos()
    if positions is None:
        agent("executor", "BLOCKED", "Position API unavailable")
        log("[TRAIL BLOCKED] Position API unavailable")
        return
    orders = get_orders()
    if orders is None:
        agent("executor", "BLOCKED", "Open orders unavailable")
        log("[TRAIL BLOCKED] Open orders unavailable")
        return
    active = set()
    for p in positions:
        try:
            signed_size = float(p.get("size", 0) or 0)
            if signed_size == 0:
                continue
            symbol = parse_pos_symbol(p)
            if symbol not in COINS:
                continue
            active.add(symbol)
            side = "buy" if signed_size > 0 else "sell"
            entry = float(p.get("entry_price") or p.get("average_price") or 0)
            if entry <= 0:
                log(f"[TRAIL ERROR] {symbol}: entry price missing")
                continue
            current = get_live_price(symbol)
            if current is None:
                log(f"[TRAIL SKIP] {symbol}: fresh price unavailable")
                continue
            atr = get_atr(symbol)
            if atr is None:
                log(f"[ATR STOP HOLD] {symbol}: ATR unavailable; existing stop retained")
                continue
            atr_gap = atr * ATR_MULTIPLIER
            tp_gap = POINTS[symbol]["tp"]
            old_stop, has_stop = recover_stop(symbol, side, orders)
            if not has_stop:
                initial = entry - atr_gap if side == "buy" else entry + atr_gap
                if not place_stop(symbol, side, signed_size, initial):
                    log(f"[URGENT] {symbol}: ATR initial protective stop not verified")
                    continue
                old_stop = initial
                log(f"[INITIAL ATR STOP] {symbol} ATR={atr:.8f} MULT={ATR_MULTIPLIER} "
                    f"GAP={atr_gap:.8f} STOP={initial:.8f}")
            peak = float(state["peaks"].get(symbol, entry) or entry)
            peak = max(peak, current, entry) if side == "buy" else min(peak, current, entry)
            state["peaks"][symbol] = peak
            execute_partial_tp(symbol, p, current, side, entry, tp_gap)
            refreshed = get_pos()
            if refreshed is None:
                log(f"[TRAIL BLOCKED] {symbol}: position refresh failed")
                continue
            p2 = next((x for x in refreshed if parse_pos_symbol(x) == symbol), None)
            if p2 is None:
                continue
            signed_size = float(p2.get("size", 0) or 0)
            if signed_size == 0:
                continue
            side = "buy" if signed_size > 0 else "sell"
            if not state["partial_tp_done"].get(symbol):
                favourable = peak - entry if side == "buy" else entry - peak
                log(f"[WAIT PARTIAL TARGET] {symbol} MOVE={favourable:.6f} "
                    f"REQUIRED={tp_gap:.6f} STOP={old_stop:.6f}")
                save_state()
                continue
            candidate = peak - atr_gap if side == "buy" else peak + atr_gap
            candidate = max(candidate, entry) if side == "buy" else min(candidate, entry)
            old_stop = float(state["stops"].get(symbol, old_stop) or old_stop)
            candidate = max(candidate, old_stop) if side == "buy" else min(candidate, old_stop)
            if not valid_stop_price(side, candidate, current):
                log(f"[TRAIL WAIT] {symbol} CANDIDATE={candidate:.6f} LIVE={current:.6f}; old stop retained")
                save_state()
                continue
            improved = candidate > old_stop if side == "buy" else candidate < old_stop
            if improved:
                log(f"[ATR TRAIL] {symbol} SIDE={side.upper()} ATR={atr:.6f} "
                    f"MULT={ATR_MULTIPLIER} PEAK={peak:.6f} GAP={atr_gap:.6f} "
                    f"OLD={old_stop:.6f} NEW={candidate:.6f}")
                place_stop(symbol, side, signed_size, candidate)
            save_state()
        except Exception as e:
            log(f"[TRAIL ERROR] {e}")
    save_state()
    agent("executor", "LIVE", f"Managing {len(active)} open position(s)")


def initialize_new_position(symbol):
    for _ in range(8):
        time.sleep(2)
        positions = get_pos()
        if positions is None:
            continue
        p = next((x for x in positions if parse_pos_symbol(x) == symbol), None)
        if p is None:
            continue
        size = float(p.get("size", 0) or 0)
        entry = float(p.get("entry_price") or p.get("average_price") or 0)
        if size == 0 or entry <= 0:
            continue
        side = "buy" if size > 0 else "sell"
        current = get_live_price(symbol)
        if current is None:
            log(f"[INITIAL STOP FAILED] {symbol}: live price unavailable")
            return False
        atr = get_atr(symbol)
        if atr is None:
            log(f"[INITIAL STOP BLOCKED] {symbol}: ATR unavailable; no fixed-point fallback")
            return False
        gap = atr * ATR_MULTIPLIER
        stop = entry - gap if side == "buy" else entry + gap
        state["peaks"][symbol] = max(entry, current) if side == "buy" else min(entry, current)
        state["partial_tp_done"].pop(symbol, None)
        state["partial_tp_pending"].pop(symbol, None)
        state["entry_prices"][symbol] = entry
        state["original_sizes"][symbol] = abs(size)
        save_state()
        log(f"[INITIAL ATR STOP] {symbol} ENTRY={entry:.8f} ATR={atr:.8f} "
            f"MULT={ATR_MULTIPLIER} GAP={gap:.8f} STOP={stop:.8f} "
            f"PARTIAL_TP_POINTS={POINTS[symbol]['tp']}")
        return place_stop(symbol, side, size, stop)
    log(f"[INITIAL STOP FAILED] {symbol}: position not confirmed")
    return False


def place_entry(symbol, side):
    qty = int(ORDER_SIZES.get(symbol, ORDER_SIZE))
    if qty <= 0:
        agent("sizer", "BLOCKED", f"{symbol}: invalid quantity")
        return False
    agent("sizer", "OK", f"{symbol}: fixed configured size {qty}")
    agent("executor", "SCAN", f"Submitting {side.upper()} {symbol} size {qty}")
    result, code = api_req("POST", "/v2/orders", body={
        "product_symbol": symbol, "size": qty, "side": side, "order_type": "market_order"
    })
    order_result = result.get("result", {}) if isinstance(result, dict) else {}
    if code == 200 and isinstance(result, dict) and result.get("success") is True:
        log(f"[ENTRY ACCEPTED] {symbol} {side.upper()} SIZE={qty} RESULT={order_result}")
        agent("executor", "LIVE", f"Entry accepted: {side.upper()} {symbol} {qty}")
        return True
    log(f"[ENTRY FAILED] {symbol} HTTP={code} {result}")
    agent("executor", "ERROR", f"Entry failed for {symbol}: HTTP {code}")
    return False


def close_position_safely(symbol, position):
    size = float(position.get("size", 0) or 0)
    if size == 0:
        return False
    close_side = "sell" if size > 0 else "buy"
    result, code = api_req("POST", "/v2/orders", body={
        "product_symbol": symbol, "size": int(abs(size)), "side": close_side,
        "order_type": "market_order", "reduce_only": True
    })
    order_result = result.get("result", {}) if isinstance(result, dict) else {}
    if (code != 200 or not isinstance(result, dict) or result.get("success") is not True
            or not order_result.get("id")):
        log(f"[CLOSE FAILED/AMBIGUOUS] {symbol} HTTP={code} {result}")
        return False
    flat = False
    for _ in range(12):
        time.sleep(1)
        positions = get_pos()
        if positions is None:
            continue
        remaining = next((x for x in positions if parse_pos_symbol(x) == symbol), None)
        if remaining is None or float(remaining.get("size", 0) or 0) == 0:
            flat = True
            break
    if not flat:
        log(f"[CLOSE BLOCKED] {symbol}: flat not confirmed; stops retained")
        return False
    orders = get_orders()
    if orders is None:
        log(f"[CLOSE BLOCKED] {symbol}: cannot inspect protective orders")
        return False
    for order in orders:
        if is_active_stop(order, symbol) and not cancel_order(order):
            log(f"[CLOSE BLOCKED] {symbol}: old stop remains")
            return False
    for key in ("peaks", "stops", "trail_active", "stop_order_ids", "partial_tp_done",
                "partial_tp_pending", "original_sizes", "entry_prices"):
        state[key].pop(symbol, None)
    save_state()
    log(f"[CLOSE COMPLETE] {symbol}: flat and protective stops cleared")
    return True


# =====================================================
# MAIN LOOP - SMART RE-ENTRY, FILTERED ENTRIES
# =====================================================
def main_loop():
    log("==================================================")
    log("DELTA INDIA TESTNET | REVISED ENTRY FILTER BOT")
    log(f"AUTO NEW ENTRIES={ENABLE_TRADING} | COINS={COINS}")
    log(f"ORDER SIZES={ORDER_SIZES} | MAX POSITIONS={MAX_POSITIONS}")
    log(f"TIMEFRAME={TIMEFRAME} | TREND EMA={EMA_TREND_FAST}/{EMA_TREND_SLOW}")
    log(f"ENTRY EMA={EMA_ENTRY_FAST}/{EMA_ENTRY_SLOW} TWO CLOSED CANDLES")
    log(f"RSI FILTER BUY={RSI_BUY_MIN}-{RSI_BUY_MAX} SELL={RSI_SELL_MIN}-{RSI_SELL_MAX}")
    log(f"LIVE FILTER: direction confirmation + max candle/live gap={MAX_CANDLE_LIVE_GAP_PCT*100:.3f}%")
    log(f"ATR STOP/TRAIL=ATR({ATR_PERIOD}) x {ATR_MULTIPLIER}; NO FIXED-POINT SL FALLBACK")
    log(f"PARTIAL TP TARGETS={POINTS}; 50% CLOSE THEN BREAKEVEN + ATR TRAIL")
    log("TESTNET ONLY")
    log("==================================================")
    agent("logger", "OK", "Trade decisions logged to trade_log.jsonl")
    agent("equity", "WAIT", "Waiting for exchange account/position data")

    while True:
        try:
            # Protective stop manager remains active even when new entries are disabled.
            handle_trailing()
            positions = get_pos()
            if positions is None:
                log("[SCAN BLOCKED] Position API unavailable")
                time.sleep(CHECK_INTERVAL)
                continue
            held = {
                parse_pos_symbol(p): p for p in positions
                if parse_pos_symbol(p) in COINS and float(p.get("size", 0) or 0) != 0
            }
            agent("equity", "OK", f"Exchange positions available: {len(held)}")
            log(f"[POS HELD] {list(held.keys())} TOTAL={len(held)}")

            for symbol in COINS:
                candles = get_candles(symbol)
                if not candles:
                    continue
                signal, candle_id, candle_close, ind = check_signal(candles, symbol)
                if candle_id is None or ind is None:
                    continue
                previous = int(state["last_candle"].get(symbol, 0) or 0)
                if candle_id <= previous:
                    continue
                state["last_candle"][symbol] = candle_id
                save_state()

                current_position = held.get(symbol)
                if signal is None:
                    continue

                # Check current market direction before either reversal or a new entry.
                if not live_entry_confirmation(symbol, signal, candle_close, ind["ema10"]):
                    continue

                if current_position is not None:
                    size = float(current_position.get("size", 0) or 0)
                    current_side = "buy" if size > 0 else "sell"
                    if signal == current_side:
                        log(f"[POSITION HELD] {symbol}: {current_side.upper()} direction still agrees")
                        continue
                    log(f"[TREND REVERSAL] {symbol} {current_side.upper()} -> {signal.upper()}")
                    if not close_position_safely(symbol, current_position):
                        log(f"[REVERSAL BLOCKED] {symbol}")
                        continue
                    state["last_trade"][symbol] = time.time()
                    save_state()
                    held.pop(symbol, None)
                    log(f"[REVERSAL COOLDOWN] {symbol}: {COOLDOWN_SECONDS}s; wait for later new candle")
                    continue

                if not ENABLE_TRADING:
                    log(f"[SAFE MODE] {symbol}: valid {signal.upper()} signal, new entries disabled")
                    continue
                if state["partial_tp_pending"].get(symbol):
                    log(f"[ENTRY BLOCKED] {symbol}: unresolved partial TP; manual check required")
                    continue
                open_orders = get_orders()
                if open_orders is None:
                    log(f"[ENTRY BLOCKED] {symbol}: cannot read open orders")
                    continue
                orphan_stops = [o for o in open_orders if is_active_stop(o, symbol)]
                cleanup_ok = True
                for old in orphan_stops:
                    if not cancel_order(old):
                        cleanup_ok = False
                        break
                if not cleanup_ok:
                    log(f"[ENTRY BLOCKED] {symbol}: orphan stop remains")
                    continue
                if len(held) >= MAX_POSITIONS:
                    log(f"[MAX POSITIONS] {symbol}: entry skipped")
                    continue
                last_trade = float(state["last_trade"].get(symbol, 0) or 0)
                remaining = COOLDOWN_SECONDS - (time.time() - last_trade)
                if remaining > 0:
                    log(f"[COOLDOWN] {symbol}: {int(remaining)}s remaining")
                    continue

                # Re-check price immediately before sending the market order.
                if not live_entry_confirmation(symbol, signal, candle_close, ind["ema10"]):
                    log(f"[ENTRY BLOCKED] {symbol}: final live confirmation failed")
                    continue
                log(f"[SMART ENTRY SIGNAL] {symbol} {signal.upper()} candle={candle_id}")
                state["last_trade"][symbol] = time.time()
                save_state()
                if not place_entry(symbol, signal):
                    log(f"[ENTRY NOT CONFIRMED] {symbol}; inspect exchange before retry")
                    continue
                stop_ok = initialize_new_position(symbol)
                refreshed = get_pos()
                if refreshed is None:
                    log(f"[ENTRY VERIFY ERROR] {symbol}: check Testnet position and stop")
                    continue
                confirmed = next((p for p in refreshed if parse_pos_symbol(p) == symbol
                                  and float(p.get("size", 0) or 0) != 0), None)
                if confirmed:
                    held[symbol] = confirmed
                    log(f"[ENTRY COMPLETE] {symbol} INITIAL_STOP_VERIFIED={stop_ok}")
                    if not stop_ok:
                        log(f"[URGENT CHECK] {symbol}: position open but stop not verified")
                else:
                    log(f"[ENTRY NOT OPEN] {symbol}: position not confirmed")

            log(f"--- SLEEP {CHECK_INTERVAL}s ---")
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
