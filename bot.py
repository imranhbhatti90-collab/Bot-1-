import os
import json
import time
import threading
import logging

import requests
import pandas as pd
from flask import Flask, jsonify

# ============================================================
# DELTA EXCHANGE INDIA TESTNET - SCANNER / DIAGNOSTIC VERSION
# ORDER PLACEMENT DISABLED - TEST DISCOVERY AND SIGNALS FIRST
# ============================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
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

STATE_FILE = "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.info
app = Flask(__name__)

session = requests.Session()
session.headers.update({
    "User-Agent": "delta-india-testnet-scanner/1.0",
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

        return base

    except Exception:
        return fresh_state()


state = load_state()
state_lock = threading.RLock()


def save_state():
    with state_lock:
        try:
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)

            os.replace(STATE_FILE + ".tmp", STATE_FILE)

        except Exception as exc:
            log("[STATE SAVE ERROR] %s", exc)


def event(message):
    with state_lock:
        state["events"].append({
            "time": int(time.time()),
            "message": str(message)
        })

        state["events"] = state["events"][-100:]

    log("%s", message)
    save_state()


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

    # Remove duplicate symbols
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

                        frame.rename(
                            columns={possible: "time"},
                            inplace=True
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

            frame.dropna(
                subset=required + ["time"],
                inplace=True
            )

            frame.sort_values(
                "time",
                inplace=True
            )

            frame.drop_duplicates(
                subset=["time"],
                keep="last",
                inplace=True
            )

        else:

            frame.dropna(
                subset=required,
                inplace=True
            )

        # Remove possibly incomplete latest candle
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

    state["signals"][symbol] = record

    save_state()

    return record


# ============================================================
# FLASK STATUS / DASHBOARD
# ============================================================

@app.route("/")
def home():

    return (
        "DELTA INDIA TESTNET SCANNER RUNNING "
        "| /health | /dashboard-data"
    )


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "testnet": True,
        "trading_enabled": ENABLE_TRADING,
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
        "last_scan": state.get("last_scan")
    })


@app.route("/dashboard-data")
def dashboard_data():

    return jsonify({
        "testnet": True,
        "trading_enabled": ENABLE_TRADING,
        "scanner_only": True,
        "symbols": state.get("symbols", []),
        "signals": state.get("signals", {}),
        "events": state.get("events", [])[-50:],
        "discovery_error": state.get(
            "discovery_error"
        ),
        "last_scan": state.get("last_scan")
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
        "DELTA INDIA TESTNET SCANNER STARTED "
        "(ORDER PLACEMENT DISABLED)"
    )

    log("BASE URL: %s", BASE_URL)

    log("TRADING ENABLED: %s", ENABLE_TRADING)

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

            log(
                "[SCAN DONE] symbols=%d next scan in %ds",
                len(symbols),
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
