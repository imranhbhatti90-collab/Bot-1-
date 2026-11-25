import os
import json
import time
import threading
import logging
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
from flask import Flask, jsonify, Response

# ============================================================
# DELTA EXCHANGE INDIA - TESTNET ONLY
# SIGNAL SCANNER / NO ORDER EXECUTION
# ============================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

# SAFETY: ORDER PLACEMENT IS DISABLED
ENABLE_TRADING = True 

TREND_RESOLUTION = "15m"
ENTRY_RESOLUTION = "5m"

SCAN_INTERVAL = 30
DISCOVERY_INTERVAL = 900

TREND_EMA_FAST = 50
TREND_EMA_SLOW = 200

ENTRY_EMA_FAST = 10
ENTRY_EMA_SLOW = 20

RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_MA_PERIOD = 20
VOLUME_MULTIPLIER = 1.10

BUY_RSI_MIN = 50
BUY_RSI_MAX = 68

SELL_RSI_MIN = 32
SELL_RSI_MAX = 50

TREND_CANDLE_COUNT = 1000
ENTRY_CANDLE_COUNT = 1000

MIN_TREND_CANDLES = 205
MIN_ENTRY_CANDLES = 50

SIGNAL_HISTORY_LIMIT = 200
CHECKPOINT_EVERY = 10

STATE_FILE = "state.json"

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("delta_scanner")

# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Delta-Testnet-Signal-Scanner/2.0"
})

# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

# ============================================================
# STATE
# ============================================================

state_lock = threading.Lock()

state = {
    "bot_status": "STARTING",
    "trading_enabled": False,
    "exchange": "DELTA INDIA TESTNET",
    "symbols": [],
    "total_symbols": 0,
    "scan_count": 0,
    "last_scan": None,
    "last_scan_duration": None,
    "active_signals": {},
    "signal_history": [],
    "last_diagnostics": {},
    "errors": [],
    "started_at": None
}

active_signals = {}
signal_history = []
last_diagnostics = {}

scan_count = 0
last_scan_time = None
last_scan_duration = None
last_discovery_time = 0
symbols = []

# ============================================================
# UTILITIES
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=0.0):
    try:
        value = float(value)
        if pd.isna(value):
            return default
        return value
    except Exception:
        return default


def add_error(message):
    with state_lock:
        state["errors"].append({
            "time": utc_now(),
            "message": str(message)
        })
        state["errors"] = state["errors"][-50:]


def api_get(path, params=None, timeout=15):
    url = BASE_URL + path

    last_error = None

    for attempt in range(3):
        try:
            response = session.get(
                url,
                params=params,
                timeout=timeout
            )

            response.raise_for_status()
            data = response.json()

            if not data.get("success", True):
                raise RuntimeError(
                    f"API returned success=False: {data}"
                )

            return data

        except Exception as exc:
            last_error = exc

            logger.warning(
                "[API RETRY] %s attempt=%s error=%s",
                path,
                attempt + 1,
                exc
            )

            time.sleep(1 + attempt)

    raise RuntimeError(
        f"API request failed: {path}: {last_error}"
    )

# ============================================================
# ROBUST PRODUCT DISCOVERY WITH FALLBACK
# ============================================================

FALLBACK_SYMBOLS = [
    "1000SHIBUSD",
    "ADAUSD",
    "BTCUSD",
    "DOGEUSD",
    "DOGSUSD",
    "ETHUSD",
    "NVDAXUSD",
    "PAXGUSD",
    "QQQXUSD",
    "SILVERUSD",
    "SLVONUSD",
    "SNDKBUSD",
    "SOLUSD",
    "SPYXUSD",
    "USOONUSD",
    "XAUTUSD",
    "XRPUSD"
]


