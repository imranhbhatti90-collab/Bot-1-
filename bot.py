import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import hmac
import hashlib
import json
import requests

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

print("Fast Candle Strategy Bot Started...", flush=True)

# Force print exact outbound IP on startup
try:
    ip_response = requests.get('https://api.ipify.org?format=json', timeout=5)
    my_ip = ip_response.json().get('ip')
    print(f"#################################################", flush=True)
    print(f"--> EXACT RENDER OUTBOUND IP: {my_ip}", flush=True)
    print(f"#################################################", flush=True)
except Exception as e:
    print(f"IP Fetch Error: {e}", flush=True)

API_KEY = 'HDou10O61WI9QV2fuINZxOeKPbOYAH'
API_SECRET = 'k4E5UkY1633GAHkskl5mxoAEMtlzANXGMohqdiOdAAYnX9Cumfc8z61BJESZ'

BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'DOGEUSD']

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
        response = requests.get(url, timeout=10)
        data = response.json()
        for product in data.get('result', []):
            symbol = product.get('symbol')
            if symbol in TARGET_COINS:
                p_id = product.get('id')
                min_size = float(product.get('contract_unit', 1))
                if 'spot_lot_size' in product:
                    min_size = float(product.get('spot_lot_size'))
                
                product_info[symbol] = {
                    "id": p_id,
                    "min_size": max(min_size, 1.0)
                }
    except Exception as e:
        print("Error fetching products info:", e, flush=True)
    return product_info

def get_latest_candle_info(product_id, symbol):
    try:
        current_time = int(time.time())
        start_time = current_time - 3600  # last 1 hour
        
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&symbol={symbol}&resolution=5m&start={start_time}&end={current_time}&limit=2"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if not candles:
            return None, 0, 0, 0
            
        latest = candles[0]
        candle_time = latest.get('time', 0)
        open_price = float(latest.get('open', 0))
        close_price = float(latest.get('close', 0))
        candle_diff = close_price - open_price
        return candle_time, open_price, close_price, candle_diff
    except Exception as e:
        print(f"Error fetching candle data for {symbol}: {e}", flush=True)
    return None, 0, 0, 0

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
        print(f"[Executing] Sending {side.upper()} order for {symbol} with size {size}...", flush=True)
        response = requests.post(BASE_URL + path, headers=headers, data=payload, timeout=10)
        print(f"[{side.upper()}] API ORDER RESPONSE for {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
    except Exception as e:
        print(f"Order execution failed for {symbol}: {e}", flush=True)

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("Fast candle monitoring started...", flush=True)

DIP_THRESHOLD = 20.0    
PUMP_THRESHOLD = 20.0   

last_traded_candle = {}

while True:
    for symbol, info in product_data.items():
        p_id = info["id"]
        trade_size = info["min_size"]
        
        c_time, open_p, close_p, candle_diff = get_latest_candle_info(p_id, symbol)
        if c_time is None:
            continue
            
        if symbol not in last_traded_candle:
            last_traded_candle[symbol] = 0
            
        if c_time != last_traded_candle[symbol]:
            print(f"[Live Check] {symbol} -> Open: {open_p} | Close: {close_p} | Diff: {candle_diff}", flush=True)
            
            if candle_diff <= -DIP_THRESHOLD:
                print(f"--> TRIGGER: 5m Red Candle Drop on {symbol}! Diff: {abs(candle_diff)}. Executing BUY...", flush=True)
                execute_order(p_id, symbol, "buy", size=trade_size)
                last_traded_candle[symbol] = c_time
                
            elif candle_diff >= PUMP_THRESHOLD:
                print(f"--> TRIGGER: 5m Green Candle Pump on {symbol}! Diff: {candle_diff}. Executing SELL...", flush=True)
                execute_order(p_id, symbol, "sell", size=trade_size)
                last_traded_candle[symbol] = c_time
                
    time.sleep(15)
