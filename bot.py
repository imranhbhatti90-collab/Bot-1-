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
# DELTA EXCHANGE INDIA TESTNET - SIGNAL SCANNER ONLY
# HARD SAFETY: NO ORDER PLACEMENT CODE IN THIS FILE
# ============================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
TRADING_ENABLED = True 

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
VOLUME_MULTIPLIER = 1.00
MAX_ATR_PERCENT = 5.0  # Reject suspiciously abnormal ATR readings
MAX_SINGLE_CANDLE_MOVE_PERCENT = 8.0  # hard safety against malformed/jump candles
MAX_CLOSE_TO_CLOSE_MOVE_PERCENT = 8.0
MAX_CANDLE_GAP_MULTIPLE = 2.0
MAX_STALE_CANDLE_MULTIPLE = 2.0
RAW_CANDLE_DIAGNOSTIC_COUNT = 5

BUY_RSI_MIN = 50
BUY_RSI_MAX = 70
SELL_RSI_MIN = 30
SELL_RSI_MAX = 50

TREND_CANDLE_COUNT = 1000
ENTRY_CANDLE_COUNT = 1000
MIN_TREND_CANDLES = 220
MIN_ENTRY_CANDLES = 50

SIGNAL_HISTORY_LIMIT = 200
CHECKPOINT_EVERY = 10
STATE_FILE = "state.json"

FALLBACK_SYMBOLS = [
    "1000SHIBUSD", "ADAUSD", "BTCUSD", "DOGEUSD", "DOGSUSD",
    "ETHUSD", "NVDAXUSD", "PAXGUSD", "QQQXUSD", "SILVERUSD",
    "SLVONUSD", "SNDKBUSD", "SOLUSD", "SPYXUSD", "USOONUSD",
    "XAUTUSD", "XRPUSD"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("delta_scanner")

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Delta-India-Testnet-Signal-Scanner/3.0"
})

app = Flask(__name__)
state_lock = threading.RLock()

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
raw_diagnostic_last_logged = {}

# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=None):
    try:
        value = float(value)
        if pd.isna(value) or not pd.notna(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


def add_error(message):
    with state_lock:
        state["errors"].append({"time": utc_now(), "message": str(message)})
        state["errors"] = state["errors"][-50:]


def api_get(path, params=None, timeout=15):
    url = BASE_URL + path
    last_error = None

    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, dict) and payload.get("success") is False:
                raise RuntimeError(f"API success=False: {payload}")

            return payload

        except Exception as exc:
            last_error = exc
            logger.warning("[API RETRY] %s attempt=%s error=%s",
                          path, attempt + 1, exc)
            if attempt < 2:
                time.sleep(1 + attempt)

    raise RuntimeError(f"API failed: {path}: {last_error}")


def result_list(payload):
    if not isinstance(payload, dict):
        return []
    result = payload.get("result", [])
    return result if isinstance(result, list) else []


# ------------------------------------------------------------
# SYMBOL DISCOVERY
# ------------------------------------------------------------