def discover_symbols(force=False):
    global symbols, last_discovery_time

    now = time.time()

    if (
        not force
        and symbols
        and now - last_discovery_time < DISCOVERY_INTERVAL
    ):
        return symbols

    discovered = set()
    cursor = None
    pages = 0

    try:
        while pages < 10:

            params = {"page_size": 100}

            if cursor:
                params["after"] = cursor

            data = api_get(
                "/v2/products",
                params=params
            )

            products = data.get("result", [])

            if not isinstance(products, list):
                logger.error(
                    "[DISCOVERY] Invalid product response format"
                )
                break

            pages += 1

            for product in products:

                if not isinstance(product, dict):
                    continue

                sym = str(
                    product.get("symbol", "")
                ).strip().upper()

                if not sym:
                    continue

                # Exclude option contracts
                if sym.startswith("C-") or sym.startswith("P-"):
                    continue

                # USD-symbol scanner universe
                if not sym.endswith("USD"):
                    continue

                # Flexible contract-type detection
                contract_type = str(
                    product.get("contract_type")
                    or product.get("product_type")
                    or product.get("type")
                    or ""
                ).strip().lower()

                description = str(
                    product.get("description", "")
                ).lower()

                # If contract type is provided, verify it
                if contract_type:
                    if "perpetual" not in contract_type:
                        continue

                # If contract type is missing, use description
                elif description:
                    if "perpetual" not in description:
                        continue

                # Flexible live-state validation
                product_state = str(
                    product.get("state")
                    or product.get("status")
                    or ""
                ).strip().lower()

                if product_state:
                    if product_state not in (
                        "live",
                        "active",
                        "trading"
                    ):
                        continue

                discovered.add(sym)

            meta = data.get("meta", {}) or {}
            next_cursor = meta.get("after")

            if not products or not next_cursor:
                break

            if next_cursor == cursor:
                logger.warning(
                    "[DISCOVERY] Cursor unchanged, stopping pagination"
                )
                break

            cursor = next_cursor

        discovered = sorted(discovered)

        logger.info(
            "[DISCOVERY] pages=%s discovered=%s",
            pages,
            len(discovered)
        )

        # Successful API discovery
        if discovered:

            symbols = discovered
            last_discovery_time = now

            logger.info(
                "[DISCOVERY SYMBOLS] %s",
                ",".join(symbols)
            )

        # Fallback if API filtering finds zero
        else:

            logger.warning(
                "[DISCOVERY FALLBACK] API returned no filtered symbols; "
                "using known scanner universe"
            )

            symbols = FALLBACK_SYMBOLS.copy()
            last_discovery_time = now

            logger.warning(
                "[DISCOVERY SYMBOLS] %s",
                ",".join(symbols)
            )

        with state_lock:
            state["symbols"] = symbols
            state["total_symbols"] = len(symbols)

        return symbols

    except Exception as exc:

        logger.error(
            "[DISCOVERY ERROR] %s",
            exc
        )

        add_error(f"Discovery error: {exc}")

        # Keep previous universe if available
        if symbols:
            logger.warning(
                "[DISCOVERY] Keeping previous %s symbols",
                len(symbols)
            )
            return symbols

        # Last-resort fallback
        symbols = FALLBACK_SYMBOLS.copy()
        last_discovery_time = now

        logger.warning(
            "[DISCOVERY FALLBACK] Using %s known symbols",
            len(symbols)
        )

        with state_lock:
            state["symbols"] = symbols
            state["total_symbols"] = len(symbols)

        return symbols



# ============================================================
# CANDLE DATA
# ============================================================

