import os
import time
import requests
import threading
from flask import Flask
from datetime import datetime, timezone

# --- FLASK SERVER FOR RENDER PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Aman's Master Precision Bot is Active and Running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- CONFIGURATION & ENV VARS ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8981662979:AAFg2MAiHOeYlK_bxbIXXLK9JdNSGqoksfc")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1862803975")
SHEET_WEBAPP_URL = os.environ.get("SHEET_WEBAPP_URL", "https://script.google.com/macros/s/AKfycbwLHsJgFTMYwulKxxZaXtWQNP94ZAPoDiy54jDIWgXajnYyz9j-ZloFiIy8RxQfIBZnNw/exec")

ACCOUNT_BALANCE = 10000.0   # Initial Paper Trading Capital ($)
FIXED_RISK_USD = 100.0      # Fixed $100 Risk per trade
SL_POINTS = 200.0           # Fixed 200 Points SL
TP_POINTS = 2000.0          # Fixed 1:10 RR Target (2000 Points)

# Trailing Config
TRAIL_TRIGGER_PTS = 1600.0
TRAIL_LOCK_PTS = 600.0

# State Tracking
current_position = None     # Dict for active position
zone_sl_count = 0           # Continuous SL count in current zone (0 to 3)
current_zone = 1            # Zone 1, Zone 2, Zone 3
total_strategy_trades = 0   # Persistent SL counter across full setup (Max 9)
active_setup_type = None    # Track active setup type
event_finished = False      # Lock flag: Lockdown on Max 9 SLs
INITIALIZED = False         # Warmup guard for fresh deploy/restarts
m_invalid_alert_sent = False # Prevention for Telegram spamming on Monthly Invalidation

# FOLLOW-THROUGH SPECIFIC TRACKING
last_tp_hit_price = None
follow_through_direction = None

# EVENT SPECIFIC HIGH/LOW TRACKING
event_high = None
event_low = None

# Tracking HTF Event IDs to detect NEW Events
last_processed_event_id = None 

# Re-entry Loop Prevention
last_trade_candle_time = None
last_trade_price = None

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Error: {e}")

def log_to_sheet(data):
    if not SHEET_WEBAPP_URL:
        return
    try:
        requests.post(SHEET_WEBAPP_URL, json=data, timeout=5)
    except Exception as e:
        print(f"Sheet Error: {e}")

