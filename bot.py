from flask import Flask
import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote

app = Flask(__name__)
@app.route('/')
def home():
    return "Closing USOON..."

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
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, headers=headers, json=body, timeout=15)
    print(f"{method} {path} -> {r.status_code} {r.text[:500]}")
    return r.json(), r.status_code

def close_usoon():
    # Pehle positions dekho
    res, code = api_req("GET", "/v2/positions/margined")
    if code == 200 and res.get("result"):
        lst = res["result"] if isinstance(res["result"], list) else [res["result"]]
        for p in lst:
            sym = p.get("product_symbol") or p.get("product",{}).get("symbol") or ""
            size = float(p.get("size",0))
            if "USOON" in str(sym) and size != 0:
                print(f"Found USOON {sym} size {size}")
                # Cancel all open orders first
                res2, _ = api_req("GET", "/v2/orders", params={"states":"open"})
                if res2.get("result"):
                    for o in res2["result"]:
                        if "USOON" in o.get("product_symbol",""):
                            api_req("DELETE", f"/v2/orders/{o.get('id')}")
                            time.sleep(0.5)
                # Now close with exact size
                side = "buy" if size < 0 else "sell"
                body = {
                    "product_symbol": "USOONUSD",
                    "size": abs(size),
                    "side": side,
                    "order_type": "market_order",
                    "reduce_only": True
                }
                print(f"Trying to close {sym} with {body}")
                res3, code3 = api_req("POST", "/v2/orders", body=body)
                print(f"CLOSE RESULT: {code3} {res3}")
                # Agar market fail ho to limit try karo
                if code3 != 200:
                    body["order_type"] = "limit_order"
                    body["limit_price"] = "149.8"
                    print(f"Trying limit close {body}")
                    res4, code4 = api_req("POST", "/v2/orders", body=body)
                    print(f"LIMIT CLOSE RESULT: {code4} {res4}")

# Run once on start
close_usoon()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