def fetch_candles(symbol, resolution, count):
    now = int(time.time())

    if resolution == "5m":
        candle_seconds = 300
    elif resolution == "15m":
        candle_seconds = 900
    else:
        raise ValueError("Unsupported resolution")

    start = now - count * candle_seconds

    data = api_get(
        "/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": symbol,
            "start": start,
            "end": now
        }
    )

    rows = data.get("result", [])

    if not rows:
        raise ValueError("No candle data returned")

    frame = pd.DataFrame(rows)

    required = [
        "time", "open", "high", "low", "close", "volume"
    ]

    missing = [
        column for column in required
        if column not in frame.columns
    ]

    if missing:
        raise ValueError(
            f"Missing candle columns: {missing}"
        )

    # Convert columns safely
    for column in required:
        frame[column] = pd.to_numeric(
            frame[column],
            errors="coerce"
        )

    # Remove invalid rows
    frame = frame.dropna(subset=required)

    # Sort and remove duplicates
    frame = frame.sort_values("time")
    frame = frame.drop_duplicates(
        subset=["time"],
        keep="last"
    )

    # Remove invalid OHLC candles
    frame = frame[
        (frame["open"] > 0) &
        (frame["high"] > 0) &
        (frame["low"] > 0) &
        (frame["close"] > 0) &
        (frame["high"] >= frame["low"]) &
        (frame["high"] >= frame["open"]) &
        (frame["high"] >= frame["close"]) &
        (frame["low"] <= frame["open"]) &
        (frame["low"] <= frame["close"])
    ]

    # Exclude incomplete/live candle
    last_closed_time = now - (now % candle_seconds)

    frame = frame[
        frame["time"] < last_closed_time
    ]

    frame = frame.reset_index(drop=True)

    if frame.empty:
        raise ValueError(
            "No valid CLOSED candles after validation"
        )

    return frame


# ============================================================
# INDICATORS
# ============================================================

def calculate_indicators(frame):

    frame = frame.copy()

    frame["ema10"] = frame["close"].ewm(
        span=10,
        adjust=False
    ).mean()

    frame["ema20"] = frame["close"].ewm(
        span=20,
        adjust=False
    ).mean()

    frame["ema50"] = frame["close"].ewm(
        span=50,
        adjust=False
    ).mean()

    frame["ema200"] = frame["close"].ewm(
        span=200,
        adjust=False
    ).mean()

    # RSI using Wilder-style exponential smoothing
    delta = frame["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD,
        min_periods=RSI_PERIOD,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD,
        min_periods=RSI_PERIOD,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))

    frame["rsi"] = 100 - (100 / (1 + rs))

    frame.loc[
        (avg_loss == 0) & (avg_gain > 0),
        "rsi"
    ] = 100

    frame.loc[
        (avg_gain == 0) & (avg_loss > 0),
        "rsi"
    ] = 0

    # ATR
    previous_close = frame["close"].shift(1)

    tr1 = frame["high"] - frame["low"]
    tr2 = (frame["high"] - previous_close).abs()
    tr3 = (frame["low"] - previous_close).abs()

    frame["tr"] = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    frame["atr"] = frame["tr"].ewm(
        alpha=1 / ATR_PERIOD,
        min_periods=ATR_PERIOD,
        adjust=False
    ).mean()

    # Volume average excluding current candle
    frame["volume_avg"] = (
        frame["volume"]
        .shift(1)
        .rolling(VOLUME_MA_PERIOD)
        .mean()
    )

    return frame


# ============================================================
# TREND ANALYSIS
# ============================================================

def analyze_trend(frame):

    if len(frame) < MIN_TREND_CANDLES:
        return {
            "trend": "NOT_READY",
            "reason": "Insufficient trend candles"
        }

    frame = calculate_indicators(frame)

    row = frame.iloc[-1]

    close = safe_float(row["close"])
    ema50 = safe_float(row["ema50"])
    ema200 = safe_float(row["ema200"])

    if any(
        pd.isna(row[key])
        for key in ["ema50", "ema200"]
    ):
        return {
            "trend": "NOT_READY",
            "reason": "EMA50/EMA200 not ready"
        }

    if ema50 > ema200 and close > ema50 and close > ema200:
        trend = "BULLISH"

    elif ema50 < ema200 and close < ema50 and close < ema200:
        trend = "BEARISH"

    else:
        trend = "MIXED"

    return {
        "trend": trend,
        "close": close,
        "ema50": ema50,
        "ema200": ema200,
        "reason": ""
    }


# ============================================================
# ENTRY ANALYSIS
# ============================================================

