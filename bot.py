from flask import Flask
import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote

app = Flask(__name__)
@app.route('/')
def home():
    return "Closing USOON Final Try"

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
            # DELETE returns empty body
            if r.status_code == 200 or r.status_code == 204:
                return {"success": True}, r.status_code
            try:
                return r.json(), r.status_code
            except:
                return {"success": True}, r.status_code
        else:
            r = requests.post(url, headers=headers, json=body, timeout=15)
        try:
            return r.json(), r.status_code
        except:
            return {"text": r.text[:500]}, r.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def close_usoon():
    res, code = api_req("GET", "/v2/positions/margined")
    if code == 200 and res.get("result"):
        lst = res["result"] if isinstance(res["result"], list) else [res["result"]]
        for p in lst:
            sym = p.get("product_symbol") or p.get("product",{}).get("symbol") or ""
            size = float(p.get("size",0))
            if "USOON" in str(sym) and size != 0:
                print(f"Found USOON {sym} size {size}", flush=True)
                # Cancel all open USOON orders
                res2, _ = api_req("GET", "/v2/orders", params={"states":"open"})
                if res2.get("result"):
                    for o in res2["result"]:
                        if "USOON" in o.get("product_symbol",""):
                            oid = o.get("id")
                            print(f"Cancelling order {oid}", flush=True)
                            api_req("DELETE", f"/v2/orders/{oid}")
                            time.sleep(1)
                # Try with higher price - 152
                side = "buy" if size < 0 else "sell"
                body = {
                    "product_symbol": "USOONUSD",
                    "size": abs(size),
                    "side": side,
                    "order_type": "limit_order",
                    "limit_price": "152.0",
                    "reduce_only": True
                }
                print(f"Trying close with {body}", flush=True)
                res3, code3 = api_req("POST", "/v2/orders", body=body)
                print(f"CLOSE RESULT: {code3} {res3}", flush=True)

close_usoon()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
