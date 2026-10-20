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
        self.wfile.write(b"Always-Active TradingView 5m EMA Scalping Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Always-Active Bot Server Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD']

last_trade_close_time = 0
COOLDOWN_SECONDS = 30
last_trailing_level = {}

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
        start_time = end_time - (6 * 3600)  # Last 6 hours of 5m candles
        url = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution=5m&start={start_time}&end={end_time}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        print(f"[DEBUG Candles API] Symbol {symbol} Response Status: {response.status_code}", flush=True)
        
        candles = data.get('result', [])
        if not candles:
            print(f"[WARNING] No candles returned from API for {symbol}. Raw data: {data}", flush=True)
            return []
            
        candles = sorted(candles, key=lambda x: x.get('time', 0))
        closes = [float(c['close']) for c in candles if 'close' in c]
        return closes
    except Exception as e:
        print(f"Error fetching 5m candles for {symbol}: {e}", flush=True)
    return []

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
    global last_trade_close_time, last_trailing_level
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
        print(f"[Closing] Exiting position for {symbol} via {side.upper()}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[CLOSE RESPONSE] {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
        last_trade_close_time = time.time()
        if product_id in last_trailing_level:
            del last_trailing_level[product_id]
    except Exception as e:
        print(f"Failed to close position for {symbol}: {e}", flush=True)

def execute_order(product_id, symbol, side, size, entry_price):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    sl_offset = 60.0
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
        print(f"[Executing] Sending {side.upper()} order for {symbol} (Size: {size}, Initial SL: {stop_loss_price}, TP: 60 pts)...", flush=True)
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

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("Always-Active TradingView 5m Bot Running & Entering Main Loop...", flush=True)

while True:
    try:
        print("[Loop Check] Fetching positions and candles...", flush=True)
        open_positions = get_open_positions()
        
        for symbol, info in product_data.items():
            p_id = info["id"]
            trade_size = info["min_size"]
            
            closes = get_historical_candles(symbol)
            print(f"[{symbol}] Total Candles Fetched: {len(closes)}", flush=True)
            
            if len(closes) < 25:
                print(f"[{symbol}] Waiting for more candles (Got {len(closes)}/25 needed)...", flush=True)
                time.sleep(10)
                continue
                
            current_price = closes[-1]
            
            ema_fast = calculate_ema(closes, 8)
            ema_slow = calculate_ema(closes, 21)
            
            if ema_fast is None or ema_slow is None:
                continue
                
            print(f"[{symbol}] 5m Close: {current_price} | Fast(8): {round(ema_fast, 2)} | Slow(21): {round(ema_slow, 2)}", flush=True)
                
            has_position = p_id in open_positions
            pos_side = open_positions[p_id]["side"] if has_position else None
            entry_price = open_positions[p_id]["entry_price"] if has_position else 0
            
            if has_position:
                profit_points = 0
                if pos_side == "buy":
                    profit_points = current_price - entry_price
                elif pos_side == "sell":
                    profit_points = entry_price - current_price
                
                print(f"[{symbol}] Active {pos_side.upper()} | Entry: {entry_price} | PnL: {round(profit_points, 2)} pts", flush=True)
                
                if profit_points >= 60.0:
                    print(f"--> TARGET REACHED (+60 Points)! Taking profit on {symbol}.", flush=True)
                    close_position(p_id, symbol, pos_side, open_positions[p_id]["size"])
                    continue
                
                current_level = last_trailing_level.get(p_id, 0)
                if profit_points >= 50.0 and current_level < 50:
                    last_trailing_level[p_id] = 50
                    print(f"--> TRAILING UPGRADE: Locking profit at level 50 pts!", flush=True)
                elif profit_points >= 30.0 and current_level < 30:
                    last_trailing_level[p_id] = 30
                    print(f"--> TRAILING UPGRADE: Moving SL to Breakeven!", flush=True)
                
                active_sl_limit = -60.0
                lvl = last_trailing_level.get(p_id, 0)
                if lvl == 30:
                    active_sl_limit = 0.0
                elif lvl == 50:
                    active_sl_limit = 25.0
                
                if profit_points <= active_sl_limit:
                    print(f"--> STOP LOSS / TRAILING HIT! Closing {symbol} position.", flush=True)
                    close_position(p_id, symbol, pos_side, open_positions[p_id]["size"])
                    continue
            
            if not has_position:
                if time.time() - last_trade_close_time < COOLDOWN_SECONDS:
                    print(f"[{symbol}] In Cooldown Period...", flush=True)
                    continue
            
            ema_diff = abs(ema_fast - ema_slow)
            min_gap_required = current_price * 0.00008
            
            is_uptrend = (ema_fast > ema_slow) and (ema_diff > min_gap_required)
            is_downtrend = (ema_fast < ema_slow) and (ema_diff > min_gap_required)
            
            if has_position:
                if pos_side == "sell" and is_uptrend:
                    print(f"--> TREND SHIFT BUY: Closing SELL and buying {symbol}!", flush=True)
                    close_position(p_id, symbol, "sell", open_positions[p_id]["size"])
                    time.sleep(2)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=current_price)
                    
                elif pos_side == "buy" and is_downtrend:
                    print(f"--> TREND SHIFT SELL: Closing BUY and selling {symbol}!", flush=True)
                    close_position(p_id, symbol, "buy", open_positions[p_id]["size"])
                    time.sleep(2)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=current_price)
            else:
                if is_uptrend:
                    print(f"--> SIGNAL UPTREND: Executing BUY on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=current_price)
                    
                elif is_downtrend:
                    print(f"--> SIGNAL DOWNTREND: Executing SELL on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=current_price)
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(10)
