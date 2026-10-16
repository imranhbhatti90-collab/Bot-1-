API_SECRET, method, timestamp, path, '', payload)
    
    headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(BASE_URL + path, headers=headers, data=payload)
        print(f"Leverage set to {LEVERAGE}x for {symbol}:", response.json())
    except Exception as e:
        print(f"Failed to set leverage for {symbol}:", e)

def get_latest_candle_info(product_id):
    try:
        url = f"{BASE_URL}/v2/history/candles?product_id={product_id}&resolution=5m&limit=2"
        response = requests.get(url)
        data = response.json()
        candles = data.get('result', [])
        if candles and len(candles) >= 1:
            latest = candles[0]
            candle_time = latest.get('time', 0)
            open_price = float(latest.get('open', 0))
            close_price = float(latest.get('close', 0))
            candle_diff = close_price - open_price
            return candle_time, open_price, close_price, candle_diff
    except Exception as e:
        print("Error fetching candle data:", e)
    return None, 0, 0, 0

def execute_order(product_id, symbol, side, size):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": product_id,
        "size": int(size),
        "side": side,
        "order_type": "market_order"
    }
    
    payload = json.dumps(payload_data)
    signature = generate_signature(API_SECRET, method, timestamp, path, '', payload)
    
    headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(BASE_URL + path, headers=headers, data=payload)
        print(f"[{side.upper()}] Candle Order Response for {symbol}:", response.json())
    except Exception as e:
        print(f"Order execution failed for {symbol}:", e)

product_data = get_target_product_info()
print(f"Loaded Target Products & Sizes: {product_data}")

for symbol, info in product_data.items():
    set_product_leverage(info["id"], symbol)

print("Fast candle monitoring started...")

DIP_THRESHOLD = 20.0    
PUMP_THRESHOLD = 20.0   

last_traded_candle = {}

while True:
    for symbol, info in product_data.items():
        p_id = info["id"]
        trade_size = info["min_size"]
        
        c_time, open_p, close_p, candle_diff = get_latest_candle_info(p_id)
        if c_time is None:
            continue
            
        if symbol not in last_traded_candle:
            last_traded_candle[symbol] = 0
            
        if c_time != last_traded_candle[symbol]:
            print(f"[Live Check] {symbol} -> Open: {open_p} | Close: {close_p} | Diff: {candle_diff}")
            
            if candle_diff <= -DIP_THRESHOLD:
                print(f"5m Red Candle Drop on {symbol}! Diff: {abs(candle_diff)}. Executing BUY...")
                execute_order(p_id, symbol, "buy", size=trade_size)
                last_traded_candle[symbol] = c_time
                
            elif candle_diff >= PUMP_THRESHOLD:
                print(f"5m Green Candle Pump on {symbol}! Diff: {candle_diff}. Executing SELL...")
                execute_order(p_id, symbol, "sell", size=trade_size)
                last_traded_candle[symbol] = c_time
                
    time.sleep(15)
