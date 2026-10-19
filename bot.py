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
        self.wfile.write(b"Aggressive Scalping Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Aggressive Scalping Bot Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'

BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'BNBUSD', 'XAUTUSD', 'PAXGUSD']

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
                    custom_size = 40.0  
                elif symbol == 'ETHUSD':
                    custom_size = 40.0
                elif symbol in ['XRPUSD', 'SOLUSD', 'BNBUSD', 'XAUTUSD', 'PAXGUSD']:
                    custom_size = max(min_size, 10.0)
                
                product_info[symbol] = {
                    "id": p_id,
                    "min_size": custom_size
                }
    except Exception as e:
        print("Error fetching products info:", e, flush=True)
    return product_info

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
                positions[pos.get('product_id')] = {
                    "side": pos.get('side'),
                    "size": float(pos.get('size')),
                    "entry_price": float(pos.get('entry_price', 0))
                }
        return positions
    except Exception as e:
        print("Error fetching positions:", e, flush=True)
    return {}

def close_position(product_id, symbol, current_side, size):
    side = "sell" if current_side == "buy" else "buy"
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": product_id,
        "size": int(size),
        "side": side,
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
        print(f"[Closing] Switching position for {symbol} via {side.upper()}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[CLOSE RESPONSE] {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
    except Exception as e:
        print(f"Failed to close position for {symbol}: {e}", flush=True)

def execute_order(product_id, symbol, side, size, entry_price):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    sl_offset = 25.0
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
        
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&symbol={symbol}&resolution=5m&start={start_time}&end={current_time}&limit=30"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        print(f"[API Fetch] {symbol} candles count: {len(candles)}", flush=True)
        if len(candles) < 25:
            return None, 0, 0, 0
            
        closes = [float(c.get('close', 0)) for c in candles]
        
        latest = candles[0]
        c_time = latest.get('time', 0)
        close_p = float(latest.get('close', 0))
        
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        
        return c_time, close_p, ema9, ema21
    except Exception as e:
        print(f"Error fetching market data for {symbol}: {e}", flush=True)
    return None, 0, 0, 0

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("Aggressive Scalping Strategy Started & Monitoring Loop Active...", flush=True)

while True:
    try:
        print("[Loop] Checking market trends...", flush=True)
        open_positions = get_open_positions()
        
        for symbol, info in product_data.items():
            p_id = info["id"]
            trade_size = info["min_size"]
            
            c_time, close_p, ema9, ema21 = get_market_data(p_id, symbol)
            if c_time is None or ema9 is None or ema21 is None:
                print(f"[Skip] Skipping {symbol} due to missing data (c_time: {c_time}, ema9: {ema9}, ema21: {ema21})", flush=True)
                continue
            
            print(f"[{symbol}] Price: {close_p} | EMA9: {round(ema9, 2)} | EMA21: {round(ema21, 2)}", flush=True)
                
            has_position = p_id in open_positions
            pos_side = open_positions[p_id]["side"] if has_position else None
            
            is_buy_trend = ema9 > ema21
            is_sell_trend = ema9 < ema21
            
            if has_position:
                if pos_side == "sell" and is_buy_trend:
                    print(f"--> TREND SWITCH [BUY] on {symbol}: EMA9 crossed above EMA21!", flush=True)
                    close_position(p_id, symbol, "sell", open_positions[p_id]["size"])
                    time.sleep(2)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=close_p)
                    
                elif pos_side == "buy" and is_sell_trend:
                    print(f"--> TREND SWITCH [SELL] on {symbol}: EMA9 crossed below EMA21!", flush=True)
                    close_position(p_id, symbol, "buy", open_positions[p_id]["size"])
                    time.sleep(2)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
            else:
                if is_buy_trend:
                    print(f"--> AGGRESSIVE TRIGGER [BUY] on {symbol}: Uptrend Active!", flush=True)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=close_p)
                    
                elif is_sell_trend:
                    print(f"--> AGGRESSIVE TRIGGER [SELL] on {symbol}: Downtrend Active!", flush=True)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(15)
