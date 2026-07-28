import json
import os
import threading
import time
import urllib.parse
import urllib.request
import traceback
from flask import Flask
import numpy as np
import pandas as pd

# ===================================================
# 🌐 FLASK WEB SERVER (Render 24/7 Keep-Alive)
# ===================================================
app = Flask(__name__)

@app.route('/')
def home():
    return '🤖 BTC 200pt Buffer MTF Bot is Live & Running 24/7!'

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# ===================================================
# 🔑 LIVE CONFIGURATION
# ===================================================
BOT_TOKEN = '8981662979:AAFg2MAiHOeYlK_bxbIXXLK9JdNSGqoksfc'
CHAT_ID = '1862803975'
SHEET_WEBAPP_URL = 'https://script.google.com/macros/s/AKfycbzQHhdk3UH4vZStrRuIuHI4K4V9FGbj6R3UqpPNBXmHTv3CIf9P4jS3393G_32sapfolQ/exec'

def send_telegram(msg):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': CHAT_ID,
        'text': msg,
        'parse_mode': 'Markdown',
    }).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        print(f'❌ Telegram Error: {e}')

def log_to_google_sheet(timestamp, trade_type, entry_p, exit_p, pnl_pts, pnl_usd, balance):
    if not SHEET_WEBAPP_URL:
        return
    payload = json.dumps({
        'timestamp': timestamp,
        'type': trade_type,
        'entry_price': round(entry_p, 2),
        'exit_price': round(exit_p, 2),
        'pnl_pts': round(pnl_pts, 2),
        'pnl_usd': round(pnl_usd, 2),
        'balance': round(balance, 2),
    }).encode('utf-8')

    try:
        req = urllib.request.Request(
            SHEET_WEBAPP_URL, data=payload, headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print('📊 Trade successfully synced to Google Sheet!')
    except Exception as e:
        print(f'❌ Google Sheet Sync Error: {e}')

# ===================================================
# 🌐 LIVE DATA FETCH ENGINE
# ===================================================
def fetch_recent_klines(symbol='BTCUSDT', interval='5m', limit=1000):
    url = f'https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.loads(response.read().decode())
        
    all_candles = [
        [c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
        for c in data
    ]
    df = pd.DataFrame(
        all_candles,
        columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'],
    )
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def process_mtf_live(df_5m):
    # ATR Calculation
    high_low = df_5m['high'] - df_5m['low']
    high_cp = np.abs(df_5m['high'] - df_5m['close'].shift())
    low_cp = np.abs(df_5m['low'] - df_5m['close'].shift())
    df_5m['tr'] = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    df_5m['atr'] = df_5m['tr'].rolling(14).mean()

    # Daily Doji Logic
    df_daily = df_5m.resample('D', on='datetime').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna().reset_index()
    df_daily['body'] = np.abs(df_daily['close'] - df_daily['open'])
    df_daily['range'] = df_daily['high'] - df_daily['low']
    df_daily['daily_doji'] = df_daily['body'] <= (df_daily['range'] * 0.25)
    df_daily['date'] = df_daily['datetime'].dt.date

    # Weekly Strong Candle Logic
    df_weekly = df_5m.resample('W', on='datetime').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna().reset_index()
    df_weekly['body'] = np.abs(df_weekly['close'] - df_weekly['open'])
    df_weekly['range'] = df_weekly['high'] - df_weekly['low']
    df_weekly['weekly_strong_bull'] = (df_weekly['close'] > df_weekly['open']) & (df_weekly['body'] >= df_weekly['range'] * 0.80)
    df_weekly['weekly_strong_bear'] = (df_weekly['open'] > df_weekly['close']) & (df_weekly['body'] >= df_weekly['range'] * 0.80)

    # Weekly Shift(1) to avoid lookahead bias in live trading
    df_weekly['weekly_strong_bull'] = df_weekly['weekly_strong_bull'].shift(1)
    df_weekly['weekly_strong_bear'] = df_weekly['weekly_strong_bear'].shift(1)

    df_5m['date'] = df_5m['datetime'].dt.date
    df_5m['week_id'] = df_5m['datetime'].dt.isocalendar().year.astype(str) + '_' + df_5m['datetime'].dt.isocalendar().week.astype(str)
    df_weekly['week_id'] = df_weekly['datetime'].dt.isocalendar().year.astype(str) + '_' + df_weekly['datetime'].dt.isocalendar().week.astype(str)

    df_5m = df_5m.merge(df_daily[['date', 'daily_doji']], on='date', how='left')
    df_5m = df_5m.merge(df_weekly[['week_id', 'weekly_strong_bull', 'weekly_strong_bear']], on='week_id', how='left')

    return df_5m

# ===================================================
# 🚀 LIVE PAPER TRADING ENGINE (200pt BUFFER + MTF)
# ===================================================
def start_paper_trading():
    print('🚀 Initializing 200pt Buffer MTF Paper Trading Bot...')
    send_telegram(
        '🟢 *200pt BUFFER MTF PAPER BOT IS ONLINE!*\n\n'
        '💰 *Starting Balance:* $1,000.00\n'
        '📊 *Strategy:* 200pt Buffer MTF Sweep\n'
        '⚡ *Latency:* Real-Time Execution'
    )

    balance = 1000.0
    position = None
    entry_price = 0.0
    sl_price = 0.0
    tp_price = 0.0
    position_size = 0.0
    risk_per_trade = 0.02
    pt_buffer = 200.0
    rr_ratio = 3.0

    current_date = None
    daily_trades = 0
    zone_level = 1
    zone_sl_count = 0
    day_high = 0.0
    day_low = float('inf')
    marked_level = None
    trade_side = None

    while True:
        try:
            df_raw = fetch_recent_klines()
            df_processed = process_mtf_live(df_raw)

            last_row = df_processed.iloc[-1]
            current_price = last_row['close']
            current_time = last_row['datetime'].strftime('%Y-%m-%d %H:%M')
            candle_date = last_row['date']

            print(f'[{current_time}] BTC Price: ${current_price:.2f} | Active Position: {position} | Zone: {zone_level}')

            # --- NEW DAY RESET ---
            if candle_date != current_date:
                current_date = candle_date
                daily_trades = 0
                zone_level = 1
                zone_sl_count = 0
                day_high = last_row['high']
                day_low = last_row['low']
                marked_level = None
                trade_side = None
            else:
                day_high = max(day_high, last_row['high'])
                day_low = min(day_low, last_row['low'])

            # --- 1. EXIT CHECK ---
            if position == 'BUY':
                if last_row['low'] <= sl_price:
                    pnl_usd = (sl_price - entry_price) * position_size
                    balance += pnl_usd
                    zone_sl_count += 1
                    daily_trades += 1
                    send_telegram(
                        f'🔴 *STOP LOSS HIT (BUY)*\n\n'
                        f'🎯 *Entry:* ${entry_price:.2f}\n'
                        f'🔻 *Loss:* ${pnl_usd:.2f}\n'
                        f'🏁 *Balance:* ${balance:.2f}'
                    )
                    log_to_google_sheet(current_time, 'BUY', entry_price, sl_price, sl_price - entry_price, pnl_usd, balance)
                    position = None

                elif last_row['high'] >= tp_price:
                    pnl_usd = (tp_price - entry_price) * position_size
                    balance += pnl_usd
                    daily_trades += 1
                    send_telegram(
                        f'🎯 *TARGET HIT (BUY 1:3)* 🎉\n\n'
                        f'🎯 *Entry:* ${entry_price:.2f}\n'
                        f'💰 *Profit:* +${pnl_usd:.2f}\n'
                        f'🏁 *Balance:* ${balance:.2f}'
                    )
                    log_to_google_sheet(current_time, 'BUY', entry_price, tp_price, tp_price - entry_price, pnl_usd, balance)
                    position = None
                    zone_sl_count = 0

            elif position == 'SELL':
                if last_row['high'] >= sl_price:
                    pnl_usd = (entry_price - sl_price) * position_size
                    balance += pnl_usd
                    zone_sl_count += 1
                    daily_trades += 1
                    send_telegram(
                        f'🔴 *STOP LOSS HIT (SELL)*\n\n'
                        f'🎯 *Entry:* ${entry_price:.2f}\n'
                        f'🔻 *Loss:* ${pnl_usd:.2f}\n'
                        f'🏁 *Balance:* ${balance:.2f}'
                    )
                    log_to_google_sheet(current_time, 'SELL', entry_price, sl_price, entry_price - sl_price, pnl_usd, balance)
                    position = None

                elif last_row['low'] <= tp_price:
                    pnl_usd = (entry_price - tp_price) * position_size
                    balance += pnl_usd
                    daily_trades += 1
                    send_telegram(
                        f'🎯 *TARGET HIT (SELL 1:3)* 🎉\n\n'
                        f'🎯 *Entry:* ${entry_price:.2f}\n'
                        f'💰 *Profit:* +${pnl_usd:.2f}\n'
                        f'🏁 *Balance:* ${balance:.2f}'
                    )
                    log_to_google_sheet(current_time, 'SELL', entry_price, tp_price, entry_price - tp_price, pnl_usd, balance)
                    position = None
                    zone_sl_count = 0

            # --- 2. ZONE SHIFT LOGIC ---
            if zone_sl_count >= 3:
                if zone_level < 3:
                    zone_level += 1
                    zone_sl_count = 0
                    if trade_side == 'SELL':
                        marked_level = day_high + pt_buffer
                    else:
                        marked_level = day_low - pt_buffer
                else:
                    marked_level = None

            # --- 3. ENTRY TRIGGER CHECK ---
            if position is None and daily_trades < 9:
                if marked_level is None and zone_level == 1:
                    if last_row['daily_doji'] or last_row['weekly_strong_bull']:
                        trade_side = 'BUY'
                        marked_level = current_price + pt_buffer
                    elif last_row['daily_doji'] or last_row['weekly_strong_bear']:
                        trade_side = 'SELL'
                        marked_level = current_price - pt_buffer

                if marked_level is not None:
                    atr = last_row['atr']
                    risk_amount = balance * risk_per_trade

                    if trade_side == 'BUY' and last_row['high'] >= marked_level:
                        position = 'BUY'
                        entry_price = marked_level
                        sl_price = entry_price - (atr * 1.5)
                        tp_price = entry_price + ((atr * 1.5) * rr_ratio)
                        position_size = risk_amount / max(1.0, (entry_price - sl_price))
                        send_telegram(
                            f'🚀 *BUY ORDER EXECUTED*\n\n'
                            f'📍 *Entry:* ${entry_price:.2f}\n'
                            f'🛑 *SL:* ${sl_price:.2f}\n'
                            f'🎯 *TP:* ${tp_price:.2f}\n'
                            f'📍 *Zone:* {zone_level}'
                        )

                    elif trade_side == 'SELL' and last_row['low'] <= marked_level:
                        position = 'SELL'
                        entry_price = marked_level
                        sl_price = entry_price + (atr * 1.5)
                        tp_price = entry_price - ((atr * 1.5) * rr_ratio)
                        position_size = risk_amount / max(1.0, (sl_price - entry_price))
                        send_telegram(
                            f'🔻 *SELL ORDER EXECUTED*\n\n'
                            f'📍 *Entry:* ${entry_price:.2f}\n'
                            f'🛑 *SL:* ${sl_price:.2f}\n'
                            f'🎯 *TP:* ${tp_price:.2f}\n'
                            f'📍 *Zone:* {zone_level}'
                        )

            time.sleep(300)

        except Exception as e:
            print(f'⚠️ Error in live paper trading loop: {e}')
            traceback.print_exc()
            time.sleep(15)

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    start_paper_trading()
