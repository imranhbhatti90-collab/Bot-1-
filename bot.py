import os
import time
import json
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlencode, quote

import requests
import pandas as pd
from flask import Flask, jsonify

# =========================================================
# DELTA EXCHANGE INDIA TESTNET BOT - FINAL FIXED
# =========================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"

TIMEFRAME_MINUTES = 5
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20

ORDER_SIZE = 40
LEVERAGE = 50
MIN_VOLUME_MULTIPLIER = 1.0
TRAILING_STOP_PERCENT = 0.5
ENABLE_TRADING = True
POLL_SECONDS = 20
STATE_FILE = "state.json"
REQUEST_TIMEOUT = 15
CLOSE_RETRY_SECONDS = 30

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
app = Flask(__name__)
session = requests.Session()
bot_lock = threading.RLock()

bot_status = {
    "running": False,
    "trading_enabled": ENABLE_TRADING,
    "last_scan": None,
    "last_error": None,
    "products_scanned": 0,
    "last_signal": None
}

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logging.error("State file read failed: %s", exc)
        return {}

state = load_state()

def save_state():
    temp_file = STATE_FILE + ".tmp"
    try:
        with open(temp_file, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(temp_file, STATE_FILE)
    except Exception as exc:
        logging.error("State save failed: %s", exc)

def api_request(method, path, params=None, payload=None, auth=True):
    method = method.upper()
    params = params or {}
    query_string = urlencode(sorted(params.items()), doseq=True, quote_via=quote)
    request_path = path + (("?" + query_string) if query_string else "")
    body_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
    headers = {"Content-Type": "application/json"}
    if auth:
        if not API_KEY or not API_SECRET:
            raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET missing")
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + request_path + body_text
        signature = hmac.new(API_SECRET.encode("utf-8"), signature_data.encode("utf-8"), hashlib.sha256).hexdigest()
        headers.update({"api-key": API_KEY, "timestamp": timestamp, "signature": signature})
    url = BASE_URL + path
    response = session.request(method=method, url=url, params=params if params else None, data=body_text if body_text else None, headers=headers, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} {path}: {response.text[:500]}")
    try:
        result = response.json()
    except ValueError:
        raise RuntimeError(f"Non-JSON response: {response.text[:300]}")
    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(f"API rejected {path}: {result}")
    return result

def result_data(result):
    if isinstance(result, dict):
        return result.get("result", result)
    return result

def get_all_active_products():
    result = api_request("GET", "/v2/products", auth=False)
    products = result_data(result)
    if isinstance(products, dict):
        products = products.get("products", [])
    if not isinstance(products, list):
        raise RuntimeError("Unexpected products API response")
    active = []
    for product in products:
        if not isinstance(product, dict):
            continue
        symbol = str(product.get("symbol", "")).upper()
        status = str(product.get("state", "")).lower()
        if status != "active":
            continue
        if "USD" not in symbol:
            continue
        if product.get("id") is None:
            continue
        active.append(product)
    return active

def fetch_market_candles(symbol):
    result = api_request("GET", "/v2/history/candles", params={"symbol": symbol, "resolution": f"{TIMEFRAME_MINUTES}m", "start": int(time.time()) - (60 * TIMEFRAME_MINUTES * 350), "end": int(time.time())}, auth=False)
    candles = result_data(result)
    if isinstance(candles, dict):
        candles = candles.get("candles", [])
    if not isinstance(candles, list) or len(candles) < 220:
        raise RuntimeError(f"{symbol}: insufficient candle data")
    df = pd.DataFrame(candles)
    if df.empty:
        raise RuntimeError(f"{symbol}: candle data is empty")
    aliases = {"time": ["time", "timestamp", "t"], "open": ["open", "o"], "high": ["high", "h"], "low": ["low", "l"], "close": ["close", "c"], "volume": ["volume", "v"]}
    rename = {}
    for target, options in aliases.items():
        for option in options:
            if option in df.columns:
                rename[option] = target
                break
    df = df.rename(columns=rename)
    if "close" not in df.columns:
        raise RuntimeError(f"{symbol}: missing candle field close")
    for column in ["close", "volume"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    if "time" in df.columns:
        df = df.sort_values("time")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if "time" in df.columns and len(df) > 2:
        try:
            latest_time = float(df["time"].iloc[-1])
            if latest_time > 10**12:
                latest_time /= 1000
            if time.time() - latest_time < TIMEFRAME_MINUTES * 60:
                df = df.iloc[:-1].reset_index(drop=True)
        except:
            pass
    if len(df) < 220:
        raise RuntimeError(f"{symbol}: not enough completed candles")
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))
    df["volume_ma"] = df["volume"].rolling(VOLUME_MA_PERIOD).mean() if "volume" in df.columns else float("nan")
    return df