def analyze_entry(symbol, trend_data, frame):

    if len(frame) < MIN_ENTRY_CANDLES:
        return {
            "signal": "NONE",
            "reason": "INSUFFICIENT_ENTRY_CANDLES"
        }

    frame = calculate_indicators(frame)

    row = frame.iloc[-1]

    close = safe_float(row["close"])
    ema10 = safe_float(row["ema10"])
    ema20 = safe_float(row["ema20"])
    rsi = safe_float(row["rsi"], float("nan"))
    atr = safe_float(row["atr"], float("nan"))

    current_volume = safe_float(row["volume"])
    volume_avg = safe_float(
        row["volume_avg"],
        float("nan")
    )

    trend = trend_data.get("trend", "NOT_READY")

    reasons_buy = []
    reasons_sell = []

    # --------------------------------------------------------
    # Data validation
    # --------------------------------------------------------

    if not close or close <= 0:
        reasons_buy.append("INVALID_PRICE")
        reasons_sell.append("INVALID_PRICE")

    if pd.isna(rsi):
        reasons_buy.append("RSI_NOT_READY")
        reasons_sell.append("RSI_NOT_READY")

    if pd.isna(atr) or atr <= 0:
        reasons_buy.append("ATR_NOT_READY")
        reasons_sell.append("ATR_NOT_READY")

    if pd.isna(volume_avg) or volume_avg <= 0:
        reasons_buy.append("VOLUME_AVERAGE_INVALID")
        reasons_sell.append("VOLUME_AVERAGE_INVALID")

    if current_volume <= 0:
        reasons_buy.append("CURRENT_VOLUME_ZERO")
        reasons_sell.append("CURRENT_VOLUME_ZERO")

    volume_ok = (
        current_volume > 0 and
        volume_avg > 0 and
        current_volume >= volume_avg * VOLUME_MULTIPLIER
    )

    # --------------------------------------------------------
    # BUY checks
    # --------------------------------------------------------

    if trend != "BULLISH":
        reasons_buy.append(
            f"TREND_NOT_BULLISH({trend})"
        )

    if not ema10 > ema20:
        reasons_buy.append("EMA10_NOT_ABOVE_EMA20")

    if not (
        BUY_RSI_MIN <= rsi <= BUY_RSI_MAX
    ):
        reasons_buy.append(
            f"RSI_OUTSIDE_BUY_RANGE({rsi:.2f})"
        )

    if not volume_ok:
        reasons_buy.append("VOLUME_FILTER_FAIL")

    # --------------------------------------------------------
    # SELL checks
    # --------------------------------------------------------

    if trend != "BEARISH":
        reasons_sell.append(
            f"TREND_NOT_BEARISH({trend})"
        )

    if not ema10 < ema20:
        reasons_sell.append("EMA10_NOT_BELOW_EMA20")

    if not (
        SELL_RSI_MIN <= rsi <= SELL_RSI_MAX
    ):
        reasons_sell.append(
            f"RSI_OUTSIDE_SELL_RANGE({rsi:.2f})"
        )

    if not volume_ok:
        reasons_sell.append("VOLUME_FILTER_FAIL")

    # --------------------------------------------------------
    # Signal
    # --------------------------------------------------------

    signal = "NONE"

    if not reasons_buy:
        signal = "BUY"

    elif not reasons_sell:
        signal = "SELL"

    # --------------------------------------------------------
    # Diagnostics
    # --------------------------------------------------------

    diagnostic = {
        "symbol": symbol,
        "time": utc_now(),
        "trend": trend,
        "close": close,
        "ema10": ema10,
        "ema20": ema20,
        "ema50": trend_data.get("ema50"),
        "ema200": trend_data.get("ema200"),
        "rsi": rsi,
        "volume": current_volume,
        "volume_avg": volume_avg,
        "volume_ratio": (
            current_volume / volume_avg
            if volume_avg > 0
            else None
        ),
        "volume_ok": volume_ok,
        "atr": atr,
        "signal": signal,
        "buy_blocked": reasons_buy,
        "sell_blocked": reasons_sell,
        "candle_time": int(row["time"])
    }

    if signal == "BUY":
        diagnostic["reason"] = "ALL_BUY_FILTERS_PASSED"

    elif signal == "SELL":
        diagnostic["reason"] = "ALL_SELL_FILTERS_PASSED"

    else:
        diagnostic["reason"] = "FILTERS_BLOCKED"

    return diagnostic


