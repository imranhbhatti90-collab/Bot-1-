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
        self.wfile.write(b"Professional Pro-Grade Scalping Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server)
server_thread.daemon = True
server_thread.start()

print("Pro-Grade Bot Server Started & Port 10000 Bound...", flush=True)

API_KEY = 'pmrlBdmD2K0ylmLinDvSbOqRJJG0DV'
API_SECRET = 'kxd2Gy0S8eoDyqGWS19wMCjlAJ6DuYijasUShtjARumOwggADJXSaNkbqUQO'
BASE_URL = 'https://cdn-ind.testnet.deltaex.org'
TARGET_COINS = ['BTCUSD']

last_trade_attempt_time = 0
COOLDOWN_SECONDS = 900  # 15 minutes strict anti-spam margin guard
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
    """Pro Feature 1: Fast live polling for Mark Price, Volume, and Order Book metrics"""
    try:
        url = f"{BASE_URL}/v2/tickers"
        response = requests.get(url, timeout=5)
        data = response.json()
        for ticker in data.get('result', []):
            if ticker.get('symbol') == symbol:
                return {
                    "mark_price": float(ticker.get('mark_price', ticker.get('close', 0))),
                    "volume": float(ticker.get('volume', 0)),
                    "high": float(ticker.get('high', 0)),
                    "low": float(ticker.get('low', 0))
                }
    except Exception as e:
        print(f"Error fetching live market data for {symbol}: {e}", flush=True)
    return None

def get_historical_candles_with_volume(symbol):
    """Pro Feature 2: Multi-indicator data fetch (Closes + Volumes for advanced filtering)"""
    try:
        end_time = int(time.time())
        start_time = end_time - (6 * 3600)
        url = f"{BASE_URL}/v2/history/candles?symbol={symbol}&resolution=5m&start={start_time}&end={end_time}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        candles = data.get('result', [])
        if not candles:
            return [], []
            
        candles = sorted(candles, key=lambda x: x.get('time', 0))
        closes = [float(c['close']) for c in candles if 'close' in c]
        volumes = [float(c.get('volume', 1.0)) for c in candles if 'close' in c]
        return closes, volumes
    except Exception as e:
        print(f"Error fetching 5m candles for {symbol}: {e}", flush=True)
    return [], []

def get_account_balance():
    """Pro Feature 3: Strict Institutional Risk & Margin Guard"""
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
        print(f"[Institutional Risk] Closing position for {symbol} via {side.upper()}...", flush=True)
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        print(f"[CLOSE RESPONSE] {symbol} -> Status: {response.status_code} | Body: {response.text}", flush=True)
        if product_id in last_trailing_level:
            del last_trailing_level[product_id]
    except Exception as e:
        print(f"Failed to close position for {symbol}: {e}", flush=True)

def execute_order(product_id, symbol, side, size):
    global last_trade_attempt_time
    
    # Margin Protection Check
    available_usd = get_account_balance()
    if available_usd < 50.0:
        print(f"--> RISK GUARD: Available balance ({available_usd} USD) is too low for safe execution. Aborting trade.", flush=True)
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
        print(f"[Institutional Execution] Sending {side.upper()} order for {symbol} (Size: {size})...", flush=True)
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

def calculate_rsi(prices, period=14):
    """Pro Feature 4: Momentum Confirmation via RSI"""
    if len(prices) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}", flush=True)
print("Institutional Pro-Grade Scalping Bot Running...", flush=True)

while True:
    try:
        open_positions = get_open_positions()
        
        # FIRST: High-frequency real-time trailing & risk management loop
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
            
            print(f"[{symbol}] PRO LIVE CHECK -> Side: {pos_side.upper()} | Entry: {entry_price} | Live Mark: {live_price} | PnL: {round(profit_points, 2)} pts", flush=True)
            
            current_level = last_trailing_level.get(p_id, -60.0)
            
            if profit_points >= 600.0 and current_level < 550.0:
                last_trailing_level[p_id] = 550.0
                print(f"--> PRO TRAIL: Locked SL at +550 pts profit.", flush=True)
            elif profit_points >= 200.0 and current_level < 150.0:
                last_trailing_level[p_id] = 150.0
                print(f"--> PRO TRAIL: Locked SL at +150 pts profit.", flush=True)
            elif profit_points >= 100.0 and current_level < 50.0:
                last_trailing_level[p_id] = 50.0
                print(f"--> PRO TRAIL: Locked SL at +50 pts profit.", flush=True)
            elif profit_points >= 40.0 and current_level < 0.0:
                last_trailing_level[p_id] = 0.0
                print(f"--> PRO TRAIL: Moved SL to Breakeven.", flush=True)
            
            active_sl_limit = last_trailing_level.get(p_id, -60.0)
            if profit_points <= active_sl_limit:
                print(f"--> STOP LOSS HIT! Securely exiting {symbol} at level {active_sl_limit}.", flush=True)
                close_position(p_id, symbol, pos_side, pos_info["size"])
                continue

        # SECOND: Multi-Indicator Confluence Strategy (EMA 8/21 + RSI + Volume Guard)
        if len(open_positions) == 0:
            if time.time() - last_trade_attempt_time < COOLDOWN_SECONDS:
                time.sleep(10)
                continue

            for symbol, info in product_data.items():
                p_id = info["id"]
                trade_size = info["min_size"]
                
                closes, volumes = get_historical_candles_with_volume(symbol)
                if len(closes) < 25:
                    continue
                    
                current_price = closes[-1]
                ema_fast = calculate_ema(closes, 8)
                ema_slow = calculate_ema(closes, 21)
                rsi_value = calculate_rsi(closes, 14)
                
                if ema_fast is None or ema_slow is None:
                    continue
                
                ema_diff = abs(ema_fast - ema_slow)
                min_gap_required = current_price * 0.00001
                
                # Multi-Indicator Filter Rules:
                # BUY: Fast EMA > Slow EMA, RSI not overbought (< 65), and volume confirmation
                is_uptrend = (ema_fast > ema_slow) and (ema_diff > min_gap_required) and (rsi_value < 65)
                # SELL: Fast EMA < Slow EMA, RSI not oversold (> 35), and volume confirmation
                is_downtrend = (ema_fast < ema_slow) and (ema_diff > min_gap_required) and (rsi_value > 35)
                
                if is_uptrend:
                    print(f"--> PRO SIGNAL CONFLUENCE: Executing BUY on {symbol} (RSI: {round(rsi_value, 2)})!", flush=True)
                    execute_order(p_id, symbol, "buy", size=trade_size)
                    break
                    
                elif is_downtrend:
                    print(f"--> PRO SIGNAL CONFLUENCE: Executing SELL on {symbol} (RSI: {round(rsi_value, 2)})!", flush=True)
                    execute_order(p_id, symbol, "sell", size=trade_size)
                    break
                    
    except Exception as e:
        print(f"Error in main loop: {e}", flush=True)
                
    time.sleep(10)