def check_market_filters(df, side):
    row = df.iloc[-1]
    rsi = row.get("rsi")
    if pd.isna(rsi):
        return False, "RSI not available"
    if side == "buy" and not (50 <= rsi <= 75):
        return False, f"BUY RSI filter rejected: {rsi:.2f}"
    if side == "sell" and not (25 <= rsi <= 50):
        return False, f"SELL RSI filter rejected: {rsi:.2f}"
    volume = row.get("volume")
    volume_ma = row.get("volume_ma")
    if volume is not None and volume_ma is not None and not pd.isna(volume) and not pd.isna(volume_ma) and volume_ma > 0:
        if volume < volume_ma * MIN_VOLUME_MULTIPLIER:
            return False, f"Volume filter rejected: {volume:.2f} < {(volume_ma * MIN_VOLUME_MULTIPLIER):.2f}"
    return True, "Filters passed"

def scan_symbol(symbol):
    df = fetch_market_candles(symbol)
    previous = df.iloc[-2]
    current = df.iloc[-1]
    if previous["ema_fast"] <= previous["ema_slow"] and current["ema_fast"] > current["ema_slow"]:
        side = "buy"
    elif previous["ema_fast"] >= previous["ema_slow"] and current["ema_fast"] < current["ema_slow"]:
        side = "sell"
    else:
        return None, df, "No EMA crossover"
    passed, reason = check_market_filters(df, side)
    if not passed:
        return None, df, reason
    candle_id = str(current["time"]) if "time" in df.columns else str(len(df))
    return {"symbol": symbol, "side": side, "price": float(current["close"]), "candle_id": candle_id, "rsi": float(current["rsi"])}, df, reason

# =========================================================
# FIXED POSITION FETCH - YAHI MAIN FIX HAI
# =========================================================
def get_all_positions():
    all_positions = []
    # 1. Try margined endpoint (India me ye kaam karta hai)
    try:
        result = api_request("GET", "/v2/positions/margined", auth=True)
        positions = result_data(result)
        if isinstance(positions, dict):
            positions = positions.get("positions", positions.get("result", []))
        if isinstance(positions, list) and len(positions) > 0:
            return positions
    except Exception as e:
        logging.info(f"margined positions try failed: {e}")

    # 2. Underlying se fetch karo (BTC, ETH, SOL...)
    underlying_assets = set()
    # State me jo symbols hain unse underlying nikalo
    for s in list(state.keys()):
        # BTCUSD -> BTC, ETHUSD -> ETH
        base = s.replace("USD", "").replace("USDT", "").replace("PERP", "")
        # Common assets
        for asset in ["BTC", "ETH", "SOL", "XRP", "BNB", "AVAX", "MATIC", "DOGE", "SHIB", "LTC"]:
            if asset in base:
                underlying_assets.add(asset)
    
    if not underlying_assets:
        underlying_assets = {"BTC", "ETH", "SOL", "XRP", "BNB"}

    for underlying in underlying_assets:
        try:
            result = api_request("GET", "/v2/positions", params={"underlying_asset_symbol": underlying}, auth=True)
            positions = result_data(result)
            if isinstance(positions, dict):
                positions = positions.get("positions", [])
            if isinstance(positions, list):
                all_positions.extend(positions)
        except Exception as exc:
            logging.warning(f"Position fetch for {underlying} failed: {exc}")
            continue

    return all_positions

def get_positions_for_symbol(symbol, all_positions=None):
    if all_positions is None:
        all_positions = get_all_positions()
    result = []
    for p in all_positions:
        if not isinstance(p, dict):
            continue
        prod_sym = str(p.get("product_symbol", p.get("symbol", ""))).upper()
        if prod_sym == symbol.upper() and abs(float(p.get("size", 0) or 0)) > 0:
            result.append(p)
    return result

