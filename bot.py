import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("5-Minute Candle Strategy Bot Started...")

import time
import hmac
hashlib = __import__('hashlib')
import json
import requests

API_KEY = 'HDou10O61WI9QV2fuINZxOeKPbOYAH'
API_SECRET = 'k4E5UkY1633GAHkskl5mxoAEMtlzANXGMohqdiOdAAYnX9Cumfc8z61BJESZ'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'

TARGET_COINS = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'DOGEUSD']
LEVERAGE = 30

def generate_signature(secret, method, timestamp, path, query_string='', payload=''):
    message = method + timestamp + path + query_string + payload
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

def get_target_product_info():
    product_info = {}
    try:
        url = BASE_URL + "/v2/products"
        response = requests.get(url)
        data = response.json()
        for product in data.get('result', []):
            symbol = product.get('symbol')
            if symbol in TARGET_COINS:
                p_id = product.get('id')
                # Exchange se har coin ka minimum lot/contract size automatic nikal rahe hain
                min_size = float(product.get('contract_unit', 1))
                # Agar lot size alag format mein ho toh minimum limit set kar rahe hain
                if 'spot_lot_size' in product:
                    min_size = float(product.get('spot_lot_size'))
                
                product_info[symbol] = {
                    "id": p_id,
                    "min_size": max(min_size, 1.0) # Minimum safe size
                }
    except Exception as e:
        print("Error fetching products info:", e)
    return product_info

def set_product_leverage(product_id, symbol):
    path = '/v2/orders/leverage'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": product_id,
        "leverage": LEVERAGE
    }
    
    payload = json.dumps(payload_data)
    signature = generate_signature(API_SECRET, method, timestamp, path, '', payload)
    
    headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(BASE_URL + path, headers=headers, data=payload)
        print(f"Leverage set to {LEVERAGE}x for {symbol}:", response.json())
    except Exception as e:
        print(f"Failed to set leverage for {symbol}:", e)

def get_latest_candle_change(product_id):
    try:
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&resolution=5m&limit=2"
        response = requests.get(url)
        data = response.json()
        candles = data.get('result', [])
        if candles and len(candles) >= 1:
            latest = candles[0]
            open_price = float(latest.get('open', 0))
            close_price = float(latest.get('close', 0))
            candle_diff = close_price - open_price
            return open_price, close_price, candle_diff
    except Exception as e:
        print("Error fetching candle data:", e)
    return None, None, 0

def execute_order(product_id, symbol, side, size):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": product_id,
        "size": int(size),
        "side": side,
        "order_type": "market_order"
    }
    
    payload = json.dumps(payload_data)
    signature = generate_signature(API_SECRET, method, timestamp, path, '', payload)
    
    headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(BASE_URL + path, headers=headers, data=payload)
        print(f"[{side.upper()}] Candle Order Response for {symbol}:", response.json())
    except Exception as e:
        print(f"Order execution failed for {symbol}:", e)

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}")

for symbol, info in product_data.items():
    set_product_leverage(info["id"], symbol)

print("Candle monitoring started...")

DIP_THRESHOLD = 20.0    
PUMP_THRESHOLD = 20.0   

while True:
    time.sleep(300)
    
    for symbol, info in product_data.items():
        p_id = info["id"]
        trade_size = info["min_size"]
        
        open_p, close_p, candle_diff = get_latest_candle_change(p_id)
        if open_p is None:
            continue
            
        print(f"[5m Candle] {symbol} -> Open: {open_p} | Close: {close_p} | Diff: {candle_diff}")
        
        if candle_diff <= -DIP_THRESHOLD:
            print(f"5m Red Candle Drop on {symbol}! Diff: {abs(candle_diff)}. Executing BUY...")
            execute_order(p_id, symbol, "buy", size=trade_size)
                
        elif candle_diff >= PUMP_THRESHOLD:
            print(f"5m Green Candle Pump on {symbol}! Diff: {candle_diff}. Executing SELL...")
            execute_order(p_id, symbol, "sell", size=trade_size)
