from flask import Flask
import os, json, time, hmac, hashlib, requests
from urllib.parse import urlencode, quote

app = Flask(__name__)
@app.route('/')
def home():
    return "Closing USOON 1 by 1"

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
    for attempt in range(40):
        res, code = api_req("GET", "/v2/positions/margined")
        if code != 200 or not res.get("result"):
            print(f"No positions", flush=True)
            time.sleep(2)
            continue
        lst = res["result"] if isinstance(res["result"], list) else [res["result"]]
        usoon_pos = None
        for p in lst:
            sym = p.get("product_symbol") or p.get("product",{}).get("symbol") or ""
            size = float(p.get("size",0))
            if "USOON" in str(sym) and size != 0:
                usoon_pos = (sym, size)
                break
        if not usoon_pos:
            print(f"✅ USOON ALL CLOSED! No position left!", flush=True)
            return
        sym, size = usoon_pos
        print(f"Attempt {attempt}: Found {sym} size {size} - Closing 1 lot", flush=True)
        # Cancel all open USOON orders first
        res2, _ = api_req("GET", "/v2/orders", params={"states":"open"})
        if res2.get("result"):
            for o in res2["result"]:
                if "USOON" in o.get("product_symbol",""):
                    api_req("DELETE", f"/v2/orders/{o.get('id')}")
                    time.sleep(0.5)
        # Close 1 lot only
        side = "buy" if size < 0 else "sell"
        body = {
            "product_symbol": "USOONUSD",
            "size": 1,
            "side": side,
            "order_type": "limit_order",
            "limit_price": "160.0",
            "reduce_only": True
        }
        res3, code3 = api_req("POST", "/v2/orders", body=body)
        print(f"Close 1 lot: {code3} {res3}", flush=True)
        time.sleep(3)
        # Check if filled
        res4, _ = api_req("GET", "/v2/orders", params={"states":"open"})
        # If still open after 3 sec, cancel and try higher price
        if res4.get("result"):
            for o in res4["result"]:
                if "USOON" in o.get("product_symbol",""):
                    print(f"Order not filled, cancelling and trying higher price", flush=True)
                    api_req("DELETE", f"/v2/orders/{o.get('id')}")
        time.sleep(2)

close_usoon()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
