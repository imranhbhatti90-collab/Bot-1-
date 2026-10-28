from flask import Flask
import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote

app = Flask(__name__)
@app.route('/')
def home():
    return "Closing USOON No ReduceOnly"

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
    for i in range(5):
        res, code = api_req("GET", "/v2/positions/margined")
        print(f"Check {i} Positions -> {code}", flush=True)
        if code == 200 and res.get("result"):
            lst = res["result"] if isinstance(res["result"], list) else [res["result"]]
            found = False
            for p in lst:
                sym = p.get("product_symbol") or p.get("product",{}).get("symbol") or ""
                size = float(p.get("size",0))
                if "USOON" in str(sym) and size != 0:
                    found = True
                    print(f"Found USOON {sym} size {size} - Attempt {i}", flush=True)
                    # Cancel all
                    res2, _ = api_req("GET", "/v2/orders", params={"states":"open"})
                    if res2.get("result"):
                        for o in res2["result"]:
                            if "USOON" in o.get("product_symbol",""):
                                api_req("DELETE", f"/v2/orders/{o.get('id')}")
                                time.sleep(1)
                    # Try WITHOUT reduce_only - direct close
                    side = "buy" if size < 0 else "sell"
                    # Try market first
                    body = {
                        "product_symbol": "USOONUSD",
                        "size": abs(size),
                        "side": side,
                        "order_type": "market_order"
                    }
                    print(f"Trying MARKET close WITHOUT reduce_only: {body}", flush=True)
                    res3, code3 = api_req("POST", "/v2/orders", body=body)
                    print(f"MARKET CLOSE: {code3} {res3}", flush=True)
                    time.sleep(2)
                    # If market fails, try limit
                    if code3 != 200:
                        body = {
                            "product_symbol": "USOONUSD",
                            "size": abs(size),
                            "side": side,
                            "order_type": "limit_order",
                            "limit_price": "155.0"
                        }
                        print(f"Trying LIMIT close WITHOUT reduce_only: {body}", flush=True)
                        res4, code4 = api_req("POST", "/v2/orders", body=body)
                        print(f"LIMIT CLOSE: {code4} {res4}", flush=True)
            if not found:
                print("No USOON position found - ALL CLOSED! ✅", flush=True)
                return
        time.sleep(3)

close_usoon()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