# ============================================================
# SIGNAL TRACKING
# ============================================================

def record_signal(diagnostic):

    symbol = diagnostic["symbol"]
    signal = diagnostic["signal"]

    now = utc_now()

    previous = active_signals.get(symbol)

    # No valid signal
    if signal == "NONE":

        if previous:

            history_entry = {
                "event": "END",
                "symbol": symbol,
                "signal": previous["signal"],
                "first_seen": previous["first_seen"],
                "last_seen": previous["last_seen"],
                "duration_scans": previous["scans"],
                "ended_at": now,
                "last_price": diagnostic.get("close"),
                "reason": diagnostic.get("reason")
            }

            signal_history.append(history_entry)

            logger.info(
                "[SIGNAL END] %s %s scans=%s",
                symbol,
                previous["signal"],
                previous["scans"]
            )

            active_signals.pop(symbol, None)

        return

    # New signal
    if not previous:

        active_signals[symbol] = {
            "symbol": symbol,
            "signal": signal,
            "first_seen": now,
            "last_seen": now,
            "scans": 1,
            "last_price": diagnostic.get("close"),
            "last_rsi": diagnostic.get("rsi"),
            "last_atr": diagnostic.get("atr"),
            "trend": diagnostic.get("trend")
        }

        signal_history.append({
            "event": "START",
            "symbol": symbol,
            "signal": signal,
            "time": now,
            "price": diagnostic.get("close"),
            "rsi": diagnostic.get("rsi"),
            "atr": diagnostic.get("atr"),
            "trend": diagnostic.get("trend")
        })

        logger.warning(
            "[SIGNAL START] %s %s price=%s RSI=%s ATR=%s",
            symbol,
            signal,
            diagnostic.get("close"),
            diagnostic.get("rsi"),
            diagnostic.get("atr")
        )

        return

    # Signal changed
    if previous["signal"] != signal:

        signal_history.append({
            "event": "CHANGE",
            "symbol": symbol,
            "old_signal": previous["signal"],
            "new_signal": signal,
            "time": now,
            "price": diagnostic.get("close")
        })

        logger.warning(
            "[SIGNAL CHANGE] %s %s -> %s",
            symbol,
            previous["signal"],
            signal
        )

        active_signals[symbol] = {
            "symbol": symbol,
            "signal": signal,
            "first_seen": now,
            "last_seen": now,
            "scans": 1,
            "last_price": diagnostic.get("close"),
            "last_rsi": diagnostic.get("rsi"),
            "last_atr": diagnostic.get("atr"),
            "trend": diagnostic.get("trend")
        }

        return

    # Signal continues
    previous["last_seen"] = now
    previous["scans"] += 1
    previous["last_price"] = diagnostic.get("close")
    previous["last_rsi"] = diagnostic.get("rsi")
    previous["last_atr"] = diagnostic.get("atr")
    previous["trend"] = diagnostic.get("trend")

    if previous["scans"] % CHECKPOINT_EVERY == 0:

        signal_history.append({
            "event": "CHECKPOINT",
            "symbol": symbol,
            "signal": signal,
            "time": now,
            "scans": previous["scans"],
            "first_seen": previous["first_seen"],
            "price": diagnostic.get("close"),
            "rsi": diagnostic.get("rsi"),
            "atr": diagnostic.get("atr")
        })

        logger.info(
            "[SIGNAL CHECKPOINT] %s %s scans=%s",
            symbol,
            signal,
            previous["scans"]
        )

    # Limit history
    if len(signal_history) > SIGNAL_HISTORY_LIMIT:
        del signal_history[
            :-SIGNAL_HISTORY_LIMIT
        ]


