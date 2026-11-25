import os
import json
import time
import threading
import logging

import requests
import pandas as pd
from flask import Flask, jsonify

# ============================================================
# DELTA EXCHANGE INDIA TESTNET - SIGNAL TRACKING SCANNER
# ORDER PLACEMENT DISABLED
# ============================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

# SAFETY: scanner only, no trading or order placement
ENABLE_TRADING = True 

TREND_TF = "15m"
ENTRY_TF = "5m"

SCAN_INTERVAL = 30
DISCOVERY_INTERVAL = 900
MAX_DISCOVERY_PAGES = 20

EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200
EMA_ENTRY_FAST = 10
EMA_ENTRY_SLOW = 20

RSI_PERIOD = 14
VOLUME_PERIOD = 20
VOL_MULT = 1.10
ATR_PERIOD = 14

# Signal history settings
MAX_SIGNAL_HISTORY = 200
MAX_EVENTS = 100

STATE_FILE = "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.info
app = Flask(__name__)

session = requests.Session()
session.headers.update({
    "User-Agent": "delta-india-testnet-scanner/2.0",
    "Accept": "application/json"
})

# ============================================================
# STATE
# ============================================================

def fresh_state():
    return {
        "symbols": [],
        "products": {},
        "events": [],
        "signals": {},
        "signal_history": [],
        "active_signals": {},
        "last_scan": None,
        "discovery_error": None,
        "discovery_count": 0
}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        base = fresh_state()

        if isinstance(loaded, dict):
            base.update(loaded)

        # Repair older state files
        if not isinstance(base.get("signal_history"), list):
            base["signal_history"] = []

        if not isinstance(base.get("active_signals"), dict):
            base["active_signals"] = {}

        if not isinstance(base.get("signals"), dict):
            base["signals"] = {}

        if not isinstance(base.get("events"), list):
            base["events"] = []

        return base

    except Exception:
        return fresh_state()


state = load_state()
state_lock = threading.RLock()


def save_state():
    with state_lock:
        try:
            with open(
                STATE_FILE + ".tmp",
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    state,
                    f,
                    indent=2,
                    ensure_ascii=False,
                    default=str
                )

            os.replace(STATE_FILE + ".tmp", STATE_FILE)

        except Exception as exc:
            log("[STATE SAVE ERROR] %s", exc)


def event(message):
    with state_lock:
        state["events"].append({
            "time": int(time.time()),
            "message": str(message)
        })

        state["events"] = state["events"][-MAX_EVENTS:]

    log("%s", message)
    save_state()


# ============================================================
# SIGNAL HISTORY / CONSECUTIVE TRACKING
# ============================================================

def add_signal_history(item):
    """
    Store signal lifecycle events.
    START, CHANGE, END and occasional continuation checkpoints.
    """

    with state_lock:
        state["signal_history"].append(item)

        state["signal_history"] = (
            state["signal_history"][-MAX_SIGNAL_HISTORY:]
        )


