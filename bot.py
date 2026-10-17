import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import hmac
import hashlib
import json
import requests

# Render Server Uptime Handler
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running successfully!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Advanced Video Strategy Bot Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'

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
                
                custom_size = max(min_size, 1.0)
                if symbol == 'BTCUSD':
                    custom_size = 30.0  
                elif symbol == 'ETHUSD':
                    custom_size = 30.0
                
                product_info[symbol] = {
                    "id": p_id,
                    "min_size": custom_size
                }
    except Exception as e:
        print("Error fetching products info:", e, flush=True)
    return product_info

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def get_market_data(product_id, symbol):
    try:
        current_time = int(time.time())
        start_time = current_time - 7200
        
        # 1m candles fetch kar rahe hain multiple candles ke sath EMA calculate karne ke liye
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&symbol={symbol}&resolution=1m&start={start_time}&end={current_time}&limit=20"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if len(candles) < 10:
            return None, 0, 0, 0, 0, 0
            
        closes = [float(c.get('close', 0)) for c in candles]
        latest = candles[0]
        
        candle_time = latest.get('time', 0)
        open_price = float(latest.get('open', 0))
        close_price = float(latest.get('close', 0))
        candle_diff = close_price - open_price
        
        # Video jaisi MA/EMA filter logic (e.g., 5 and 10 period EMA)
        ema_fast = calculate_ema(closes, 5)
        ema_slow = calculate_ema(closes, 10)
        
        return candle_time, open_price, close_price, candle_diff, ema_fast, ema_slow
    except Exception as e:
        print(f"Error fetching market data for {symbol}: {e}", flush=True)
    return None, 0, 0, 0, 0, 0

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
    
    req_headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'python-rest-client',
        'Content-Type': 'application/json'
    }
    
    try:
        print(f"[Executing] Sending {side.upper()} order for {symbol} with size {size}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[{side.upper()}] API ORDER RESPONSE for {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
    except Exception as e:
        print(f"Order execution failed for {symbol}: {e}", flush=True)

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("Advanced strategy monitoring started...", flush=True)

THRESHOLD = 25.0    # Threshold ko thoda filter out karne ke liye badhaya hai taaki false signals reduce ho sakein
last_traded_candle = {}

while True:
    for symbol, info in product_data.items():
        p_id = info["id"]
        trade_size = info["min_size"]
        
        c_time, open_p, close_p, candle_diff, ema_fast, ema_slow = get_market_data(p_id, symbol)
        if c_time is None or ema_fast is None or ema_slow is None:
            continue
            
        if symbol not in last_traded_candle:
            last_traded_candle[symbol] = 0
            
        if c_time != last_traded_candle[symbol]:
            print(f"[Check] {symbol} -> Diff: {candle_diff:.2f} | Fast EMA: {ema_fast:.2f} | Slow EMA: {ema_slow:.2f}", flush=True)
            
            # Video strategy simulation: Candle momentum + EMA Trend Confirmation
            # Buy tabhi hoga jab green pump ho aur Fast EMA Slow EMA ke upar ho (Bullish Trend)
            if candle_diff >= THRESHOLD and ema_fast > ema_slow:
                print(f"--> TRIGGER: Valid Bullish Pump on {symbol}. Executing BUY...", flush=True)
                execute_order(p_id, symbol, "buy", size=trade_size)
                last_traded_candle[symbol] = c_time
                
            # Sell tabhi hoga jab red drop ho aur Fast EMA Slow EMA ke niche ho (Bearish Trend)
            elif candle_diff <= -THRESHOLD and ema_fast < ema_slow:
                print(f"--> TRIGGER: Valid Bearish Drop on {symbol}. Executing SELL...", flush=True)
                execute_order(p_id, symbol, "sell", size=trade_size)
                last_traded_candle[symbol] = c_time
                
    time.sleep(15)
