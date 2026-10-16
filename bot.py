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

print("Fast Candle Strategy Bot Started...")

API_KEY = 'HDou10O61WI9QV2fuINZxOeKPbOYAH'
API_SECRET = 'k4E5UkY1633GAHkskl5mxoAEMtlzANXGMohqdiOdAAYnX9Cumfc8z61BJESZ'

# CDN URL for public data (Products, Candles)
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
# Trading API URL for private requests (Leverage, Orders)
TRADE_URL = 'https://testnet-api.deltaex.org'

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
        data = 
