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
        self.wfile.write(b"Native Stop-Loss Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Native SL Bot Server Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD']

last_trade_attempt_time = 0
COOLDOWN_SECONDS = 1800  # 30 minutes strict gap to ensure high quality setups

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

def get_live_market_data(symbol):
    try:
        url = f"{BASE_URL}/v2/tickers"
        response = requests.get(url, timeout=5)
        data = response.json()
        for ticker in data.get('result', []):
            if ticker.get('symbol') == symbol:
                return {
                    "mark_price": float(ticker.get('mark_price', ticker.get('close', 0)))
                }
    except Exception as e:
        print(f"Error fetching live market data for {symbol}: {e}", flush=True)
    return None

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

def execute_order_with_native_sl(product_id, symbol, side, size, current_price):
    global last_trade_attempt_time
    
    available_usd = get_account_balance()
    if available_usd < 40.0:
        print(f"--> RISK GUARD: Available balance ({available_usd} USD) is too low. Aborting.", flush=True)
        return

    # Calculate strict native stop loss (e.g., 40 points away from entry)
    stop_loss_distance = 40.0
    if side == "buy":
        sl_price = current_price - stop_loss_distance
        tp_price = current_price + 100.0
        exit_side = "sell"
    else:
        sl_price = current_price + stop_loss_distance
        tp_price = current_price - 100.0
        exit_side = "buy"

    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    # Main Market Order Payload
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
        print(f"[Execution] Placing Market {side.upper()} order for {symbol} (Size: {size})...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[{side.upper()}] API ORDER RESPONSE -> Status: {response.status_code} | Body: {response.text}", flush=True)
        
        if response.status_code == 200:
            # Immediately place a Native Server-Side Stop-Loss Order on Exchange
            time.sleep(1) # short buffer for position registration
            sl_payload_data = {
                "product_id": int(product_id),
                "size": int(size),
                "side": exit_side,
                "order_type": "stop_market",
                "stop_price": round(sl_price, 1),
                "reduce_only": True
            }
            sl_payload = json.dumps(sl_payload_data)
            sl_timestamp = str(int(time.time()))
            sl_signature = generate_signature(API_SECRET, 'POST', sl_timestamp, path, '', sl_payload)
            
            sl_headers = {
                'api-key': API_KEY,
                'timestamp': sl_timestamp,
                'signature': sl_signature,
                'User-Agent': 'python-rest-client',
                'Content-Type': 'application/json'
            }
            
            sl_response = requests.post(BASE_URL + path, headers=sl_headers, data=sl_payload, timeout=10)
            print(f"[NATIVE STOP-LOSS] Placed at {round(sl_price, 1)} -> Status: {sl_response.status_code} | Body: {sl_response.text}", flush=True)
            
        last_trade_attempt_time = time.time()
    except Exception as e:
        print(f"Order/SL execution failed for {symbol}: {e}", flush=True)

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
print("Native Server-Side SL Bot Running...", flush=True)

while True:
    try:
        open_positions = get_open_positions()
        
        # If position is open, let exchange handle the native stop loss completely.
        if len(open_positions) > 0:
            print("Active position found. Managing via Exchange Native Server-Side Stop-Loss...", flush=True)
            time.sleep(20)
            continue

        # Look for New High-Conviction Entries
        if time.time() - last_trade_attempt_time < COOLDOWN_SECONDS:
            time.sleep(15)
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
                print(f"--> NATIVE SL UPTREND: Executing BUY on {symbol} at {current_price}!", flush=True)
                execute_order_with_native_sl(p_id, symbol, "buy", size=trade_size, current_price=current_price)
                break
                
            elif is_strong_downtrend:
                print(f"--> NATIVE SL DOWNTREND: Executing SELL on {symbol} at {current_price}!", flush=True)
                execute_order_with_native_sl(p_id, symbol, "sell", size=trade_size, current_price=current_price)
                break
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(15)
