import os
import time
import hmac
import hashlib
import requests
from flask import Flask

app = Flask(__name__)

# --- CONFIGURATION ---
API_KEY = os.environ.get("DELTA_API_KEY", "")
API_SECRET = os.environ.get("DELTA_API_SECRET", "")
BASE_URL = "https://testnet-api.delta.exchange"  # Testnet URL

PRODUCT_ID = 84

# Trailing Milestones & Offsets (in points)
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

def place_stop_loss_order(side, size, stop_price):
    endpoint = '/v2/orders'
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
    res = send_request('GET', '/v2/orders', params={'product_id': PRODUCT_ID, 'state': 'open'})
    if res and res.get('success'):
        for order in res.get('result', []):
            if order.get('order_type') == 'stop_order':
                send_request('DELETE', '/v2/orders', data={'id': order['id'], 'product_id': PRODUCT_ID})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    print(f"Bot Server Started & Port {port} Bound...")
    app.run(host='0.0.0.0', port=port)