# ============================================================
# SCAN SYMBOL
# ============================================================

def scan_symbol(symbol):

    try:

        # Trend timeframe
        trend_frame = fetch_candles(
            symbol,
            TREND_RESOLUTION,
            TREND_CANDLE_COUNT
        )

        trend_data = analyze_trend(
            trend_frame
        )

        if trend_data["trend"] == "NOT_READY":

            diagnostic = {
                "symbol": symbol,
                "time": utc_now(),
                "signal": "NONE",
                "trend": "NOT_READY",
                "reason": trend_data.get("reason"),
                "buy_blocked": [
                    trend_data.get("reason")
                ],
                "sell_blocked": [
                    trend_data.get("reason")
                ]
            }

            logger.warning(
                "[TREND SKIP] %s %s",
                symbol,
                trend_data.get("reason")
            )

            record_signal(diagnostic)
            last_diagnostics[symbol] = diagnostic

            return diagnostic

        # Entry timeframe
        entry_frame = fetch_candles(
            symbol,
            ENTRY_RESOLUTION,
            ENTRY_CANDLE_COUNT
        )

        diagnostic = analyze_entry(
            symbol,
            trend_data,
            entry_frame
        )

        last_diagnostics[symbol] = diagnostic

        # Diagnostic log
        logger.info(
            "[SIGNAL DIAG] %s "
            "TREND=%s "
            "EMA10=%.8f "
            "EMA20=%.8f "
            "RSI=%.2f "
            "VOL=%.4f "
            "AVG=%.4f "
            "VOL_OK=%s "
            "ATR=%.8f "
            "SIGNAL=%s",
            symbol,
            diagnostic.get("trend"),
            safe_float(diagnostic.get("ema10")),
            safe_float(diagnostic.get("ema20")),
            safe_float(diagnostic.get("rsi")),
            safe_float(diagnostic.get("volume")),
            safe_float(diagnostic.get("volume_avg")),
            diagnostic.get("volume_ok"),
            safe_float(diagnostic.get("atr")),
            diagnostic.get("signal")
        )

        # Exact block reasons
        if diagnostic["signal"] == "NONE":

            logger.info(
                "[BLOCKED BUY] %s: %s",
                symbol,
                " | ".join(
                    diagnostic.get("buy_blocked", [])
                )
            )

            logger.info(
                "[BLOCKED SELL] %s: %s",
                symbol,
                " | ".join(
                    diagnostic.get("sell_blocked", [])
                )
            )

        elif diagnostic["signal"] in ("BUY", "SELL"):

            logger.warning(
                "[VALID SIGNAL] %s %s price=%s RSI=%s",
                symbol,
                diagnostic["signal"],
                diagnostic.get("close"),
                diagnostic.get("rsi")
            )

        record_signal(diagnostic)

        return diagnostic

    except Exception as exc:

        error_text = (
            f"{symbol}: {type(exc).__name__}: {exc}"
        )

        logger.error(
            "[SYMBOL ERROR] %s",
            error_text
        )

        logger.debug(
            traceback.format_exc()
        )

        add_error(error_text)

        diagnostic = {
            "symbol": symbol,
            "time": utc_now(),
            "signal": "NONE",
            "trend": "ERROR",
            "reason": error_text,
            "buy_blocked": [error_text],
            "sell_blocked": [error_text]
        }

        last_diagnostics[symbol] = diagnostic

        # Do not end existing signal on API error
        return diagnostic


# ============================================================
# MAIN SCANNER
# ============================================================