def discover_symbols(force=False):
    global symbols, last_discovery_time

    now = time.time()
    if not force and symbols and now - last_discovery_time < DISCOVERY_INTERVAL:
        return symbols

    found = set()
    cursor = None
    pages = 0

    try:
        while pages < 10:
            params = {"page_size": 100}
            if cursor:
                params["after"] = cursor

            payload = api_get("/v2/products", params=params)
            products = result_list(payload)
            pages += 1

            for product in products:
                if not isinstance(product, dict):
                    continue

                sym = str(product.get("symbol", "")).strip().upper()
                if not sym or not sym.endswith("USD"):
                    continue
                if sym.startswith(("C-", "P-")):
                    continue

                contract_type = str(
                    product.get("contract_type")
                    or product.get("product_type")
                    or product.get("type")
                    or ""
                ).strip().lower()
                description = str(product.get("description", "")).lower()

                # If product metadata identifies type, require perpetual.
                type_text = contract_type or description
                if type_text and "perpetual" not in type_text:
                    continue

                status = str(
                    product.get("state") or product.get("status") or ""
                ).strip().lower()
                if status and status not in ("live", "active", "trading"):
                    continue

                found.add(sym)

            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            next_cursor = meta.get("after")
            if not products or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        discovered = sorted(found)
        logger.info("[DISCOVERY] pages=%s discovered=%s", pages, len(discovered))

        if discovered:
            symbols = discovered
            logger.info("[DISCOVERY SYMBOLS] %s", ",".join(symbols))
        else:
            symbols = FALLBACK_SYMBOLS.copy()
            logger.warning("[DISCOVERY FALLBACK] Using known symbols: %s",
                          ",".join(symbols))

        last_discovery_time = now
        with state_lock:
            state["symbols"] = symbols.copy()
            state["total_symbols"] = len(symbols)
        return symbols

    except Exception as exc:
        logger.error("[DISCOVERY ERROR] %s", exc)
        add_error(f"Discovery error: {exc}")

        if symbols:
            logger.warning("[DISCOVERY] Retaining previous symbol list (%s)",
                          len(symbols))
            return symbols

        symbols = FALLBACK_SYMBOLS.copy()
        last_discovery_time = now
        with state_lock:
            state["symbols"] = symbols.copy()
            state["total_symbols"] = len(symbols)
        logger.warning("[DISCOVERY FALLBACK] Using %s symbols", len(symbols))
        return symbols


# ------------------------------------------------------------
# CANDLES / DATA QUALITY
# ------------------------------------------------------------

def candle_quality(frame, resolution, now=None):
    """Inspect recent closed candles; return flags and concise metrics."""
    now = int(now or time.time())
    interval = {"5m": 300, "15m": 900}[resolution]
    flags = []
    if frame.empty:
        return {"ok": False, "flags": ["NO_CANDLES"], "metrics": {}}

    recent = frame.tail(RAW_CANDLE_DIAGNOSTIC_COUNT).copy()
    latest = recent.iloc[-1]
    age = now - int(latest["time"])
    if age > interval * MAX_STALE_CANDLE_MULTIPLE:
        flags.append(f"STALE_LAST_CANDLE({age}s)")

    if len(recent) >= 2:
        times = recent["time"].astype(int).tolist()
        for left, right in zip(times, times[1:]):
            gap = right - left
            if gap > interval * MAX_CANDLE_GAP_MULTIPLE:
                flags.append(f"CANDLE_GAP({gap}s)")
                break

        # Detect suspicious open-to-close and close-to-close movements.
        for _, candle in recent.iterrows():
            move = abs(float(candle["close"]) / float(candle["open"])-1) * 100
            if move > MAX_SINGLE_CANDLE_MOVE_PERCENT:
                flags.append(f"CANDLE_BODY_JUMP({move:.2f}%)")
                break
        closes = recent["close"].astype(float).tolist()
        for a, b in zip(closes, closes[1:]):
            if a > 0:
                move = abs(b / a - 1) * 100
                if move > MAX_CLOSE_TO_CLOSE_MOVE_PERCENT:
                    flags.append(f"CLOSE_JUMP({move:.2f}%)")
                    break

    zero_recent = int((recent["volume"] <= 0).sum())
    if zero_recent:
        flags.append(f"ZERO_VOLUME_RECENT({zero_recent}/{len(recent)})")

    metrics = {
        "latest_candle_age_seconds": age,
        "recent_zero_volume_count": zero_recent,
        "recent_candle_count": len(recent),
        "last_candle_time": int(latest["time"]),
        "last_close": float(latest["close"]),
        "last_volume": float(latest["volume"]),
    }
    return {"ok": not any(flag.startswith(("STALE_LAST_CANDLE", "CANDLE_GAP", "CANDLE_BODY_JUMP", "CLOSE_JUMP")) for flag in flags),
            "flags": flags, "metrics": metrics}


