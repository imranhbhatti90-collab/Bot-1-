  = str(pos.get('product_id'))
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

def get_open_orders(product_id):
    path = f'/v2/orders?product_id={product_id}&state=open'
    method = 'GET'
    timestamp = str(int(time.time()))
    signature = generate_signature(API_SECRET, method, timestamp, '/v2/orders', f'product_id={product_id}&state=open', '')
    
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
        return data.get('result', [])
    except Exception as e:
        print(f"Failed to fetch open orders: {e}", flush=True)
    return []

def cancel_order(order_id, product_id):
    if not order_id:
        return
    path = f'/v2/orders?order_id={order_id}&product_id={product_id}'
    method = 'DELETE'
    timestamp = str(int(time.time()))
    signature = generate_signature(API_SECRET, method, timestamp, '/v2/orders', f'order_id={order_id}&product_id={product_id}', '')
    
    req_headers = {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'python-rest-client',
        'Content-Type': 'application/json'
    }
    try:
        response = requests.delete(BASE_URL + path, headers=req_headers, timeout=10)
        print(f"[CANCEL ORDER] ID {order_id} -> Status: {response.status_code}", flush=True)
    except Exception as e:
        print(f"Failed to cancel order: {e}", flush=True)

def place_stop_loss_order(product_id, size, exit_side, sl_price):
    path = '/v2/orders'
    method = 'POST'
    timestamp = str(int(time.time()))
    
    payload_data = {
        "product_id": int(product_id),
        "size": int(size),
        "side": exit_side,
        "order_type": "stop_order",
        "stop_order_type": "market_order",  # Added required parameter for Delta Exchange
        "stop_price": round(sl_price, 1),
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
        response = requests.post(BASE_URL + path, headers=req_headers, data=payload, timeout=10)
        data = response.json()
        print(f"[STOP ORDER PLACED] SL at {round(sl_price, 1)} -> Status: {response.status_code} | Body: {response.text}", flush=True)
        if data.get('success'):
            return data.get('result', {}).get('id')
    except Exception as e:
        print(f"Failed to place stop loss order: {e}", flush=True)
    return None

def execute_trade_pure_market(product_id, symbol, side, size, current_price):
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
    } e:
        print(f"Error in 