def position_side(position):
    side = str(position.get("side", "")).lower()
    if side in ("buy", "long"): return "buy"
    if side in ("sell", "short"): return "sell"
    size = float(position.get("size", 0) or 0)
    return "buy" if size > 0 else "sell" if size < 0 else None

def position_size(position):
    return abs(int(float(position.get("size", 0) or 0)))

def sync_positions_with_exchange(products):
    try:
        all_pos = get_all_positions()
    except Exception as exc:
        logging.warning("Startup position sync failed: %s", exc)
        return

    for product in products:
        symbol = product.get("symbol")
        if not symbol:
            continue
        positions = get_positions_for_symbol(symbol, all_pos)
        if not positions:
            if symbol in state:
                logging.info("%s: exchange reports no open position; removing stale local state", symbol)
                state.pop(symbol, None)
                save_state()
            continue
        if len(positions) > 1:
            logging.warning("%s: multiple positions found; skip", symbol)
            continue
        p = positions[0]
        side = position_side(p)
        entry_price = p.get("entry_price") or p.get("average_entry_price")
        product_id = p.get("product_id") or product.get("id")
        if side is None or not entry_price or not product_id:
            continue
        old = state.get(symbol, {})
        state[symbol] = {
            "symbol": symbol,
            "product_id": int(product_id),
            "side": side,
            "entry_price": float(entry_price),
            "size": position_size(p),
            "highest_price": max(float(old.get("highest_price", entry_price)), float(entry_price)),
            "lowest_price": min(float(old.get("lowest_price", entry_price)), float(entry_price)),
            "entry_pending": False,
            "close_pending_at": old.get("close_pending_at"),
            "recovered": True
        }
        save_state()

def place_order(product, side, price, candle_id, rsi):
    symbol = product["symbol"]
    product_id = int(product["id"])
    if not ENABLE_TRADING:
        logging.info("[SAFE MODE] %s %s signal; order not sent", side.upper(), symbol)
        return False
    if symbol in state:
        logging.info("%s: local position already exists", symbol)
        return False
    payload = {"product_id": product_id, "size": ORDER_SIZE, "side": side, "order_type": "market_order", "leverage": LEVERAGE}
    try:
        result = api_request("POST", "/v2/orders", payload=payload, auth=True)
        logging.info("[ORDER RESPONSE] %s %s: %s", side.upper(), symbol, result)
        state[symbol] = {"symbol": symbol, "product_id": product_id, "side": side, "entry_price": float(price), "size": ORDER_SIZE, "highest_price": float(price), "lowest_price": float(price), "entry_pending": True, "entry_pending_at": time.time(), "close_pending_at": None, "signal_candle_id": str(candle_id), "rsi_at_entry": float(rsi)}
        save_state()
        return True
    except Exception as exc:
        bot_status["last_error"] = str(exc)
        logging.error("Order placement failed for %s: %s", symbol, exc)
        return False

def close_position(symbol, position, reason):
    if not ENABLE_TRADING:
        logging.info("[SAFE MODE] Would close %s: %s", symbol, reason)
        return False
    current_size = position_size(position)
    if current_size <= 0:
        return False
    side = position_side(position)
    if side not in ("buy", "sell"):
        return False
    close_side = "sell" if side == "buy" else "buy"
    product_id = position.get("product_id") or state.get(symbol, {}).get("product_id")
    if not product_id:
        return False
    payload = {"product_id": int(product_id), "size": current_size, "side": close_side, "order_type": "market_order", "reduce_only": True}
    try:
        result = api_request("POST", "/v2/orders", payload=payload, auth=True)
        logging.warning("[CLOSE ORDER SUBMITTED] %s reason=%s response=%s", symbol, reason, result)
        if symbol in state:
            state[symbol]["close_pending_at"] = time.time()
            state[symbol]["close_reason"] = reason
            save_state()
        return True
    except Exception as exc:
        bot_status["last_error"] = str(exc)
        logging.error("Close order failed for %s: %s", symbol, exc)
        return False

def get_mark_price(position):
    for key in ("mark_price", "markPrice", "last_price"):
        value = position.get(key)
        try:
            if value not in (None, "", 0, "0") and float(value) > 0:
                return float(value)
        except:
            pass
    return None

