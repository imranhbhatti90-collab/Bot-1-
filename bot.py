import time
import logging
import pandas as pd
import json
import os
import threading
from flask import Flask

# Logging configuration setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")

# ==================================================
# CONFIGURATION & CONSTANTS
# ==================================================
ENABLE_TRADING = True
SYMBOL = "BTCUSD"
ORDER_SIZE = 40
LEVERAGE = 50

# Dynamic Trailing & Filter Constants
TRAILING_BUFFER_POINTS = 200  
RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20         
MIN_VOLUME_MULTIPLIER = 1.2   


# ==================================================
# STATE INITIALIZATION
# ==================================================
STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(current_state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(current_state, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to save state: {e}")

# Global state object load kar rahe hain
state = load_state()
if "position" not in state:
    state["position"] = None


# ==================================================
# 1. RSI & VOLUME CONFIRMATION FILTERS
# ==================================================
def check_market_filters(df, side):
    if len(df) < max(RSI_PERIOD, VOLUME_MA_PERIOD) + 1:
        return True  

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    current_rsi = df['rsi'].iloc[-1]

    df['vol_ma'] = df['volume'].rolling(window=VOLUME_MA_PERIOD).mean()
    current_volume = df['volume'].iloc[-1]
    avg_volume = df['vol_ma'].iloc[-1]

    logging.info(
        "[FILTER CHECK] Side: %s | RSI: %.2f | Vol: %.1f (Avg: %.1f)",
        side.upper(), current_rsi, current_volume, avg_volume
    )

    if side == "buy" and current_rsi < 50:
        logging.info("[FILTER BLOCKED] Buy signal rejected: RSI (%.2f) is below 50", current_rsi)
        return False
    if side == "sell" and current_rsi > 50:
        logging.info("[FILTER BLOCKED] Sell signal rejected: RSI (%.2f) is above 50", current_rsi)
        return False

    if current_volume < (avg_volume * MIN_VOLUME_MULTIPLIER):
        logging.info(
            "[FILTER BLOCKED] %s signal rejected: Volume (%.1f) is too low",
            side.upper(), current_volume
        )
        return False

    logging.info("[FILTER PASSED] %s signal approved!", side.upper())
    return True


# ==================================================
# 2. DYNAMIC CONTINUOUS TRAILING STOP-LOSS
# ==================================================
def check_and_update_dynamic_trailing(product, position):
    current_price = get_latest_price(product["symbol"])
    if not current_price:
        return

    side = position["side"]
    entry_price = float(position["entry"])
    size = position["size"]
    stop_order_id = position.get("stop_order_id")
    current_stop_price = float(position.get("stop_price", 0))

    if side == "buy":
        profit_points = current_price - entry_price
        new_stop_price = current_price - TRAILING_BUFFER_POINTS
    else:  
        profit_points = entry_price - current_price
        new_stop_price = current_price + TRAILING_BUFFER_POINTS

    if profit_points > TRAILING_BUFFER_POINTS:
        should_update = False
        
        if side == "buy" and new_stop_price > current_stop_price:
            should_update = True
        elif side == "sell" and (current_stop_price == 0 or new_stop_price < current_stop_price):
            should_update = True

        if should_update:
            tick_size = float(product["tick_size"])
            new_stop_price = round(new_stop_price / tick_size) * tick_size
            
            logging.info(
                "[DYNAMIC TRAIL] Profit: %.1f pts | Moving SL to %.1f",
                profit_points, new_stop_price
            )
            
            if stop_order_id:
                cancel_order(stop_order_id)
                
            stop_result = place_stop_order(product, side, size, new_stop_price)
            new_stop_order = stop_result.get("result", {})
            new_stop_id = new_stop_order.get("id")
            
            if new_stop_id:
                position["stop_order_id"] = new_stop_id
                position["stop_price"] = str(new_stop_price)
                save_state(state)
                logging.info("[SL UPDATED] New ID: %s, Price: %.1f", new_stop_id, new_stop_price)


# ==================================================
# 3. MARKET ENTRY
# ==================================================
def place_entry(product, side):
    if not ENABLE_TRADING:
        logging.warning("[SAFE MODE] ENABLE_TRADING=false; no order sent.")
        return

    if not API_KEY or not API_SECRET:
        raise RuntimeError("API credentials missing")

    if state.get("position"):
        logging.warning("Saved bot position exists; refusing another entry")
        return

    current = get_open_position()
    if current:
        logging.warning("An exchange position is already open; refusing entry")
        return

    set_leverage(product)
    product_id = int(product["id"])

    body = {
        "product_id": product_id,
        "product_symbol": SYMBOL,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "market_order",
        "time_in_force": "gtc",
        "reduce_only": False,
        "client_order_id": f"ema921_{int(time.time())}",
    }

    logging.info("[ENTRY REQUEST] %s", body)
    result = api_request("POST", "/v2/orders", body=body)
    order = result.get("result", {})
    order_id = order.get("id")

    position = None
    for _ in range(20):
        time.sleep(1)
        position = get_open_position()
        if position:
            break

    if not position:
        raise RuntimeError("Entry order accepted but no open position appeared within 20s.")

    entry = position["entry"]
    size = position["size"]
    actual_side = position["side"]

    if actual_side != side:
        raise RuntimeError("Exchange position side differs from signal.")

    stop = initial_stop(actual_side, entry, product)

    state["position"] = {
        "entry_order_id": order_id,
        "side": actual_side,
        "entry": str(entry),
        "size": size,
        "product_id": product_id,
        "stop_order_id": None,
        "stop_price": str(stop),
        "highest_profit_step": 0,
    }
    save_state(state)

    stop_result = place_stop_order(product, actual_side, size, stop)
    stop_order = stop_result.get("result", {})
    stop_id = stop_order.get("id")

    if not stop_id:
        raise RuntimeError("Stop order ID missing from response")

    state["position"]["stop_order_id"] = stop_id
    save_state(state)
    logging.info("[PROTECTED POSITION] SL=%s", stop)


# ==================================================
# 4. FLASK HEALTH-CHECK SERVER (For Render Port Binding)
# ==================================================
app = Flask(__name__)

@app.route("/")
def health_check():
    return "EMA 9/21 Trading Bot is running!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ==================================================
# 5. MAIN EXECUTION (Flask + Trailing + EMA Crossover)
# ==================================================
if __name__ == "__main__":
    logging.info("Initializing EMA 9/21 Trading Bot services...")

    # Start Flask web server in a background thread so Render's port check passes
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    logging.info("Flask health-check server started successfully.")

    # Main Trading Bot Loop
    while True:
        try:
            product = {"symbol": SYMBOL, "tick_size": 0.1, "id": 1}

            # --- 1. POSITION ACTIVE: Dynamic Trailing Stop-Loss ---
            if state and state.get("position"):
                logging.info("[POSITION ACTIVE] Running Dynamic Trailing Stop-Loss check...")
                
                current_exchange_pos = get_open_position()
                if not current_exchange_pos:
                    logging.info("[POSITION CLOSED] Exchange position closed (Stop-loss hit). Resetting state.")
                    state["position"] = None
                    save_state(state)
                else:
                    check_and_update_dynamic_trailing(product, state["position"])
            
            # --- 2. NO POSITION: Scan Market for EMA 9/21 Crossover ---
            else:
                logging.info("[MARKET SCAN] Fetching candles and checking EMA 9/21 crossover...")
                
                df = fetch_market_candles(SYMBOL)
                
                if df is not None and not df.empty:
                    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
                    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
                
                    price = df['close'].iloc[-1]
                    ema9 = df['ema9'].iloc[-1]
                    ema21 = df['ema21'].iloc[-1]
                
                    logging.info("[MARKET] price=%.1f EMA9=%.2f EMA21=%.2f", price, ema9, ema21)
                
                    prev_ema9 = df['ema9'].iloc[-2]
                    prev_ema21 = df['ema21'].iloc[-2]
                
                    buy_signal = (prev_ema9 <= prev_ema21) and (ema9 > ema21)
                    sell_signal = (prev_ema9 >= prev_ema21) and (ema9 < ema21)
                
                    if buy_signal and check_market_filters(df, "buy"):
                        logging.info("[SIGNAL] Buy crossover detected and filters passed!")
                        place_entry(product, "buy")
                    elif sell_signal and check_market_filters(df, "sell"):
                        logging.info("[SIGNAL] Sell crossover detected and filters passed!")
                        place_entry(product, "sell")
            
            time.sleep(20)
            
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(10)
