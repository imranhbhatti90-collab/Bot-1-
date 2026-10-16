import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# 1. Chota sa dummy server jo Render ko port open dikhayega
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

# Server ko background thread mein chala rahe hain taaki trading bot na ruke
server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

# ==========================================
# 2. Aapka apna trading bot ka code yahan se shuru hoga
# ==========================================
print("Trading bot started successfully...")

import time
import hmac
hashlib = __import__('hashlib')
import json
import requests

# Apni API Key aur Secret yahan dalein
API_KEY = 'HDou10O61WI9QV2fuINZxOeKPbOYAH'
API_SECRET = 'k4E5UkY1633GAHkskl5mxoAEMtlzANXGMohqdiOdAAYnX9Cumfc8z61BJESZ'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'

# Jin-jin coins ko scan karna hai
TARGET_COINS = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'DOGEUSD']

# Yahan aap apna trade size (contracts/amount) set kar sakte hain
TRADE_SIZE = 1  # Agar size badhana ho toh yahan 2, 5 ya usse zyada kar sakte hain

def generate_signature(secret, method, timestamp, path, query_string='', payload=''):
    message = method + timestamp + path + query_string + payload
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

def get_target_product_ids():
    product_map = {}
    try:
        url = BASE_URL + "/v2/products"
        response = requests.get(url)
        data = response.json()
        for product in data.get('result', []):
            symbol = product.get('symbol')
            p_id = product.get('id')
            if symbol in TARGET_COINS and p_id:
                product_map[symbol] = p_id
    except Exception as e:
        print("Error fetching products:", e)
    return product_map

def get_market_prices(valid_symbols):
    prices = {}
    try:
        url = BASE_URL + "/v2/tickers"
        response = requests.get(url)
        data = response.json()
        for ticker in data.get('result', []):
            symbol = ticker.get('symbol')
            if symbol in valid_symbols:
                price = ticker.get('close') or ticker.get('mark_price') or ticker.get('spot_price')
                if price:
                    prices[symbol] = float(price)
    except Exception as e:
        print("Error fetching market data:", e)
    return prices

def execute_order(product_id, symbol, side, size):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": product_id,
        "size": size,
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
        print(f"[{side.upper()}] Order Response for {symbol}:", response.json())
    except Exception as e:
        print(f"Order execution failed for {symbol}:", e)

print("Multi-Coin Scalping Bot Started...")

valid_products = get_target_product_ids()
print(f"Loaded Target Products: {valid_products}")

last_prices = get_market_prices(valid_products.keys())
while not last_prices:
    print("Waiting for initial market prices...")
    time.sleep(3)
    last_prices = get_market_prices(valid_products.keys())

print("Prices loaded. Scanning all target coins...")

# Strategy Thresholds (Aap ise apne hisab se adjust kar sakte hain)
DIP_THRESHOLD = 50.0    
PUMP_THRESHOLD = 50.0   

while True:
    current_prices = get_market_prices(valid_products.keys())
    if not current_prices:
        time.sleep(5)
        continue
        
    for symbol, current_price in current_prices.items():
        if symbol not in last_prices:
            last_prices[symbol] = current_price
            continue
            
        last_price = last_prices[symbol]
        price_diff = current_price - last_price
        
        print(f"Scanning {symbol} -> Current: {current_price} | Diff: {price_diff}")
        
        # 1. Dip Buying Logic
        if price_diff <= -DIP_THRESHOLD:
            print(f"Dip detected on {symbol}! Drop: {abs(price_diff)}. Executing BUY...")
            p_id = valid_products.get(symbol)
            if p_id:
                execute_order(p_id, symbol, "buy", size=TRADE_SIZE)
                
        # 2. Breakout Logic
        elif price_diff >= PUMP_THRESHOLD:
            print(f"Breakout detected on {symbol}! Rise: {price_diff}. Executing SELL...")
            p_id = valid_products.get(symbol)
            if p_id:
                execute_order(p_id, symbol, "sell", size=TRADE_SIZE)
                
        last_prices[symbol] = current_price
        
    time.sleep(3)
