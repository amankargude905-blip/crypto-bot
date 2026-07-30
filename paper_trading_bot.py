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
TELEGRAM_BOT_TOKEN = "8981662979:AAFg2MAiHOeYlK_bxbIXXLK9JdNSGqoksfc"
TELEGRAM_CHAT_ID = "1862803975"
SHEET_WEBAPP_URL = "https://script.google.com/macros/s/AKfycbzQHhdk3UH4vZStrRuIuHI4K4V9FGbj6R3UqpPNBXmHTv3CIf9P4jS3393G_32sapfolQ/exec"

ACCOUNT_BALANCE = 10000.0  # Initial Paper Trading Capital ($)
RISK_PER_TRADE_PCT = 0.01  # 1% Risk per trade
SL_POINTS = 200.0           # Fixed 200 Points SL
TP_POINTS = 2000.0          # Fixed 1:10 RR Target (2000 Points)

# Trailing Config
TRAIL_TRIGGER_PTS = 1600.0
TRAIL_LOCK_PTS = 600.0

# State Tracking
current_position = None  # Dict for active position
zone_sl_count = 0        # Continuous SL count in current zone (0 to 3)
current_zone = 1         # Zone 1, Zone 2, Zone 3
total_strategy_trades = 0 # Persistent SL counter across full setup (Max 9)
event_finished = False   # Lock flag when Target is hit or Max SLs reached

day_high = None
day_low = None
current_day_str = ""

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

    # Primary: Binance Global Vision API
    binance_vision = "https://data-api.binance.vision"
    try:
        r = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=1", headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            current_price = float(data[0][4])

            d_data = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=5", headers=headers, timeout=5).json()
            w_data = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1w&limit=3", headers=headers, timeout=5).json()
            m_data = requests.get(f"{binance_vision}/api/v3/klines?symbol=BTCUSDT&interval=1M&limit=3", headers=headers, timeout=5).json()

            return current_price, d_data, w_data, m_data
    except Exception:
        pass

    # Secondary Backup
    try:
        cc_url = "https://min-api.cryptocompare.com/data/v2/histominute?fsym=BTC&tsym=USDT&limit=1"
        r_cc = requests.get(cc_url, headers=headers, timeout=5).json()
        current_price = float(r_cc['Data']['Data'][-1]['close'])

        d_data = requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=5", headers=headers, timeout=5).json()
        w_data = requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1w&limit=3", headers=headers, timeout=5).json()
        m_data = requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1M&limit=3", headers=headers, timeout=5).json()

        return current_price, d_data, w_data, m_data
    except Exception as e:
        print(f"Data Fetch Error: {e}")
        return None, None, None, None

def analyze_candle_structure(open_p, high_p, low_p, close_p):
    total_range = high_p - low_p
    if total_range == 0:
        return "NORMAL", 0.0
    body = abs(close_p - open_p)
    body_pct = (body / total_range) * 100
    
    is_green = close_p >= open_p
    close_pct_from_low = ((close_p - low_p) / total_range) * 100
    close_pct_from_high = ((high_p - close_p) / total_range) * 100

    if body_pct <= 25.0:
        return "DOJI", body_pct
    elif body_pct >= 80.0:
        return "STRONG_BULL" if is_green else "STRONG_BEAR", body_pct
    elif is_green and close_pct_from_high >= 30.0:
        return "DICY_GREEN", close_pct_from_high
    elif not is_green and close_pct_from_low >= 30.0:
        return "DICY_RED", close_pct_from_low
    
    return "NORMAL", body_pct