def log_raw_candles(symbol, resolution, frame, quality):
    """Log the last five raw candles only when quality concerns are detected."""
    if not quality.get("flags"):
        return
    key = f"{symbol}:{resolution}"
    now = time.time()
    if now - raw_diagnostic_last_logged.get(key, 0) < 300:
        return  # avoid flooding Render logs; repeat raw snapshot every 5 minutes
    raw_diagnostic_last_logged[key] = now
    rows = frame.tail(RAW_CANDLE_DIAGNOSTIC_COUNT)
    logger.warning("[DATA QUALITY] %s %s flags=%s recent=%s", symbol, resolution,
                  quality.get("flags"), len(rows))
    for _, r in rows.iterrows():
        logger.warning("[RAW CANDLE] %s %s time=%s O=%s H=%s L=%s C=%s V=%s",
                      symbol, resolution, int(r["time"]), r["open"], r["high"],
                      r["low"], r["close"], r["volume"])


def fetch_candles(symbol, resolution, count):
    now = int(time.time())
    candle_seconds = {"5m": 300, "15m": 900}.get(resolution)
    if not candle_seconds:
        raise ValueError(f"Unsupported candle resolution: {resolution}")

    start = now - count * candle_seconds
    payload = api_get(
        "/v2/history/candles",
        params={"resolution": resolution, "symbol": symbol, "start": start, "end": now}
    )
    rows = result_list(payload)
    if not rows:
        raise ValueError("API returned no candle rows")

    frame = pd.DataFrame(rows)
    required = ["time", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"Missing candle columns: {missing}")
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required)
    if not frame.empty and frame["time"].median() > 10_000_000_000:
        frame["time"] = frame["time"] / 1000.0
    frame = frame.sort_values("time").drop_duplicates("time", keep="last")

    frame = frame[(frame["open"] > 0) & (frame["high"] > 0) &
                  (frame["low"] > 0) & (frame["close"] > 0) &
                  (frame["volume"] >= 0) & (frame["high"] >= frame["low"]) &
                  (frame["high"] >= frame["open"]) & (frame["high"] >= frame["close"]) &
                  (frame["low"] <= frame["open"]) & (frame["low"] <= frame["close"])]

    current_bar_open = now - (now % candle_seconds)
    frame = frame[frame["time"] < current_bar_open].reset_index(drop=True)
    if frame.empty:
        raise ValueError("No valid closed candles after filtering")

    quality = candle_quality(frame, resolution, now)
    frame.attrs["quality"] = quality
    log_raw_candles(symbol, resolution, frame, quality)
    logger.debug("[CANDLES] %s %s rows=%s first=%s last=%s", symbol, resolution,
                 len(frame), int(frame["time"].iloc[0]), int(frame["time"].iloc[-1]))
    return frame


# ------------------------------------------------------------
# INDICATORS (EMA, RSI-WILDER, ATR-WILDER, VOLUME MA)
# ------------------------------------------------------------

def calculate_indicators(frame):
    frame = frame.copy()

    for span in (10, 20, 50, 200):
        frame[f"ema{span}"] = frame["close"].ewm(
            span=span, adjust=False, min_periods=span
        ).mean()

    delta = frame["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(
        alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False
    ).mean()
    avg_loss = losses.ewm(
        alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    frame["rsi"] = 100 - (100 / (1 + rs))
    frame.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    frame.loc[(avg_gain == 0) & (avg_loss > 0), "rsi"] = 0
    frame.loc[(avg_gain == 0) & (avg_loss == 0), "rsi"] = 50

    previous_close = frame["close"].shift(1)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous_close).abs(),
        (frame["low"] - previous_close).abs()
    ], axis=1).max(axis=1)
    frame["tr"] = tr
    frame["atr"] = tr.ewm(
        alpha=1 / ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False
    ).mean()

    frame["volume_avg"] = frame["volume"].shift(1).rolling(
        VOLUME_MA_PERIOD, min_periods=VOLUME_MA_PERIOD
    ).mean()

    return frame


# ------------------------------------------------------------
# TREND ANALYSIS
# ------------------------------------------------------------

