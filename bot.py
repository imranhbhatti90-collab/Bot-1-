from flask import Flask
import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote

app = Flask(__name__)
@app.route('/')
def home():
    return "Closing USOON at market price"

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

def api_req(method, path, params=None, body=None):
    ts = str(int(time.time()))
    if params:
        qs = urlencode(params, quote_via=quote)
        pqs = f"{path}?{qs}"
    else:
        pqs = path
    if body:
        msg = method + ts + pqs + json.dumps(body)
    else:
        msg = method + ts + pqs
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    headers = {"api-key": API_KEY, "timestamp": ts, "signature": sig, "Content-Type": "application/json"}
    url = BASE_URL + pqs
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=15)
        elif method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=15)
            return {"success": True}, r.status_code
        else:
            r = requests.post(url, headers=headers, json=body, timeout=15)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def close_usoon():
    # Get orderbook first
    res, code = api_req("GET", "/v2/l2orderbook/USOONUSD")
    print(f"Orderbook: {res}", flush=True)
    
    for attempt in range(20):
        res, code = api_req("GET", "/v2/positions/margined")
        if code != 200 or not res.get("result"):
            time.sleep(2)
            continue
        lst = res["result"] if isinstance(res["result"], list) else [res["result"]]
        usoon_pos = None
        for p in lst:
            sym = p.get("product_symbol") or p.get("product",{}).get("symbol") or ""
            size = float(p.get("size",0))
            if "USOON" in str(sym) and size != 0:
                usoon_pos = (sym, size, p.get("entry_price"))
                break
        if not usoon_pos:
            print(f"✅ USOON ALL CLOSED!", flush=True)
            return
        sym, size, entry = usoon_pos
        print(f"Attempt {attempt}: {sym} size {size} entry {entry}", flush=True)
        
        # Cancel all
        res2, _ = api_req("GET", "/v2/orders", params={"states":"open"})
        if res2.get("result"):
            for o in res2["result"]:
                if "USOON" in o.get("product_symbol",""):
                    api_req("DELETE", f"/v2/orders/{o.get('id')}")
                    time.sleep(0.5)
        
        # Try with ask price + small buffer - use 147.45 which is near mark
        for price in ["147.50", "147.80", "148.00", "148.50"]:
            side = "buy" if size < 0 else "sell"
            body = {
                "product_symbol": "USOONUSD",
                "size": 1,
                "side": side,
                "order_type": "limit_order",
                "limit_price": price,
                "reduce_only": True
            }
            print(f"Trying price {price}", flush=True)
            res3, code3 = api_req("POST", "/v2/orders", body=body)
            print(f"Result {price}: {code3} {str(res3)[:200]}", flush=True)
            time.sleep(4)
            # Check if position reduced
            res5, _ = api_req("GET", "/v2/positions/margined")
            if res5.get("result"):
                lst5 = res5["result"] if isinstance(res5["result"], list) else [res5["result"]]
                for p5 in lst5:
                    sym5 = p5.get("product_symbol") or ""
                    size5 = float(p5.get("size",0))
                    if "USOON" in str(sym5):
                        if abs(size5) < abs(size):
                            print(f"Progress! Size reduced from {size} to {size5}", flush=True)
                            break
            # Cancel this order before next price
            res6, _ = api_req("GET", "/v2/orders", params={"states":"open"})
            if res6.get("result"):
                for o in res6["result"]:
                    if "USOON" in o.get("product_symbol",""):
                        api_req("DELETE", f"/v2/orders/{o.get('id')}")
            time.sleep(1)

close_usoon()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