def track_signal(record):
    """
    Tracks each symbol's current signal streak.

    BUY/SELL:
      - Starts a new streak
      - Continues existing streak
      - Counts consecutive scans
      - Tracks first_seen and last_seen

    NONE:
      - Ends an active streak
      - Saves an END event
    """

    symbol = record["symbol"]
    signal = record["signal"]
    now = int(time.time())

    with state_lock:

        active = state["active_signals"].get(symbol)

        # ----------------------------------------------------
        # SIGNAL NONE - END ACTIVE STREAK
        # ----------------------------------------------------
        if signal == "NONE":

            if active:

                duration = max(
                    0,
                    now - int(active.get("first_seen", now))
                )

                ended = {
                    "event": "END",
                    "symbol": symbol,
                    "side": active.get("side"),
                    "first_seen": active.get("first_seen"),
                    "last_seen": active.get("last_seen"),
                    "ended_at": now,
                    "consecutive_scans": active.get(
                        "consecutive_scans", 0
                    ),
                    "duration_seconds": duration,
                    "last_price": record.get("price"),
                    "reason": "Signal no longer valid"
                }

                add_signal_history(ended)

                log(
                    "[SIGNAL STREAK END] %s side=%s "
                    "scans=%d duration=%ds",
                    symbol,
                    active.get("side"),
                    active.get("consecutive_scans", 0),
                    duration
                )

                state["active_signals"].pop(symbol, None)

            return

        # ----------------------------------------------------
        # SAME SIGNAL - CONTINUE STREAK
        # ----------------------------------------------------
        if active and active.get("side") == signal:

            active["consecutive_scans"] = (
                int(active.get("consecutive_scans", 0)) + 1
            )

            active["last_seen"] = now
            active["last_price"] = record.get("price")
            active["last_rsi"] = record.get("rsi")
            active["last_atr"] = record.get("atr")
            active["last_volume"] = record.get("volume")
            active["last_volume_avg"] = record.get("volume_avg")
            active["last_trend"] = record.get("trend")

            duration = max(
                0,
                now - int(active.get("first_seen", now))
            )

            active["duration_seconds"] = duration

            # Save periodic checkpoints, not every scan
            scans = active["consecutive_scans"]

            if scans % 10 == 0:

                checkpoint = {
                    "event": "CHECKPOINT",
                    "symbol": symbol,
                    "side": signal,
                    "first_seen": active.get("first_seen"),
                    "last_seen": now,
                    "consecutive_scans": scans,
                    "duration_seconds": duration,
                    "price": record.get("price"),
                    "rsi": record.get("rsi"),
                    "trend": record.get("trend")
                }

                add_signal_history(checkpoint)

                log(
                    "[SIGNAL CHECKPOINT] %s %s "
                    "scans=%d duration=%ds",
                    symbol,
                    signal,
                    scans,
                    duration
                )

            else:

                log(
                    "[SIGNAL HOLD] %s %s "
                    "consecutive_scans=%d duration=%ds",
                    symbol,
                    signal,
                    scans,
                    duration
                )

            return

        # ----------------------------------------------------
        # SIGNAL CHANGED OR NEW SIGNAL START
        # ----------------------------------------------------

        previous_side = None

        if active:

            previous_side = active.get("side")

            ended = {
                "event": "CHANGE",
                "symbol": symbol,
                "side": previous_side,
                "new_side": signal,
                "first_seen": active.get("first_seen"),
                "last_seen": active.get("last_seen"),
                "ended_at": now,
                "consecutive_scans": active.get(
                    "consecutive_scans", 0
                ),
                "duration_seconds": max(
                    0,
                    now - int(active.get("first_seen", now))
                ),
                "reason": "Signal changed"
            }

            add_signal_history(ended)

        # New streak
        new_active = {
            "symbol": symbol,
            "side": signal,
            "first_seen": now,
            "last_seen": now,
            "consecutive_scans": 1,
            "duration_seconds": 0,
            "first_price": record.get("price"),
            "last_price": record.get("price"),
            "first_rsi": record.get("rsi"),
            "last_rsi": record.get("rsi"),
            "first_atr": record.get("atr"),
            "last_atr": record.get("atr"),
            "trend": record.get("trend"),
            "last_trend": record.get("trend")
        }

        state["active_signals"][symbol] = new_active

        start_event = {
            "event": "START",
            "symbol": symbol,
            "side": signal,
            "previous_side": previous_side,
            "first_seen": now,
            "last_seen": now,
            "consecutive_scans": 1,
            "duration_seconds": 0,
            "price": record.get("price"),
            "rsi": record.get("rsi"),
            "atr": record.get("atr"),
            "trend": record.get("trend"),
            "volume": record.get("volume"),
            "volume_avg": record.get("volume_avg")
        }

        add_signal_history(start_event)

        log(
            "[SIGNAL START] %s %s "
            "price=%.8f RSI=%.2f trend=%s",
            symbol,
            signal,
            record.get("price", 0),
            record.get("rsi", 0),
            record.get("trend", "UNKNOWN")
        )


def signal_summary():
    with state_lock:

        active = state.get("active_signals", {})

        buys = [
            item for item in active.values()
            if item.get("side") == "BUY"
        ]

        sells = [
            item for item in active.values()
            if item.get("side") == "SELL"
        ]

        return {
            "active_count": len(active),
            "active_buys": len(buys),
            "active_sells": len(sells),
            "buy_symbols": sorted(
                item.get("symbol")
                for item in buys
            ),
            "sell_symbols": sorted(
                item.get("symbol")
                for item in sells
            )
        }


# ============================================================
# PUBLIC API
# ============================================================