def analyze_trend(frame):
    if len(frame) < MIN_TREND_CANDLES:
        return {
            "trend": "NOT_READY",
            "reason": f"Insufficient trend candles ({len(frame)}/{MIN_TREND_CANDLES})"
        }

    quality = frame.attrs.get("quality", {"ok": True, "flags": [], "metrics": {}})
    if not quality.get("ok", True):
        return {"trend": "DATA_INVALID", "reason": "TREND_CANDLE_QUALITY:" + "|".join(quality.get("flags", [])),
                "quality": quality}
    enriched = calculate_indicators(frame)
    row = enriched.iloc[-1]

    close = safe_float(row["close"])
    ema50 = safe_float(row["ema50"])
    ema200 = safe_float(row["ema200"])

    if None in (close, ema50, ema200):
        return {"trend": "NOT_READY", "reason": "Trend EMA values invalid"}

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
        "trend_candle_time": int(row["time"]),
        "quality": quality,
        "reason": ""
    }


# ------------------------------------------------------------
# ENTRY ANALYSIS
# ------------------------------------------------------------

def analyze_entry(symbol, trend_data, frame):
    if len(frame) < MIN_ENTRY_CANDLES:
        return {
            "symbol": symbol, "signal": "NONE",
            "reason": f"INSUFFICIENT_ENTRY_CANDLES({len(frame)})",
            "buy_blocked": ["INSUFFICIENT_ENTRY_CANDLES"],
            "sell_blocked": ["INSUFFICIENT_ENTRY_CANDLES"]
        }

    quality = frame.attrs.get("quality", {"ok": True, "flags": [], "metrics": {}})
    enriched = calculate_indicators(frame)
    row = enriched.iloc[-1]

    close = safe_float(row["close"])
    ema10 = safe_float(row["ema10"])
    ema20 = safe_float(row["ema20"])
    rsi = safe_float(row["rsi"])
    atr = safe_float(row["atr"])
    current_volume = safe_float(row["volume"], 0.0)
    volume_avg = safe_float(row["volume_avg"])

    trend = trend_data.get("trend", "NOT_READY")
    buy_reasons, sell_reasons = [], []

    data_quality_block = not quality.get("ok", True)
    if data_quality_block:
        quality_reason = "CANDLE_DATA_INVALID:" + "|".join(quality.get("flags", []))
        buy_reasons.append(quality_reason)
        sell_reasons.append(quality_reason)
    if trend == "DATA_INVALID":
        trend_quality = trend_data.get("quality", {})
        reason = "TREND_DATA_INVALID:" + "|".join(trend_quality.get("flags", []))
        buy_reasons.append(reason)
        sell_reasons.append(reason)

    if close is None or close <= 0:
        buy_reasons.append("INVALID_PRICE")
        sell_reasons.append("INVALID_PRICE")
    if ema10 is None or ema20 is None:
        buy_reasons.append("ENTRY_EMA_NOT_READY")
        sell_reasons.append("ENTRY_EMA_NOT_READY")
    if rsi is None:
        buy_reasons.append("RSI_NOT_READY")
        sell_reasons.append("RSI_NOT_READY")
    atr_percent = (atr / close * 100.0) if atr is not None and close and close > 0 else None
    if atr is None or atr <= 0:
        buy_reasons.append("ATR_NOT_READY")
        sell_reasons.append("ATR_NOT_READY")
    elif atr_percent is not None and atr_percent > MAX_ATR_PERCENT:
        buy_reasons.append(f"ATR_OUTLIER({atr_percent:.2f}%>{MAX_ATR_PERCENT:.2f}%)")
        sell_reasons.append(f"ATR_OUTLIER({atr_percent:.2f}%>{MAX_ATR_PERCENT:.2f}%)")
    if volume_avg is None or volume_avg <= 0:
        buy_reasons.append("VOLUME_AVERAGE_INVALID")
        sell_reasons.append("VOLUME_AVERAGE_INVALID")
    if current_volume <= 0:
        buy_reasons.append("CURRENT_VOLUME_ZERO")
        sell_reasons.append("CURRENT_VOLUME_ZERO")

    volume_ok = (
        current_volume > 0 and volume_avg is not None and volume_avg > 0
        and current_volume >= volume_avg * VOLUME_MULTIPLIER
    )

    if trend != "BULLISH":
        buy_reasons.append(f"TREND_NOT_BULLISH({trend})")
    if ema10 is None or ema20 is None or not ema10 > ema20:
        buy_reasons.append("EMA10_NOT_ABOVE_EMA20")
    if rsi is None or not (BUY_RSI_MIN <= rsi <= BUY_RSI_MAX):
        buy_reasons.append(f"RSI_OUTSIDE_BUY_RANGE({rsi})")
    if not volume_ok:
        buy_reasons.append("VOLUME_FILTER_FAIL")

    if trend != "BEARISH":
        sell_reasons.append(f"TREND_NOT_BEARISH({trend})")
    if ema10 is None or ema20 is None or not ema10 < ema20:
        sell_reasons.append("EMA10_NOT_BELOW_EMA20")
    if rsi is None or not (SELL_RSI_MIN <= rsi <= SELL_RSI_MAX):
        sell_reasons.append(f"RSI_OUTSIDE_SELL_RANGE({rsi})")
    if not volume_ok:
        sell_reasons.append("VOLUME_FILTER_FAIL")

    signal = "NONE"
    if not buy_reasons:
        signal = "BUY"
    elif not sell_reasons:
        signal = "SELL"

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
            if volume_avg is not None and volume_avg > 0 else None
        ),
        "volume_ok": volume_ok,
        "atr": atr,
        "atr_percent": atr_percent,
        "entry_data_quality": quality,
        "trend_data_quality": trend_data.get("quality", {}),
        "signal": signal,
        "buy_blocked": buy_reasons,
        "sell_blocked": sell_reasons,
        "candle_time": int(row["time"]),
        "trend_candle_time": trend_data.get("trend_candle_time"),
        "reason": "ALL_BUY_FILTERS_PASSED" if signal == "BUY"
                  else "ALL_SELL_FILTERS_PASSED" if signal == "SELL"
                  else "FILTERS_BLOCKED"
    }
    return diagnostic


