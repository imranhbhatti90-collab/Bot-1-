import os
import time
import hmac
import hashlib
import logging
import requests
import pandas as pd
import numpy as np
import json
from datetime import datetime, timezone
from flask import Flask

# ================== CONFIG ==================
BASE_URL = os.getenv("DELTA_BASE_URL", "https://cdn-ind.testnet.deltaex.org")
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

RSI_PERIOD = 14
OVERBOUGHT = 73
OVERSOLD = 27
ORDER_SIZE = int(os.getenv("DELTA_ORDER_SIZE", "5"))

TAKE_PROFIT_PERCENT = 1.5
STOP_LOSS_PERCENT = 1.0
CHECK_INTERVAL = 30

DEFAULT_COINS = [
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD",
    "ADAUSD", "AVAXUSD", "LINKUSD", "DOTUSD", "MATICUSD",
    "LTCUSD", "ATOMUSD", "NEARUSD", "APTUSD", "ARBUSD",
    "OPUSD", "SUIUSD", "INJUSD", "TIAUSD", "SEIUSD"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)
app = Flask(__name__)

# product_id cache (shuru mein load hoga)
PRODUCT_MAP = {}

# ================== DELTA HELPERS ==================
def generate_signature(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def delta_request(method, endpoint, params=None, data=None):
    timestamp = str(int(time.time()))
    query_string = ""
    if params:
        # Delta ko sorted query string chahiye
        query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    
    path = endpoint
    if query_string:
        path += "?" + query_string

    payload = ""
    if data:
        # Ahem: proper JSON, separators se spaces hatao
        payload = json.dumps(data, separators=(',', ':'))

    signature_data = method + timestamp + path + query_string + payload
    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": generate_signature(API_SECRET, signature_data),
        "Content-Type": "application/json"
    }

    url = BASE_URL + endpoint
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, params=params, timeout=10)
        else:
            response = requests.post(url, headers=headers, data=payload, timeout=10)
        res_json = response.json()
        if not res_json.get("success", True) and res_json.get("error"):
            logger.error(f"API Error: {res_json}")
        return res_json
    except Exception as e:
        logger.error(f"Request error: {e}")
        return {}

def load_product_map():
    """Shuru mein tamam products load karein, symbol -> id mapping banayein"""
    global PRODUCT_MAP
    res = delta_request("GET", "/v2/products")
    products = res.get("result", [])
    for p in products:
        PRODUCT_MAP[p["symbol"]] = p["id"]
    logger.info(f"Loaded {len(PRODUCT_MAP)} products")

def get_ticker(symbol):
    res = delta_request("GET", f"/v2/tickers/{symbol}")
    return res.get("result", {})

def get_candles(symbol, resolution="1m", limit=50):
    res = delta_request("GET", "/v2/history/candles", params={
        "symbol": symbol,
        "resolution": resolution,
        "limit": limit
    })
    return res.get("result", [])

def get_open_positions():
    res = delta_request("GET", "/v2/positions")
    return res.get("result", [])

def place_order(product_id, side, size, order_type="market_order"):
    data = {
        "product_id": product_id,
        "size": size,
        "side": side,
        "order_type": order_type
    }
    res = delta_request("POST", "/v2/orders", data=data)
    if res.get("result"):
        logger.info(f"✅ Order Placed → {side.upper()} | Size: {size} | Product ID: {product_id}")
    else:
        logger.error(f"❌ Order Failed: {res}")
    return res

def close_position(product_id, size, side):
    close_side = "sell" if side == "buy" else "buy"
    return place_order(product_id, close_side, size)

# ================== RSI + SL/TP LOGIC ==================
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(window=period).mean().iloc[-1]
    avg_loss = pd.Series(losses).rolling(window=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def manage_open_positions():
    positions = get_open_positions()
    for pos in positions:
        try:
            product_id = pos.get("product_id")
            size = abs(float(pos.get("size", 0)))
            entry_price = float(pos.get("entry_price", 0))
            side = "buy" if float(pos.get("size", 0)) > 0 else "sell"
            if size == 0 or entry_price == 0:
                continue

            symbol = pos.get("product_symbol") or pos.get("symbol")
            ticker = get_ticker(symbol)
            current_price = float(ticker.get("mark_price") or ticker.get("close", 0))
            if current_price == 0:
                continue

            pnl_percent = ((current_price - entry_price) / entry_price * 100) if side == "buy" else ((entry_price - current_price) / entry_price * 100)
            logger.info(f"Position → {symbol} | Side: {side} | PnL: {pnl_percent:.2f}%")

            if pnl_percent >= TAKE_PROFIT_PERCENT:
                logger.info(f"🎯 Take Profit → {symbol}")
                close_position(product_id, size, side)
            elif pnl_percent <= -STOP_LOSS_PERCENT:
                logger.info(f"🛑 Stop Loss → {symbol}")
                close_position(product_id, size, side)
        except Exception as e:
            logger.error(f"Error managing position: {e}")

def check_and_trade(symbol):
    candles = get_candles(symbol)
    if not candles or len(candles) < RSI_PERIOD + 5:
        return

    closes = [float(c["close"]) for c in candles]
    rsi = calculate_rsi(closes, RSI_PERIOD)
    if rsi is None:
        return

    product_id = PRODUCT_MAP.get(symbol)
    if not product_id:
        logger.warning(f"Product ID not found for {symbol}")
        return

    logger.info(f"{symbol} → RSI: {rsi}")

    if rsi >= OVERBOUGHT:
        place_order(product_id, "sell", ORDER_SIZE)
    elif rsi <= OVERSOLD:
        place_order(product_id, "buy", ORDER_SIZE)

# ================== MAIN LOOP ==================
def run_bot():
    load_product_map()  # Ahem: pehle products load karein
    logger.info("🚀 RSI Bot Started (SL/TP enabled)")
    while True:
        try:
            manage_open_positions()
            for symbol in DEFAULT_COINS:
                check_and_trade(symbol)
                time.sleep(1)
        except Exception as e:
            logger.error(f"Main loop error: {e}")
        time.sleep(CHECK_INTERVAL)

@app.route("/")
def home():
    return "Delta RSI Bot is running!"

if __name__ == "__main__":
    import threading
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