def public_request(path, params=None):

    try:

        response = session.get(
            BASE_URL + path,
            params=params or {},
            timeout=(5, 20)
        )

        try:
            return response.json(), response.status_code

        except Exception:
            return {
                "raw": response.text[:1000]
            }, response.status_code

    except Exception as exc:

        return {
            "error": str(exc)
        }, 500


# ============================================================
# PRODUCT DISCOVERY - LIVE PERPETUAL USD CONTRACTS
# ============================================================

def discover_products():

    all_products = []
    cursor = None
    seen_cursors = set()

    for page_num in range(1, MAX_DISCOVERY_PAGES + 1):

        params = {
            "contract_types": "perpetual_futures",
            "states": "live",
            "page_size": 100
        }

        if cursor:
            params["after"] = cursor

        response, http_code = public_request(
            "/v2/products",
            params
        )

        if (
            http_code != 200
            or not isinstance(response, dict)
            or not response.get("success")
        ):

            state["discovery_error"] = (
                f"HTTP {http_code}: {response}"
            )

            event(
                "[DISCOVERY ERROR] "
                + str(state["discovery_error"])
            )

            return []

        batch = response.get("result") or []

        if not isinstance(batch, list):

            state["discovery_error"] = (
                "API result format was not a list"
            )

            event("[DISCOVERY ERROR] Unexpected result format")

            return []

        all_products.extend(batch)

        meta = response.get("meta") or {}
        next_cursor = meta.get("after")

        if (
            not next_cursor
            or next_cursor in seen_cursors
            or not batch
        ):
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor

    eligible = []

    for product in all_products:

        symbol = str(
            product.get("symbol") or ""
        ).upper().strip()

        contract_type = str(
            product.get("contract_type") or ""
        ).lower().strip()

        status = str(
            product.get("state")
            or product.get("status")
            or ""
        ).lower().strip()

        product_id = product.get("id")

        if not symbol:
            continue

        if not symbol.endswith("USD"):
            continue

        if (
            contract_type
            and contract_type != "perpetual_futures"
        ):
            continue

        if status and status != "live":
            continue

        if not product_id:
            continue

        eligible.append(product)

    product_map = {
        p["symbol"]: p
        for p in eligible
    }

    state["products"] = product_map
    state["symbols"] = sorted(product_map.keys())
    state["discovery_count"] = len(all_products)
    state["discovery_error"] = None

    save_state()

    event(
        f"[DISCOVERY] catalog={len(all_products)} "
        f"eligible-live-perpetuals={len(product_map)}"
    )

    if not product_map:

        sample = []

        for p in all_products[:8]:

            sample.append({
                k: p.get(k)
                for k in (
                    "id",
                    "symbol",
                    "contract_type",
                    "state",
                    "status",
                    "product_type"
                )
            })

        event(
            "[DISCOVERY DIAGNOSTIC SAMPLE] "
            + json.dumps(sample, ensure_ascii=False)
        )

    else:

        event(
            "[DISCOVERY SYMBOLS] "
            + ",".join(state["symbols"])
        )

    return state["symbols"]


# ============================================================
# CANDLES
# ============================================================

def get_candles(symbol, resolution, count=500):

    now = int(time.time())

    minutes = (
        15 if resolution == "15m"
        else 5
    )

    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": now - count * minutes * 60,
        "end": now
    }

    response, code = public_request(
        "/v2/history/candles",
        params
    )

    if (
        code == 200
        and isinstance(response, dict)
        and response.get("success")
    ):

        return response.get("result") or []

    log(
        "[CANDLE ERROR] %s %s HTTP=%s response=%s",
        symbol,
        resolution,
        code,
        response
    )

    return []