def fetch_btc_data():
    """Fetch current BTC price and HTF data with Cloud-Block Bypass Endpoints"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    binance_vision = "https://data-api.binance.vision"
    try:
        r = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=1", headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            current_price = float(data[0][4])
            candle_time = data[0][0]

            d_data = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=5", headers=headers, timeout=5).json()
            w_data = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1w&limit=3", headers=headers, timeout=5).json()
            m_data = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1M&limit=3", headers=headers, timeout=5).json()

            return current_price, candle_time, d_data, w_data, m_data
    except Exception:
        pass

    try:
        cc_url = "https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USDT&limit=1"
        r_cc = requests.get(cc_url, headers=headers, timeout=5).json()
        current_price = float(r_cc['Data']['Data'][-1]['close'])
        candle_time = r_cc['Data']['Data'][-1]['time'] * 1000

        d_data = requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=5", headers=headers, timeout=5).json()
        w_data = requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1w&limit=3", headers=headers, timeout=5).json()
        m_data = requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1M&limit=3", headers=headers, timeout=5).json()

        return current_price, candle_time, d_data, w_data, m_data
    except Exception as e:
        print(f"Data Fetch Error: {e}")
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
    """Helper to reset zone states and event tracking limits"""
    global current_zone, zone_sl_count, total_strategy_trades, active_setup_type
    global event_high, event_low, last_tp_hit_price, follow_through_direction
    current_zone = 1
    zone_sl_count = 0
    total_strategy_trades = 0
    active_setup_type = None
    event_high = None
    event_low = None
    last_tp_hit_price = None
    follow_through_direction = None

def run_bot():
    global current_position, ACCOUNT_BALANCE, zone_sl_count, current_zone, total_strategy_trades
    global event_high, event_low, event_finished, INITIALIZED, m_invalid_alert_sent
    global last_processed_event_id, last_trade_candle_time, last_trade_price, active_setup_type
    global last_tp_hit_price, follow_through_direction

    print("🚀 Aman's Precision Master Strategy Bot Started...")
    send_telegram("🚀 *Master Paper Trading Bot Started Live on Render!*")

    while True:
        try:
            btc_price, candle_time, d_klines, w_klines, m_klines = fetch_btc_data()
            if btc_price is None or not d_klines or not w_klines or not m_klines:
                time.sleep(10)
                continue

            current_event_id = f"{d_klines[-2][0]}_{w_klines[-2][0]}_{m_klines[-2][0]}"

            if not INITIALIZED:
                print("🔄 System Initializing/Redeployed. Syncing HTF Event ID...")
                last_processed_event_id = current_event_id
                INITIALIZED = True
                time.sleep(10)
                continue

            # TRACK HIGH AND LOW FROM EVENT START TIME
            prev_event_high = event_high
            prev_event_low = event_low

            event_high = btc_price if event_high is None else max(event_high, btc_price)
            event_low = btc_price if event_low is None else min(event_low, btc_price)

            # --- 1. ACTIVE POSITION MANAGEMENT ---
            if current_position is not None:
                side = current_position['side']
                entry_p = current_position['entry_price']
                sl_p = current_position['sl_price']
                tp_p = current_position['tp_price']
                qty = current_position['qty']

                if side == 'BUY':
                    max_favorable = btc_price - entry_p
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p + TRAIL_LOCK_PTS
                        if new_sl > sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram(f"🛡️ *Trailing SL Updated (BUY)*\nNew SL Locked: ${new_sl:.2f}")

                    if btc_price >= tp_p:
                        pnl = qty * (tp_p - entry_p)
                        ACCOUNT_BALANCE += pnl
                        
                        # Prepare Follow-Through Tracking
                        last_tp_hit_price = tp_p
                        follow_through_direction = "BUY"
                        
                        send_telegram(f"🎯 *TARGET HIT (BUY)!*\nProfit: +${pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}\n🚀 *Target Hit! Standing by for Follow-Through Entry (+200 pts).*")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_TP",
                            "exit": round(btc_price, 2),
                            "pnl_pts": TP_POINTS,
                            "pnl_usd": round(pnl, 2),
                            "exit_reason": "Target Hit (1:10)",
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None
                        # Reset zone limits for fresh 3-zone system on follow-through
                        current_zone = 1
                        zone_sl_count = 0
                        total_strategy_trades = 0

                    elif btc_price <= sl_p:
                        loss = qty * (entry_p - sl_p)
                        ACCOUNT_BALANCE -= loss
                        zone_sl_count += 1
                        total_strategy_trades += 1
                        
                        # Cancel follow-through if SL hits on ongoing trade
                        last_tp_hit_price = None
                        follow_through_direction = None

                        send_telegram(f"❌ *STOP LOSS HIT (BUY)!*\nLoss: -${loss:.2f}\nZone {current_zone} SL Count: {zone_sl_count}/3\nTotal Setup Trades: {total_strategy_trades}/9")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_SL",
                            "exit": round(btc_price, 2),
                            "pnl_pts": -SL_POINTS,
                            "pnl_usd": -round(loss, 2),
                            "exit_reason": "Stop Loss Hit",
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None

                elif side == 'SELL':
                    max_favorable = entry_p - btc_price
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p - TRAIL_LOCK_PTS
                        if new_sl < sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram(f"🛡️ *Trailing SL Updated (SELL)*\nNew SL Locked: ${new_sl:.2f}")

                    if btc_price <= tp_p:
                        pnl = qty * (entry_p - tp_p)
                        ACCOUNT_BALANCE += pnl

                        # Prepare Follow-Through Tracking
                        last_tp_hit_price = tp_p
                        follow_through_direction = "SELL"

                        send_telegram(f"🎯 *TARGET HIT (SELL)!*\nProfit: +${pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}\n🚀 *Target Hit! Standing by for Follow-Through Entry (-200 pts).*")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_TP",
                            "exit": round(btc_price, 2),
                            "pnl_pts": TP_POINTS,
                            "pnl_usd": round(pnl, 2),
                            "exit_reason": "Target Hit (1:10)",
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None
                        # Reset zone limits for fresh 3-zone system on follow-through
                        current_zone = 1
                        zone_sl_count = 0
                        total_strategy_trades = 0

                    elif btc_price >= sl_p:
                        loss = qty * (sl_p - entry_p)
                        ACCOUNT_BALANCE -= loss
                        zone_sl_count += 1
                        total_strategy_trades += 1

                        # Cancel follow-through if SL hits
                        last_tp_hit_price = None
                        follow_through_direction = None

                        send_telegram(f"❌ *STOP LOSS HIT (SELL)!*\nLoss: -${loss:.2f}\nZone {current_zone} SL Count: {zone_sl_count}/3\nTotal Setup Trades: {total_strategy_trades}/9")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_SL",
                            "exit": round(btc_price, 2),
                            "pnl_pts": -SL_POINTS,
                            "pnl_usd": -round(loss, 2),
                            "exit_reason": "Stop Loss Hit",
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None

                # ZONE SHIFT EVALUATION AFTER SL HIT
                if zone_sl_count >= 3:
                    if current_zone < 3:
                        current_zone += 1
                        zone_sl_count = 0
                        send_telegram(f"⚠️ *Zone Shift Triggered!* Switching to Zone {current_zone}")
                    else:
                        send_telegram(f"🚨 *All 3 Zones Failed (Max 9 SLs Hit)!* Event Terminated.")
                        reset_event_state()
                        event_finished = True

            # --- 2. EVALUATE HTF SIGNALS & CANDLE CHANGE RESET ---
            else:
                day1_close = float(d_klines[-3][4])  # 2 days ago Close
                day2_close = float(d_klines[-2][4])  # 1 day ago Close
                daily_close_distance = abs(day1_close - day2_close)

                # HTF Open-Close Distance Logic for Weekly & Monthly Doji
                w_open = float(w_klines[-2][1])
                w_close = float(w_klines[-2][4])
                weekly_dist = abs(w_open - w_close)

                m_open = float(m_klines[-1][1])
                m_prev_open = float(m_klines[-2][1])
                m_prev_close = float(m_klines[-2][4])
                monthly_dist = abs(m_prev_open - m_prev_close)

                w_type, _ = analyze_candle_structure(float(w_klines[-2][1]), float(w_klines[-2][2]), float(w_klines[-2][3]), float(w_klines[-2][4]))
                m_type, _ = analyze_candle_structure(float(m_klines[-2][1]), float(m_klines[-2][2]), float(m_klines[-2][3]), float(m_klines[-2][4]))

                # Current Candle Context Identification
                current_detected_setup = None
                
                # Check 2 Daily Close Distance <= 300 pts
                if daily_close_distance <= 300.0:
                    current_detected_setup = "2_DOJI"
                elif weekly_dist <= 1200.0:
                    current_detected_setup = "WEEKLY_DOJI"
                elif monthly_dist <= 2000.0:
                    current_detected_setup = "MONTHLY_DOJI"
                elif w_type == "STRONG_BULL" or m_type == "STRONG_BULL":
                    current_detected_setup = "STRONG_BULL"
                elif w_type == "STRONG_BEAR" or m_type == "STRONG_BEAR":
                    current_detected_setup = "STRONG_BEAR"
                elif w_type == "DICY_GREEN" or m_type == "DICY_GREEN":
                    current_detected_setup = "DICY_GREEN"
                elif w_type == "DICY_RED" or m_type == "DICY_RED":
                    current_detected_setup = "DICY_RED"

                # NEW SETUP / CANDLE FORMATION CHANGE DETECTION
                if active_setup_type is not None and current_detected_setup != active_setup_type:
                    send_telegram(f"🔄 *Candle Structure Changed!* ({active_setup_type} ➡️ {current_detected_setup}). Resetting setup counters for fresh 9 SL strategy.")
                    reset_event_state()
                    active_setup_type = current_detected_setup

                # --- 3000-POINT INVALIDATION CHECK FOR MONTHLY DICY CANDLES ---
                m_invalidated = False
                if m_type == "DICY_GREEN" and btc_price >= (m_open + 3000.0):
                    m_invalidated = True
                    if not m_invalid_alert_sent:
                        send_telegram(f"🚨 *Monthly Dicy Green Invalidated!* Price moved +3000 pts above Current Monthly Open (${m_open:.2f}). Unlocking scanning...")
                        m_invalid_alert_sent = True

                elif m_type == "DICY_RED" and btc_price <= (m_open - 3000.0):
                    m_invalidated = True
                    if not m_invalid_alert_sent:
                        send_telegram(f"🚨 *Monthly Dicy Red Invalidated!* Price moved -3000 pts below Current Monthly Open (${m_open:.2f}). Unlocking scanning...")
                        m_invalid_alert_sent = True

                if m_invalidated:
                    event_finished = False
                    reset_event_state()

                if event_finished and current_event_id != last_processed_event_id:
                    event_finished = False
                    reset_event_state()
                    m_invalid_alert_sent = False
                    send_telegram(f"🔓 *New HTF Event Detected! Event Lockdown Lifted.*")

                if not event_finished and total_strategy_trades < 9:
                    buy_trigger = False
                    sell_trigger = False
                    entry_reason = ""
                    entry_tf = ""
                    ref_price = float(d_klines[-2][4])

                    # --- FOLLOW-THROUGH LOGIC EVALUATION ---
                    if last_tp_hit_price is not None and follow_through_direction is not None:
                        if follow_through_direction == "BUY" and btc_price >= (last_tp_hit_price + 200.0):
                            buy_trigger = True
                            entry_reason = f"Follow-Through BUY Breakout (+200 pts above Prev TP ${last_tp_hit_price:.2f})"
                            entry_tf = "Trend Continuation"
                        elif follow_through_direction == "SELL" and btc_price <= (last_tp_hit_price - 200.0):
                            sell_trigger = True
                            entry_reason = f"Follow-Through SELL Breakout (-200 pts below Prev TP ${last_tp_hit_price:.2f})"
                            entry_tf = "Trend Continuation"

                    # --- STANDARD ENTRY LOGIC (IF NO FOLLOW-THROUGH ACTIVE) ---
                    if not buy_trigger and not sell_trigger:
                        # --- ZONE 1: INITIAL STRATEGY CALCULATED LEVEL ---
                        if current_zone == 1:
                            if current_detected_setup == "2_DOJI":
                                if btc_price >= ref_price + 200: 
                                    buy_trigger = True
                                    entry_reason = "Zone 1: 2-Day Close Consolidation Breakout (<= 300 pts range)"
                                    entry_tf = "Daily (1D)"
                                elif btc_price <= ref_price - 200: 
                                    sell_trigger = True
                                    entry_reason = "Zone 1: 2-Day Close Consolidation Breakout (<= 300 pts range)"
                                    entry_tf = "Daily (1D)"
                            
                            elif current_detected_setup == "WEEKLY_DOJI":
                                if btc_price >= ref_price + 500: 
                                    buy_trigger = True
                                    entry_reason = "Zone 1: Weekly Doji Breakout (<= 1200 pts range)"
                                    entry_tf = "Weekly (1W)"
                                elif btc_price <= ref_price - 500: 
                                    sell_trigger = True
                                    entry_reason = "Zone 1: Weekly Doji Breakout (<= 1200 pts range)"
                                    entry_tf = "Weekly (1W)"

                            elif current_detected_setup == "MONTHLY_DOJI":
                                if btc_price >= ref_price + 500: 
                                    buy_trigger = True
                                    entry_reason = "Zone 1: Monthly Doji Breakout (<= 2000 pts range)"
                                    entry_tf = "Monthly (1M)"
                                elif btc_price <= ref_price - 500: 
                                    sell_trigger = True
                                    entry_reason = "Zone 1: Monthly Doji Breakout (<= 2000 pts range)"
                                    entry_tf = "Monthly (1M)"

                            elif current_detected_setup == "STRONG_BULL":
                                if btc_price >= ref_price + 500: 
                                    buy_trigger = True
                                    entry_reason = "Zone 1: Strong Bullish Breakout"
                                    entry_tf = "Weekly / Monthly"

                            elif current_detected_setup == "STRONG_BEAR":
                                if btc_price <= ref_price - 500: 
                                    sell_trigger = True
                                    entry_reason = "Zone 1: Strong Bearish Breakout"
                                    entry_tf = "Weekly / Monthly"

                            elif current_detected_setup == "DICY_GREEN" and not m_invalidated:
                                if btc_price <= ref_price - 500: 
                                    sell_trigger = True
                                    entry_reason = "Zone 1: Dicy Green Trap Setup"
                                    entry_tf = "Weekly / Monthly"
                            
                            elif current_detected_setup == "DICY_RED" and not m_invalidated:
                                if btc_price >= ref_price + 500: 
                                    buy_trigger = True
                                    entry_reason = "Zone 1: Dicy Red Trap Setup"
                                    entry_tf = "Weekly / Monthly"

                        # --- ZONE 2 & ZONE 3: DYNAMIC EVENT HIGH / LOW SHIFTS ---
                        elif current_zone in [2, 3]:
                            if prev_event_high is not None and btc_price >= prev_event_high: 
                                buy_trigger = True
                                entry_reason = f"Zone {current_zone}: Event High Breakout (${prev_event_high:.2f})"
                                entry_tf = f"Zone {current_zone} Event High"
                            elif prev_event_low is not None and btc_price <= prev_event_low: 
                                sell_trigger = True
                                entry_reason = f"Zone {current_zone}: Event Low Breakout (${prev_event_low:.2f})"
                                entry_tf = f"Zone {current_zone} Event Low"

                    is_same_candle = (candle_time == last_trade_candle_time)
                    is_same_level = (last_trade_price is not None and abs(btc_price - last_trade_price) < 50.0)

                    if (buy_trigger or sell_trigger) and not (is_same_candle and is_same_level):
                        side = "BUY" if buy_trigger else "SELL"
                        
                        qty = FIXED_RISK_USD / SL_POINTS  # $100 / 200 = 0.5 BTC

                        sl = btc_price - SL_POINTS if side == "BUY" else btc_price + SL_POINTS
                        tp = btc_price + TP_POINTS if side == "BUY" else btc_price - TP_POINTS

                        current_position = {
                            "side": side,
                            "entry_price": btc_price,
                            "sl_price": sl,
                            "tp_price": tp,
                            "qty": qty,
                            "reason": entry_reason,
                            "timeframe": entry_tf
                        }

                        # Reset follow through flags once active trade is executed
                        last_tp_hit_price = None
                        follow_through_direction = None

                        active_setup_type = current_detected_setup
                        last_processed_event_id = current_event_id
                        last_trade_candle_time = candle_time
                        last_trade_price = btc_price

                        msg = (f"🚨 *NEW TRADE EXECUTED ({side})*\n\n"
                               f"📌 *Entry Reason:* {entry_reason}\n"
                               f"⏱️ *Timeframe:* {entry_tf}\n"
                               f"📍 *Zone:* Zone {current_zone}\n\n"
                               f"🎯 *Entry Level:* ${btc_price:.2f}\n"
                               f"🛑 *Stop Loss (SL):* ${sl:.2f} (200 pts)\n"
                               f"🎯 *Take Profit (TP):* ${tp:.2f} (1:10 RR)\n\n"
                               f"💰 *Position Size:* {qty:.4f} BTC\n"
                               f"💵 *Fixed Capital Risk:* ${FIXED_RISK_USD:.2f}\n"
                               f"📊 *Account Balance:* ${ACCOUNT_BALANCE:.2f}")
                        
                        print(msg)
                        send_telegram(msg)
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": f"ENTRY_{side}",
                            "reason": entry_reason,
                            "entry": round(btc_price, 2),
                            "risk": FIXED_RISK_USD,
                            "rr": "1:10",
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price: ${btc_price:.2f} | Pos: {current_position['side'] if current_position else 'None'} | Zone: {current_zone} | Event High: {event_high} | Event Low: {event_low} | Active Setup: {active_setup_type}")
            time.sleep(300)

        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