# ------------------------------------------------------------
# SIGNAL HISTORY / TRACKING
# ------------------------------------------------------------

def record_signal(diagnostic):
    symbol = diagnostic.get("symbol")
    signal = diagnostic.get("signal", "NONE")
    now = utc_now()
    previous = active_signals.get(symbol)

    if signal == "NONE":
        if previous:
            signal_history.append({
                "event": "END", "symbol": symbol,
                "signal": previous["signal"],
                "first_seen": previous["first_seen"],
                "last_seen": previous["last_seen"],
                "duration_scans": previous["scans"],
                "ended_at": now,
                "last_price": diagnostic.get("close"),
                "reason": diagnostic.get("reason")
            })
            logger.info("[SIGNAL END] %s %s scans=%s",
                        symbol, previous["signal"], previous["scans"])
            active_signals.pop(symbol, None)
        trim_history()
        return

    if not previous:
        active_signals[symbol] = {
            "symbol": symbol, "signal": signal,
            "first_seen": now, "last_seen": now,
            "scans": 1, "last_price": diagnostic.get("close"),
            "last_rsi": diagnostic.get("rsi"),
            "last_atr": diagnostic.get("atr"),
            "trend": diagnostic.get("trend")
        }
        signal_history.append({
            "event": "START", "symbol": symbol, "signal": signal,
            "time": now, "price": diagnostic.get("close"),
            "rsi": diagnostic.get("rsi"), "atr": diagnostic.get("atr"),
            "trend": diagnostic.get("trend")
        })
        logger.warning("[SIGNAL START] %s %s price=%s RSI=%s ATR=%s",
                      symbol, signal, diagnostic.get("close"),
                      diagnostic.get("rsi"), diagnostic.get("atr"))
        trim_history()
        return

    if previous["signal"] != signal:
        signal_history.append({
            "event": "CHANGE", "symbol": symbol,
            "old_signal": previous["signal"], "new_signal": signal,
            "time": now, "price": diagnostic.get("close")
        })
        logger.warning("[SIGNAL CHANGE] %s %s -> %s",
                      symbol, previous["signal"], signal)
        active_signals[symbol] = {
            "symbol": symbol, "signal": signal,
            "first_seen": now, "last_seen": now,
            "scans": 1, "last_price": diagnostic.get("close"),
            "last_rsi": diagnostic.get("rsi"),
            "last_atr": diagnostic.get("atr"),
            "trend": diagnostic.get("trend")
        }
        trim_history()
        return

    previous.update({
        "last_seen": now,
        "scans": previous.get("scans", 0) + 1,
        "last_price": diagnostic.get("close"),
        "last_rsi": diagnostic.get("rsi"),
        "last_atr": diagnostic.get("atr"),
        "trend": diagnostic.get("trend")
    })

    if previous["scans"] % CHECKPOINT_EVERY == 0:
        signal_history.append({
            "event": "CHECKPOINT", "symbol": symbol, "signal": signal,
            "time": now, "scans": previous["scans"],
            "first_seen": previous["first_seen"],
            "price": diagnostic.get("close"),
            "rsi": diagnostic.get("rsi"), "atr": diagnostic.get("atr")
        })
        logger.info("[SIGNAL CHECKPOINT] %s %s scans=%s",
                    symbol, signal, previous["scans"])
    trim_history()