def candle_frame(raw):

    if not raw:
        return None

    try:

        if isinstance(raw[0], dict):

            frame = pd.DataFrame(raw)

            if "time" not in frame.columns:

                for possible in (
                    "timestamp",
                    "t",
                    "start",
                    "datetime"
                ):

                    if possible in frame.columns:

                        frame = frame.rename(
                            columns={possible: "time"}
                        )

                        break

        else:

            frame = pd.DataFrame(
                raw,
                columns=[
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume"
                ]
            )

        required = [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]

        if not all(
            column in frame.columns
            for column in required
        ):

            log(
                "[CANDLE FORMAT ERROR] "
                "Required OHLCV fields missing: %s",
                list(frame.columns)
            )

            return None

        for column in required:

            frame[column] = pd.to_numeric(
                frame[column],
                errors="coerce"
            )

        if "time" in frame.columns:

            frame["time"] = pd.to_numeric(
                frame["time"],
                errors="coerce"
            )

            frame = frame.dropna(
                subset=required + ["time"]
            )

            frame = frame.sort_values(
                "time"
            )

            frame = frame.drop_duplicates(
                subset=["time"],
                keep="last"
            )

        else:

            frame = frame.dropna(
                subset=required
            )

        # Remove latest potentially incomplete candle
        if len(frame) > 1:
            frame = frame.iloc[:-1].copy()

        if len(frame) < 205:
            return None

        # EMA indicators
        for span in (10, 20, 50, 200):

            frame[f"ema{span}"] = (
                frame["close"].ewm(
                    span=span,
                    adjust=False
                ).mean()
            )

        # RSI
        change = frame["close"].diff()

        gain = change.clip(
            lower=0
        ).rolling(RSI_PERIOD).mean()

        loss = (
            -change.clip(upper=0)
        ).rolling(RSI_PERIOD).mean()

        rs = gain / loss.replace(
            0,
            float("nan")
        )

        frame["rsi"] = (
            100 - (100 / (1 + rs))
        )

        # Volume average
        frame["vol_avg"] = (
            frame["volume"].rolling(
                VOLUME_PERIOD
            ).mean()
        )

        # ATR
        previous_close = frame["close"].shift(1)

        true_range = pd.concat(
            [
                frame["high"] - frame["low"],
                (frame["high"] - previous_close).abs(),
                (frame["low"] - previous_close).abs()
            ],
            axis=1
        ).max(axis=1)

        frame["atr"] = (
            true_range.rolling(
                ATR_PERIOD
            ).mean()
        )

        return frame.reset_index(drop=True)

    except Exception as exc:

        log("[INDICATOR ERROR] %s", exc)

        return None


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_symbol(symbol):

    trend_df = candle_frame(
        get_candles(symbol, TREND_TF)
    )

    entry_df = candle_frame(
        get_candles(symbol, ENTRY_TF)
    )

    if (
        trend_df is None
        or entry_df is None
        or len(entry_df) < 2
    ):

        log(
            "[DATA SKIP] %s candle data insufficient",
            symbol
        )

        return None

    trend = trend_df.iloc[-1]
    entry = entry_df.iloc[-1]

    checks = (
        "ema10",
        "ema20",
        "rsi",
        "vol_avg",
        "atr"
    )

    if (
        pd.isna(trend.get("ema50"))
        or pd.isna(trend.get("ema200"))
        or any(
            pd.isna(entry.get(key))
            for key in checks
        )
    ):

        log(
            "[INDICATOR SKIP] %s "
            "indicator values not ready",
            symbol
        )

        return None

    bullish = (
        trend.ema50 > trend.ema200
        and trend.close > trend.ema200
    )

    bearish = (
        trend.ema50 < trend.ema200
        and trend.close < trend.ema200
    )

    volume_ok = (
        entry.volume >= entry.vol_avg * VOL_MULT
    )

    buy_ok = (
        entry.ema10 > entry.ema20
        and 50 <= entry.rsi <= 68
        and volume_ok
    )

    sell_ok = (
        entry.ema10 < entry.ema20
        and 32 <= entry.rsi <= 50
        and volume_ok
    )

    signal = (
        "BUY"
        if bullish and buy_ok
        else "SELL"
        if bearish and sell_ok
        else "NONE"
    )

    candle_time = (
        int(entry.time)
        if "time" in entry
        and pd.notna(entry.time)
        else int(time.time() // 300 * 300)
    )

    record = {
        "symbol": symbol,
        "trend": (
            "BULLISH"
            if bullish
            else "BEARISH"
            if bearish
            else "MIXED"
        ),
        "signal": signal,
        "price": float(entry.close),
        "ema10": float(entry.ema10),
        "ema20": float(entry.ema20),
        "rsi": float(entry.rsi),
        "volume": float(entry.volume),
        "volume_avg": float(entry.vol_avg),
        "volume_ok": bool(volume_ok),
        "atr": float(entry.atr),
        "candle_time": candle_time,
        "updated_at": int(time.time())
    }

    log(
        "[SIGNAL DIAG] %s TREND=%s "
        "EMA10=%.8f EMA20=%.8f RSI=%.2f "
        "VOL=%.4f AVG=%.4f VOL_OK=%s "
        "ATR=%.8f SIGNAL=%s",
        symbol,
        record["trend"],
        record["ema10"],
        record["ema20"],
        record["rsi"],
        record["volume"],
        record["volume_avg"],
        record["volume_ok"],
        record["atr"],
        signal
    )

    # Update main signal snapshot
    with state_lock:
        state["signals"][symbol] = record

    # Update signal streak/history
    track_signal(record)

    save_state()

    return record


# ============================================================
# FLASK STATUS / DASHBOARD API
# ============================================================

@app.route("/")
def home():

    return (
        "DELTA INDIA TESTNET SCANNER RUNNING "
        "| /health | /dashboard-data | /signal-history"
    )


@app.route("/health")
def health():

    summary = signal_summary()

    with state_lock:

        return jsonify({
            "status": "ok",
            "testnet": True,
            "trading_enabled": False,
            "scanner_only": True,
            "eligible_symbols": len(
                state.get("symbols", [])
            ),
            "discovery_count": state.get(
                "discovery_count",
                0
            ),
            "discovery_error": state.get(
                "discovery_error"
            ),
            "last_scan": state.get("last_scan"),
            "active_signals": summary
        })


@app.route("/dashboard-data")
def dashboard_data():

    with state_lock:

        return jsonify({
            "testnet": True,
            "trading_enabled": False,
            "scanner_only": True,
            "symbols": state.get("symbols", []),
            "signals": state.get("signals", {}),
            "active_signals": state.get(
                "active_signals", {}
            ),
            "signal_summary": signal_summary(),
            "signal_history": state.get(
                "signal_history", []
            )[-100:],
            "events": state.get(
                "events", []
            )[-50:],
            "discovery_error": state.get(
                "discovery_error"
            ),
            "last_scan": state.get("last_scan")
        })


@app.route("/signal-history")
def signal_history_endpoint():

    with state_lock:

        return jsonify({
            "testnet": True,
            "trading_enabled": False,
            "history": state.get(
                "signal_history", []
            )[-MAX_SIGNAL_HISTORY:],
            "active_signals": state.get(
                "active_signals", {}
            ),
            "summary": signal_summary()
        })


def run_server():

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        use_reloader=False
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    log("=" * 70)

    log(
        "DELTA INDIA TESTNET SIGNAL TRACKING SCANNER STARTED "
        "(ORDER PLACEMENT DISABLED)"
    )

    log("BASE URL: %s", BASE_URL)

    log("TRADING ENABLED: FALSE - SAFETY MODE")

    log(
        "TREND TF=%s | ENTRY TF=%s | SCAN=%ss | DISCOVERY=%ss",
        TREND_TF,
        ENTRY_TF,
        SCAN_INTERVAL,
        DISCOVERY_INTERVAL
    )

    log(
        "FILTERS: EMA50/200 trend | EMA10/20 entry | "
        "RSI14 | Volume x%.2f | ATR14",
        VOL_MULT
    )

    log(
        "SIGNAL TRACKING: history=%d | "
        "consecutive scans | first_seen | last_seen",
        MAX_SIGNAL_HISTORY
    )

    log("=" * 70)

    threading.Thread(
        target=run_server,
        daemon=True
    ).start()

    symbols = []
    last_discovery = 0

    while True:

        try:

            if (
                time.time() - last_discovery
                >= DISCOVERY_INTERVAL
                or not symbols
            ):

                discovered = discover_products()

                if discovered:
                    symbols = discovered

                last_discovery = time.time()

            if not symbols:

                event(
                    "[SCAN HALT] No eligible perpetual symbols "
                    "returned; waiting for discovery"
                )

                time.sleep(SCAN_INTERVAL)

                continue

            for symbol in symbols:

                analyze_symbol(symbol)

                # API pacing
                time.sleep(0.25)

            state["last_scan"] = int(time.time())

            save_state()

            summary = signal_summary()

            log(
                "[SCAN DONE] symbols=%d "
                "active_signals=%d BUY=%d SELL=%d "
                "next scan in %ds",
                len(symbols),
                summary["active_count"],
                summary["active_buys"],
                summary["active_sells"],
                SCAN_INTERVAL
            )

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:

            event("[BOT STOP] Interrupted")

            break

        except Exception as exc:

            log("[MAIN LOOP ERROR] %s", exc)

            time.sleep(10)


if __name__ == "__main__":
    main()