def check_and_manage_positions(products):
    try:
        all_pos = get_all_positions()
    except Exception as exc:
        logging.warning("Position check failed; keeping state: %s", exc)
        return

    for symbol in list(state.keys()):
        positions = get_positions_for_symbol(symbol, all_pos)
        if not positions:
            logging.info("%s: exchange confirms no open position", symbol)
            state.pop(symbol, None)
            save_state()
            continue
        if len(positions) > 1:
            continue
        position = positions[0]
        info = state.get(symbol, {})
        exchange_side = position_side(position)
        if exchange_side not in ("buy", "sell"):
            continue
        entry = position.get("entry_price") or position.get("average_entry_price") or info.get("entry_price")
        try:
            entry = float(entry)
        except:
            continue
        info["side"] = exchange_side
        info["entry_price"] = entry
        info["size"] = position_size(position)
        info["entry_pending"] = False
        if info.get("close_pending_at"):
            elapsed = time.time() - float(info["close_pending_at"])
            if elapsed < CLOSE_RETRY_SECONDS:
                save_state()
                continue
            info["close_pending_at"] = None
        mark_price = get_mark_price(position)
        if mark_price is None:
            save_state()
            continue
        highest = float(info.get("highest_price", entry))
        lowest = float(info.get("lowest_price", entry))
        if exchange_side == "buy":
            highest = max(highest, mark_price, entry)
            info["highest_price"] = highest
            trailing_price = highest * (1 - TRAILING_STOP_PERCENT / 100)
            logging.info("[TRAIL LONG] %s mark=%.4f high=%.4f trigger=%.4f", symbol, mark_price, highest, trailing_price)
            save_state()
            if mark_price <= trailing_price:
                close_position(symbol, position, f"Long trailing stop: mark={mark_price:.4f}, trigger={trailing_price:.4f}")
        else:
            lowest = min(lowest, mark_price, entry)
            info["lowest_price"] = lowest
            trailing_price = lowest * (1 + TRAILING_STOP_PERCENT / 100)
            logging.info("[TRAIL SHORT] %s mark=%.4f low=%.4f trigger=%.4f", symbol, mark_price, lowest, trailing_price)
            save_state()
            if mark_price >= trailing_price:
                close_position(symbol, position, f"Short trailing stop: mark={mark_price:.4f}, trigger={trailing_price:.4f}")

def bot_loop():
    bot_status["running"] = True
    logging.info("BOT STARTED | trading=%s | size=%s | leverage=%sx", ENABLE_TRADING, ORDER_SIZE, LEVERAGE)
    while True:
        try:
            products = get_all_active_products()
            bot_status["products_scanned"] = len(products)
            sync_positions_with_exchange(products)
            check_and_manage_positions(products)
            for product in products:
                symbol = product.get("symbol")
                if not symbol or symbol in state:
                    continue
                try:
                    signal, df, reason = scan_symbol(symbol)
                    if signal is None:
                        continue
                    bot_status["last_signal"] = {"symbol": symbol, "side": signal["side"], "price": signal["price"], "time": time.time()}
                    logging.warning("[SIGNAL] %s %s price=%.4f RSI=%.2f", symbol, signal["side"].upper(), signal["price"], signal["rsi"])
                    place_order(product, signal["side"], signal["price"], signal["candle_id"], signal["rsi"])
                except Exception as exc:
                    logging.error("Scan failed for %s: %s", symbol, exc)
                    bot_status["last_error"] = str(exc)
                time.sleep(0.5)
            bot_status["last_scan"] = time.time()
        except Exception as exc:
            bot_status["last_error"] = str(exc)
            logging.exception("Main bot loop error")
        time.sleep(POLL_SECONDS)

@app.route("/")
def home():
    return jsonify({"bot": "Delta Testnet EMA Bot - FIXED", "trading_enabled": ENABLE_TRADING, "timeframe_minutes": TIMEFRAME_MINUTES, "status": "running" if bot_status["running"] else "starting"})

@app.route("/status")
def status():
    return jsonify({**bot_status, "open_or_pending_symbols": list(state.keys()), "positions_count": len(state)})

@app.route("/check")
def check():
    return jsonify({"api_key_present": bool(API_KEY), "api_secret_present": bool(API_SECRET), "base_url": BASE_URL, "trading_enabled": ENABLE_TRADING})

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True, name="delta-testnet-bot").start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