def trim_history():
    if len(signal_history) > SIGNAL_HISTORY_LIMIT:
        del signal_history[:-SIGNAL_HISTORY_LIMIT]


# ------------------------------------------------------------
# PER-SYMBOL SCAN
# ------------------------------------------------------------

def scan_symbol(symbol):
    try:
        trend_frame = fetch_candles(symbol, TREND_RESOLUTION, TREND_CANDLE_COUNT)
        trend_data = analyze_trend(trend_frame)

        if trend_data["trend"] in ("NOT_READY", "DATA_INVALID"):
            diagnostic = {
                "symbol": symbol, "time": utc_now(),
                "signal": "NONE", "trend": trend_data["trend"],
                "reason": trend_data.get("reason"),
                "buy_blocked": [trend_data.get("reason")],
                "sell_blocked": [trend_data.get("reason")]
            }
            logger.warning("[TREND SKIP] %s %s",
                          symbol, trend_data.get("reason"))
            record_signal(diagnostic)
            last_diagnostics[symbol] = diagnostic
            return diagnostic

        entry_frame = fetch_candles(symbol, ENTRY_RESOLUTION, ENTRY_CANDLE_COUNT)
        diagnostic = analyze_entry(symbol, trend_data, entry_frame)
        last_diagnostics[symbol] = diagnostic

        logger.info(
            "[SIGNAL DIAG] %s TREND=%s CLOSE=%s EMA10=%s EMA20=%s "
            "RSI=%s VOL=%s AVG=%s VOL_RATIO=%s VOL_OK=%s ATR=%s SIGNAL=%s",
            symbol, diagnostic.get("trend"), diagnostic.get("close"),
            diagnostic.get("ema10"), diagnostic.get("ema20"),
            diagnostic.get("rsi"), diagnostic.get("volume"),
            diagnostic.get("volume_avg"), diagnostic.get("volume_ratio"),
            diagnostic.get("volume_ok"), diagnostic.get("atr"),
            diagnostic.get("signal")
        )

        entry_quality = diagnostic.get("entry_data_quality", {})
        if entry_quality.get("flags"):
            logger.warning("[QUALITY SUMMARY] %s entry_flags=%s trend_flags=%s",
                          symbol, entry_quality.get("flags"),
                          diagnostic.get("trend_data_quality", {}).get("flags", []))

        if diagnostic["signal"] == "NONE":
            logger.info("[BLOCKED BUY] %s: %s", symbol,
                       " | ".join(diagnostic.get("buy_blocked", [])))
            logger.info("[BLOCKED SELL] %s: %s", symbol,
                       " | ".join(diagnostic.get("sell_blocked", [])))
        else:
            logger.warning("[VALID SIGNAL] %s %s price=%s RSI=%s",
                           symbol, diagnostic["signal"],
                           diagnostic.get("close"), diagnostic.get("rsi"))

        record_signal(diagnostic)
        return diagnostic

    except Exception as exc:
        error_text = f"{symbol}: {type(exc).__name__}: {exc}"
        logger.error("[SYMBOL ERROR] %s", error_text)
        logger.debug(traceback.format_exc())
        add_error(error_text)

        diagnostic = {
            "symbol": symbol, "time": utc_now(),
            "signal": "NONE", "trend": "ERROR",
            "reason": error_text,
            "buy_blocked": [error_text],
            "sell_blocked": [error_text]
        }
        last_diagnostics[symbol] = diagnostic
        # API error intentionally does not end a previously active signal.
        return diagnostic