def scanner_loop():

    global scan_count
    global last_scan_time
    global last_scan_duration

    logger.info(
        "DELTA INDIA TESTNET SIGNAL TRACKING SCANNER "
        "STARTED (ORDER PLACEMENT DISABLED)"
    )

    logger.info("BASE URL: %s", BASE_URL)
    logger.info("TRADING ENABLED: FALSE - SAFETY MODE")

    logger.info(
        "TREND TF=%s | ENTRY TF=%s | SCAN=%ss | DISCOVERY=%ss",
        TREND_RESOLUTION,
        ENTRY_RESOLUTION,
        SCAN_INTERVAL,
        DISCOVERY_INTERVAL
    )

    logger.info(
        "FILTERS: EMA50/200 trend | EMA10/20 entry | "
        "RSI14 | Volume x%.2f | ATR14",
        VOLUME_MULTIPLIER
    )

    logger.info(
        "SIGNAL TRACKING: history=%s | consecutive scans | "
        "first_seen | last_seen",
        SIGNAL_HISTORY_LIMIT
    )

    while True:

        scan_started = time.time()

        try:

            current_symbols = discover_symbols()

            if not current_symbols:

                logger.error(
                    "[SCAN] No eligible symbols available"
                )

                time.sleep(SCAN_INTERVAL)
                continue

            buy_count = 0
            sell_count = 0

            for symbol in current_symbols:

                diagnostic = scan_symbol(symbol)

                if diagnostic.get("signal") == "BUY":
                    buy_count += 1

                elif diagnostic.get("signal") == "SELL":
                    sell_count += 1

            scan_count += 1

            last_scan_time = utc_now()

            last_scan_duration = round(
                time.time() - scan_started,
                2
            )

            logger.info(
                "[SCAN DONE] symbols=%s active_signals=%s "
                "BUY=%s SELL=%s duration=%.2fs "
                "next scan in %ss",
                len(current_symbols),
                len(active_signals),
                buy_count,
                sell_count,
                last_scan_duration,
                SCAN_INTERVAL
            )

            with state_lock:

                state["bot_status"] = "RUNNING"
                state["scan_count"] = scan_count
                state["last_scan"] = last_scan_time
                state["last_scan_duration"] = last_scan_duration
                state["active_signals"] = active_signals.copy()
                state["signal_history"] = signal_history[-SIGNAL_HISTORY_LIMIT:]
                state["last_diagnostics"] = last_diagnostics.copy()
                state["symbols"] = current_symbols
                state["total_symbols"] = len(current_symbols)

        except Exception as exc:

            logger.error(
                "[SCANNER LOOP ERROR] %s",
                exc
            )

            logger.debug(
                traceback.format_exc()
            )

            add_error(str(exc))

        elapsed = time.time() - scan_started

        sleep_time = max(
            1,
            SCAN_INTERVAL - elapsed
        )

        time.sleep(sleep_time)


# ============================================================
# STATE SAVE / LOAD
# ============================================================

def save_state():

    try:

        payload = {
            "active_signals": active_signals,
            "signal_history": signal_history[-SIGNAL_HISTORY_LIMIT:],
            "scan_count": scan_count,
            "last_scan": last_scan_time,
            "symbols": symbols
        }

        temp_file = STATE_FILE + ".tmp"

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                payload,
                file,
                indent=2,
                default=str
            )

        os.replace(
            temp_file,
            STATE_FILE
        )

    except Exception as exc:

        logger.error(
            "[STATE SAVE ERROR] %s",
            exc
        )


def load_state():

    global active_signals
    global signal_history
    global scan_count
    global symbols

    try:

        if not os.path.exists(STATE_FILE):
            return

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            payload = json.load(file)

        active_signals = payload.get(
            "active_signals",
            {}
        )

        signal_history = payload.get(
            "signal_history",
            []
        )

        scan_count = payload.get(
            "scan_count",
            0
        )

        symbols = payload.get(
            "symbols",
            []
        )

        logger.info(
            "[STATE RESTORED] active=%s history=%s scan_count=%s",
            len(active_signals),
            len(signal_history),
            scan_count
        )

    except Exception as exc:

        logger.error(
            "[STATE LOAD ERROR] %s",
            exc
        )


# ============================================================
# DASHBOARD DATA
# ============================================================

