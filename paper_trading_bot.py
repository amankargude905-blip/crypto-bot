import os
import time
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ==========================================
# RENDER PORT BINDING (DUMMY SERVER)
# ==========================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running active on Render!")

    def log_message(self, format, *args):
        return  # Silence standard HTTP logs to keep console clean

def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

# Background thread for Render port health check
threading.Thread(target=start_dummy_server, daemon=True).start()


# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK", "")

FIXED_RISK_USD = 100.0  # Fixed USD Risk per trade
SL_POINTS = 200.0       # Standard Stop Loss points
TRAIL_TRIGGER_PTS = 500.0
TRAIL_LOCK_PTS = 200.0


# ==========================================
# GLOBAL STATE VARIABLES
# ==========================================
ACCOUNT_BALANCE = 1000.0
current_position = None
pyramid_position = None

base_zone_sl_count = 0
base_current_zone = 1
base_total_trades = 0

pyramid_zone_sl_count = 0
pyramid_current_zone = 1
pyramid_total_trades = 0
pyramid_done_for_trade = False

event_high = None
event_low = None
event_finished = False
INITIALIZED = False
m_invalid_alert_sent = False

last_processed_event_id = None
last_trade_candle_time = None
last_trade_price = None
active_setup_type = None

last_tp_hit_price = None
follow_through_direction = None
zone1_ref_level = None


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram tokens not configured.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Send Error: {e}")


def log_to_sheet(data):
    if not GOOGLE_SHEET_WEBHOOK:
        return
    try:
        requests.post(GOOGLE_SHEET_WEBHOOK, json=data, timeout=5)
    except Exception as e:
        print(f"Google Sheet Log Error: {e}")


def fetch_btc_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    btc_price = None

    # --- STEP 1: FETCH PRICE (Binance -> Bybit Fallback) ---
    try:
        ticker_url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        ticker_res = requests.get(ticker_url, headers=headers, timeout=5).json()
        if isinstance(ticker_res, dict) and 'price' in ticker_res:
            btc_price = float(ticker_res['price'])
    except Exception:
        pass

    # Backup API: Bybit (agar Binance Render IP ko limit kare)
    if btc_price is None:
        try:
            bybit_url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
            bybit_res = requests.get(bybit_url, headers=headers, timeout=5).json()
            btc_price = float(bybit_res['result']['list'][0]['lastPrice'])
        except Exception:
            return None, None, None, None, None

    # --- STEP 2: FETCH KLINES ---
    try:
        d_url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=5"
        d_klines = requests.get(d_url, headers=headers, timeout=5).json()

        w_url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1w&limit=5"
        w_klines = requests.get(w_url, headers=headers, timeout=5).json()

        m_url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1M&limit=5"
        m_klines = requests.get(m_url, headers=headers, timeout=5).json()

        if not (isinstance(d_klines, list) and isinstance(w_klines, list) and isinstance(m_klines, list)):
            return None, None, None, None, None

        candle_time = d_klines[-1][0]
        return btc_price, candle_time, d_klines, w_klines, m_klines

    except Exception:
        return None, None, None, None, None


def analyze_candle_structure(open_p, high_p, low_p, close_p):
    total_range = high_p - low_p
    if total_range == 0:
        return "NEUTRAL", 0.0

    body = abs(close_p - open_p)
    body_pct = (body / total_range) * 100

    if body_pct >= 60.0:
        return ("STRONG_BULL" if close_p > open_p else "STRONG_BEAR"), body_pct
    elif body_pct <= 30.0:
        return ("DICY_GREEN" if close_p > open_p else "DICY_RED"), body_pct
    else:
        return "NEUTRAL", body_pct


def reset_event_state():
    global base_zone_sl_count, base_current_zone, base_total_trades
    global pyramid_zone_sl_count, pyramid_current_zone, pyramid_total_trades, pyramid_done_for_trade
    global event_high, event_low, zone1_ref_level, active_setup_type
    
    base_zone_sl_count = 0
    base_current_zone = 1
    base_total_trades = 0

    pyramid_zone_sl_count = 0
    pyramid_current_zone = 1
    pyramid_total_trades = 0
    pyramid_done_for_trade = False

    event_high = None
    event_low = None
    zone1_ref_level = None
    active_setup_type = None


