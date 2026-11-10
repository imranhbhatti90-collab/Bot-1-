import os, json, time, hmac, hashlib, threading, logging
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

# SAFE MODE: validate the new signals before enabling automatic entries.
ENABLE_TRADING = False

TREND_TIMEFRAME = "15m"
ENTRY_TIMEFRAME = "5m"
TREND_SECONDS = 900
ENTRY_SECONDS = 300

EMA_ENTRY_FAST, EMA_ENTRY_SLOW = 10, 20
EMA_TREND_FAST, EMA_TREND_SLOW = 50, 200
RSI_PERIOD = 14
RSI_BUY_MIN, RSI_BUY_MAX = 50.0, 68.0
RSI_SELL_MIN, RSI_SELL_MAX = 32.0, 50.0
VOLUME_PERIOD = 20
VOLUME_MULTIPLIER = 1.10
SWING_LOOKBACK = 20
LEVEL_TOLERANCE_PCT = 0.0015
BREAKOUT_LOOKBACK = 6

# Risk settings preserved from the supplied bot.
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
for k, v in DEFAULT_STATE.items():
    if k not in state or not isinstance(state[k], type(v)):
        state[k] = v.copy() if isinstance(v, dict) else v


def log(msg):
    print(msg, flush=True)
    try:
        with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                "message": str(msg)}) + "\n")
    except Exception:
        pass


def save_state():
    try:
        with state_lock:
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
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
    a = request.authorization
    return bool(a and hmac.compare_digest(a.username or "", STATE_VIEW_USER)
                and hmac.compare_digest(a.password or "", STATE_VIEW_PASSWORD))


@app.route("/")
def home():
    return ("DELTA INDIA TESTNET | 15m EMA50/200 TREND + 5m PA/SR/VOLUME "
            "| EMA10/20 + RSI | ATR(15m) STOP | PARTIAL TP 50%")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok", "environment": "TESTNET",
        "automatic_trading": ENABLE_TRADING, "coins": COINS,
        "max_positions": MAX_POSITIONS,
        "trend_timeframe": TREND_TIMEFRAME, "entry_timeframe": ENTRY_TIMEFRAME,
        "entry_filters": ["15m EMA50/200 trend", "5m EMA10/20 pullback",
                          "HH/HL or LH/LL price action", "support/resistance breakout-retest",
                          "RSI", "volume confirmation", "fresh live-price confirmation"],
        "atr_timeframe": TREND_TIMEFRAME, "atr_period": ATR_PERIOD,
        "atr_multiplier": ATR_MULTIPLIER, "partial_tp_points": POINTS,
        "order_sizes": ORDER_SIZES, "partial_tp_percent": PARTIAL_TP_PERCENT * 100,
        "dashboard": "/dashboard (Basic Auth required)"
    }), 200


