# ==================================================
# MARKET ENTRY
# ==================================================

def place_entry(product, side):
    if not ENABLE_TRADING:
        logging.warning(
            "[SAFE MODE] ENABLE_TRADING=false; no order sent. "
            "Signal=%s",
            side.upper(),
        )
        return

    if not API_KEY or not API_SECRET:
        raise RuntimeError("API credentials missing")

    if state.get("position"):
        logging.warning(
            "Saved bot position exists; refusing another entry"
        )
        return

    current = get_open_position()

    if current:
        logging.warning(
            "An exchange position is already open; refusing entry"
        )
        return

    set_leverage(product)

    product_id = int(product["id"])

    body = {
        "product_id": product_id,
        "product_symbol": SYMBOL,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "market_order",
        "time_in_force": "gtc",
        "reduce_only": False,
        "client_order_id": (
            f"ema921_{int(time.time())}"
        ),
    }

    logging.info("[ENTRY REQUEST] %s", body)

    result = api_request(
        "POST",
        "/v2/orders",
        body=body,
    )

    order = result.get("result", {})
    order_id = order.get("id")

    logging.info("[ENTRY RESPONSE] %s", result)

    # Wait up to 20 seconds for the market order to fill and position to appear.
    position = None

    for _ in range(20):
        time.sleep(1)
        position = get_open_position()

        if position:
            break

    if not position:
        raise RuntimeError(
            "Entry order was accepted but no open position appeared within 20s. "
            "Check Testnet order history before restarting."
        )

    entry = position["entry"]
    size = position["size"]
    actual_side = position["side"]

    if actual_side != side:
        raise RuntimeError(
            "Exchange position side differs from signal; "
            "manual review required"
        )

    stop = initial_stop(actual_side, entry, product)

    state["position"] = {
        "entry_order_id": order_id,
        "side": actual_side,
        "entry": str(entry),
        "size": size,
        "product_id": product_id,
        "stop_order_id": None,
        "stop_price": str(stop),
        "highest_profit_step": 0,
    }
    save_state(state)

    try:
        stop_result = place_stop_order(
            product,
            actual_side,
            size,
            stop,
        )

        stop_order = stop_result.get("result", {})
        stop_id = stop_order.get("id")

        if not stop_id:
            raise RuntimeError(
                f"Stop order ID missing from response: {stop_result}"
            )

        state["position"]["stop_order_id"] = stop_id
        save_state(state)

        logging.info(
            "[PROTECTED POSITION] side=%s entry=%s size=%s SL=%s "
            "stop_order_id=%s",
            actual_side,
            entry,
            size,
            stop,
            stop_id,
        )

    except Exception:
        logging.exception(
            "CRITICAL: protective stop order failed after entry."
        )
        raise
