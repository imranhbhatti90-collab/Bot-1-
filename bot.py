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
        self.wfile.write(b"Indicator Strategy Bot is running!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Indicator Strategy Bot Started & Port 10000 Bound...", flush=True)

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
        print(f"[Closing] Reversing position for {symbol} via {side.upper()}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[CLOSE RESPONSE] {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
    except Exception as e:
        print(f"Failed to close position for {symbol}: {e}", flush=True)

def execute_order(product_id, symbol, side, size, entry_price):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
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
        
        # 15m timeframe candles
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&symbol={symbol}&resolution=15m&start={start_time}&end={current_time}&limit=25"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if len(candles) < 15:
            return None, 0, 0, 0, 0, 0, 0
            
        closes = [float(c.get('close', 0)) for c in candles]
        latest = candles[0]
        prev = candles[1]
        prev2 = candles[2]
        
        c_time = latest.get('time', 0)
        open_p = float(latest.get('open', 0))
        close_p = float(latest.get('close', 0))
        
        prev_open = float(prev.get('open', 0))
        prev_close = float(prev.get('close', 0))
        prev_diff = prev_close - prev_open
        
        ema_fast = calculate_ema(closes, 9)
        ema_slow = calculate_ema(closes, 21)
        
        return c_time, open_p, close_p, prev_diff, ema_fast, ema_slow, closes[-1]
    except Exception as e:
        print(f"Error fetching market data for {symbol}: {e}", flush=True)
    return None, 0, 0, 0, 0, 0, 0

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("Exact Indicator Strategy Bot Started...", flush=True)

EXHAUSTION_LEVEL = 12.0  # Video ke jaisa sensitive exhaustion point
last_traded_candle = {}

while True:
    open_positions = get_open_positions()
    
    for symbol, info in product_data.items():
        p_id = info["id"]
        trade_size = info["min_size"]
        
        c_time, open_p, close_p, prev_diff, ema_fast, ema_slow, current_price = get_market_data(p_id, symbol)
        if c_time is None or ema_fast is None or ema_slow is None:
            continue
            
        if symbol not in last_traded_candle:
            last_traded_candle[symbol] = 0
            
        current_diff = close_p - open_p
        has_position = p_id in open_positions
        pos_side = open_positions[p_id]["side"] if has_position else None
        
        # Indicator Signal Logic (Video style crossover + exhaustion point touch)
        is_bullish_exhaustion = prev_diff <= -EXHAUSTION_LEVEL and current_diff > 0
        is_bearish_exhaustion = prev_diff >= EXHAUSTION_LEVEL and current_diff < 0
        
        if has_position:
            # Reverse position when opposite indicator signal occurs
            if pos_side == "sell" and is_bullish_exhaustion and (current_price > ema_fast):
                print(f"--> INDICATOR SIGNAL [BUY REVERSAL] on {symbol}!", flush=True)
                close_position(p_id, symbol, "sell", open_positions[p_id]["size"])
                time.sleep(2)
                execute_order(p_id, symbol, "buy", size=trade_size, entry_price=close_p)
                last_traded_candle[symbol] = c_time
                
            elif pos_side == "buy" and is_bearish_exhaustion and (current_price < ema_fast):
                print(f"--> INDICATOR SIGNAL [SELL REVERSAL] on {symbol}!", flush=True)
                close_position(p_id, symbol, "buy", open_positions[p_id]["size"])
                time.sleep(2)
                execute_order(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
                last_traded_candle[symbol] = c_time
        else:
            if c_time != last_traded_candle[symbol]:
                if is_bullish_exhaustion and (current_price > ema_fast):
                    print(f"--> INDICATOR TRIGGER [BUY] on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=close_p)
                    last_traded_candle[symbol] = c_time
                    
                elif is_bearish_exhaustion and (current_price < ema_fast):
                    print(f"--> INDICATOR TRIGGER [SELL] on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
                    last_traded_candle[symbol] = c_time
                
    time.sleep(15)
