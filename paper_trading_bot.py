import os
import time
import hmac
import hashlib
import json
import requests
import threading
from flask import Flask
from datetime import datetime

# --- FLASK SERVER FOR RENDER PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Aman's Master Precision Real Trading Bot Active!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- CONFIGURATION & ENV VARS ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EXCHANGE_API_KEY = os.environ.get("EXCHANGE_API_KEY", "")
EXCHANGE_SECRET_KEY = os.environ.get("EXCHANGE_SECRET_KEY", "")
SHEET_WEBAPP_URL = os.environ.get("SHEET_WEBAPP_URL", "")

# --- LIVE TRADING SWITCH ---
# False = current behaviour: internal simulation + Telegram alerts only (safe default).
# True  = every entry/exit in run_bot() ALSO calls the real Shark Exchange endpoints below.
# Keep this False until you've test-fired shark_place_order() manually and confirmed
# it fills correctly, and until the two "CONFIRM WITH ME" items below are resolved.
# Controlled from Render's Environment tab: set LIVE_TRADING_ENABLED=true to go live.
# If the variable is missing or anything else, the bot stays in PAPER mode (no real orders).
LIVE_TRADING_ENABLED = os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
MODE_TAG = "LIVE" if LIVE_TRADING_ENABLED else "PAPER"

# ==========================================================================
# SHARK EXCHANGE (sharkexchange.in) - REST CLIENT
# Base URL + HMAC-SHA256 signing pattern taken from their API docs screenshots.
# ==========================================================================
SHARK_BASE_URL = "https://api.sharkexchange.in"          # authenticated endpoints
SHARK_PUBLIC_BASE_URL = "https://api.sharkexchange.in"    # public /v1/market/* endpoints
SHARK_SYMBOL = "BTCUSDT"        # confirmed from their example response (contractType: PERPETUAL)
SHARK_MARGIN_ASSET = "INR"      # confirmed from their example response (INR-native platform)