# ------------------------------------------------------------
# STATE PERSISTENCE
# ------------------------------------------------------------

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
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, default=str)
        os.replace(temp_file, STATE_FILE)
    except Exception as exc:
        logger.error("[STATE SAVE ERROR] %s", exc)


def load_state():
    global active_signals, signal_history, scan_count, symbols
    try:
        if not os.path.exists(STATE_FILE):
            logger.info("[STATE] No saved state file yet")
            return
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            payload = json.load(file)

        active_signals = payload.get("active_signals", {})
        signal_history = payload.get("signal_history", [])[-SIGNAL_HISTORY_LIMIT:]
        scan_count = payload.get("scan_count", 0)
        saved_symbols = payload.get("symbols", [])
        if saved_symbols:
            symbols = saved_symbols

        logger.info("[STATE RESTORED] active=%s history=%s scan_count=%s",
                    len(active_signals), len(signal_history), scan_count)
    except Exception as exc:
        logger.error("[STATE LOAD ERROR] %s", exc)


# ------------------------------------------------------------
# SCANNER LOOP
# ------------------------------------------------------------

def scanner_loop():
    global scan_count, last_scan_time, last_scan_duration

    logger.info("DELTA INDIA TESTNET SIGNAL SCANNER STARTED - SIGNALS ONLY")
    logger.info("BASE URL: %s | ORDER PLACEMENT: DISABLED", BASE_URL)
    logger.info("TF: trend=%s entry=%s | scan=%ss | discovery=%ss",
                TREND_RESOLUTION, ENTRY_RESOLUTION,
                SCAN_INTERVAL, DISCOVERY_INTERVAL)
    logger.info("FILTERS: EMA50/200 trend | EMA10/20 entry | RSI14 | "
                "volume x%.2f | ATR14", VOLUME_MULTIPLIER)
    logger.info("RSI: BUY %s-%s | SELL %s-%s",
                BUY_RSI_MIN, BUY_RSI_MAX, SELL_RSI_MIN, SELL_RSI_MAX)

    while True:
        scan_started = time.time()
        try:
            current_symbols = discover_symbols()
            if not current_symbols:
                logger.error("[SCAN] No symbols available")
            else:
                buy_count, sell_count = 0, 0
                for symbol in current_symbols:
                    diagnostic = scan_symbol(symbol)
                    if diagnostic.get("signal") == "BUY":
                        buy_count += 1
                    elif diagnostic.get("signal") == "SELL":
                        sell_count += 1

                scan_count += 1
                last_scan_time = utc_now()
                last_scan_duration = round(time.time() - scan_started, 2)

                logger.info(
                    "[SCAN DONE] symbols=%s active_signals=%s BUY=%s SELL=%s "
                    "duration=%.2fs next_scan_in=%ss",
                    len(current_symbols), len(active_signals),
                    buy_count, sell_count, last_scan_duration, SCAN_INTERVAL
                )

                with state_lock:
                    state.update({
                        "bot_status": "RUNNING",
                        "scan_count": scan_count,
                        "last_scan": last_scan_time,
                        "last_scan_duration": last_scan_duration,
                        "active_signals": active_signals.copy(),
                        "signal_history": signal_history[-SIGNAL_HISTORY_LIMIT:],
                        "last_diagnostics": last_diagnostics.copy(),
                        "symbols": current_symbols.copy(),
                        "total_symbols": len(current_symbols)
                    })

                # Save state each completed scan; previously this was never called.
                save_state()

        except Exception as exc:
            logger.error("[SCANNER LOOP ERROR] %s", exc)
            logger.debug(traceback.format_exc())
            add_error(str(exc))
            with state_lock:
                state["bot_status"] = "ERROR"

        elapsed = time.time() - scan_started
        time.sleep(max(1, SCAN_INTERVAL - elapsed))


