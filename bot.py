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
        self.wfile.write(b"Fixed Schema Native SL & TP Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Fixed Schema Bot Server Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD']

last_trade_attempt_time = 0
COOLDOWN_SECONDS = 1800

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
                p_id = str(product.get('id'))
                product_info[symbol] = {
                    "id": p_id,
                    "min_size": 40.0
                }
    except Exception as e:
        print("Error fetching products info:", e, flush=True)
    return product_info

def get_historical_candles(symbol):
    try:
        end_time = int(time.time())
        start_time = end_time - (12 * 3600)
        url = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution=15m&start={start_time}&end={end_time}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if not candles:
            return []
            
        candles = sorted(candles, key=lambda x: x.get('time', 0))
        closes = [float(c['close']) for c in candles if 'close' in c]
        return closes
    except Exception as e:
        print(f"Error fetching candles for {symbol}: {e}", flush=True)
    return []

def get_account_balance():
    path = '/v2/wallet/balances'
    method = 'GET'
    timestamp = str(int(time.time()))
    signature = generate_signature(API_SECRET, method, timestamp, path, '', '')
    
    req_headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'python-rest-client',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.get(BASE_URL + path, headers=req_headers, timeout=10)
        data = response.json()
        for asset in data.get('result', []):
            if asset.get('asset_symbol') == 'USD':
                return float(asset.get('available_balance', 0))
    except Exception as e:
        print("Error fetching account balance:", e, flush=True)
    return 0.0

def get_open_positions():
    path = '/v2/positions'
    method = 'GET'
    timestamp = str(int(time.time()))
    signature = generate_signature(API_SECRET, method, timestamp, path, '', '')
    
    req_headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'python-rest-client',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.get(BASE_URL + path, headers=req_headers, timeout=10)
        data = response.json()
        positions = {}
        for pos in data.get('result', []):
            if float(pos.get('size', 0)) != 0:
                p_id = str(pos.get('product_id'))
                positions[p_id] = {
                    "symbol": pos.get('product_symbol', 'BTCUSD'),
                    "side": pos.get('side'),
                    "size": float(pos.get('size')),
                    "entry_price": float(pos.get('entry_price', 0))
                }
        return positions
    except Exception as e:
        print("Error fetching positions:", e, flush=True)
    return {}

def place_native_order(product_id, size, side, order_type, limit_price=None, stop_price=None):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": order_type,
        "reduce_only": True
    }
    if limit_price:
        payload_data["limit_price"] = round(limit_price, 1)
    if stop_price:
        payload_data["stop_price"] = round(stop_price, 1)
        
    payload = json.dumps(payload_data)
    signature = generate_signature(API_SECRET, method, timestamp, path, '', payload)
    
    req_headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'python-rest-client',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[ORDER {order_type.upper()}] Status: {response.status_code} | Body: {response.text}", flush=True)
        return response.status_code == 200
    except Exception as e:
        print(f"Failed to place {order_type}: {e}", flush=True)
        return False

def execute_trade_with_fixed_risk(product_id, symbol, side, size, current_price):
    global last_trade_attempt_time
    
    available_usd = get_account_balance()
    if available_usd < 40.0:
        print(f"--> RISK GUARD: Available balance ({available_usd} USD) is too low. Aborting.", flush=True)
        return

    if side == "buy":
        sl_price = current_price - 50.0
        tp_price = current_price + 100.0
        exit_side = "sell"
    else:
        sl_price = current_price + 50.0
        tp_price = current_price - 100.0
        exit_side = "buy"

    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": int(product_id),
        "size": int(size),
        "side": side,
        "order_type": "market_order"
    }
    
    payload = json.dumps(payload_data)
    signature = generate_signature(API_SECRET, method, timestamp, path, '', payload)
    
    req_headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'python-rest-client',
        'Content-Type': 'application/json'
    }
    
    try:
        print(f"[Execution] Placing Market {side.upper()} order for {symbol} at {current_price}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[MARKET ENTRY] Status: {response.status_code} | Body: {response.text}", flush=True)
        
        if response.status_code == 200:
            time.sleep(1)
            # Correct order_type 'stop_order' and parameter 'limit_price'
            place_native_order(product_id, size, exit_side, "stop_order", stop_price=sl_price)
            place_native_order(product_id, size, exit_side, "limit_order", limit_price=tp_price)
            
        last_trade_attempt_time = time.time()
    except Exception as e:
        print(f"Trade execution failed for {symbol}: {e}", flush=True)

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("High-Speed Fixed Schema Bot Running...", flush=True)

while True:
    try:
        open_positions = get_open_positions()
        
        if len(open_positions) > 0:
            print("Position active. Exchange server managing Fixed SL & TP...", flush=True)
            time.sleep(1)
            continue

        if time.time() - last_trade_attempt_time < COOLDOWN_SECONDS:
            time.sleep(1)
            continue

        for symbol, info in product_data.items():
            p_id = info["id"]
            trade_size = info["min_size"]
            
            closes = get_historical_candles(symbol)
            if len(closes) < 30:
                continue
                
            current_price = closes[-1]
            ema_fast = calculate_ema(closes, 12)
            ema_slow = calculate_ema(closes, 26)
            
            if ema_fast is None or ema_slow is None:
                continue
            
            is_strong_uptrend = (ema_fast > ema_slow) and (current_price > ema_fast)
            is_strong_downtrend = (ema_fast < ema_slow) and (current_price < ema_fast)
            
            if is_strong_uptrend:
                print(f"--> UPTREND DETECTED: Executing BUY on {symbol}!", flush=True)
                execute_trade_with_fixed_risk(p_id, symbol, "buy", size=trade_size, current_price=current_price)
                break
                
            elif is_strong_downtrend:
                print(f"--> DOWNTREND DETECTED: Executing SELL on {symbol}!", flush=True)
                execute_trade_with_fixed_risk(p_id, symbol, "sell", size=trade_size, current_price=current_price)
                break
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(1)
