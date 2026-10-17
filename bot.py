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
        current_time = int(time.time())
        start_time = current_time - 7200
        
        # Video ke mutabiq 30m ya high timeframe exhaustion check ke liye resolution set kar sakte hain
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&symbol={symbol}&resolution=30m&start={start_time}&end={current_time}&limit=5"
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
        print(f"Error fetching candles for {symbol}: {e}", flush=True)
    return None, 0, 0, 0

def execute_order_with_sl(product_id, symbol, side, size, entry_price):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    # Risk management ke liye automatic Stop Loss offset
    sl_offset = 20.0
    stop_loss_price = entry_price - sl_offset if side == 'buy' else entry_price + sl_offset
    
    payload_data = {
        "product_id": product_id,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "stop_loss_price": round(stop_loss_price, 2)
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
        print(f"[Executing] Sending {side.upper()} order for {symbol} with size {size} & SL at {stop_loss_price}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[{side.upper()}] API ORDER RESPONSE for {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
    except Exception as e:
        print(f"Order execution failed for {symbol}: {e}", flush=True)

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("Exhaustion strategy monitoring started...", flush=True)

EXHAUSTION_THRESHOLD = 30.0    
last_traded_candle = {}

while True:
    for symbol, info in product_data.items():
        p_id = info["id"]
        trade_size = info["min_size"]
        
        c_time, open_p, close_p, candle_diff = get_market_candles(p_id, symbol)
        if c_time is None:
            continue
            
        if symbol not in last_traded_candle:
            last_traded_candle[symbol] = 0
            
        if c_time != last_traded_candle[symbol]:
            print(f"[Check] {symbol} -> Open: {open_p} | Close: {close_p} | Diff: {candle_diff:.2f}", flush=True)
            
            # Reversal / Exhaustion Strategy Logic (Bidirectional):
            # 1. Agar market bohot zyada dip (negative diff) kar gaya hai, toh exhaustion par BUY karenge
            if candle_diff <= -EXHAUSTION_THRESHOLD:
                print(f"--> TRIGGER: Oversold Exhaustion Dip on {symbol}! Buying...", flush=True)
                execute_order_with_sl(p_id, symbol, "buy", size=trade_size, entry_price=close_p)
                last_traded_candle[symbol] = c_time
                
            # 2. Agar market bohot zyada pump (positive diff) kar gaya hai, toh peak exhaustion par SELL karenge
            elif candle_diff >= EXHAUSTION_THRESHOLD:
                print(f"--> TRIGGER: Overbought Exhaustion Peak on {symbol}! Selling...", flush=True)
                execute_order_with_sl(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
                last_traded_candle[symbol] = c_time
                
    time.sleep(30)