def run_bot():
    global current_position, ACCOUNT_BALANCE, zone_sl_count, current_zone, total_strategy_trades
    global day_high, day_low, current_day_str, event_finished

    print("🚀 Aman's Precision Master Strategy Bot Started...")
    send_telegram("🚀 *Master Paper Trading Bot Started Live on Render!*")

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today_str = now_utc.strftime("%Y-%m-%d")

            # Daily High/Low Reset at UTC midnight
            if today_str != current_day_str:
                current_day_str = today_str
                day_high = None
                day_low = None
                print(f"🔄 New UTC Day Reset (Price High/Low Reset Only): {current_day_str}")

            btc_price, d_klines, w_klines, m_klines = fetch_btc_data()
            if btc_price is None or not d_klines or not w_klines or not m_klines:
                time.sleep(10)
                continue

            # Store Previous High/Low before updating
            prev_day_high = day_high
            prev_day_low = day_low

            # Dynamic Day High/Low Tracking
            day_high = btc_price if day_high is None else max(day_high, btc_price)
            day_low = btc_price if day_low is None else min(day_low, btc_price)

            # Check for Active Position Management
            if current_position is not None:
                side = current_position['side']
                entry_p = current_position['entry_price']
                sl_p = current_position['sl_price']
                tp_p = current_position['tp_price']
                qty = current_position['qty']

                # 1. Step-Trailing Logic (+1600 pts move -> Lock +600 pts)
                if side == 'BUY':
                    max_favorable = btc_price - entry_p
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p + TRAIL_LOCK_PTS
                        if new_sl > sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram(f"🛡️ *Trailing SL Updated (BUY)*\nNew SL Locked: ${new_sl:.2f}")

                    # Check TP / SL Hit
                    if btc_price >= tp_p:
                        pnl = qty * (tp_p - entry_p)
                        ACCOUNT_BALANCE += pnl
                        event_finished = True  # EVENT COMPLETE LOCKDOWN
                        send_telegram(f"🎯 *TARGET HIT (BUY)!*\nProfit: +${pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}\n🔒 *Event Completed Successfully! No further trades for this setup.*")
                        log_to_sheet({"type": "EXIT_TP", "pnl": round(pnl, 2), "balance": round(ACCOUNT_BALANCE, 2)})
                        current_position = None
                        current_zone = 1
                        zone_sl_count = 0
                        total_strategy_trades = 0

                    elif btc_price <= sl_p:
                        loss = qty * (entry_p - sl_p)
                        ACCOUNT_BALANCE -= loss
                        zone_sl_count += 1
                        total_strategy_trades += 1
                        send_telegram(f"❌ *STOP LOSS HIT (BUY)!*\nLoss: -${loss:.2f}\nZone {current_zone} SL Count: {zone_sl_count}/3\nTotal Setup Trades: {total_strategy_trades}/9")
                        log_to_sheet({"type": "EXIT_SL", "loss": round(loss, 2), "balance": round(ACCOUNT_BALANCE, 2)})
                        current_position = None

                elif side == 'SELL':
                    max_favorable = entry_p - btc_price
                    if max_favorable >= TRAIL_TRIGGER_PTS:
                        new_sl = entry_p - TRAIL_LOCK_PTS
                        if new_sl < sl_p:
                            current_position['sl_price'] = new_sl
                            send_telegram(f"🛡️ *Trailing SL Updated (SELL)*\nNew SL Locked: ${new_sl:.2f}")

                    # Check TP / SL Hit
                    if btc_price <= tp_p:
                        pnl = qty * (entry_p - tp_p)
                        ACCOUNT_BALANCE += pnl
                        event_finished = True  # EVENT COMPLETE LOCKDOWN
                        send_telegram(f"🎯 *TARGET HIT (SELL)!*\nProfit: +${pnl:.2f}\nNew Balance: ${ACCOUNT_BALANCE:.2f}\n🔒 *Event Completed Successfully! No further trades for this setup.*")
                        log_to_sheet({"type": "EXIT_TP", "pnl": round(pnl, 2), "balance": round(ACCOUNT_BALANCE, 2)})
                        current_position = None
                        current_zone = 1
                        zone_sl_count = 0
                        total_strategy_trades = 0

                    elif btc_price >= sl_p:
                        loss = qty * (sl_p - entry_p)
                        ACCOUNT_BALANCE -= loss
                        zone_sl_count += 1
                        total_strategy_trades += 1
                        send_telegram(f"❌ *STOP LOSS HIT (SELL)!*\nLoss: -${loss:.2f}\nZone {current_zone} SL Count: {zone_sl_count}/3\nTotal Setup Trades: {total_strategy_trades}/9")
                        log_to_sheet({"type": "EXIT_SL", "loss": round(loss, 2), "balance": round(ACCOUNT_BALANCE, 2)})
                        current_position = None

                # Persistent Zone Transition & Setup Reset Logic
                if zone_sl_count >= 3:
                    if current_zone < 3:
                        current_zone += 1
                        zone_sl_count = 0
                        send_telegram(f"⚠️ *Zone Shift Triggered!* Switching to Zone {current_zone}")
                    else:
                        send_telegram(f"🚨 *All 3 Zones Failed (Total 9 SLs Hit)!* Event Ended. Resetting sequence.")
                        current_zone = 1
                        zone_sl_count = 0
                        total_strategy_trades = 0
                        event_finished = True  # Max attempts done

            # 2. Entry Signal Evaluation
            elif not event_finished and total_strategy_trades < 9:
                d_type1, _ = analyze_candle_structure(float(d_klines[-3][1]), float(d_klines[-3][2]), float(d_klines[-3][3]), float(d_klines[-3][4]))
                d_type2, _ = analyze_candle_structure(float(d_klines[-2][1]), float(d_klines[-2][2]), float(d_klines[-2][3]), float(d_klines[-2][4]))
                
                w_type, _ = analyze_candle_structure(float(w_klines[-2][1]), float(w_klines[-2][2]), float(w_klines[-2][3]), float(w_klines[-2][4]))
                m_type, _ = analyze_candle_structure(float(m_klines[-2][1]), float(m_klines[-2][2]), float(m_klines[-2][3]), float(m_klines[-2][4]))

                buy_trigger = False
                sell_trigger = False
                entry_reason = ""
                entry_tf = ""
                ref_price = float(d_klines[-2][4])

                # Buffer Rules Setup (Zone 1)
                if current_zone == 1:
                    if d_type1 == "DOJI" and d_type2 == "DOJI":
                        if btc_price >= ref_price + 200: 
                            buy_trigger = True
                            entry_reason = "2 Consecutive Dojis Breakout"
                            entry_tf = "Daily (1D)"
                        elif btc_price <= ref_price - 200: 
                            sell_trigger = True
                            entry_reason = "2 Consecutive Dojis Breakout"
                            entry_tf = "Daily (1D)"
                    
                    elif w_type == "DOJI" or m_type == "DOJI":
                        if btc_price >= ref_price + 200: 
                            buy_trigger = True
                            entry_reason = "Single Doji Breakout"
                            entry_tf = "Weekly / Monthly"
                        elif btc_price <= ref_price - 200: 
                            sell_trigger = True
                            entry_reason = "Single Doji Breakout"
                            entry_tf = "Weekly / Monthly"

                    elif w_type == "STRONG_BULL" or m_type == "STRONG_BULL":
                        if btc_price >= ref_price + 500: 
                            buy_trigger = True
                            entry_reason = "Strong Bullish Breakout"
                            entry_tf = "Weekly / Monthly"
                    elif w_type == "STRONG_BEAR" or m_type == "STRONG_BEAR":
                        if btc_price <= ref_price - 500: 
                            sell_trigger = True
                            entry_reason = "Strong Bearish Breakout"
                            entry_tf = "Weekly / Monthly"

                    elif w_type == "DICY_GREEN" or m_type == "DICY_GREEN":
                        if btc_price <= ref_price - 500: 
                            sell_trigger = True
                            entry_reason = "Dicy Green Trap Setup"
                            entry_tf = "Weekly / Monthly"
                    elif w_type == "DICY_RED" or m_type == "DICY_RED":
                        if btc_price >= ref_price + 500: 
                            buy_trigger = True
                            entry_reason = "Dicy Red Trap Setup"
                            entry_tf = "Weekly / Monthly"

                # Zone 2 & 3: Breakouts using prev_day_high / prev_day_low
                elif current_zone in [2, 3]:
                    if prev_day_high is not None and btc_price >= prev_day_high: 
                        buy_trigger = True
                        entry_reason = f"Zone {current_zone} Day High Breakout"
                        entry_tf = "Intraday"
                    elif prev_day_low is not None and btc_price <= prev_day_low: 
                        sell_trigger = True
                        entry_reason = f"Zone {current_zone} Day Low Breakout"
                        entry_tf = "Intraday"

                # Reset Lockdown Flag If A Fresh Setup Pattern Appears
                if buy_trigger or sell_trigger:
                    event_finished = False

                # Execute Trade Entry
                if (buy_trigger or sell_trigger) and not event_finished:
                    side = "BUY" if buy_trigger else "SELL"
                    risk_amt = ACCOUNT_BALANCE * RISK_PER_TRADE_PCT
                    qty = risk_amt / SL_POINTS

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

                    msg = (f"🚨 *NEW TRADE EXECUTED ({side})*\n\n"
                           f"📌 *Entry Reason:* {entry_reason}\n"
                           f"⏱️ *Timeframe:* {entry_tf}\n"
                           f"📍 *Zone:* Zone {current_zone}\n\n"
                           f"🎯 *Entry Level:* ${btc_price:.2f}\n"
                           f"🛑 *Stop Loss (SL):* ${sl:.2f} (200 pts)\n"
                           f"🎯 *Take Profit (TP):* ${tp:.2f} (1:10 RR)\n\n"
                           f"💰 *Position Size:* {qty:.4f} BTC\n"
                           f"💵 *Capital Risk:* ${risk_amt:.2f} (1%)\n"
                           f"📊 *Account Balance:* ${ACCOUNT_BALANCE:.2f}")
                    
                    print(msg)
                    send_telegram(msg)
                    log_to_sheet({
                        "type": f"ENTRY_{side}", 
                        "reason": entry_reason,
                        "tf": entry_tf,
                        "entry": round(btc_price, 2), 
                        "sl": round(sl, 2), 
                        "tp": round(tp, 2), 
                        "zone": current_zone
                    })

            print(f"[{datetime.now().strftime('%H:%M:%S')}] BTC Price: ${btc_price:.2f} | Position: {current_position['side'] if current_position else 'None'} | Zone: {current_zone} | Trades: {total_strategy_trades}/9 | Event Done: {event_finished}")
            time.sleep(300)

        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