# ==========================================
# MAIN TRADING BOT LOOP
# ==========================================
def run_bot():
    global current_position, pyramid_position, ACCOUNT_BALANCE
    global base_zone_sl_count, base_current_zone, base_total_trades
    global pyramid_zone_sl_count, pyramid_current_zone, pyramid_total_trades, pyramid_done_for_trade
    global event_high, event_low, event_finished, INITIALIZED, m_invalid_alert_sent
    global last_processed_event_id, last_trade_candle_time, last_trade_price, active_setup_type
    global last_tp_hit_price, follow_through_direction, zone1_ref_level

    print("🚀 Master Strategy Bot (with Pyramiding & Port Binding) Started...")
    send_telegram("🚀 *Master Trading Bot Active on Render!*")

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

            # --- 1. ACTIVE POSITION MANAGEMENT & PYRAMIDING ---
            if current_position is not None:
                side = current_position['side']
                entry_p = current_position['entry_price']
                sl_p = current_position['sl_price']
                tp_p = current_position['tp_price']
                qty = current_position['qty']

                # --- A. CHECK FOR PYRAMIDING ENTRY TRIGGER (+500 PTS) ---
                if not pyramid_done_for_trade and pyramid_position is None and pyramid_total_trades < 9:
                    pyramid_trigger = False
                    if side == 'BUY' and btc_price >= (entry_p + 500.0):
                        pyramid_trigger = True
                    elif side == 'SELL' and btc_price <= (entry_p - 500.0):
                        pyramid_trigger = True

                    if pyramid_trigger:
                        p_entry = btc_price
                        p_sl = p_entry - 200.0 if side == 'BUY' else p_entry + 200.0
                        p_qty = FIXED_RISK_USD / SL_POINTS

                        pyramid_position = {
                            "side": side,
                            "entry_price": p_entry,
                            "sl_price": p_sl,
                            "qty": p_qty
                        }
                        pyramid_done_for_trade = True

                        msg_pyr = (f"🔺 *PYRAMIDING TRADE EXECUTED ({side})*\n\n"
                                   f"📍 *Entry Price:* ${p_entry:.2f} (+500 pts move from Base)\n"
                                   f"🛑 *Pyramid SL:* ${p_sl:.2f} (200 pts)\n"
                                   f"🎯 *Unified Target (from Main):* ${tp_p:.2f}\n"
                                   f"📊 *Pyramid Zone:* Zone {pyramid_current_zone}\n"
                                   f"💰 *Position Size:* {p_qty:.4f} BTC")
                        print(msg_pyr)
                        send_telegram(msg_pyr)

                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": f"PYRAMID_ENTRY_{side}",
                            "entry": round(p_entry, 2),
                            "risk": FIXED_RISK_USD,
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                # --- B. PYRAMIDING INDEPENDENT POSITION MANAGEMENT ---
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

                        send_telegram(f"❌ *PYRAMID STOP LOSS HIT!*\nLoss: -${loss:.2f}\nPyramid Zone {pyramid_current_zone} SL Count: {pyramid_zone_sl_count}/3\nTotal Pyramid Setup Trades: {pyramid_total_trades}/9")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "PYRAMID_EXIT_SL",
                            "exit": round(btc_price, 2),
                            "pnl_usd": -round(loss, 2),
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        pyramid_position = None

                        if pyramid_zone_sl_count >= 3:
                            if pyramid_current_zone < 3:
                                pyramid_current_zone += 1
                                pyramid_zone_sl_count = 0
                                send_telegram(f"⚠️ *Pyramid Zone Shift!* Switched to Pyramid Zone {pyramid_current_zone}")
                            else:
                                send_telegram(f"🚨 *All 3 Pyramid Zones Failed (Max 9 SLs Hit)!* Pyramiding disabled for current HTF event.")

                # --- C. MAIN POSITION TRAILING & TARGET MANAGEMENT ---
                if side == 'BUY':
                    max_favorable = btc_price - entry_p
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p + TRAIL_LOCK_PTS
                        if new_sl > sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram(f"🛡️ *Trailing SL Updated (BUY)*\nNew SL Locked: ${new_sl:.2f}")

                    # MAIN TARGET HIT
                    if btc_price >= tp_p:
                        pnl_main = qty * (tp_p - entry_p)
                        total_pnl = pnl_main

                        if pyramid_position is not None:
                            p_pnl = pyramid_position['qty'] * (tp_p - pyramid_position['entry_price'])
                            total_pnl += p_pnl
                            send_telegram(f"🎯 *PYRAMID POSITION CLOSED AT MAIN TARGET!*\nPyramid Profit: +${p_pnl:.2f}")
                            pyramid_position = None

                        ACCOUNT_BALANCE += total_pnl
                        
                        last_tp_hit_price = tp_p
                        follow_through_direction = "BUY"
                        
                        send_telegram(f"🎯 *TARGET HIT (BUY)!*\nTotal Profit: +${total_pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}\n🚀 *Standing by for Follow-Through Entry (+200 pts).*")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_TP",
                            "exit": round(btc_price, 2),
                            "pnl_usd": round(total_pnl, 2),
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None
                        pyramid_done_for_trade = False
                        base_current_zone = 1
                        base_zone_sl_count = 0
                        base_total_trades = 0

                    # MAIN STOP LOSS HIT
                    elif btc_price <= sl_p:
                        loss_main = qty * (entry_p - sl_p)
                        total_loss = loss_main
                        base_zone_sl_count += 1
                        base_total_trades += 1

                        if pyramid_position is not None:
                            p_loss = pyramid_position['qty'] * (pyramid_position['entry_price'] - btc_price)
                            total_loss += p_loss
                            send_telegram(f"⚠️ *Main Trade SL Hit! Force Closing Active Pyramiding Trade.* Loss: -${p_loss:.2f}")
                            pyramid_position = None

                        ACCOUNT_BALANCE -= total_loss

                        last_tp_hit_price = None
                        follow_through_direction = None

                        send_telegram(f"❌ *STOP LOSS HIT (BUY)!*\nTotal Loss: -${total_loss:.2f}\nBase Zone {base_current_zone} SL Count: {base_zone_sl_count}/3\nTotal Base Trades: {base_total_trades}/9")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_SL",
                            "exit": round(btc_price, 2),
                            "pnl_usd": -round(total_loss, 2),
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None
                        pyramid_done_for_trade = False

                elif side == 'SELL':
                    max_favorable = entry_p - btc_price
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p - TRAIL_LOCK_PTS
                        if new_sl < sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram(f"🛡️ *Trailing SL Updated (SELL)*\nNew SL Locked: ${new_sl:.2f}")

                    # MAIN TARGET HIT
                    if btc_price <= tp_p:
                        pnl_main = qty * (entry_p - tp_p)
                        total_pnl = pnl_main

                        if pyramid_position is not None:
                            p_pnl = pyramid_position['qty'] * (pyramid_position['entry_price'] - tp_p)
                            total_pnl += p_pnl
                            send_telegram(f"🎯 *PYRAMID POSITION CLOSED AT MAIN TARGET!*\nPyramid Profit: +${p_pnl:.2f}")
                            pyramid_position = None

                        ACCOUNT_BALANCE += total_pnl

                        last_tp_hit_price = tp_p
                        follow_through_direction = "SELL"

                        send_telegram(f"🎯 *TARGET HIT (SELL)!*\nTotal Profit: +${total_pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}\n🚀 *Standing by for Follow-Through Entry (-200 pts).*")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_TP",
                            "exit": round(btc_price, 2),
                            "pnl_usd": round(total_pnl, 2),
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None
                        pyramid_done_for_trade = False
                        base_current_zone = 1
                        base_zone_sl_count = 0
                        base_total_trades = 0

                    # MAIN STOP LOSS HIT
                    elif btc_price >= sl_p:
                        loss_main = qty * (sl_p - entry_p)
                        total_loss = loss_main
                        base_zone_sl_count += 1
                        base_total_trades += 1

                        if pyramid_position is not None:
                            p_loss = pyramid_position['qty'] * (btc_price - pyramid_position['entry_price'])
                            total_loss += p_loss
                            send_telegram(f"⚠️ *Main Trade SL Hit! Force Closing Active Pyramiding Trade.* Loss: -${p_loss:.2f}")
                            pyramid_position = None

                        ACCOUNT_BALANCE -= total_loss

                        last_tp_hit_price = None
                        follow_through_direction = None

                        send_telegram(f"❌ *STOP LOSS HIT (SELL)!*\nTotal Loss: -${total_loss:.2f}\nBase Zone {base_current_zone} SL Count: {base_zone_sl_count}/3\nTotal Base Trades: {base_total_trades}/9")
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": "EXIT_SL",
                            "exit": round(btc_price, 2),
                            "pnl_usd": -round(total_loss, 2),
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

                        current_position = None
                        pyramid_done_for_trade = False

                # BASE ZONE SHIFT EVALUATION AFTER SL HIT
                if base_zone_sl_count >= 3:
                    if base_current_zone < 3:
                        base_current_zone += 1
                        base_zone_sl_count = 0
                        send_telegram(f"⚠️ *Base Zone Shift Triggered!* Switching to Base Zone {base_current_zone}")
                    else:
                        send_telegram(f"🚨 *All 3 Base Zones Failed (Max 9 SLs Hit)!* Event Terminated.")
                        reset_event_state()
                        event_finished = True

            # --- 2. EVALUATE HTF SIGNALS & CANDLE CHANGE RESET ---
            else:
                day1_open = float(d_klines[-3][1])
                day1_close = float(d_klines[-3][4])
                day1_diff = abs(day1_open - day1_close)

                day2_open = float(d_klines[-2][1])
                day2_close = float(d_klines[-2][4])
                day2_diff = abs(day2_open - day2_close)

                w_open = float(w_klines[-2][1])
                w_close = float(w_klines[-2][4])
                weekly_dist = abs(w_open - w_close)

                m_open = float(m_klines[-1][1])
                m_prev_open = float(m_klines[-2][1])
                m_prev_close = float(m_klines[-2][4])
                monthly_dist = abs(m_prev_open - m_prev_close)

                w_type, _ = analyze_candle_structure(float(w_klines[-2][1]), float(w_klines[-2][2]), float(w_klines[-2][3]), float(w_klines[-2][4]))
                m_type, _ = analyze_candle_structure(float(m_klines[-2][1]), float(m_klines[-2][2]), float(m_klines[-2][3]), float(m_klines[-2][4]))

                current_detected_setup = None
                
                if day1_diff < 300.0 and day2_diff < 300.0:
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

                if active_setup_type is not None and current_detected_setup != active_setup_type:
                    send_telegram(f"🔄 *Candle Structure Changed!* ({active_setup_type} ➡️ {current_detected_setup}). Resetting setup counters.")
                    reset_event_state()
                    active_setup_type = current_detected_setup

                m_invalidated = False
                if m_type == "DICY_GREEN" and btc_price >= (m_open + 3000.0):
                    m_invalidated = True
                    if not m_invalid_alert_sent:
                        send_telegram(f"🚨 *Monthly Dicy Green Invalidated!* Unlocking scanning...")
                        m_invalid_alert_sent = True

                elif m_type == "DICY_RED" and btc_price <= (m_open - 3000.0):
                    m_invalidated = True
                    if not m_invalid_alert_sent:
                        send_telegram(f"🚨 *Monthly Dicy Red Invalidated!* Unlocking scanning...")
                        m_invalid_alert_sent = True

                if m_invalidated:
                    event_finished = False
                    reset_event_state()

                if event_finished and current_event_id != last_processed_event_id:
                    event_finished = False
                    reset_event_state()
                    m_invalid_alert_sent = False
                    send_telegram(f"🔓 *New HTF Event Detected! Event Lockdown Lifted.*")

                if not event_finished and base_total_trades < 9:
                    buy_trigger = False
                    sell_trigger = False
                    entry_reason = ""
                    entry_tf = ""
                    ref_price = float(d_klines[-2][4])

                    if base_current_zone == 1 and zone1_ref_level is None:
                        zone1_ref_level = ref_price

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

                    # --- STANDARD ENTRY LOGIC ---
                    if not buy_trigger and not sell_trigger:
                        if base_current_zone == 1:
                            if current_detected_setup == "2_DOJI":
                                if btc_price >= ref_price + 200: buy_trigger = True; entry_reason = "Zone 1: 2-Day Doji Breakout"
                                elif btc_price <= ref_price - 200: sell_trigger = True; entry_reason = "Zone 1: 2-Day Doji Breakout"
                            elif current_detected_setup == "WEEKLY_DOJI":
                                if btc_price >= ref_price + 500: buy_trigger = True; entry_reason = "Zone 1: Weekly Doji Breakout"
                                elif btc_price <= ref_price - 500: sell_trigger = True; entry_reason = "Zone 1: Weekly Doji Breakout"
                            elif current_detected_setup == "MONTHLY_DOJI":
                                if btc_price >= ref_price + 500: buy_trigger = True; entry_reason = "Zone 1: Monthly Doji Breakout"
                                elif btc_price <= ref_price - 500: sell_trigger = True; entry_reason = "Zone 1: Monthly Doji Breakout"
                            elif current_detected_setup == "STRONG_BULL":
                                if btc_price >= ref_price + 500: buy_trigger = True; entry_reason = "Zone 1: Strong Bullish Breakout"
                            elif current_detected_setup == "STRONG_BEAR":
                                if btc_price <= ref_price - 500: sell_trigger = True; entry_reason = "Zone 1: Strong Bearish Breakout"
                            elif current_detected_setup == "DICY_GREEN" and not m_invalidated:
                                if btc_price <= ref_price - 500: sell_trigger = True; entry_reason = "Zone 1: Dicy Green Trap Setup"
                            elif current_detected_setup == "DICY_RED" and not m_invalidated:
                                if btc_price >= ref_price + 500: buy_trigger = True; entry_reason = "Zone 1: Dicy Red Trap Setup"

                        elif base_current_zone in [2, 3]:
                            if prev_event_high is not None and btc_price >= prev_event_high: 
                                buy_trigger = True; entry_reason = f"Base Zone {base_current_zone}: Event High Breakout"
                            elif prev_event_low is not None and btc_price <= prev_event_low: 
                                sell_trigger = True; entry_reason = f"Base Zone {base_current_zone}: Event Low Breakout"

                    is_same_candle = (candle_time == last_trade_candle_time)
                    is_same_level = (last_trade_price is not None and abs(btc_price - last_trade_price) < 50.0)

                    if (buy_trigger or sell_trigger) and not (is_same_candle and is_same_level):
                        side = "BUY" if buy_trigger else "SELL"
                        
                        if base_current_zone == 1 and zone1_ref_level is not None:
                            entry_p = zone1_ref_level
                        else:
                            entry_p = btc_price

                        sl_p = entry_p + 200.0 if side == "SELL" else entry_p - 200.0
                        tp_p = entry_p - 2000.0 if side == "SELL" else entry_p + 2000.0

                        qty = FIXED_RISK_USD / SL_POINTS

                        current_position = {
                            "side": side,
                            "entry_price": entry_p,
                            "sl_price": sl_p,
                            "tp_price": tp_p,
                            "qty": qty,
                            "reason": entry_reason,
                            "timeframe": entry_tf
                        }

                        last_tp_hit_price = None
                        follow_through_direction = None
                        pyramid_done_for_trade = False

                        active_setup_type = current_detected_setup
                        last_processed_event_id = current_event_id
                        last_trade_candle_time = candle_time
                        last_trade_price = btc_price

                        msg = (f"🚨 *NEW MAIN TRADE EXECUTED ({side})*\n\n"
                               f"📌 *Entry Reason:* {entry_reason}\n"
                               f"📍 *Base Zone:* Zone {base_current_zone}\n\n"
                               f"🎯 *Entry Level:* ${entry_p:.2f}\n"
                               f"🛑 *Stop Loss (SL):* ${sl_p:.2f} (200 pts)\n"
                               f"🎯 *Take Profit (TP):* ${tp_p:.2f} (1:10 RR)\n\n"
                               f"💰 *Position Size:* {qty:.4f} BTC\n"
                               f"💵 *Fixed Capital Risk:* ${FIXED_RISK_USD:.2f}\n"
                               f"📊 *Account Balance:* ${ACCOUNT_BALANCE:.2f}")
                        
                        print(msg)
                        send_telegram(msg)
                        
                        log_to_sheet({
                            "script": "BTCUSDT",
                            "type": f"ENTRY_{side}",
                            "reason": entry_reason,
                            "entry": round(entry_p, 2),
                            "risk": FIXED_RISK_USD,
                            "balance": round(ACCOUNT_BALANCE, 2)
                        })

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price: ${btc_price:.2f} | Pos: {current_position['side'] if current_position else 'None'} | Base Zone: {base_current_zone} | Pyr Zone: {pyramid_current_zone} | Active Setup: {active_setup_type}")
            time.sleep(10)

        except Exception as e:
            time.sleep(10)


# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    run_bot()
