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
        self.wfile.write(b"Dynamic Pure Internal Trailing Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Dynamic Trailing Bot Server Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD']

last_trade_close_time = 0
COOLDOWN_SECONDS = 15
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

def get_live_price(symbol):
    try:
        url = f"{BASE_URL}/v2/tickers"
        response = requests.get(url, timeout=5)
        data = response.json()
        for ticker in data.get('result', []):
            if ticker.get('symbol') == symbol:
                return float(ticker.get('mark_price', ticker.get('close', 0)))
    except Exception as e:
        print(f"Error fetching live price for {symbol}: {e}", flush=True)
    return None

def get_historical_candles(symbol):
    try:
        end_time = int(time.time())
        start_time = end_time - (6 * 3600)
        url = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution=5m&start={start_time}&end={end_time}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if not candles:
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
                    "symbol": pos.get('product_symbol', 'BTCUSD'),
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
    
    # Pure market order execution without fixed exchange stop loss to allow full bot trailing control
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
        print(f"[Executing] Sending pure {side.upper()} order for {symbol} (Size: {size}) with Internal Dynamic Trailing...", flush=True)
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
print("Dynamic Internal Trailing Bot Running...", flush=True)

while True:
    try:
        open_positions = get_open_positions()
        
        # FIRST: Real-time price tracking for dynamic trailing stop loss & maximum profit protection
        for p_id, pos_info in open_positions.items():
            symbol = pos_info["symbol"]
            pos_side = pos_info["side"]
            entry_price = pos_info["entry_price"]
            
            live_price = get_live_price(symbol)
            if live_price is None:
                continue
                
            profit_points = 0
            if pos_side == "buy":
                profit_points = live_price - entry_price
            elif pos_side == "sell":
                profit_points = entry_price - live_price
            
            print(f"[{symbol}] LIVE CHECK -> Side: {pos_side.upper()} | Entry: {entry_price} | Live Price: {live_price} | PnL: {round(profit_points, 2)} pts", flush=True)
            
            current_level = last_trailing_level.get(p_id, -60.0)
            
            # Dynamic Trailing Milestones
            if profit_points >= 600.0 and current_level < 550.0:
                last_trailing_level[p_id] = 550.0
                print(f"--> DYNAMIC TRAIL: Profit crossed 600 pts! Locking SL at +550 pts profit.", flush=True)
            elif profit_points >= 200.0 and current_level < 150.0:
                last_trailing_level[p_id] = 150.0
                print(f"--> DYNAMIC TRAIL: Profit crossed 200 pts! Locking SL at +150 pts profit.", flush=True)
            elif profit_points >= 100.0 and current_level < 50.0:
                last_trailing_level[p_id] = 50.0
                print(f"--> DYNAMIC TRAIL: Profit crossed 100 pts! Locking SL at +50 pts profit.", flush=True)
            elif profit_points >= 40.0 and current_level < 0.0:
                last_trailing_level[p_id] = 0.0
                print(f"--> DYNAMIC TRAIL: Profit crossed 40 pts! Moving SL to Breakeven.", flush=True)
            
            active_sl_limit = last_trailing_level.get(p_id, -60.0)
            if profit_points <= active_sl_limit:
                print(f"--> STOP LOSS / TRAILING HIT! Closing {symbol} safely at profit level {active_sl_limit}.", flush=True)
                close_position(p_id, symbol, pos_side, pos_info["size"])
                continue

        # SECOND: Check candles for EMA Crossover Signals
        for symbol, info in product_data.items():
            p_id = info["id"]
            trade_size = info["min_size"]
            
            closes = get_historical_candles(symbol)
            if len(closes) < 25:
                continue
                
            current_price = closes[-1]
            ema_fast = calculate_ema(closes, 8)
            ema_slow = calculate_ema(closes, 21)
            
            if ema_fast is None or ema_slow is None:
                continue
                
            has_position = p_id in open_positions
            pos_side = open_positions[p_id]["side"] if has_position else None
            
            if not has_position:
                if time.time() - last_trade_close_time < COOLDOWN_SECONDS:
                    continue
            
            ema_diff = abs(ema_fast - ema_slow)
            min_gap_required = current_price * 0.00001
            
            is_uptrend = (ema_fast > ema_slow) and (ema_diff > min_gap_required)
            is_downtrend = (ema_fast < ema_slow) and (ema_diff > min_gap_required)
            
            if has_position:
                if pos_side == "sell" and is_uptrend:
                    print(f"--> INSTANT TREND SHIFT BUY: Closing SELL and buying {symbol}!", flush=True)
                    close_position(p_id, symbol, "sell", open_positions[p_id]["size"])
                    time.sleep(1)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=current_price)
                    
                elif pos_side == "buy" and is_downtrend:
                    print(f"--> INSTANT TREND SHIFT SELL: Closing BUY and selling {symbol}!", flush=True)
                    close_position(p_id, symbol, "buy", open_positions[p_id]["size"])
                    time.sleep(1)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=current_price)
            else:
                if is_uptrend:
                    print(f"--> INSTANT SIGNAL UPTREND: Executing BUY on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "buy", size=trade_size, entry_price=current_price)
                    
                elif is_downtrend:
                    print(f"--> INSTANT SIGNAL DOWNTREND: Executing SELL on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "sell", size=trade_size, entry_price=current_price)
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(5)
