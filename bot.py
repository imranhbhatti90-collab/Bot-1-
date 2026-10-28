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
            print(f"[DELETE RESP] {r.text}", flush=True)  # Debug
            return {"success": True}, r.status_code
        else:
            r = requests.post(url, headers=headers, json=body, timeout=15)
        print(f"[API DEBUG] {method} {path} -> {r.status_code} BODY: {r.text[:500]}", flush=True)  # Ye line add kar
        return r.json(), r.status_code
    except Exception as e:
        return {"error": str(e)}, 500