@app.route("/")
def home():

    return Response(
        """
        <!DOCTYPE html>
        <html>
        <head>
        <title>Delta Testnet Signal Scanner</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
        body {
            background:#10151c;
            color:#e7edf5;
            font-family:Arial,sans-serif;
            padding:20px;
        }
        h1 {color:#64d8a4;}
        .card {
            background:#1b2530;
            padding:15px;
            margin:10px 0;
            border-radius:10px;
        }
        a {color:#6ec9ff;}
        pre {
            white-space:pre-wrap;
            overflow-wrap:anywhere;
        }
        </style>
        </head>
        <body>
        <h1>DELTA INDIA TESTNET</h1>
        <h2>Signal Tracking Scanner</h2>
        <div class="card">
        <b>ORDER PLACEMENT: DISABLED</b>
        <br>
        SAFETY MODE - SIGNALS ONLY
        </div>
        <div class="card">
        <a href="/health">Health</a><br>
        <a href="/dashboard-data">Dashboard Data</a><br>
        <a href="/signal-history">Signal History</a>
        </div>
        <div class="card">
        <h3>Current Scanner Data</h3>
        <pre id="data">Loading...</pre>
        </div>
        <script>
        async function update() {
            try {
                const r = await fetch('/dashboard-data');
                const d = await r.json();
                document.getElementById('data').textContent =
                    JSON.stringify(d, null, 2);
            } catch(e) {
                document.getElementById('data').textContent =
                    'Dashboard loading error';
            }
        }
        update();
        setInterval(update, 10000);
        </script>
        </body>
        </html>
        """,
        mimetype="text/html"
    )


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "bot_status": state.get("bot_status"),
        "trading_enabled": False,
        "exchange": "DELTA INDIA TESTNET",
        "symbols": state.get("total_symbols", 0),
        "scan_count": scan_count,
        "active_signals": len(active_signals),
        "last_scan": last_scan_time,
        "last_scan_duration": last_scan_duration,
        "time": utc_now()
    }), 200


@app.route("/dashboard-data")
def dashboard_data():

    return jsonify({
        "bot_status": state.get("bot_status"),
        "trading_enabled": False,
        "exchange": "DELTA INDIA TESTNET",
        "symbols": symbols,
        "total_symbols": len(symbols),
        "scan_count": scan_count,
        "last_scan": last_scan_time,
        "last_scan_duration": last_scan_duration,
        "active_signals": active_signals,
        "signal_history": signal_history[-SIGNAL_HISTORY_LIMIT:],
        "last_diagnostics": last_diagnostics,
        "errors": state.get("errors", [])[-20:],
        "settings": {
            "trend_resolution": TREND_RESOLUTION,
            "entry_resolution": ENTRY_RESOLUTION,
            "trend_ema_fast": TREND_EMA_FAST,
            "trend_ema_slow": TREND_EMA_SLOW,
            "entry_ema_fast": ENTRY_EMA_FAST,
            "entry_ema_slow": ENTRY_EMA_SLOW,
            "rsi_period": RSI_PERIOD,
            "atr_period": ATR_PERIOD,
            "volume_ma_period": VOLUME_MA_PERIOD,
            "volume_multiplier": VOLUME_MULTIPLIER,
            "scan_interval": SCAN_INTERVAL,
            "discovery_interval": DISCOVERY_INTERVAL
        }
    })


@app.route("/signal-history")
def signal_history_endpoint():

    return jsonify({
        "active_signals": active_signals,
        "history": signal_history[-SIGNAL_HISTORY_LIMIT:],
        "count": len(signal_history),
        "time": utc_now()
    })


@app.route("/diagnostics")
def diagnostics_endpoint():

    return jsonify({
        "symbols": symbols,
        "diagnostics": last_diagnostics,
        "time": utc_now()
    })


# ============================================================
# STARTUP
# ============================================================

def start_scanner():

    load_state()

    state["started_at"] = utc_now()
    state["bot_status"] = "RUNNING"
    state["trading_enabled"] = False

    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True,
        name="delta-scanner"
    )

    scanner_thread.start()


if __name__ == "__main__":

    start_scanner()

    port = int(
        os.getenv("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
