import os
import time
import json
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlencode

import requests
import pandas as pd
from flask import Flask, jsonify

# ==================================================
# CONFIGURATION
# ==================================================

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
TIMEFRAME_MINUTES = 5

EMA_FAST = 9
EMA_SLOW = 21

ORDER_SIZE = 40
LEVERAGE = 50

RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20
MIN_VOLUME_MULTIPLIER = 1.0 

ENABLE_TRADING = True
POLL_SECONDS = 20
STATE_FILE = "state.json"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

log = logging.getLogger("delta-multicoin-bot")
session = requests.Session()
app = Flask(__name__)

# State ab multiple coins ke liye track karega
state = {
    "positions": {},          # symbol -> position details
    "last_processed_candle": {}, # symbol -> candle_id
    "best_price": {},         # symbol -> best price for trailing
}

bot_status = {
    "running": True,
    "trading_enabled": ENABLE_TRADING,
    "last_signal": "NONE",
    "last_error": None,
}


# ==================================================
# STATE MANAGEMENT
# ==================================================

def load_state():
    global state

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)

            if isinstance(saved, dict):
                state.update(saved)

        except Exception as exc:
            log.error("State file read failed: %s", exc)

    state.setdefault("positions", {})
    state.setdefault("last_processed_candle", {})
    state.setdefault("best_price", {})


def save_state():
    try:
        temp_file = STATE_FILE + ".tmp"

        with open(temp_file, "w") as f:
            json.dump(state, f, indent=2)

        os.replace(temp_file, STATE_FILE)

    except Exception as exc:
        log.error("State save failed: %s", exc)


# ==================================================
# DELTA API REQUEST
# ==================================================

def api_request(method, path, params=None, body=None, signed=False):
    method = method.upper()
    params = params or {}

    query = urlencode(params)
    request_path = path + (("?" + query) if query else "")

    body_text = (
        json.dumps(body, separators=(",", ":"))
        if body is not None else ""
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if signed:
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "DELTA_API_KEY ya DELTA_API_SECRET missing hai."
            )

        timestamp = str(int(time.time()))

        signature_data = (
            method + timestamp + request_path + body_text
        )

        signature = hmac.new(
            API_SECRET.encode(),
            signature_data.encode(),
            hashlib.sha256,
        ).hexdigest()

        headers.update({
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": signature,
        })

    response = session.request(
        method,
        BASE_URL + request_path,
        headers=headers,
        data=body_text if body is not None else None,
        timeout=20,
    )

    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text}

    if not response.ok or data.get("success") is False:
        raise RuntimeError(
            f"Delta API HTTP {response.status_code}: {data}"
        )

    return data


# ==================================================
# FETCH ALL ACTIVE SYMBOLS
# ==================================================

def get_all_active_products():
    try:
        data = api_request("GET", "/v2/products")
        products = data.get("result", [])
        active_products = [
            p for p in products 
            if p.get("state") == "active" and "USD" in p.get("symbol", "")
        ]
        return active_products
    except Exception as exc:
        log.error("Failed to fetch products list: %s", exc)
        return []


# ==================================================
# FETCH CANDLES FOR ANY SYMBOL
# ==================================================

def fetch_market_candles(symbol):
    end_time = int(time.time())
    start_time = end_time - (24 * 60 * 60)

    params = {
        "resolution": str(TIMEFRAME_MINUTES),
        "symbol": symbol,
        "start": start_time,
        "end": end_time,
    }

    data = api_request("GET", "/v2/history/candles", params=params)
    candles = data.get("result", [])

    if not candles:
        raise RuntimeError(f"Delta candle response empty for {symbol}.")

    df = pd.DataFrame(candles)
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Candle response columns missing for {symbol}: {missing}")

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    df = (
        df.dropna(subset=["time", "close"])
        .sort_values("time")
        .drop_duplicates(subset=["time"])
        .reset_index(drop=True)
    )

    now = int(time.time())
    current_candle_start = (
        now // (TIMEFRAME_MINUTES * 60)
    ) * (TIMEFRAME_MINUTES * 60)

    df = df[
        pd.to_numeric(df["time"], errors="coerce")
        < current_candle_start
    ].reset_index(drop=True)

    if len(df) < max(EMA_SLOW + 3, VOLUME_MA_PERIOD + 2):
        raise RuntimeError(f"Closed candles insufficient for {symbol}: {len(df)}")

    return df


# ==================================================
# FILTERS & SIGNAL SCANNER
# ==================================================

def check_market_filters(df, side, symbol):
    if len(df) < max(RSI_PERIOD, VOLUME_MA_PERIOD) + 2:
        return False

    close = df["close"]
    delta = close.diff()

    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.rolling(RSI_PERIOD).mean()
    avg_loss = losses.rolling(RSI_PERIOD).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    current_rsi = rsi.iloc[-1]

    if side == "buy" and current_rsi < 40:
        log.info("[%s] BUY filtered: RSI (%.2f) < 40", symbol, current_rsi)
        return False

    if side == "sell" and current_rsi > 60:
        log.info("[%s] SELL filtered: RSI (%.2f) > 60", symbol, current_rsi)
        return False

    return True


