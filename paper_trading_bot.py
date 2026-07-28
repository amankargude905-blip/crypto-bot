import json
import os
import threading
import time
import urllib.parse
import urllib.request
from flask import Flask
import numpy as np
import pandas as pd

# ===================================================
# 🌐 FLASK WEB SERVER (Render 24/7 Keep-Alive Ke Liye)
# ===================================================
app = Flask(__name__)


@app.route('/')
def home():
    return '🤖 BTC RL Trading Bot is Live & Running 24/7!'


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
        with urllib.request.urlopen(req) as resp:
            pass
    except Exception as e:
        print(f'❌ Telegram Error: {e}')


def log_to_google_sheet(
    timestamp, trade_type, entry_p, exit_p, pnl_pts, pnl_usd, balance
):
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
        with urllib.request.urlopen(req) as resp:
            print('📊 Trade successfully synced to Google Sheet!')
    except Exception as e:
        print(f'❌ Google Sheet Sync Error: {e}')


# ===================================================
# 🌐 LIVE DATA FETCH ENGINE (451 FIXED)
# ===================================================
def fetch_recent_klines(symbol='BTCUSDT', interval='5m', limit=1000):
    # Fixed URL: Changed from api.binance.com to data-api.binance.vision to bypass Render US IP block
    url = f'https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
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


def process_htf_levels(df_5m, tf_code):
    df_res = (
        df_5m.resample(tf_code, on='datetime')
        .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
        .dropna()
    )
    body = (df_res['close'] - df_res['open']).abs()
    rng = df_res['high'] - df_res['low']
    ratio = np.where(rng > 0, body / rng, 0.0)

    is_doji = ratio <= 0.25
    doji_pivot = np.where(
        is_doji, (df_res['open'] + df_res['close']) / 2.0, np.nan
    )

    is_strong_bull = (ratio >= 0.80) & (df_res['close'] > df_res['open'])
    is_strong_bear = (ratio >= 0.80) & (df_res['close'] < df_res['open'])

    strong_buy_level = np.where(is_strong_bull, df_res['close'] + 500.0, np.nan)
    strong_sell_level = np.where(
        is_strong_bear, df_res['close'] - 500.0, np.nan
    )

    res_df = pd.DataFrame(
        {
            f'{tf_code}_doji_pivot': doji_pivot,
            f'{tf_code}_strong_buy': strong_buy_level,
            f'{tf_code}_strong_sell': strong_sell_level,
        },
        index=df_res.index,
    )

    return res_df.shift(1)


def build_live_features(df_5m):
    df_d = process_htf_levels(df_5m, '1D')
    df_w = process_htf_levels(df_5m, '1W')
    df_m = process_htf_levels(df_5m, '1ME')

    df_5m_idx = df_5m.set_index('datetime')
    df_merged = df_5m_idx.join(df_d).join(df_w).join(df_m)

    level_cols = [
        c
        for c in df_merged.columns
        if any(x in c for x in ['doji', 'strong'])
    ]
    df_merged[level_cols] = df_merged[level_cols].ffill()

    df_merged['ema200'] = (
        df_merged['close'].ewm(span=200, adjust=False).mean()
    )
    high_low = df_merged['high'] - df_merged['low']
    high_cp = np.abs(df_merged['high'] - df_merged['close'].shift())
    low_cp = np.abs(df_merged['low'] - df_merged['close'].shift())
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    df_merged['atr'] = tr.rolling(14).mean().bfill()

    return df_merged.reset_index()


# ===================================================
# 🚀 LIVE PAPER TRADING ENGINE
# ===================================================
def start_paper_trading():
    print('🚀 Initializing RL Paper Trading Bot...')
    send_telegram(
        '🟢 *PAPER TRADING BOT IS ONLINE!*\n\n'
        '💰 *Starting Balance:* $1,000.00\n'
        '📊 *Google Sheet Sync:* Connected ✅\n'
        '⚡ *Latency:* 0 Seconds (Real-Time)'
    )

    balance = 1000.0
    position = None
    entry_price = 0.0
    risk_usd = 20.0  # 2% Risk per trade ($20)
    be_locked = False
    sl_dist = 200.0
    tp_dist = 2000.0

    while True:
        try:
            df_raw = fetch_recent_klines()
            df_processed = build_live_features(df_raw)

            last_row = df_processed.iloc[-1]
            current_price = last_row['close']
            current_time = last_row['datetime'].strftime('%Y-%m-%d %H:%M')

            print(f'[{current_time}] BTC Price: ${current_price:.2f}')

            if position is not None:
                pnl_pts = (
                    (current_price - entry_price)
                    if position == 'BUY'
                    else (entry_price - current_price)
                )

                # Breakeven Check
                if pnl_pts >= 400.0 and not be_locked:
                    be_locked = True
                    send_telegram(
                        f'🛡️ *BREAKEVEN LOCKED (+400 pts)*\n\n'
                        f'📌 *Position:* {position}\n'
                        f'🎯 *Entry:* ${entry_price:.2f}\n'
                        f'📈 *Current Price:* ${current_price:.2f}'
                    )

                # Target Hit (1:10)
                elif pnl_pts >= tp_dist:
                    gain = risk_usd * 10.0
                    balance += gain
                    send_telegram(
                        f'🎯 *TARGET HIT (1:10 WIN)!* 🎉\n\n'
                        f'📌 *Type:* {position}\n'
                        f'💰 *Profit Earned:* +${gain:.2f}\n'
                        f'🏁 *New Account Balance:* ${balance:.2f}'
                    )
                    log_to_google_sheet(
                        current_time, position, entry_price, current_price, pnl_pts, gain, balance
                    )
                    position = None

                # Breakeven Exit
                elif be_locked and pnl_pts <= 0.0:
                    send_telegram(
                        f'🛡️ *BREAKEVEN EXIT*\n\n'
                        f'📌 *Type:* {position}\n'
                        f'💵 *PnL:* $0.00\n'
                        f'🏁 *Account Balance:* ${balance:.2f}'
                    )
                    log_to_google_sheet(
                        current_time, position, entry_price, current_price, 0.0, 0.0, balance
                    )
                    position = None

                # Stop Loss Hit
                elif not be_locked and pnl_pts <= -sl_dist:
                    balance -= risk_usd
                    send_telegram(
                        f'🔴 *STOP LOSS HIT (200pt)*\n\n'
                        f'📌 *Type:* {position}\n'
                        f'🔻 *Loss:* -${risk_usd:.2f}\n'
                        f'🏁 *New Account Balance:* ${balance:.2f}'
                    )
                    log_to_google_sheet(
                        current_time, position, entry_price, current_price, -sl_dist, -risk_usd, balance
                    )
                    position = None

            # Sleep for 5 minutes
            time.sleep(300)

        except Exception as e:
            print(f'⚠️ Error in live loop: {e}')
            time.sleep(10)


if __name__ == '__main__':
    # Background mein Flask server start karein (Render deployment ke liye)
    threading.Thread(target=run_flask, daemon=True).start()

    # Main Bot Loop start karein
    start_paper_trading()
