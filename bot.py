import os
import time
import hmac
import hashlib
import requests
from flask import Flask

app = Flask(__name__)

# --- CONFIGURATION ---
API_KEY = os.environ.get("DELTA_API_KEY", "apki_api_key")
API_SECRET = os.environ.get("DELTA_API_SECRET", "apki_api_secret")
BASE_URL = "https://testnet-api.delta.exchange"  # Testnet URL

SYMBOL = "BTCUSD"
PRODUCT_ID = 84
SIZE = 40  # Base order size

# Trailing Milestones & Offsets (in points)
# Milestone 1: +100 pts -> Move SL to Entry (Breakeven)
# Milestone 2: +200 pts -> Move SL to Entry + 100 pts
# Milestone 3: +400 pts -> Move SL to Entry + 300 pts
TRAILING_STEPS = [
    {"trigger": 100, "lock": 0},
    {"trigger": 200, "lock": 100},
    {"trigger": 400, "lock": 300}
]

# Bot State Tracking
current_sl_order_id = None
last_triggered_step = -1

@app.route('/')
def home():
    return "Independent Stop Order Trailing Bot Running..."

def generate_signature(method, endpoint, query_string='', payload=''):
    current_time = str(int(time.time() * 1000))
    signature_data = method + current_time + endpoint + query_string + payload
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        signature_data.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return current_time, signature

def send_request(method, endpoint, params=None, data=None):
    import json
    url = BASE_URL + endpoint
    query_string = ''
    if params:
        query_string = '?' + '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        url += query_string
    
    payload_str = json.dumps(data) if data else ''
    timestamp, signature = generate_signature(method, endpoint, query_string, payload_str)
    
    headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'Content-Type': 'application/json'
    }
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers)
        elif method == 'POST':
            response = requests.post(url, headers=headers, data=payload_str)
        elif method == 'DELETE':
            response = requests.delete(url, headers=headers, data=payload_str)
        return response.json()
    except Exception as e:
        print(f"[API Error] {e}")
        return None

def get_market_price():
    res = send_request('GET', f'/v2/products/{PRODUCT_ID}')
    if res and res.get('success'):
        result = res.get('result', {})
        return float(result.get('spot_price', 0))
    return 0.0

def get_open_position():
    res = send_request('GET', '/v2/positions', params={'product_id': PRODUCT_ID})
    if res and res.get('success'):
        positions = res.get('result', [])
        for pos in positions:
            if float(pos.get('size', 0)) != 0:
                return pos
    return None

def place_market_order(side, size):
    endpoint = '/v2/orders'
    payload = {
        "product_id": PRODUCT_ID,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "time_in_force": "ioc"
    }
    print(f"[Execution] Placing Pure Market {side.upper()} order for BTCUSD with size {size}...")
    res = send_request('POST', endpoint, data=payload)
    print(f"[ENTRY RESPONSE] {res}")
    return res

def place_stop_loss_order(side, size, stop_price):
    endpoint = '/v2/orders'
    # For a SELL position, stop-loss order to close is BUY. For a BUY position, it is SELL.
    close_side = "buy" if side == "sell" else "sell"
    payload = {
        "product_id": PRODUCT_ID,
        "size": int(abs(size)),
        "side": close_side,
        "order_type": "stop_order",
        "stop_price": str(round(stop_price, 1)),
        "stop_order_type": "market",
        "reduce_only": True,
        "time_in_force": "gtc"
    }
    res = send_request('POST', endpoint, data=payload)
    return res

def cancel_existing_orders():
    # Cancel open stop orders if any
    res = send_request('GET', '/v2/orders', params={'product_id': PRODUCT_ID, 'state': 'open'})
    if res and res.get('success'):
        for order in res.get('result', []):
            if order.get('order_type') == 'stop_order':
                send_request('DELETE', '/v2/orders', data={'id': order['id'], 'product_id': PRODUCT_ID})

def check_strategy_and_manage():
    global current_sl_order_id, last_triggered_step
    
    position = get_open_position()
    current_price = get_market_price()
    
    if not position:
        # No position open, reset tracking state
        if current_sl_order_id is not None:
            cancel_existing_orders()
            current_sl_order_id = None
            last_triggered_step = -1
            print("[State Reset] Position closed. Resetting TSL states.")
        
        # Simple entry logic simulation (e.g. DOWNTREND signal example or your EMA condition)
        # Yahan aap apna EMA crossover condition laga sakte hain. Filhal bot ready state mein hai.
        return

    # If position exists
    pos_size = float(position.get('size', 0))
    entry_price = float(position.get('entry_price', 0))
    side = "sell" if pos_size < 0 else "buy"
    abs_size = abs(pos_size)

    if current_price == 0 or entry_price == 0:
        return

    # Calculate profit in points
    if side == "sell":
        profit_pts = entry_price - current_price
    else:
        profit_pts = current_price - entry_price

    print(f"[Position Active] Side: {side.upper()} | Size: {abs_size} | Entry: {entry_price} | LTP: {current_price} | Profit Pts: {profit_pts:.1f}")

    # Check Trailing Milestones
    matched_step_index = -1
    lock_offset = 0

    for i, step in enumerate(TRAILING_STEPS):
        if profit_pts >= step["trigger"]:
            matched_step_index = i
            lock_offset = step["lock"]

    # If a new milestone is reached
    if matched_step_index > last_triggered_step:
        # Calculate new stop price
        if side == "sell":
            new_stop_price = entry_price - lock_offset
        else:
            new_stop_price = entry_price + lock_offset

        print(f"[TSL Triggered] Milestone {matched_step_index} reached! Updating Stop Loss to {new_stop_price}")
        
        # Cancel old stop orders and place new updated stop order
        cancel_existing_orders()
        res = place_stop_loss_order(side, abs_size, new_stop_price)
        
        if res and res.get('success'):
            last_triggered_step = matched_step_index
            print(f"[TSL Success] Stop Loss successfully updated on exchange.")
        else:
            print(f"[TSL Error] Failed to update stop loss: {res}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    print(f"Bot Server Started & Port {port} Bound...")
    # Background loop ya periodic check ke liye yahan app run hoti hai
    app.run(host='0.0.0.0', port=port)