DASHBOARD_HTML = r"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delta Testnet Bot</title><style>
*{box-sizing:border-box}body{margin:0;background:#0b1018;color:#e8edf5;font:14px Arial}
.wrap{max-width:1200px;margin:auto;padding:16px}.muted,small{color:#9aa8ba}h1{font-size:21px}
.pill{padding:6px 10px;border-radius:20px;background:#123b2b;color:#61e6a2}
.card{background:#151e2b;border:1px solid #263448;border-radius:12px;padding:14px;margin:10px 0}
.grid,.agents{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.big{font-size:25px;font-weight:bold;margin:7px 0}.scroll{overflow:auto}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px 6px;border-bottom:1px solid #293649}
</style></head><body><div class="wrap"><small>DELTA EXCHANGE INDIA · TESTNET</small>
<h1>15m TREND / 5m ENTRY BOT</h1><div class="pill" id="run">CONNECTING</div>
<div class="grid"><div class="card"><small>Open positions</small><div class="big" id="positions">--</div></div>
<div class="card"><small>Active orders</small><div class="big" id="orders">--</div></div>
<div class="card"><small>New entries</small><div class="big" id="entries">--</div></div></div>
<div class="card"><b>AGENT STATUS</b><div class="agents" id="agents"></div></div>
<div class="card"><b>MARKET SCAN</b><div class="scroll"><table><thead><tr>
<th>Symbol</th><th>Trend</th><th>Signal</th><th>Last scan</th></tr></thead><tbody id="markets"></tbody></table></div></div>
<div class="card"><b>OPEN POSITIONS</b><div class="scroll"><table><thead><tr>
<th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark / P&amp;L</th></tr></thead><tbody id="posrows"></tbody></table></div></div>
<small>Read-only dashboard. New entries are OFF by default; protective management remains active.</small>
</div><script>
async function refresh(){try{const r=await fetch('/dashboard-data',{cache:'no-store'});if(!r.ok)throw Error();
const d=await r.json();document.getElementById('run').textContent='● '+d.status;
document.getElementById('positions').textContent=d.positions.length;
document.getElementById('orders').textContent=d.active_orders;
document.getElementById('entries').textContent=d.automatic_trading?'ON':'OFF';
const names=['Scanner','Risk Check','Position Sizer','Executor','Logger','Equity'];
const keys=['scanner','risk','sizer','executor','logger','equity'];
document.getElementById('agents').innerHTML=names.map((n,i)=>{const a=d.agents[keys[i]]||{status:'WAIT',detail:'Waiting'};
return `<div class="card"><b>${n}</b><div>${a.status}</div><small>${a.detail||''}</small></div>`}).join('');
document.getElementById('markets').innerHTML=d.coins.map(s=>{const x=d.signals[s]||{};
return `<tr><td>${s}</td><td>${x.trend||'--'}</td><td>${x.signal||'--'}</td><td>${x.updated||'--'}</td></tr>`}).join('');
document.getElementById('posrows').innerHTML=d.positions.map(p=>
`<tr><td>${p.symbol}</td><td>${p.side}</td><td>${p.size}</td><td>${p.entry}</td><td>${p.mark} / ${p.pnl}</td></tr>`
).join('')||'<tr><td colspan="5">No open positions</td></tr>';
}catch(e){document.getElementById('run').textContent='CONNECTION ERROR'}}
refresh();setInterval(refresh,10000);
</script></body></html>"""


@app.route("/dashboard")
def dashboard():
    if not require_dashboard_auth():
        r = Response("Dashboard requires STATE_VIEW_USER and STATE_VIEW_PASSWORD.", 401)
        r.headers["WWW-Authenticate"] = 'Basic realm="Delta Testnet Dashboard"'
        return r
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
            sz = float(p.get("size", 0) or 0)
            if sz:
                rows.append({"symbol": parse_pos_symbol(p),
                    "side": "LONG" if sz > 0 else "SHORT", "size": abs(sz),
                    "entry": p.get("entry_price", p.get("average_price", "--")),
                    "mark": p.get("mark_price", p.get("last_price", "--")),
                    "pnl": p.get("unrealized_pnl", "--")})
        except Exception:
            pass
    with state_lock:
        snap = json.loads(json.dumps(state))
    return jsonify({"status": "TESTNET LIVE", "automatic_trading": ENABLE_TRADING,
        "coins": COINS, "positions": rows, "active_orders": len(orders),
        "agents": snap["agent_status"], "signals": snap["last_signals"]})


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), use_reloader=False)


# =====================================================
# API AUTH / DATA
# =====================================================
def api_req(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        return {"error": "API credentials missing"}, 401
    method = method.upper()
    query = "?" + urlencode(params, quote_via=quote) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
    ts = str(int(time.time()))
    msg = method + ts + path + query + payload
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    headers = {"api-key": API_KEY, "timestamp": ts, "signature": sig,
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "delta-testnet-pa-sr-volume-bot"}
    try:
        r = session.request(method, BASE_URL + path + query, headers=headers,
                            data=payload or None, timeout=(5, 15))
        try:
            return r.json(), r.status_code
        except Exception:
            return {"error": r.text[:500]}, r.status_code
    except Exception as e:
        return {"error": str(e)}, 500


def parse_timestamp(raw):
    if raw in (None, ""):
        return 0.0
    try:
        v = float(raw)
        if v > 1e14: v /= 1_000_000
        elif v > 1e11: v /= 1_000
        return v
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0


def fresh_price(raw_price, raw_ts, symbol, source):
    try: price = float(raw_price or 0)
    except (TypeError, ValueError): return None
    ts = parse_timestamp(raw_ts)
    age = time.time() - ts if ts else 999999
    if price <= 0 or ts <= 0 or age < -10 or age > MAX_TRADE_AGE_SECONDS:
        log(f"[STALE PRICE] {symbol} SOURCE={source} AGE={age:.1f}s")
        return None
    return price


def get_live_price(symbol):
    try:
        r = session.get(BASE_URL + f"/v2/trades/{symbol}", timeout=(5, 10))
        if r.status_code == 200:
            d = r.json()
            if d.get("success") is True:
                res = d.get("result", {})
                trades = res.get("trades", []) if isinstance(res, dict) else res if isinstance(res, list) else []
                if trades:
                    t = max(trades, key=lambda x: parse_timestamp(
                        x.get("timestamp", x.get("t", x.get("created_at")))))
                    p = fresh_price(t.get("price", t.get("p")),
                        t.get("timestamp", t.get("t", t.get("created_at"))), symbol, "recent_trade")
                    if p is not None: return p
    except Exception as e:
        log(f"[TRADE PRICE ERROR] {symbol}: {e}")
    try:
        r = session.get(BASE_URL + f"/v2/tickers/{symbol}", timeout=(5, 10))
        if r.status_code != 200: return None
        d = r.json()
        if d.get("success") is not True: return None
        res = d.get("result", {})
        if isinstance(res, list):
            res = next((x for x in res if x.get("symbol") == symbol), res[0] if res else {})
        if not isinstance(res, dict): return None
        return fresh_price(res.get("close"), res.get("timestamp"), symbol, "ticker_close")
    except Exception as e:
        log(f"[TICKER ERROR] {symbol}: {e}")
        return None


def get_candles(symbol, timeframe):
    end = int(time.time())
    params = {"resolution": timeframe, "symbol": symbol,
              "start": str(end - CANDLE_LOOKBACK_SECONDS), "end": str(end)}
    result, code = api_req("GET", "/v2/history/candles", params=params)
    if code == 200 and isinstance(result, dict) and result.get("success") is True:
        return result.get("result", [])
    log(f"[CANDLE API ERROR] {symbol} TF={timeframe} HTTP={code} {result}")
    return []


def candles_to_df(candles, symbol="?", timeframe=ENTRY_TIMEFRAME):
    try:
        if not candles: return None
        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles).copy()
        else:
            df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
        required = ["time", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in required):
            log(f"[CANDLE INVALID] {symbol} {timeframe}: missing columns")
            return None
        for c in ["high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["time"] = df["time"].apply(parse_timestamp)
        df = df.dropna(subset=["time"])
        df = df[df["time"] > 0].copy()
        if df.empty: return None
        df["time"] = df["time"].astype("int64")
        df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        seconds = TREND_SECONDS if timeframe == TREND_TIMEFRAME else ENTRY_SECONDS
        now = int(time.time())
        closed = df[df["time"] + seconds <= now].dropna(
            subset=["high", "low", "close", "volume"]).reset_index(drop=True)
        log(f"[CANDLE DIAGNOSTIC] {symbol} TF={timeframe} raw={len(df)} closed={len(closed)}")
        if len(closed) < MIN_CLOSED_CANDLES:
            log(f"[CANDLE INSUFFICIENT] {symbol} {timeframe}: need {MIN_CLOSED_CANDLES}, got {len(closed)}")
            return None
        return closed
    except Exception as e:
        log(f"[CANDLE PARSE ERROR] {symbol} {timeframe}: {e}")
        return None


def calculate_rsi(close, period=RSI_PERIOD):
    delta = close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = ag / al.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((al == 0) & (ag > 0), 100)
    rsi = rsi.mask((ag == 0) & (al > 0), 0)
    return rsi


def add_indicators(df):
    df = df.copy()
    c = df["close"]
    df["ema10"] = c.ewm(span=EMA_ENTRY_FAST, adjust=False).mean()
    df["ema20"] = c.ewm(span=EMA_ENTRY_SLOW, adjust=False).mean()
    df["ema50"] = c.ewm(span=EMA_TREND_FAST, adjust=False).mean()
    df["ema200"] = c.ewm(span=EMA_TREND_SLOW, adjust=False).mean()
    df["rsi"] = calculate_rsi(c)
    df["vol_avg"] = df["volume"].rolling(VOLUME_PERIOD).mean()
    return df


def get_atr(symbol):
    # Risk management deliberately remains on 15-minute candles.
    raw = get_candles(symbol, TREND_TIMEFRAME)
    df = candles_to_df(raw, symbol, TREND_TIMEFRAME)
    if df is None or len(df) < ATR_PERIOD + 2:
        log(f"[ATR UNAVAILABLE] {symbol}: insufficient 15m candles")
        return None
    try:
        h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
        pc = c.shift(1)
        tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
        val = float(tr.ewm(alpha=1/ATR_PERIOD, adjust=False,
                           min_periods=ATR_PERIOD).mean().iloc[-1])
        return val if pd.notna(val) and val > 0 else None
    except Exception as e:
        log(f"[ATR CALC ERROR] {symbol}: {e}")
        return None


# =====================================================
# MULTI-TIMEFRAME ENTRY: 15m TREND + 5m PRICE ACTION/SR/VOLUME
# =====================================================
def check_signal(trend_candles, entry_candles, symbol):
    agent("scanner", "SCAN", f"{symbol}: 15m trend + 5m setup")
    tdf = candles_to_df(trend_candles, symbol, TREND_TIMEFRAME)
    edf = candles_to_df(entry_candles, symbol, ENTRY_TIMEFRAME)
    if tdf is None or edf is None:
        agent("scanner", "BLOCKED", f"{symbol}: insufficient candles")
        return None, None, None, None
    tdf = add_indicators(tdf)
    edf = add_indicators(edf)
    t, e = tdf.iloc[-1], edf.iloc[-1]
    ep = edf.iloc[-2]
    candle_id = int(e["time"])
    candle_age = time.time() - (candle_id + ENTRY_SECONDS)
    if candle_age < -5 or candle_age > MAX_CLOSED_CANDLE_AGE_SECONDS:
        log(f"[ENTRY BLOCKED] {symbol}: stale 5m candle age={candle_age:.1f}s")
        trend, signal = "STALE 5M", None
    else:
        if t["ema50"] > t["ema200"] and t["close"] > t["ema50"] and t["close"] > t["ema200"]:
            trend = "BULLISH"
        elif t["ema50"] < t["ema200"] and t["close"] < t["ema50"] and t["close"] < t["ema200"]:
            trend = "BEARISH"
        else:
            trend = "MIXED"

        # Price action: latest two confirmed swing points should progress with trend.
        recent = edf.tail(SWING_LOOKBACK + 2)
        older = recent.iloc[:-2]
        newer = recent.iloc[-2:]
        hh_hl = (float(newer["high"].max()) > float(older["high"].max())
                 and float(newer["low"].min()) > float(older["low"].min()))
        lh_ll = (float(newer["high"].max()) < float(older["high"].max())
                 and float(newer["low"].min()) < float(older["low"].min()))

        # Nearby resistance/support from the preceding 20 closed candles (exclude latest).
        sr = edf.iloc[-(SWING_LOOKBACK + 1):-1]
        resistance = float(sr["high"].max())
        support = float(sr["low"].min())
        close = float(e["close"])
        tolerance = max(close * LEVEL_TOLERANCE_PCT, 0.00000001)
        near_support = abs(close - support) <= tolerance
        near_resistance = abs(close - resistance) <= tolerance

        # Breakout then retest: previous candles crossed the level; latest candle
        # returned close to it and closed back on the trend side.
        prior = edf.iloc[-(BREAKOUT_LOOKBACK + 2):-1]
        prior_res = float(prior["high"].max())
        prior_sup = float(prior["low"].min())
        breakout_up = (float(ep["close"]) > prior_res and
                       float(e["low"]) <= prior_res + tolerance and close > prior_res)
        breakout_down = (float(ep["close"]) < prior_sup and
                         float(e["high"]) >= prior_sup - tolerance and close < prior_sup)
        # Allow either a clear breakout-retest or a pullback holding the EMA zone.
        buy_structure = hh_hl and (breakout_up or near_support or
                          (float(e["low"]) <= float(e["ema20"]) + tolerance and close > float(e["ema10"])))
        sell_structure = lh_ll and (breakout_down or near_resistance or
                           (float(e["high"]) >= float(e["ema20"]) - tolerance and close < float(e["ema10"])))

        buy_ema = (float(ep["ema10"]) >= float(ep["ema20"]) and
                   float(e["ema10"]) > float(e["ema20"]) and close > float(e["ema10"]))
        sell_ema = (float(ep["ema10"]) <= float(ep["ema20"]) and
                    float(e["ema10"]) < float(e["ema20"]) and close < float(e["ema10"]))
        rsi = float(e["rsi"])
        buy_rsi = RSI_BUY_MIN <= rsi <= RSI_BUY_MAX
        sell_rsi = RSI_SELL_MIN <= rsi <= RSI_SELL_MAX
        vol_avg = float(e["vol_avg"]) if pd.notna(e["vol_avg"]) else 0
        volume_ok = vol_avg > 0 and float(e["volume"]) >= vol_avg * VOLUME_MULTIPLIER

        signal = None
        if trend == "BULLISH" and buy_ema and buy_rsi and buy_structure and volume_ok:
            signal = "buy"
        elif trend == "BEARISH" and sell_ema and sell_rsi and sell_structure and volume_ok:
            signal = "sell"

        log(f"[5M FILTER] {symbol} TREND15={trend} EMA_BUY={buy_ema} EMA_SELL={sell_ema} "
            f"HH_HL={hh_hl} LH_LL={lh_ll} SUPPORT={support:.6f} RESISTANCE={resistance:.6f} "
            f"NEAR_SUPPORT={near_support} NEAR_RESISTANCE={near_resistance} "
            f"BRK_RETEST_UP={breakout_up} BRK_RETEST_DN={breakout_down} "
            f"RSI={rsi:.2f} VOL={float(e['volume']):.4f}/{vol_avg:.4f} "
            f"VOL_OK={volume_ok} SIGNAL={signal}")
    detail = (f"{symbol} 15m={trend} 5m RSI={float(e['rsi']):.2f} "
              f"vol={float(e['volume']):.3f}")
    agent("scanner", "OK", detail)
    agent("risk", "OK" if signal else "BLOCKED",
          "15m/5m/PA/SR/volume aligned" if signal else f"{symbol}: filters not aligned")
    with state_lock:
        state["last_signals"][symbol] = {
            "trend": trend, "signal": (signal or "NO TRADE").upper(),
            "updated": datetime.now().strftime("%H:%M:%S")}
    log(f"[15M TREND / 5M ENTRY] {symbol} TREND={trend} SIGNAL={signal or 'NONE'} "
        f"CLOSE={float(e['close']):.6f} RSI={float(e['rsi']):.2f}")
    ind = {"ema10": float(e["ema10"]), "ema20": float(e["ema20"]),
           "close": float(e["close"]), "candle_id": candle_id,
           "candle_age": candle_age}
    return signal, candle_id, ind["close"], ind


def live_entry_confirmation(symbol, signal, candle_close, ema10):
    live = get_live_price(symbol)
    if live is None or candle_close <= 0 or ema10 <= 0:
        log(f"[LIVE FILTER BLOCK] {symbol}: fresh price unavailable")
        return False
    gap = abs(live - candle_close) / candle_close
    log(f"[LIVE ENTRY CHECK] {symbol} {signal.upper()} LIVE={live:.6f} "
        f"CLOSE={candle_close:.6f} EMA10={ema10:.6f} GAP={gap*100:.4f}%")
    if gap > MAX_CANDLE_LIVE_GAP_PCT:
        log(f"[LIVE FILTER BLOCK] {symbol}: gap exceeds {MAX_CANDLE_LIVE_GAP_PCT*100:.3f}%")
        return False
    if signal == "buy" and not (live > candle_close and live > ema10):
        log(f"[LIVE FILTER BLOCK] {symbol}: BUY live momentum not confirmed")
        return False
    if signal == "sell" and not (live < candle_close and live < ema10):
        log(f"[LIVE FILTER BLOCK] {symbol}: SELL live momentum not confirmed")
        return False
    log(f"[LIVE FILTER PASS] {symbol} {signal.upper()}")
    return True


# =====================================================
# POSITIONS / ORDERS / PROTECTIVE STOPS
# =====================================================
def parse_pos_symbol(p):
    product = p.get("product") or {}
    return p.get("product_symbol") or product.get("symbol") or p.get("symbol") or ""


def get_pos():
    result, code = api_req("GET", "/v2/positions/margined")
    if code != 200 or not isinstance(result, dict) or result.get("success") is not True:
        log(f"[POSITION ERROR] HTTP={code} {result}")
        return None
    positions = result.get("result", [])
    if isinstance(positions, dict): positions = [positions]
    if not isinstance(positions, list): return None
    active = []
    for p in positions:
        try:
            if float(p.get("size", 0) or 0) != 0: active.append(p)
        except (TypeError, ValueError): pass
    return active


def get_orders():
    result, code = api_req("GET", "/v2/orders",
        params={"states": "open,pending", "page_size": 100})
    if code == 200 and isinstance(result, dict) and result.get("success") is True:
        orders = result.get("result", [])
        return orders if isinstance(orders, list) else []
    log(f"[ORDER API ERROR] HTTP={code} {result}")
    return None


def is_active_stop(o, symbol):
    return (o.get("product_symbol") == symbol and
        o.get("stop_order_type") == "stop_loss_order" and
        o.get("state") in ("open", "pending") and
        o.get("reduce_only") in (True, 1, "true", "True"))


def cancel_order(o):
    oid, pid = o.get("id"), o.get("product_id")
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
    if live is None or live <= 0 or stop is None or stop <= 0: return False
    gap = live * MIN_STOP_GAP_PCT
    return stop < live-gap if side == "buy" else stop > live+gap


def fmt_price(symbol, price):
    return f"{price:.{POINTS[symbol]['decimals']}f}"


def verify_stop(symbol, oid):
    orders = get_orders()
    return orders is not None and any(
        str(o.get("id")) == str(oid) and is_active_stop(o, symbol) for o in orders)


def place_stop(symbol, side, size, stop_price):
    qty = int(abs(float(size)))
    if qty <= 0: return False
    live = get_live_price(symbol)
    if not valid_stop_price(side, stop_price, live):
        log(f"[STOP BLOCKED] {symbol} SIDE={side} STOP={stop_price} LIVE={live}")
        return False
    body = {"product_symbol": symbol, "size": qty,
        "side": "sell" if side == "buy" else "buy",
        "order_type": "market_order", "stop_order_type": "stop_loss_order",
        "stop_price": fmt_price(symbol, stop_price), "reduce_only": True,
        "stop_trigger_method": STOP_TRIGGER_METHOD}
    result, code = api_req("POST", "/v2/orders", body=body)
    order_result = result.get("result", {}) if isinstance(result, dict) else {}
    if code != 200 or not isinstance(result, dict) or result.get("success") is not True or not order_result.get("id"):
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
    if orders is None: return True
    for old in orders:
        if is_active_stop(old, symbol) and str(old.get("id")) != str(oid):
            cancel_order(old)
    return True


def recover_stop(symbol, side, orders):
    stops = []
    for o in orders or []:
        if is_active_stop(o, symbol):
            try: stops.append((float(o.get("stop_price")), o))
            except (TypeError, ValueError): pass
    if not stops: return float(state["stops"].get(symbol, 0) or 0), False
    price, o = max(stops, key=lambda x: x[0]) if side == "buy" else min(stops, key=lambda x: x[0])
    state["stops"][symbol] = price
    state["stop_order_ids"][symbol] = str(o.get("id", ""))
    return price, True


def verify_partial_reduction(symbol, expected):
    for i in range(12):
        time.sleep(1)
        positions = get_pos()
        if positions is None: continue
        p = next((x for x in positions if parse_pos_symbol(x) == symbol), None)
        actual = abs(float(p.get("size", 0) or 0)) if p else 0
        if abs(actual - expected) < 0.000001:
            log(f"[PARTIAL REDUCTION VERIFIED] {symbol} REMAINING={actual}")
            return True
        log(f"[PARTIAL VERIFY WAIT] {symbol} EXPECTED={expected} ACTUAL={actual} {i+1}/12")
    return False


def execute_partial_tp(symbol, position, current, side, entry, target):
    if state["partial_tp_done"].get(symbol): return
    if state["partial_tp_pending"].get(symbol):
        log(f"[PARTIAL TP LOCKED] {symbol}: unresolved previous order")
        return
    size = abs(float(position.get("size", 0) or 0))
    if size < 2: return
    favourable = current-entry if side == "buy" else entry-current
    if favourable < target: return
    close_qty = int(size * PARTIAL_TP_PERCENT)
    remaining = size-close_qty
    if close_qty <= 0 or remaining <= 0: return
    state["partial_tp_pending"][symbol] = {"time": time.time(),
        "original_size": size, "expected_remaining": remaining,
        "close_quantity": close_qty, "side": side}
    save_state()
    log(f"[PARTIAL TP TRIGGER] {symbol} MOVE={favourable:.6f} TARGET={target:.6f} "
        f"CLOSE={close_qty} KEEP={remaining}")
    result, code = api_req("POST", "/v2/orders", body={
        "product_symbol": symbol, "size": close_qty,
        "side": "sell" if side == "buy" else "buy",
        "order_type": "market_order", "reduce_only": True})
    order_result = result.get("result", {}) if isinstance(result, dict) else {}
    if code != 200 or not isinstance(result, dict) or result.get("success") is not True or not order_result.get("id"):
        log(f"[PARTIAL TP AMBIGUOUS] {symbol} HTTP={code} {result}; no retry")
        return
    if not verify_partial_reduction(symbol, remaining):
        log(f"[PARTIAL TP NOT CONFIRMED] {symbol}; lock retained")
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
            log(f"[URGENT] {symbol}: partial TP done; breakeven stop not verified")
    else:
        log(f"[BREAKEVEN WAIT] {symbol}: entry stop invalid/too close; old stop retained")


def handle_trailing():
    positions, orders = get_pos(), get_orders()
    if positions is None or orders is None:
        agent("executor", "BLOCKED", "Position or order API unavailable")
        log("[TRAIL BLOCKED] Cannot verify positions/orders")
        return
    active = set()
    for p in positions:
        try:
            signed = float(p.get("size", 0) or 0)
            symbol = parse_pos_symbol(p)
            if not signed or symbol not in COINS: continue
            active.add(symbol)
            side = "buy" if signed > 0 else "sell"
            entry = float(p.get("entry_price") or p.get("average_price") or 0)
            if entry <= 0: continue
            current = get_live_price(symbol)
            if current is None:
                log(f"[TRAIL SKIP] {symbol}: fresh price unavailable")
                continue
            atr = get_atr(symbol)
            if atr is None:
                log(f"[ATR STOP HOLD] {symbol}: existing stop retained")
                continue
            gap = atr * ATR_MULTIPLIER
            target = POINTS[symbol]["tp"]
            old_stop, has_stop = recover_stop(symbol, side, orders)
            if not has_stop:
                initial = entry-gap if side == "buy" else entry+gap
                if not place_stop(symbol, side, signed, initial):
                    log(f"[URGENT] {symbol}: initial ATR stop not verified")
                    continue
                old_stop = initial
                log(f"[INITIAL ATR STOP] {symbol} ATR={atr:.8f} GAP={gap:.8f} STOP={initial:.8f}")
            peak = float(state["peaks"].get(symbol, entry) or entry)
            peak = max(peak, current, entry) if side == "buy" else min(peak, current, entry)
            state["peaks"][symbol] = peak
            execute_partial_tp(symbol, p, current, side, entry, target)
            refreshed = get_pos()
            if refreshed is None: continue
            p2 = next((x for x in refreshed if parse_pos_symbol(x) == symbol), None)
            if p2 is None: continue
            signed2 = float(p2.get("size", 0) or 0)
            if not signed2: continue
            side = "buy" if signed2 > 0 else "sell"
            if not state["partial_tp_done"].get(symbol):
                fav = peak-entry if side == "buy" else entry-peak
                log(f"[WAIT PARTIAL TARGET] {symbol} MOVE={fav:.6f} REQUIRED={target:.6f}")
                save_state()
                continue
            candidate = peak-gap if side == "buy" else peak+gap
            candidate = max(candidate, entry) if side == "buy" else min(candidate, entry)
            old_stop = float(state["stops"].get(symbol, old_stop) or old_stop)
            candidate = max(candidate, old_stop) if side == "buy" else min(candidate, old_stop)
            if not valid_stop_price(side, candidate, current):
                log(f"[TRAIL WAIT] {symbol} CANDIDATE={candidate:.6f} LIVE={current:.6f}")
                continue
            improved = candidate > old_stop if side == "buy" else candidate < old_stop
            if improved:
                log(f"[ATR TRAIL] {symbol} SIDE={side.upper()} ATR={atr:.6f} "
                    f"PEAK={peak:.6f} OLD={old_stop:.6f} NEW={candidate:.6f}")
                place_stop(symbol, side, signed2, candidate)
            save_state()
        except Exception as e:
            log(f"[TRAIL ERROR] {e}")
    save_state()
    agent("executor", "LIVE", f"Managing {len(active)} open position(s)")


def initialize_new_position(symbol):
    for _ in range(8):
        time.sleep(2)
        positions = get_pos()
        if positions is None: continue
        p = next((x for x in positions if parse_pos_symbol(x) == symbol), None)
        if p is None: continue
        size = float(p.get("size", 0) or 0)
        entry = float(p.get("entry_price") or p.get("average_price") or 0)
        if not size or entry <= 0: continue
        side = "buy" if size > 0 else "sell"
        current = get_live_price(symbol)
        if current is None:
            log(f"[INITIAL STOP FAILED] {symbol}: live price unavailable")
            return False
        atr = get_atr(symbol)
        if atr is None:
            log(f"[INITIAL STOP BLOCKED] {symbol}: ATR unavailable; no fixed fallback")
            return False
        gap = atr * ATR_MULTIPLIER
        stop = entry-gap if side == "buy" else entry+gap
        state["peaks"][symbol] = max(entry, current) if side == "buy" else min(entry, current)
        state["partial_tp_done"].pop(symbol, None)
        state["partial_tp_pending"].pop(symbol, None)
        state["entry_prices"][symbol] = entry
        state["original_sizes"][symbol] = abs(size)
        save_state()
        log(f"[INITIAL ATR STOP] {symbol} ENTRY={entry:.8f} ATR={atr:.8f} "
            f"MULT={ATR_MULTIPLIER} GAP={gap:.8f} STOP={stop:.8f} "
            f"PARTIAL_TP={POINTS[symbol]['tp']}")
        return place_stop(symbol, side, size, stop)
    log(f"[INITIAL STOP FAILED] {symbol}: position not confirmed")
    return False


def place_entry(symbol, side):
    qty = int(ORDER_SIZES.get(symbol, ORDER_SIZE))
    if qty <= 0:
        agent("sizer", "BLOCKED", f"{symbol}: invalid quantity")
        return False
    agent("sizer", "OK", f"{symbol}: configured size {qty}")
    agent("executor", "SCAN", f"Submitting {side.upper()} {symbol} size {qty}")
    result, code = api_req("POST", "/v2/orders", body={
        "product_symbol": symbol, "size": qty, "side": side,
        "order_type": "market_order"})
    order_result = result.get("result", {}) if isinstance(result, dict) else {}
    if code == 200 and isinstance(result, dict) and result.get("success") is True:
        log(f"[ENTRY ACCEPTED] {symbol} {side.upper()} SIZE={qty} RESULT={order_result}")
        agent("executor", "LIVE", f"Entry accepted: {side.upper()} {symbol} {qty}")
        return True
    log(f"[ENTRY FAILED] {symbol} HTTP={code} {result}")
    agent("executor", "ERROR", f"Entry failed: {symbol} HTTP {code}")
    return False


# =====================================================
# MAIN LOOP - NO REVERSAL CLOSES IN SAFE MODE
# =====================================================
def main_loop():
    log("==================================================")
    log("DELTA INDIA TESTNET | 15m TREND + 5m PA/SR/VOLUME BOT")
    log(f"NEW ENTRIES={ENABLE_TRADING} | COINS={COINS}")
    log(f"ORDER SIZES={ORDER_SIZES} | MAX POSITIONS={MAX_POSITIONS}")
    log("TREND=15m EMA50/200 | ENTRY=5m EMA10/20 + RSI + HH/HL or LH/LL")
    log(f"VOLUME FILTER={VOLUME_MULTIPLIER}x {VOLUME_PERIOD}-candle average")
    log("SUPPORT/RESISTANCE + BREAKOUT/RETEST + LIVE PRICE CONFIRMATION")
    log(f"ATR STOP/TRAIL=15m ATR({ATR_PERIOD}) x {ATR_MULTIPLIER}")
    log(f"PARTIAL TP={POINTS} | CLOSE 50% THEN BREAKEVEN + ATR TRAIL")
    log("TESTNET ONLY")
    log("==================================================")
    agent("logger", "OK", "Trade decisions in trade_log.jsonl")
    agent("equity", "WAIT", "Waiting for exchange account data")

    while True:
        try:
            # Always manage protective stops and partial TP, even in safe mode.
            handle_trailing()
            positions = get_pos()
            if positions is None:
                log("[SCAN BLOCKED] Position API unavailable")
                time.sleep(CHECK_INTERVAL)
                continue
            held = {parse_pos_symbol(p): p for p in positions
                    if parse_pos_symbol(p) in COINS and float(p.get("size", 0) or 0) != 0}
            agent("equity", "OK", f"Exchange positions: {len(held)}")
            log(f"[POS HELD] {list(held.keys())} TOTAL={len(held)}")

            for symbol in COINS:
                trend_raw = get_candles(symbol, TREND_TIMEFRAME)
                entry_raw = get_candles(symbol, ENTRY_TIMEFRAME)
                if not trend_raw or not entry_raw: continue
                signal, candle_id, candle_close, ind = check_signal(trend_raw, entry_raw, symbol)
                if candle_id is None or ind is None: continue
                previous = int(state["last_candle"].get(symbol, 0) or 0)
                if candle_id <= previous: continue
                state["last_candle"][symbol] = candle_id
                save_state()

                # Do not close or reverse existing positions based on an entry signal.
                if symbol in held:
                    if signal:
                        log(f"[POSITION HELD] {symbol}: existing position managed only by SL/TP/trailing")
                    continue
                if signal is None: continue

                if not live_entry_confirmation(symbol, signal, candle_close, ind["ema10"]):
                    continue
                if not ENABLE_TRADING:
                    log(f"[SAFE MODE] {symbol}: valid {signal.upper()} signal; new entries OFF")
                    continue
                if state["partial_tp_pending"].get(symbol):
                    log(f"[ENTRY BLOCKED] {symbol}: unresolved partial TP")
                    continue
                orders = get_orders()
                if orders is None:
                    log(f"[ENTRY BLOCKED] {symbol}: cannot read open orders")
                    continue
                orphan = [o for o in orders if is_active_stop(o, symbol)]
                clean = True
                for old in orphan:
                    if not cancel_order(old):
                        clean = False
                        break
                if not clean:
                    log(f"[ENTRY BLOCKED] {symbol}: orphan stop remains")
                    continue
                if len(held) >= MAX_POSITIONS:
                    log(f"[MAX POSITIONS] {symbol}: skipped")
                    continue
                last = float(state["last_trade"].get(symbol, 0) or 0)
                remain = COOLDOWN_SECONDS - (time.time()-last)
                if remain > 0:
                    log(f"[COOLDOWN] {symbol}: {int(remain)}s remaining")
                    continue
                if not live_entry_confirmation(symbol, signal, candle_close, ind["ema10"]):
                    log(f"[ENTRY BLOCKED] {symbol}: final live confirmation failed")
                    continue
                log(f"[SMART ENTRY SIGNAL] {symbol} {signal.upper()} 5m_candle={candle_id}")
                state["last_trade"][symbol] = time.time()
                save_state()
                if not place_entry(symbol, signal):
                    log(f"[ENTRY NOT CONFIRMED] {symbol}; inspect exchange before retry")
                    continue
                stop_ok = initialize_new_position(symbol)
                refreshed = get_pos()
                if refreshed is None:
                    log(f"[ENTRY VERIFY ERROR] {symbol}: check position and protective stop")
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
