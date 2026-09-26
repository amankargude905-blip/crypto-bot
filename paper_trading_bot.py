import os
import time
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

# REAL TRADING CAPITAL PARAMETERS
ACCOUNT_BALANCE = 20.0        # $20 Total Testing Budget
FIXED_RISK_USD = 20.0         # Max $20 Risk per trade
MIN_QTY = 0.002              # Strictly 0.002 BTC Minimum Quantity
SL_POINTS = 200.0             # Fixed 200 Points SL
TP_POINTS = 2000.0            # Fixed 1:10 RR Target (2000 Points)

# Trailing Config
TRAIL_TRIGGER_PTS = 1600.0
TRAIL_LOCK_PTS = 600.0

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
                            f"📊 *Status:* Active & Listening\n"
                            f"📍 *Active Position:* {pos_info}\n"
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

def fetch_btc_data():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    binance_vision = "https://data-api.binance.vision"
    try:
        r = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=1", headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            current_price = float(data[0][4])
            candle_time = data[0][0]

            d_res = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=5", headers=headers, timeout=5)
            w_res = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1w&limit=3", headers=headers, timeout=5)
            m_res = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1M&limit=3", headers=headers, timeout=5)

            if d_res.status_code == 200 and w_res.status_code == 200 and m_res.status_code == 200:
                return current_price, candle_time, d_res.json(), w_res.json(), m_res.json()
    except Exception as e:
        print(f"Fetch Fail: {e}")

    return None, None, None, None, None

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

def run_bot():
    global current_position, pyramid_position, ACCOUNT_BALANCE
    global base_zone_sl_count, base_current_zone, base_total_trades
    global pyramid_zone_sl_count, pyramid_current_zone, pyramid_total_trades, pyramid_done_for_trade
    global event_high, event_low, event_finished, INITIALIZED, m_invalid_alert_sent
    global last_processed_event_id, last_trade_candle_time, last_trade_price, active_setup_type
    global last_tp_hit_price, follow_through_direction, zone1_ref_level, follow_through_consumed

    print("🚀 Aman's Real Trading Master Bot Active...")
    send_telegram_safe("🚀 *Master Trading Bot Active on Render!*")

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
                        p_qty = max(MIN_QTY, FIXED_RISK_USD / SL_POINTS)

                        pyramid_position = {
                            "side": side,
                            "entry_price": p_entry,
                            "sl_price": p_sl,
                            "qty": p_qty
                        }

                        msg_pyr = (f"🔺 *PYRAMIDING TRADE TRIGGERED ({side})*\n\n"
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
                            send_telegram_safe(f"🛡️ *Trailing SL Updated (BUY)*\nNew SL: ${new_sl:.2f}")

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
                        time.sleep(2)

                elif side == 'SELL':
                    max_favorable = entry_p - btc_price
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p - TRAIL_LOCK_PTS
                        if new_sl < sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram_safe(f"🛡️ *Trailing SL Updated (SELL)*\nNew SL: ${new_sl:.2f}")

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
                        time.sleep(2)

            # --- 2. EVALUATE HTF SIGNALS & ENTRY TRIGGERS ---
            else:
                day1_diff = abs(float(d_klines[-3][1]) - float(d_klines[-3][4]))
                day2_diff = abs(float(d_klines[-2][1]) - float(d_klines[-2][4]))
                weekly_dist = abs(float(w_klines[-2][1]) - float(w_klines[-2][4]))
                monthly_dist = abs(float(m_klines[-2][1]) - float(m_klines[-2][4]))

                w_type, _ = analyze_candle_structure(float(w_klines[-2][1]), float(w_klines[-2][2]), float(w_klines[-2][3]), float(w_klines[-2][4]))
                m_type, _ = analyze_candle_structure(float(m_klines[-2][1]), float(m_klines[-2][2]), float(m_klines[-2][3]), float(m_klines[-2][4]))

                current_detected_setup = None
                if day1_diff < 300.0 and day2_diff < 300.0: current_detected_setup = "2_DOJI"
                elif weekly_dist <= 1200.0: current_detected_setup = "WEEKLY_DOJI"
                elif monthly_dist <= 2000.0: current_detected_setup = "MONTHLY_DOJI"
                elif w_type == "STRONG_BULL": current_detected_setup = "STRONG_BULL_WEEKLY"
                elif w_type == "STRONG_BEAR": current_detected_setup = "STRONG_BEAR_WEEKLY"
                elif m_type == "STRONG_BULL": current_detected_setup = "STRONG_BULL_MONTHLY"
                elif m_type == "STRONG_BEAR": current_detected_setup = "STRONG_BEAR_MONTHLY"

                if active_setup_type is not None and current_detected_setup != active_setup_type:
                    reset_event_state()
                    active_setup_type = current_detected_setup

                if not event_finished and base_total_trades < 9:
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
                                if btc_price >= ref_price + 200: buy_trigger = True; entry_reason = "Zone 1: 2-Day Doji Breakout"
                                elif btc_price <= ref_price - 200: sell_trigger = True; entry_reason = "Zone 1: 2-Day Doji Breakout"
                            elif current_detected_setup and "STRONG_BULL" in current_detected_setup:
                                if btc_price >= ref_price + 500: buy_trigger = True; entry_reason = "Zone 1: Strong Bullish Breakout"
                            elif current_detected_setup and "STRONG_BEAR" in current_detected_setup:
                                if btc_price <= ref_price - 500: sell_trigger = True; entry_reason = "Zone 1: Strong Bearish Breakout"

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

                        calculated_qty = FIXED_RISK_USD / SL_POINTS
                        qty = max(MIN_QTY, calculated_qty)

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

                        msg = (f"🚨 *NEW MAIN TRADE TRIGGERED ({side})*\n\n"
                               f"📌 *Reason:* {entry_reason}\n"
                               f"🎯 *Entry:* ${entry_p:.2f}\n"
                               f"🛑 *SL:* ${sl_p:.2f}\n"
                               f"🎯 *TP:* ${tp_p:.2f}\n"
                               f"💰 *Qty:* {qty:.4f} BTC")
                        
                        send_telegram_safe(msg)
                        time.sleep(2)

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price: ${btc_price:.2f} | Pos: {current_position['side'] if current_position else 'None'}")
            time.sleep(10)

        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=check_telegram_updates, daemon=True).start()
    run_bot()
