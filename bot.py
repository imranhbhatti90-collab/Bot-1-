import time
import logging
import pandas as pd

# ==================================================
# CONFIGURATION & CONSTANTS
# ==================================================
ENABLE_TRADING = True
SYMBOL = "BTCUSD"
ORDER_SIZE = 40
LEVERAGE = 50

# Dynamic Trailing & Filter Constants
TRAILING_BUFFER_POINTS = 200  # Stop-loss hamesha best peak price se itne points piche trail karega
RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20         # Average volume calculate karne ke liye periods
MIN_VOLUME_MULTIPLIER = 1.2   # Current volume average ka kam se kam itna guna hona chahiye


# ==================================================
# 1. RSI & VOLUME CONFIRMATION FILTERS
# ==================================================
def check_market_filters(df, side):
    """
    Checks both RSI momentum and Volume strength to block fake/low-momentum crossover entries.
    For BUY: RSI >= 50 and Volume > Average Volume * multiplier
    For SELL: RSI <= 50 and Volume > Average Volume * multiplier
    """
    if len(df) < max(RSI_PERIOD, VOLUME_MA_PERIOD) + 1:
        return True  # Fallback agar data kam ho

    # --- RSI Calculation ---
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    current_rsi = df['rsi'].iloc[-1]

    # --- Volume Calculation ---
    df['vol_ma'] = df['volume'].rolling(window=VOLUME_MA_PERIOD).mean()
    current_volume = df['volume'].iloc[-1]
    avg_volume = df['vol_ma'].iloc[-1]

    logging.info(
        "[FILTER CHECK] Side: %s | RSI: %.2f | Vol: %.1f (Avg: %.1f)",
        side.upper(), current_rsi, current_volume, avg_volume
    )

    # Check RSI Condition
    if side == "buy" and current_rsi < 50:
        logging.info("[FILTER BLOCKED] Buy signal rejected: RSI (%.2f) is below 50", current_rsi)
        return False
    if side == "sell" and current_rsi > 50:
        logging.info("[FILTER BLOCKED] Sell signal rejected: RSI (%.2f) is above 50", current_rsi)
        return False

    # Check Volume Condition
    if current_volume < (avg_volume * MIN_VOLUME_MULTIPLIER):
        logging.info(
            "[FILTER BLOCKED] %s signal rejected: Volume (%.1f) is too low compared to average (%.1f)",
            side.upper(), current_volume, avg_volume
        )
        return False

    logging.info("[FILTER PASSED] %s signal approved by RSI & Volume filters!", side.upper())
    return True


# ==================================================
# 2. DYNAMIC CONTINUOUS TRAILING STOP-LOSS
# ==================================================
def check_and_update_dynamic_trailing(product, position):
    """
    Continuously trails the stop loss based on TRAILING_BUFFER_POINTS 
    from peak profit, seamlessly supporting 1000+ to 2000+ point moves.
    """
    current_price = get_latest_price(product["symbol"])
    if not current_price:
        return

    side = position["side"]
    entry_price = float(position["entry"])
    size = position["size"]
    stop_order_id = position.get("stop_order_id")
    current_stop_price = float(position.get("stop_price", 0))

    # Calculate profit in points and determine new dynamic stop price
    if side == "buy":
        profit_points = current_price - entry_price
        new_stop_price = current_price - TRAILING_BUFFER_POINTS
    else:  # sell / short
        profit_points = entry_price - current_price
        new_stop_price = current_price + TRAILING_BUFFER_POINTS

    # Start trailing only if profit exceeds the buffer distance
    if profit_points > TRAILING_BUFFER_POINTS:
        should_update = False
        
        # For long: new stop must be higher than current stop
        if side == "buy" and new_stop_price > current_stop_price:
            should_update = True
        # For short: new stop must be lower than current stop
        elif side == "sell" and (current_stop_price == 0 or new_stop_price < current_stop_price):
            should_update = True

        if should_update:
            tick_size = float(product["tick_size"])
            new_stop_price = round(new_stop_price / tick_size) * tick_size
            
            logging.info(
                "[DYNAMIC TRAIL] Profit: %.1f pts | Moving SL from %.1f to %.1f",
                profit_points, current_stop_price, new_stop_price
            )
            
            # Cancel existing stop order if present
            if stop_order_id:
                cancel_order(stop_order_id)
                
            # Place new updated trailing stop order
            stop_result = place_stop_order(product, side, size, new_stop_price)
            new_stop_order = stop_result.get("result", {})
            new_stop_id = new_stop_order.get("id")
            
            if new_stop_id:
                position["stop_order_id"] = new_stop_id
                position["stop_price"] = str(new_stop_price)
                save_state(state)
                logging.info("[SL UPDATED SUCCESSFULLY] New ID: %s, Price: %.1f", new_stop_id, new_stop_price)


# ==================================================
# 3. MARKET ENTRY (Your robust implementation)
# ==================================================
def place_entry(product, side):
    if not ENABLE_TRADING:
        logging.warning(
            "[SAFE MODE] ENABLE_TRADING=false; no order sent. "
            "Signal=%s",
            side.upper(),
        )
        return

    if not API_KEY or not API_SECRET:
        raise RuntimeError("API credentials missing")

    if state.get("position"):
        logging.warning(
            "Saved bot position exists; refusing another entry"
        )
        return

    current = get_open_position()

    if current:
        logging.warning(
            "An exchange position is already open; refusing entry"
        )
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
        "client_order_id": (
            f"ema921_{int(time.time())}"
        ),
    }

    logging.info("[ENTRY REQUEST] %s", body)

    result = api_request(
        "POST",
        "/v2/orders",
        body=body,
    )

    order = result.get("result", {})
    order_id = order.get("id")

    logging.info("[ENTRY RESPONSE] %s", result)

    # Wait up to 20 seconds for the market order to fill and position to appear.
    position = None

    for _ in range(20):
        time.sleep(1)
        position = get_open_position()

        if position:
            break

    if not position:
        raise RuntimeError(
            "Entry order was accepted but no open position appeared within 20s. "
            "Check Testnet order history before restarting."
        )

    entry = position["entry"]
    size = position["size"]
    actual_side = position["side"]

    if actual_side != side:
        raise RuntimeError(
            "Exchange position side differs from signal; "
            "manual review required"
        )

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

    try:
        stop_result = place_stop_order(
            product,
            actual_side,
            size,
            stop,
        )

        stop_order = stop_result.get("result", {})
        stop_id = stop_order.get("id")

        if not stop_id:
            raise RuntimeError(
                f"Stop order ID missing from response: {stop_result}"
            )

        state["position"]["stop_order_id"] = stop_id
        save_state(state)

        logging.info(
            "[PROTECTED POSITION] side=%s entry=%s size=%s SL=%s "
            "stop_order_id=%s",
            actual_side,
            entry,
            size,
            stop,
            stop_id,
        )

    except Exception:
        logging.exception(
            "CRITICAL: protective stop order failed after entry."
        )
        raise
