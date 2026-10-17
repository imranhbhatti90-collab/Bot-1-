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
        self.wfile.write(b"Exhaustion Reversal Bot is running successfully!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Exhaustion Reversal Strategy Bot Started & Port 10000 Bound...", flush=True)

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
                
                # 100 USD equivalent size setup (BTC aur ETH ke liye 40.0 units)
                custom_size = max(min_size, 1.0)
                if symbol == 'BTCUSD':
                    custom_size = 40.0  
                elif symbol == 'ETHUSD':
                    custom_size = 40.0
                
                product_info[symbol] = {
                    "id": p_id,
                    "min_size": custom_size
                }
    except Exception as e:
        print("Error fetching products info:", e, flush=True)
    return product_info

def get_market_candles(product_id, symbol):
    try:
        
            # 2. Agar market bohot zyada pump (positive diff) kar gaya hai, toh peak exhaustion par SELL karenge
            elif candle_diff >= EXHAUSTION_THRESHOLD:
                print(f"--> TRIGGER: Overbought Exhaustion Peak on {symbol}! Selling...", flush=True)
                execute_order_with_sl(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
                