# ------------------------------------------------------------
# HTTP DASHBOARD / API
# ------------------------------------------------------------

@app.route("/")
def home():
    return Response("""
<!doctype html>
<html><head>
<title>Delta Testnet Signal Scanner</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{background:#10151c;color:#e7edf5;font-family:Arial,sans-serif;padding:16px}
h1{color:#64d8a4}.card{background:#1b2530;padding:15px;margin:10px 0;border-radius:10px}
a{color:#6ec9ff}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style></head><body>
<h1>DELTA INDIA TESTNET</h1><h2>Signal Tracking Scanner</h2>
<div class="card"><b>ORDER PLACEMENT: DISABLED</b><br>SAFETY MODE - SIGNALS ONLY</div>
<div class="card"><a href="/health">Health</a> |
<a href="/dashboard-data">Dashboard Data</a> |
<a href="/signal-history">Signal History</a> |
<a href="/diagnostics">Diagnostics</a></div>
<div class="card"><h3>Scanner Data (refreshes every 10 seconds)</h3><pre id="data">Loading...</pre></div>
<script>
async function update(){try{const r=await fetch('/dashboard-data');const d=await r.json();
document.getElementById('data').textContent=JSON.stringify(d,null,2);
}catch(e){document.getElementById('data').textContent='Dashboard loading error';}}
update();setInterval(update,10000);
</script></body></html>
""", mimetype="text/html")


@app.route("/health")
def health():
    with state_lock:
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
    with state_lock:
        return jsonify({
            "bot_status": state.get("bot_status"),
            "trading_enabled": False,
            "exchange": "DELTA INDIA TESTNET",
            "symbols": symbols.copy(),
            "total_symbols": len(symbols),
            "scan_count": scan_count,
            "last_scan": last_scan_time,
            "last_scan_duration": last_scan_duration,
            "active_signals": active_signals.copy(),
            "signal_history": signal_history[-SIGNAL_HISTORY_LIMIT:],
            "last_diagnostics": last_diagnostics.copy(),
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
                "max_atr_percent": MAX_ATR_PERCENT,
                "volume_ma_period": VOLUME_MA_PERIOD,
                "volume_multiplier": VOLUME_MULTIPLIER,
                "buy_rsi_range": [BUY_RSI_MIN, BUY_RSI_MAX],
                "sell_rsi_range": [SELL_RSI_MIN, SELL_RSI_MAX],
                "scan_interval": SCAN_INTERVAL,
                "discovery_interval": DISCOVERY_INTERVAL
            }
        })


@app.route("/signal-history")
def signal_history_endpoint():
    with state_lock:
        return jsonify({
            "active_signals": active_signals.copy(),
            "history": signal_history[-SIGNAL_HISTORY_LIMIT:],
            "count": len(signal_history),
            "time": utc_now()
        })


@app.route("/diagnostics")
def diagnostics_endpoint():
    with state_lock:
        return jsonify({
            "symbols": symbols.copy(),
            "diagnostics": last_diagnostics.copy(),
            "errors": state.get("errors", [])[-20:],
            "time": utc_now()
        })


# ------------------------------------------------------------
# STARTUP
# ------------------------------------------------------------

def start_scanner():
    load_state()
    state["started_at"] = utc_now()
    state["bot_status"] = "STARTING"
    state["trading_enabled"] = False

    thread = threading.Thread(
        target=scanner_loop, daemon=True, name="delta-scanner"
    )
    thread.start()


if __name__ == "__main__":
    start_scanner()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