def _shark_sign(payload_str):
    """HMAC-SHA256(secret, payload) -> hex digest, per Shark's docs."""
    return hmac.new(
        EXCHANGE_SECRET_KEY.encode("utf-8"),
        payload_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def shark_get(endpoint, params=None):
    """GET request. Market endpoints (/v1/market/*) are public - no key/signature.
    Everything else is signed: signature = HMAC-SHA256(secret, query_string)."""
    params = dict(params or {})
    is_market = endpoint.startswith("/v1/market")
    base = SHARK_PUBLIC_BASE_URL if is_market else SHARK_BASE_URL
    headers = {}

    if not is_market:
        params["timestamp"] = str(int(time.time() * 1000))

    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base}{endpoint}"
    if query_string:
        url += f"?{query_string}"

    if not is_market:
        headers = {"api-key": EXCHANGE_API_KEY, "signature": _shark_sign(query_string)}

    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def shark_post(endpoint, params=None):
    """POST request. Signature = HMAC-SHA256(secret, JSON body string).
    IMPORTANT: we sign the exact same string we send as the body, so the
    server recomputes the same signature (dict key order must not shift)."""
    params = dict(params or {})
    is_market = endpoint.startswith("/v1/market")
    base = SHARK_PUBLIC_BASE_URL if is_market else SHARK_BASE_URL
    headers = {"Content-Type": "application/json"}

    if not is_market:
        params["timestamp"] = str(int(time.time() * 1000))

    body_str = json.dumps(params, separators=(',', ':'))
    url = f"{base}{endpoint}"

    if not is_market:
        headers["api-key"] = EXCHANGE_API_KEY
        headers["signature"] = _shark_sign(body_str)

    r = requests.post(url, data=body_str, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def shark_delete(endpoint, params=None):
    """DELETE request with a signed JSON body (their JS example sends the
    body via axios's `data` option on a DELETE call)."""
    params = dict(params or {})
    params["timestamp"] = str(int(time.time() * 1000))
    body_str = json.dumps(params, separators=(',', ':'))
    headers = {
        "Content-Type": "application/json",
        "api-key": EXCHANGE_API_KEY,
        "signature": _shark_sign(body_str),
    }
    url = f"{SHARK_BASE_URL}{endpoint}"
    r = requests.delete(url, data=body_str, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def shark_place_order(order_type, side, quantity, price=None, reduce_only=False,
                       take_profit_price=None, stop_loss_price=None, stop_price=None):
    """POST /v1/order/place-order
    order_type: 'MARKET' | 'LIMIT' | 'STOP_MARKET' | 'STOP_LIMIT'
    side: 'BUY' | 'SELL'
    reduce_only: True when this order is meant to CLOSE an existing position.
    take_profit_price / stop_loss_price: exchange sets these SERVER-SIDE on the
        order itself (confirmed in their docs table) - preferred over the bot
        polling price and closing manually, since it works even if the bot
        process restarts or the network drops.
    stop_price: required by their docs for STOP_MARKET / STOP_LIMIT orders.

    CONFIRMED from their official Python code sample (not just the JS one):
    - the order-type field is called `type`, NOT `orderType` (their param
      table was wrong/outdated on this).
    - `deviceType` and `userCategory` are required fields not listed in the
      param table at all - included below as 'WEB' / 'EXTERNAL' per their sample.
    - the signature must be computed over a COMPACT json.dumps (no spaces):
      json.dumps(params, separators=(',', ':')) - handled inside shark_post()."""
    if order_type not in ("MARKET", "LIMIT", "STOP_MARKET", "STOP_LIMIT"):
        raise ValueError(f"Invalid order_type: {order_type}")
    if quantity <= 0:
        raise ValueError("Quantity must be greater than 0")

    params = {
        "placeType": "ORDER_FORM",
        "quantity": quantity,
        "side": side,
        "symbol": SHARK_SYMBOL,
        "type": order_type,
        "reduceOnly": reduce_only,
        "marginAsset": SHARK_MARGIN_ASSET,
        "deviceType": "WEB",
        "userCategory": "EXTERNAL",
    }
    if order_type in ("LIMIT", "STOP_LIMIT"):
        if price is None or price <= 0:
            raise ValueError(f"Price must be specified and > 0 for {order_type} orders")
        params["price"] = price
    if order_type in ("STOP_MARKET", "STOP_LIMIT"):
        if stop_price is None:
            raise ValueError(f"stop_price is required for {order_type} orders")
        params["stopPrice"] = stop_price
    if take_profit_price is not None:
        params["takeProfitPrice"] = take_profit_price
    if stop_loss_price is not None:
        params["stopLossPrice"] = stop_loss_price

    return shark_post("/v1/order/place-order", params)


def shark_cancel_all_orders():
    """DELETE /v1/order/cancel-all-orders - confirmed from docs."""
    return shark_delete("/v1/order/cancel-all-orders", {})


def shark_close_all_positions():
    """DELETE /v1/positions/close-all-positions - confirmed from docs.
    Closes every open position on the account (not just BTCUSDT), so only
    call this when you're sure no other manual positions are open."""
    return shark_delete("/v1/positions/close-all-positions", {})


def shark_get_futures_wallet_balance():
    """GET /v1/wallet/futures-wallet/details - real account balance from
    the exchange, instead of trusting our own tracked ACCOUNT_BALANCE."""
    return shark_get("/v1/wallet/futures-wallet/details", {"marginAsset": SHARK_MARGIN_ASSET})

# REAL TRADING CAPITAL PARAMETERS
# Risk is now a PERCENTAGE of account balance, not a fixed dollar amount.
# $40 balance x 1% risk = $0.40 risk -> $0.40 / 200 SL points = 0.002 BTC,
# which lands exactly on the exchange's minimum tradable quantity.
ACCOUNT_BALANCE = 40.0        # Real account balance in USD
RISK_PERCENT = 1.0            # Risk 1% of current balance per trade
MIN_QTY = 0.002               # Strictly 0.002 BTC Minimum Quantity (exchange floor)
SL_POINTS = 200.0             # Fixed 200 Points SL
TP_POINTS = 2000.0            # Fixed 1:10 RR Target (2000 Points)

# Trailing Config
TRAIL_TRIGGER_PTS = 1600.0
TRAIL_LOCK_PTS = 600.0

# Zone Escalation Config (Max 9 SL Rule = 3 zones x 3 SL each)
MAX_SL_PER_ZONE = 3
MAX_ZONE = 3

# Monthly Dicy Trap Invalidation
DICY_INVALIDATION_PTS = 3000.0

# Base Trade State Tracking
current_position = None
base_zone_sl_count = 0
base_current_zone = 1
base_total_trades = 0

# Pyramiding State Tracking
pyramid_position = None
pyramid_zone_sl_count = 0
pyramid_current_zone = 1
pyramid_total_trades = 0
pyramid_done_for_trade = False

active_setup_type = None
event_finished = False
INITIALIZED = False
m_invalid_alert_sent = False

zone1_ref_level = None

# FOLLOW-THROUGH SPECIFIC TRACKING
last_tp_hit_price = None
follow_through_direction = None
follow_through_consumed = False

event_high = None
event_low = None

last_processed_event_id = None
last_trade_candle_time = None
last_trade_price = None


def send_telegram_safe(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Exception Caught: {e}")


def check_telegram_updates():
    """Polls Telegram for /start command directly via API"""
    if not TELEGRAM_BOT_TOKEN:
        return

    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    while True:
        try:
            params = {"timeout": 10}
            if offset:
                params["offset"] = offset

            res = requests.get(url, params=params, timeout=15)
            if res.status_code == 200:
                data = res.json()
                for result in data.get("result", []):
                    offset = result["update_id"] + 1
                    message = result.get("message", {})
                    text = message.get("text", "")

                    if text == "/start":
                        pos_info = current_position['side'] if current_position else 'None'
                        send_telegram_safe(
                            "👋 *Aman's BTC Live Trading Bot Connected!*\n\n"
                            f"📊 *Status:* Active & Listening ({MODE_TAG} mode)\n"
                            f"📍 *Active Position:* {pos_info}\n"
                            f"🧭 *Zone:* {base_current_zone} | *Event Finished:* {event_finished}\n"
                            f"💰 *Testing Balance:* ${ACCOUNT_BALANCE:.2f}\n"
                            f"⚡ *Min Quantity:* {MIN_QTY} BTC"
                        )
        except Exception as e:
            print(f"Telegram Listener Error: {e}")
        time.sleep(3)


def log_to_sheet(data):
    if not SHEET_WEBAPP_URL:
        return
    try:
        requests.post(SHEET_WEBAPP_URL, json=data, timeout=5)
    except Exception as e:
        print(f"Sheet Error: {e}")


HTF_CACHE_SECONDS = 60  # d/w/m candles only change slowly - no need to refetch every loop
_htf_cache = {"ts": 0.0, "d": None, "w": None, "m": None}

FUTURES_BASE = "https://fapi.binance.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_price_only():
    """Cheap, frequent call - just the latest 5m candle close price."""
    try:
        r = requests.get(
            f"{FUTURES_BASE}/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit=1",
            headers=_HEADERS, timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            return float(data[0][4]), data[0][0]
    except Exception as e:
        print(f"Price Fetch Fail: {e}")
    return None, None


def fetch_htf_klines():
    """Daily/Weekly/Monthly candles - cached, refreshed only every HTF_CACHE_SECONDS
    to avoid hammering the rate limit with data that barely changes."""
    now = time.time()
    if _htf_cache["d"] is not None and (now - _htf_cache["ts"]) < HTF_CACHE_SECONDS:
        return _htf_cache["d"], _htf_cache["w"], _htf_cache["m"]

    try:
        d_res = requests.get(f"{FUTURES_BASE}/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=5", headers=_HEADERS, timeout=5)
        w_res = requests.get(f"{FUTURES_BASE}/fapi/v1/klines?symbol=BTCUSDT&interval=1w&limit=3", headers=_HEADERS, timeout=5)
        m_res = requests.get(f"{FUTURES_BASE}/fapi/v1/klines?symbol=BTCUSDT&interval=1M&limit=3", headers=_HEADERS, timeout=5)

        if d_res.status_code == 200 and w_res.status_code == 200 and m_res.status_code == 200:
            _htf_cache["d"] = d_res.json()
            _htf_cache["w"] = w_res.json()
            _htf_cache["m"] = m_res.json()
            _htf_cache["ts"] = now
    except Exception as e:
        print(f"HTF Fetch Fail: {e}")

    return _htf_cache["d"], _htf_cache["w"], _htf_cache["m"]


def fetch_btc_data():
    """Combines the cheap frequent price call with the cached HTF candles."""
    current_price, candle_time = fetch_price_only()
    if current_price is None:
        return None, None, None, None, None

    d_klines, w_klines, m_klines = fetch_htf_klines()
    if not d_klines or not w_klines or not m_klines:
        return None, None, None, None, None

    return current_price, candle_time, d_klines, w_klines, m_klines


def calculate_position_qty():
    """Risk = RISK_PERCENT of the CURRENT account balance (compounds as balance changes).
    Quantity is floored at the exchange's MIN_QTY."""
    risk_usd = ACCOUNT_BALANCE * (RISK_PERCENT / 100.0)
    return max(MIN_QTY, risk_usd / SL_POINTS)


def analyze_candle_structure(open_p, high_p, low_p, close_p):
    total_range = high_p - low_p
    if total_range == 0:
        return "NORMAL", 0.0

    body = abs(close_p - open_p)
    body_pct = (body / total_range) * 100

    upper_wick_pct = ((high_p - max(open_p, close_p)) / total_range) * 100
    lower_wick_pct = ((min(open_p, close_p) - low_p) / total_range) * 100

    if upper_wick_pct >= 30.0 and close_p > open_p:
        return "DICY_GREEN", upper_wick_pct
    elif lower_wick_pct >= 30.0 and close_p < open_p:
        return "DICY_RED", lower_wick_pct
    elif body_pct <= 20.0 and upper_wick_pct >= 25.0 and lower_wick_pct >= 25.0:
        return "DOJI", body_pct
    elif body_pct >= 80.0:
        return "STRONG_BULL" if close_p >= open_p else "STRONG_BEAR", body_pct

    return "NORMAL", body_pct


def reset_event_state():
    global base_current_zone, base_zone_sl_count, base_total_trades
    global pyramid_current_zone, pyramid_zone_sl_count, pyramid_total_trades
    global active_setup_type, event_high, event_low, last_tp_hit_price, follow_through_direction, zone1_ref_level, follow_through_consumed
    global event_finished, m_invalid_alert_sent

    base_current_zone = 1
    base_zone_sl_count = 0
    base_total_trades = 0

    pyramid_current_zone = 1
    pyramid_zone_sl_count = 0
    pyramid_total_trades = 0

    active_setup_type = None
    event_high = None
    event_low = None
    last_tp_hit_price = None
    follow_through_direction = None
    follow_through_consumed = False
    zone1_ref_level = None

    event_finished = False
    m_invalid_alert_sent = False


def escalate_zone_after_sl():
    """Called right after a BASE trade SL is registered.
    Implements the 3-Zone Execution Engine (3 SL per zone, max 9 total)."""
    global base_current_zone, base_zone_sl_count, event_finished

    if base_zone_sl_count >= MAX_SL_PER_ZONE:
        if base_current_zone < MAX_ZONE:
            base_current_zone += 1
            base_zone_sl_count = 0
            send_telegram_safe(
                f"🧭 *ZONE SHIFT!* Moving to Zone {base_current_zone}\n"
                f"Trading will now trigger on Event High/Low breakout."
            )
        else:
            # Zone 3 also used up its 3 SLs -> total 9 SL reached
            event_finished = True
            send_telegram_safe(
                "🛑 *STRATEGY LOCKDOWN!*\n"
                "Max 9 SL (3 zones x 3 SL) hit. Waiting for a fresh HTF event."
            )


def check_dicy_invalidation(m_klines, btc_price):
    """Monthly Dicy 3000-point invalidation rule."""
    global active_setup_type, m_invalid_alert_sent

    if active_setup_type not in ("DICY_GREEN_MONTHLY", "DICY_RED_MONTHLY"):
        return

    monthly_open = float(m_klines[-2][1])

    if active_setup_type == "DICY_GREEN_MONTHLY" and btc_price >= monthly_open + DICY_INVALIDATION_PTS:
        if not m_invalid_alert_sent:
            send_telegram_safe(
                f"⚠️ *Dicy Green Trap INVALIDATED!*\nPrice moved +{DICY_INVALIDATION_PTS:.0f} pts above monthly open (${monthly_open:.2f}). Setup cleared."
            )
            m_invalid_alert_sent = True
        reset_event_state()

    elif active_setup_type == "DICY_RED_MONTHLY" and btc_price <= monthly_open - DICY_INVALIDATION_PTS:
        if not m_invalid_alert_sent:
            send_telegram_safe(
                f"⚠️ *Dicy Red Trap INVALIDATED!*\nPrice moved -{DICY_INVALIDATION_PTS:.0f} pts below monthly open (${monthly_open:.2f}). Setup cleared."
            )
            m_invalid_alert_sent = True
        reset_event_state()


def run_bot():
    global current_position, pyramid_position, ACCOUNT_BALANCE
    global base_zone_sl_count, base_current_zone, base_total_trades
    global pyramid_zone_sl_count, pyramid_current_zone, pyramid_total_trades, pyramid_done_for_trade
    global event_high, event_low, event_finished, INITIALIZED, m_invalid_alert_sent
    global last_processed_event_id, last_trade_candle_time, last_trade_price, active_setup_type
    global last_tp_hit_price, follow_through_direction, zone1_ref_level, follow_through_consumed

    print("🚀 Aman's Real Trading Master Bot Active...")
    send_telegram_safe(f"🚀 *Master Trading Bot Active on Render!*\n🧪 *Mode:* {MODE_TAG}")

    while True:
        try:
            btc_price, candle_time, d_klines, w_klines, m_klines = fetch_btc_data()
            if btc_price is None or not d_klines or not w_klines or not m_klines:
                time.sleep(10)
                continue

            current_event_id = f"{d_klines[-2][0]}_{w_klines[-2][0]}_{m_klines[-2][0]}"

            if not INITIALIZED:
                print("🔄 System Initializing. Syncing HTF Event ID...")
                last_processed_event_id = current_event_id
                INITIALIZED = True
                time.sleep(10)
                continue

            # --- FRESH HTF EVENT DETECTION (only when flat, no open positions) ---
            if (current_event_id != last_processed_event_id
                    and current_position is None and pyramid_position is None):
                print("🆕 New HTF Event ID detected. Resetting strategy state.")
                reset_event_state()
                last_processed_event_id = current_event_id

            event_high = btc_price if event_high is None else max(event_high, btc_price)
            event_low = btc_price if event_low is None else min(event_low, btc_price)

            # --- 1. ACTIVE POSITION MANAGEMENT & PYRAMIDING ---
            if current_position is not None:
                side = current_position['side']
                entry_p = current_position['entry_price']
                sl_p = current_position['sl_price']
                tp_p = current_position['tp_price']
                qty = current_position['qty']

                # --- A. PYRAMIDING TRIGGER (+500 PTS) ---
                if not pyramid_done_for_trade and pyramid_position is None and pyramid_total_trades < 9:
                    pyramid_trigger = False
                    if side == 'BUY' and btc_price >= (entry_p + 500.0):
                        pyramid_trigger = True
                    elif side == 'SELL' and btc_price <= (entry_p - 500.0):
                        pyramid_trigger = True

                    if pyramid_trigger:
                        pyramid_done_for_trade = True
                        p_entry = btc_price
                        p_sl = p_entry - 200.0 if side == 'BUY' else p_entry + 200.0
                        p_qty = calculate_position_qty()

                        pyramid_position = {
                            "side": side,
                            "entry_price": p_entry,
                            "sl_price": p_sl,
                            "qty": p_qty
                        }

                        if LIVE_TRADING_ENABLED:
                            try:
                                live_resp = shark_place_order(
                                    "MARKET", side, p_qty,
                                    take_profit_price=tp_p,   # same unified TP as base trade
                                    stop_loss_price=p_sl,
                                )
                                print(f"[LIVE] Pyramid order placed: {live_resp}")
                            except Exception as e:
                                send_telegram_safe(f"⚠️ *LIVE PYRAMID ORDER FAILED!*\n{e}")
                                print(f"[LIVE ERROR] Pyramid order failed: {e}")

                        msg_pyr = (f"🔺 *[{MODE_TAG}] PYRAMIDING TRADE TRIGGERED ({side})*\n\n"
                                   f"📍 *Entry Price:* ${p_entry:.2f}\n"
                                   f"🛑 *Pyramid SL:* ${p_sl:.2f}\n"
                                   f"💰 *Position Size:* {p_qty:.4f} BTC")
                        send_telegram_safe(msg_pyr)
                        time.sleep(2)

                # --- B. PYRAMIDING SL CHECK ---
                if pyramid_position is not None:
                    p_side = pyramid_position['side']
                    p_entry = pyramid_position['entry_price']
                    p_sl = pyramid_position['sl_price']
                    p_qty = pyramid_position['qty']

                    p_sl_hit = (btc_price <= p_sl) if p_side == 'BUY' else (btc_price >= p_sl)

                    if p_sl_hit:
                        loss = p_qty * abs(p_entry - p_sl)
                        ACCOUNT_BALANCE -= loss
                        pyramid_zone_sl_count += 1
                        pyramid_total_trades += 1
                        pyramid_position = None

                        send_telegram_safe(f"❌ *PYRAMID STOP LOSS HIT!*\nLoss: -${loss:.2f}")
                        time.sleep(2)

                # --- C. MAIN POSITION SL / TP CHECK ---
                if side == 'BUY':
                    max_favorable = btc_price - entry_p
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p + TRAIL_LOCK_PTS
                        if new_sl > sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram_safe(
                                f"🛡️ *Trailing SL Updated (BUY)*\nNew SL: ${new_sl:.2f}"
                                + ("\n⚠️ NOTE: this is tracked internally only — the exchange-side "
                                   "stop from entry has NOT moved (edit-order not yet wired in)."
                                   if LIVE_TRADING_ENABLED else "")
                            )

                    if btc_price >= tp_p:
                        pnl_main = qty * (tp_p - entry_p)
                        total_pnl = pnl_main

                        if pyramid_position is not None:
                            p_pnl = pyramid_position['qty'] * (tp_p - pyramid_position['entry_price'])
                            total_pnl += p_pnl
                            pyramid_position = None

                        ACCOUNT_BALANCE += total_pnl
                        last_tp_hit_price = tp_p
                        follow_through_direction = "BUY"
                        follow_through_consumed = False

                        current_position = None
                        pyramid_done_for_trade = False

                        # Target hit -> full counter reset (per Follow-Through rule)
                        base_current_zone = 1
                        base_zone_sl_count = 0
                        base_total_trades = 0

                        send_telegram_safe(f"🎯 *TARGET HIT (BUY)!*\nTotal Profit: +${total_pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}")
                        time.sleep(2)

                    elif btc_price <= sl_p:
                        loss_main = qty * (entry_p - sl_p)
                        total_loss = loss_main
                        base_zone_sl_count += 1
                        base_total_trades += 1

                        if pyramid_position is not None:
                            p_loss = pyramid_position['qty'] * (pyramid_position['entry_price'] - btc_price)
                            total_loss += p_loss
                            pyramid_position = None

                        ACCOUNT_BALANCE -= total_loss
                        last_tp_hit_price = None
                        follow_through_direction = None
                        follow_through_consumed = False

                        current_position = None
                        pyramid_done_for_trade = False

                        send_telegram_safe(f"❌ *STOP LOSS HIT (BUY)!*\nTotal Loss: -${total_loss:.2f}")
                        escalate_zone_after_sl()
                        time.sleep(2)

                elif side == 'SELL':
                    max_favorable = entry_p - btc_price
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p - TRAIL_LOCK_PTS
                        if new_sl < sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram_safe(
                                f"🛡️ *Trailing SL Updated (SELL)*\nNew SL: ${new_sl:.2f}"
                                + ("\n⚠️ NOTE: this is tracked internally only — the exchange-side "
                                   "stop from entry has NOT moved (edit-order not yet wired in)."
                                   if LIVE_TRADING_ENABLED else "")
                            )

                    if btc_price <= tp_p:
                        pnl_main = qty * (entry_p - tp_p)
                        total_pnl = pnl_main

                        if pyramid_position is not None:
                            p_pnl = pyramid_position['qty'] * (pyramid_position['entry_price'] - tp_p)
                            total_pnl += p_pnl
                            pyramid_position = None

                        ACCOUNT_BALANCE += total_pnl
                        last_tp_hit_price = tp_p
                        follow_through_direction = "SELL"
                        follow_through_consumed = False

                        current_position = None
                        pyramid_done_for_trade = False

                        # Target hit -> full counter reset (per Follow-Through rule)
                        base_current_zone = 1
                        base_zone_sl_count = 0
                        base_total_trades = 0

                        send_telegram_safe(f"🎯 *TARGET HIT (SELL)!*\nTotal Profit: +${total_pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}")
                        time.sleep(2)

                    elif btc_price >= sl_p:
                        loss_main = qty * (sl_p - entry_p)
                        total_loss = loss_main
                        base_zone_sl_count += 1
                        base_total_trades += 1

                        if pyramid_position is not None:
                            p_loss = pyramid_position['qty'] * (btc_price - pyramid_position['entry_price'])
                            total_loss += p_loss
                            pyramid_position = None

                        ACCOUNT_BALANCE -= total_loss
                        last_tp_hit_price = None
                        follow_through_direction = None
                        follow_through_consumed = False

                        current_position = None
                        pyramid_done_for_trade = False

                        send_telegram_safe(f"❌ *STOP LOSS HIT (SELL)!*\nTotal Loss: -${total_loss:.2f}")
                        escalate_zone_after_sl()
                        time.sleep(2)

            # --- 2. EVALUATE HTF SIGNALS & ENTRY TRIGGERS ---
            else:
                day1_diff = abs(float(d_klines[-3][1]) - float(d_klines[-3][4]))
                day2_diff = abs(float(d_klines[-2][1]) - float(d_klines[-2][4]))
                weekly_dist = abs(float(w_klines[-2][1]) - float(w_klines[-2][4]))
                monthly_dist = abs(float(m_klines[-2][1]) - float(m_klines[-2][4]))

                w_type, _ = analyze_candle_structure(float(w_klines[-2][1]), float(w_klines[-2][2]), float(w_klines[-2][3]), float(w_klines[-2][4]))
                m_type, _ = analyze_candle_structure(float(m_klines[-2][1]), float(m_klines[-2][2]), float(m_klines[-2][3]), float(m_klines[-2][4]))

                # --- PRIORITY CHAIN (matches rules doc order, incl. Dicy at the bottom) ---
                current_detected_setup = None
                if day1_diff < 300.0 and day2_diff < 300.0:
                    current_detected_setup = "2_DOJI"
                elif weekly_dist <= 1200.0:
                    current_detected_setup = "WEEKLY_DOJI"
                elif monthly_dist <= 2000.0:
                    current_detected_setup = "MONTHLY_DOJI"
                elif w_type == "STRONG_BULL":
                    current_detected_setup = "STRONG_BULL_WEEKLY"
                elif w_type == "STRONG_BEAR":
                    current_detected_setup = "STRONG_BEAR_WEEKLY"
                elif m_type == "STRONG_BULL":
                    current_detected_setup = "STRONG_BULL_MONTHLY"
                elif m_type == "STRONG_BEAR":
                    current_detected_setup = "STRONG_BEAR_MONTHLY"
                elif m_type == "DICY_GREEN":
                    current_detected_setup = "DICY_GREEN_MONTHLY"
                elif m_type == "DICY_RED":
                    current_detected_setup = "DICY_RED_MONTHLY"

                if active_setup_type is not None and current_detected_setup != active_setup_type:
                    reset_event_state()
                    active_setup_type = current_detected_setup

                # Check Dicy invalidation before evaluating entries
                check_dicy_invalidation(m_klines, btc_price)

                if not event_finished and base_total_trades < 9 and active_setup_type not in ("DICY_GREEN_MONTHLY", "DICY_RED_MONTHLY"):
                    buy_trigger = False
                    sell_trigger = False
                    entry_reason = ""
                    ref_price = float(d_klines[-2][4])

                    if base_current_zone == 1 and zone1_ref_level is None:
                        zone1_ref_level = ref_price

                    # --- SINGLE-USE FOLLOW THROUGH LOCK ---
                    if last_tp_hit_price is not None and follow_through_direction is not None and not follow_through_consumed:
                        if follow_through_direction == "BUY" and btc_price >= (last_tp_hit_price + 200.0):
                            buy_trigger = True
                            entry_reason = f"Follow-Through BUY Breakout (+200 pts above Prev TP ${last_tp_hit_price:.2f})"
                            follow_through_consumed = True
                        elif follow_through_direction == "SELL" and btc_price <= (last_tp_hit_price - 200.0):
                            sell_trigger = True
                            entry_reason = f"Follow-Through SELL Breakout (-200 pts below Prev TP ${last_tp_hit_price:.2f})"
                            follow_through_consumed = True

                    # STANDARD ENTRY
                    if not buy_trigger and not sell_trigger:
                        if base_current_zone == 1:
                            if current_detected_setup == "2_DOJI":
                                if btc_price >= ref_price + 200:
                                    buy_trigger = True; entry_reason = "Zone 1: 2-Day Doji Breakout"
                                elif btc_price <= ref_price - 200:
                                    sell_trigger = True; entry_reason = "Zone 1: 2-Day Doji Breakout"

                            elif current_detected_setup in ("WEEKLY_DOJI", "MONTHLY_DOJI"):
                                # "Baaki setups: Locked Ref +/- 500 pts" -- consolidation breaks either way
                                if btc_price >= ref_price + 500:
                                    buy_trigger = True; entry_reason = f"Zone 1: {current_detected_setup.replace('_', ' ').title()} Breakout"
                                elif btc_price <= ref_price - 500:
                                    sell_trigger = True; entry_reason = f"Zone 1: {current_detected_setup.replace('_', ' ').title()} Breakout"

                            elif current_detected_setup and "STRONG_BULL" in current_detected_setup:
                                if btc_price >= ref_price + 500:
                                    buy_trigger = True; entry_reason = "Zone 1: Strong Bullish Breakout"

                            elif current_detected_setup and "STRONG_BEAR" in current_detected_setup:
                                if btc_price <= ref_price - 500:
                                    sell_trigger = True; entry_reason = "Zone 1: Strong Bearish Breakout"

                        elif base_current_zone in [2, 3]:
                            if event_high is not None and btc_price >= event_high:
                                buy_trigger = True; entry_reason = f"Base Zone {base_current_zone}: Event High Breakout"
                            elif event_low is not None and btc_price <= event_low:
                                sell_trigger = True; entry_reason = f"Base Zone {base_current_zone}: Event Low Breakout"

                    is_same_candle = (candle_time == last_trade_candle_time)
                    is_same_level = (last_trade_price is not None and abs(btc_price - last_trade_price) < 50.0)

                    if (buy_trigger or sell_trigger) and not (is_same_candle and is_same_level):
                        if current_position is not None:
                            continue

                        side = "BUY" if buy_trigger else "SELL"
                        entry_p = zone1_ref_level if (base_current_zone == 1 and zone1_ref_level is not None) else btc_price
                        sl_p = entry_p + 200.0 if side == "SELL" else entry_p - 200.0
                        tp_p = entry_p - 2000.0 if side == "SELL" else entry_p + 2000.0

                        qty = calculate_position_qty()

                        current_position = {
                            "side": side,
                            "entry_price": entry_p,
                            "sl_price": sl_p,
                            "tp_price": tp_p,
                            "qty": qty,
                            "reason": entry_reason
                        }

                        last_tp_hit_price = None
                        follow_through_direction = None
                        pyramid_done_for_trade = False

                        active_setup_type = current_detected_setup
                        last_processed_event_id = current_event_id
                        last_trade_candle_time = candle_time
                        last_trade_price = btc_price

                        if LIVE_TRADING_ENABLED:
                            try:
                                live_resp = shark_place_order(
                                    "MARKET", side, qty,
                                    take_profit_price=tp_p,
                                    stop_loss_price=sl_p,
                                )
                                print(f"[LIVE] Base order placed: {live_resp}")
                            except Exception as e:
                                send_telegram_safe(f"⚠️ *LIVE MAIN ORDER FAILED!*\n{e}")
                                print(f"[LIVE ERROR] Base order failed: {e}")

                        msg = (f"🚨 *[{MODE_TAG}] NEW MAIN TRADE TRIGGERED ({side})*\n\n"
                               f"📌 *Reason:* {entry_reason}\n"
                               f"🧭 *Zone:* {base_current_zone}\n"
                               f"🎯 *Entry:* ${entry_p:.2f}\n"
                               f"🛑 *SL:* ${sl_p:.2f}\n"
                               f"🎯 *TP:* ${tp_p:.2f}\n"
                               f"💰 *Qty:* {qty:.4f} BTC")

                        send_telegram_safe(msg)
                        time.sleep(2)

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price: ${btc_price:.2f} | Pos: {current_position['side'] if current_position else 'None'} | Zone: {base_current_zone} | Finished: {event_finished}")
            time.sleep(10)

        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=check_telegram_updates, daemon=True).start()
    run_bot()