def scan_symbol(symbol):
    df = fetch_market_candles(symbol)

    df["ema9"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    previous = df.iloc[-2]
    latest = df.iloc[-1]

    price = float(latest["close"])
    ema9 = float(latest["ema9"])
    ema21 = float(latest["ema21"])
    candle_id = str(latest["time"])

    # Har coin ka live price aur EMA logs mein print hoga
    log.info(
        "[MARKET] %s price=%.2f EMA9=%.2f EMA21=%.2f",
        symbol, price, ema9, ema21
    )

    buy_signal = (
        previous["ema9"] <= previous["ema21"]
        and latest["ema9"] > latest["ema21"]
    )

    sell_signal = (
        previous["ema9"] >= previous["ema21"]
        and latest["ema9"] < latest["ema21"]
    )

    if buy_signal:
        side = "buy"
    elif sell_signal:
        side = "sell"
    else:
        side = None

    return df, {
        "symbol": symbol,
        "candle_id": candle_id,
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "side": side,
    }


# ==================================================
# ORDER EXECUTION
# ==================================================

def place_order(product_id, symbol, side, entry_price):
    try:
        body = {
            "product_id": product_id,
            "size": ORDER_SIZE,
            "side": side,
            "order_type": "market_order",
            "leverage": LEVERAGE
        }

        log.info("[ORDER] Placing %s market order for %s...", side.upper(), symbol)
        res = api_request("POST", "/v2/orders", body=body, signed=True)
        log.info("[ORDER SUCCESS][%s] Response: %s", symbol, res)
        
        state["positions"][symbol] = {
            "side": side,
            "entry": entry_price,
            "size": ORDER_SIZE
        }
        state["best_price"][symbol] = entry_price
        save_state()

    except Exception as exc:
        log.error("[ORDER FAILED][%s] Error placing order: %s", symbol, exc)


def check_and_manage_positions():
    try:
        tracked_symbols = list(state["positions"].keys())
        if not tracked_symbols:
            return

        for sym in tracked_symbols:
            asset = sym.replace("USD", "").replace("USDT", "")
            params = {"underlying_asset_symbol": asset}
            
            res = api_request("GET", "/v2/positions", params=params, signed=True)
            positions = res.get("result", [])
            
            has_position = False
            for p in positions:
                p_sym = p.get("product_symbol") or p.get("symbol")
                size = float(p.get("size", 0))
                if p_sym == sym and size != 0:
                    has_position = True
                    break
            
            if not has_position:
                log.info("[POSITION] Position for %s closed on exchange.", sym)
                state["positions"].pop(sym, None)
                state["best_price"].pop(sym, None)
                save_state()

    except Exception as exc:
        log.error("Positions management error: %s", exc)


# ==================================================
# SIGNAL HANDLER
# ==================================================

def handle_signal(product, df, signal):
    symbol = signal["symbol"]
    side = signal["side"]
    if not side:
        return

    candle_id = signal["candle_id"]
    if candle_id == state["last_processed_candle"].get(symbol):
        return

    state["last_processed_candle"][symbol] = candle_id
    save_state()

    if not check_market_filters(df, side, symbol):
        return

    if symbol in state["positions"]:
        return

    if not ENABLE_TRADING:
        log.info("[%s SAFE MODE] Signal found but trading disabled.", symbol)
        return

    place_order(product["id"], symbol, side, signal["price"])


# ==================================================
# MAIN BOT LOOP (MULTI-COIN)
# ==================================================

def bot_loop():
    log.info("Delta Multi-Coin EMA Bot started successfully.")
    while True:
        try:
            check_and_manage_positions()
            products = get_all_active_products()

            for product in products:
                symbol = product.get("symbol")
                try:
                    df, signal = scan_symbol(symbol)
                    bot_status["last_signal"] = f"{symbol}:{signal['side'].upper() if signal['side'] else 'NONE'}"
                    bot_status["last_error"] = None

                    handle_signal(product, df, signal)
                except Exception as ex:
                    pass

                time.sleep(1)

        except Exception as exc:
            bot_status["last_error"] = str(exc)
            log.exception("Bot loop error")

        time.sleep(POLL_SECONDS)


# ==================================================
# RENDER HEALTH CHECK
# ==================================================

@app.route("/")
def health_check():
    return jsonify({
        "service": "Delta Multi-Coin EMA Bot",
        "status": "running",
        "trading_enabled": ENABLE_TRADING,
    })

@app.route("/status")
def status():
    return jsonify(bot_status)

@app.route("/check")
def check():
    try:
        products = get_all_active_products()
        symbols = [p.get("symbol") for p in products]
        return jsonify({"ok": True, "active_symbols": symbols})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ==================================================
# START
# ==================================================

load_state()

if __name__ == "__main__":
    threading.Thread(
        target=bot_loop,
        daemon=True,
        name="delta-multicoin-bot",
    ).start()

    port = int(os.environ.get("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False

# =========================================================
# START
# =========================================================

load_state()

if __name__ == "__main__":
    threading.Thread(
        target=bot_loop,
        daemon=True,
        name="delta-multicoin-bot",
    ).start()

    port = int(os.environ.get("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )
