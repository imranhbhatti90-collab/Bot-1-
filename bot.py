import time
import hmac
import hashlib
import json
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Emergency Closer is running!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'

def generate_signature(secret, method, timestamp, path, query_string='', payload=''):
    message = method + timestamp + path + query_string + payload
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

def force_close_sol_remaining():
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    # Remaining size 181 (or smaller chunks) to clear the orderbook completely
    payload_data = {
        "product_id": 92572,
        "size": 181,
        "side": "buy",
        "order_type": "market_order",
        "reduce_only": True
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
        print("Sending Emergency Close Request for remaining SOLUSD size (181)...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"CLOSE RESPONSE -> Status: {response.status_code} | Body: {response.text}", flush=True)
    except Exception as e:
        print(f"Failed to close remaining SOLUSD: {e}", flush=True)

# Run once immediately
force_close_sol_remaining()

# Keep alive loop
while True:
    time.sleep(60)
