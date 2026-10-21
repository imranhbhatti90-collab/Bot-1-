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
        self.wfile.write(b"Smart Pro-Momentum Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Smart Momentum Bot Server Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD']

last_trade_attempt_time = 0
COOLDOWN_SECONDS = 3600  # 1 hour strict gap so bot waits for the absolute best setup!
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
                p_id = str(product.get('id'))
                product_info[symbol] = {
                    "id": p_id,
                    "min_size": 40.0
                }
    except Exception as e:
        print("Error fetching products info:", e, flush=True)
    return product_info

def get_live_market_data(symbol):
    try:
        url = f"{BASE_URL}/v2/tickers"
        response = requests.get(url, timeout=5)
        data = response.json()
        for ticker in data.get('result', []):
            if ticker.get('symbol') == symbol:
                return {
                    "mark_price": float(ticker.get('mark_price', ticker.get('close', 0)))
                }
    except Exception as e:
        print(f"Error fetching live market data for {symbol}: {e}", flush=True)
    return None

def get_historical_candles(symbol):
    try:
        end_time = int(time.time())
        start_time = end_time - (12 * 3600) # Look back 12 hours for solid trend data
        url = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution=15m&start={start_time}&end={end_time}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if not candles:
            return []
            
        candles = sorted(candles, key=lambda x: x.get('time', 0))
        closes = [float(c['close']) for c in candles if 'close' in c]
        return closes
    except Exception as e:
        print(f"Error fetching candles for {symbol}: {e}", flush=True)
    return []

def get_account_balance():
    path = '/v2/wallet/balances'
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
        for asset in data.get('result', []):
            if asset.get('asset_symbol') == 'USD':
                return float(asset.get('available_balance', 0))
    except Exception as e:
        print("Error fetching account balance:", e, flush=True)
    return 0.0

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
                p_id = str(pos.get('product_id'))
                positions[p_id] = {
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
    global last_trailing_level
    side = "sell" if current_side == "buy" else "buy"
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": int(product_id),
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
        print(f"[Exit] Closing position for {symbol} via {side.upper()}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[CLOSE RESPONSE] {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
        if product_id in last_trailing_level:
            del last_trailing_level[product_id]
    except Exception as e:
        print(f"Failed to close position for {symbol}: {e}", flush=True)

def execute_order(product_id, symbol, side, size):
    global last_trade_attempt_time
    
    available_usd = get_account_balance()
    if available_usd < 40.0:
        print(f"--> RISK GUARD: Available balance ({available_usd} USD) is too low. Aborting.", flush=True)
        return

    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": int(product_id),
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
        print(f"[Execution] Sending high-conviction {side.upper()} order for {symbol} (Size: {size})...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[{side.upper()}] API ORDER RESPONSE for {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
        last_trade_attempt_time = time.time()
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
print("Smart Pro-Momentum Bot Running (1-Hour Cooldown Filter Active)...", flush=True)

while True:
    try:
        open_positions = get_open_positions()
        
        # FIRST: Real-time trailing stop-loss management
        for p_id, pos_info in open_positions.items():
            symbol = pos_info["symbol"]
            pos_side = pos_info["side"]
            entry_price = pos_info["entry_price"]
            
            market_data = get_live_market_data(symbol)
            if not market_data:
                continue
            live_price = market_data["mark_price"]
                
            profit_points = 0
            if pos_side == "buy":
                profit_points = live_price - entry_price
            elif pos_side == "sell":
                profit_points = entry_price - live_price
            
            print(f"[{symbol}] LIVE CHECK -> Side: {pos_side.upper()} | Entry: {entry_price} | Live Mark: {live_price} | PnL: {round(profit_points, 2)} pts", flush=True)
            
            current_level = last_trailing_level.get(p_id, -60.0)
            
            if profit_points >= 600.0 and current_level < 550.0:
                last_trailing_level[p_id] = 550.0
                print(f"--> TRAIL: Locked SL at +550 pts profit.", flush=True)
            elif profit_points >= 200.0 and current_level < 150.0:
                last_trailing_level[p_id] = 150.0
                print(f"--> TRAIL: Locked SL at +150 pts profit.", flush=True)
            elif profit_points >= 100.0 and current_level < 50.0:
                last_trailing_level[p_id] = 50.0
                print(f"--> TRAIL: Locked SL at +50 pts profit.", flush=True)
            elif profit_points >= 40.0 and current_level < 0.0:
                last_trailing_level[p_id] = 0.0
                print(f"--> TRAIL: Moved SL to Breakeven.", flush=True)
            
            active_sl_limit = last_trailing_level.get(p_id, -60.0)
            if profit_points <= active_sl_limit:
                print(f"--> STOP LOSS HIT! Exiting {symbol} safely at level {active_sl_limit}.", flush=True)
                close_position(p_id, symbol, pos_side, pos_info["size"])
                continue

        # SECOND: High-Conviction 15m Trend-Following Entry (Strict Filtering)
        if len(open_positions) == 0:
            if time.time() - last_trade_attempt_time < COOLDOWN_SECONDS:
                time.sleep(15)
                continue

            for symbol, info in product_data.items():
                p_id = info["id"]
                trade_size = info["min_size"]
                
                closes = get_historical_candles(symbol)
                if len(closes) < 30:
                    continue
                    
                current_price = closes[-1]
                ema_fast = calculate_ema(closes, 12) # 12 Period on 15m chart
                ema_slow = calculate_ema(closes, 26) # 26 Period on 15m chart
                
                if ema_fast is None or ema_slow is None:
                    continue
                
                # High-Conviction Filters (Bade Trend par hi trade milegi)
                is_strong_uptrend = (ema_fast > ema_slow) and (current_price > ema_fast)
                is_strong_downtrend = (ema_fast < ema_slow) and (current_price < ema_fast)
                
                if is_strong_uptrend:
                    print(f"--> HIGH-CONVICTION UPTREND: Executing BUY on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "buy", size=trade_size)
                    break
                    
                elif is_strong_downtrend:
                    print(f"--> HIGH-CONVICTION DOWNTREND: Executing SELL on {symbol}!", flush=True)
                    execute_order(p_id, symbol, "sell", size=trade_size)
                    break
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(15)
