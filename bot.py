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
        self.wfile.write(b"Strict Trend BTC Scalping Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Strict Trend BTC Scalping Bot Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD']

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
        print(f"[Closing] Exiting position for {symbol} via {side.upper()}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[CLOSE RESPONSE] {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
    except Exception as e:
        print(f"Failed to close position for {symbol}: {e}", flush=True)

def execute_order(product_id, symbol, side, size, entry_price):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    # Strict 30 points Stop Loss
    sl_offset = 30.0
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
        print(f"[Executing] Sending {side.upper()} order for {symbol} (Size: {size}, SL: {stop_loss_price})...", flush=True)
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
        start_time = current_time - 14400
        
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&symbol={symbol}&resolution=5m&start={start_time}&end={current_time}&limit=100"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if len(candles) < 10:
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
print("Strict Trend & Target Bot Active...", flush=True)

while True:
    try:
        print("[Loop] Checking strict market direction...", flush=True)
        open_positions = get_open_positions()
        
        for symbol, info in product_data.items():
            p_id = info["id"]
            trade_size = info["min_size"]
            
            c_time, close_p, ema9, ema21 = get_market_data(p_id, symbol)
            if c_time is None or ema9 is None or ema21 is None:
                continue
            
            print(f"[{symbol}] Price: {close_p} | EMA9: {round(ema9, 2)} | EMA21: {round(ema21, 2)}", flush=True)
                
            has_position = p_id in open_positions
            pos_side = open_positions[p_id]["side"] if has_position else None
            entry_price = open_positions[p_id]["entry_price"] if has_position else 0
            
            # Check Profit/Loss Exit Conditions in Loop
            if has_position:
                profit_points = 0
                if pos_side == "buy":
                    profit_points = close_p - entry_price
                elif pos_side == "sell":
                    profit_points = entry_price - close_p
                
                print(f"[{symbol}] Active {pos_side.upper()} | Entry: {entry_price} | PnL Points: {round(profit_points, 2)}", flush=True)
                
                # 60 Points Profit Target Reached
                if profit_points >= 60.0:
                    print(f"--> TARGET REACHED (+60 Points)! Taking profit on {symbol}.", flush=True)
                    close_position(p_id, symbol, pos_side, open_positions[p_id]["size"])
                    continue
                
                # 30 Points Stop Loss Reached
                if profit_points <= -30.0:
                    print(f"--> STOP LOSS HIT (-30 Points)! Cutting loss on {symbol} immediately.", flush=True)
                    close_position(p_id, symbol, pos_side, open_positions[p_id]["size"])
                    continue
            
            ema_diff = abs(ema9 - ema21)
            min_gap_required = close_p * 0.0004  # Thoda aur strict gap filter taaki fake moves na pakde
            
            # Strict Directional Conditions
            is_strict_buy = (ema9 > ema21) and (ema_diff > min_gap_required)
            is_strict_sell = (ema9 < ema21) and (ema_diff > min_gap_required)
            
            if has_position:
                if pos_side == "sell" and is_strict_buy:
                    print(f"--> TREND REVERSAL TO BUY: Closing SELL and taking BUY on {symbol}!", flush=True)
                    close_position(p_id, symbol, "sell", open_positions[p_id]["size"])
                    time.sleep(2)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=close_p)
                    
                elif pos_side == "buy" and is_strict_sell:
                    print(f"--> TREND REVERSAL TO SELL: Closing BUY and taking SELL on {symbol}!", flush=True)
                    close_position(p_id, symbol, "buy", open_positions[p_id]["size"])
                    time.sleep(2)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
            else:
                if is_strict_buy:
                    print(f"--> UPTREND DETECTED: Executing BUY on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=close_p)
                    
                elif is_strict_sell:
                    print(f"--> DOWNTREND DETECTED: Executing SELL on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=close_p)
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(20)
