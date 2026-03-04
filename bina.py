import ccxt
import telebot
import time
import pandas as pd
import numpy as np
import threading
import logging
import math
import os
import socket
import requests as req_lib
from datetime import datetime, timedelta, timezone
from collections import deque
from dotenv import load_dotenv

# ==============================================================================
# ========== CẤU HÌNH & BIẾN TOÀN CỤC ==========
# ==============================================================================
if os.path.exists(".env"):
    load_dotenv(".env")

# ===== Binance API (từ .env) =====
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# ===== Telegram Bot (từ .env) =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))

# ===== Trading Config =====
TRADE_AMOUNT_USDT = 10.0
GLOBAL_LEVERAGE = 10
TIMEFRAME = "5m"
MAX_POSITIONS = 3
TRADING_ENABLED = True
TRAILING_ENABLED = True
USE_TESTNET = os.environ.get("TESTNET_MODE", "True").strip().lower() == "true"

SYMBOL_CONFIGS = {
    "BTC/USDT":   {"X": 0.15, "Y": 0.05, "Active": False},
    "ETH/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "SOL/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "BNB/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "XRP/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "DOGE/USDT":  {"X": 0.35, "Y": 0.05, "Active": True},
    "ADA/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "AVAX/USDT":  {"X": 0.35, "Y": 0.05, "Active": True},
    "DOT/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "LINK/USDT":  {"X": 0.35, "Y": 0.03, "Active": True},
    "NEAR/USDT":  {"X": 0.35, "Y": 0.05, "Active": True},
    "RLC/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "ARB/USDT":   {"X": 0.35, "Y": 0.03, "Active": True},
    "OP/USDT":    {"X": 0.35, "Y": 0.03, "Active": True},
    "INJ/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "APT/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "SUI/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "SEI/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
}

PAIRS = [sym for sym, cfg in SYMBOL_CONFIGS.items() if cfg.get("Active")]

# ==============================================================================
# ========== LOGGING ==========
# ==============================================================================
logging.basicConfig(
    filename='bot_wick_log.txt',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.info("=== EMA WICK BOT KHỞI ĐỘNG ===")

signals_log = deque(maxlen=2000)

# ==============================================================================
# ========== KẾT NỐI BINANCE & TELEGRAM ==========
# ==============================================================================
bot = telebot.TeleBot(TELEGRAM_TOKEN)

print("🔧 Đang kết nối Binance API...")
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
})

if USE_TESTNET:
    exchange.enableDemoTrading(True)
    print("🔧 Sử dụng DEMO TRADING Binance Futures")

try:
    ticker = exchange.fetch_ticker('BTC/USDT')
    print(f"✅ Kết nối API thành công! Giá BTC: {ticker['last']}")
except Exception as e:
    print(f"❌ LỖI KẾT NỐI API: {e}")

# Set Isolated Margin + Leverage cho tất cả pairs
for symbol in PAIRS:
    try:
        exchange.set_margin_mode('isolated', symbol)
    except:
        pass
    try:
        exchange.set_leverage(GLOBAL_LEVERAGE, symbol)
    except:
        pass

last_candle_ts = {symbol: 0 for symbol in PAIRS}

# ==============================================================================
# ========== CƠ CHẾ VÀO LỆNH AN TOÀN MỚI ==========
# ==============================================================================

def execute_smart_trade(symbol, side, entry_price, low, high):
    """
    Cập nhật: Xử lý trượt giá (Slippage) & Cơ chế Retry/Panic Close an toàn.
    Updated: Slippage handling & Safe Retry/Panic Close mechanism.
    """
    try:
        # Kiểm tra vị thế hiện có
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if float(p.get('contracts', 0) or 0) != 0:
                return None, "0", 0, 0, f"Đã có vị thế {p.get('side', 'unknown')}"

        # Tính Volume
        total_notional_usdt = TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE
        quantity = float(exchange.amount_to_precision(symbol, total_notional_usdt / entry_price))

        # Tính toán SL lý thuyết từ Wick
        if side == "buy":
            sl_raw = low * (1 - 0.002)
        else:
            sl_raw = high * (1 + 0.002)
        sl = float(exchange.price_to_precision(symbol, sl_raw))

        # Bước 1: Vào lệnh Market
        order = exchange.create_market_order(symbol, side, quantity)
        
        # Bước 2: Lấy giá khớp thực tế (average)
        actual_entry = order.get('average') or order.get('price') or exchange.fetch_ticker(symbol)['last']

        # Bước 3: Tính toán lại TP theo giá thực tế để đảm bảo RR 1:2
        risk = abs(actual_entry - sl)
        if side == "buy":
            tp = float(exchange.price_to_precision(symbol, actual_entry + (risk * 2)))
        else:
            tp = float(exchange.price_to_precision(symbol, actual_entry - (risk * 2)))

        sl_side = 'sell' if side == 'buy' else 'buy'
        tp_side = 'sell' if side == 'buy' else 'buy'

        # Bước 4: Đặt SL/TP với cơ chế thử lại (Safety Net)
        max_retries = 3
        sl_placed = False
        tp_placed = False
        
        for attempt in range(max_retries):
            try:
                if not sl_placed:
                    exchange.create_order(symbol, 'stop_market', sl_side, quantity,
                                          params={'stopPrice': sl, 'reduceOnly': True})
                    sl_placed = True
                
                if not tp_placed:
                    exchange.create_order(symbol, 'take_profit_market', tp_side, quantity,
                                          params={'stopPrice': tp, 'reduceOnly': True})
                    tp_placed = True
                
                if sl_placed and tp_placed:
                    break
                    
            except Exception as e:
                print(f"⚠️ Lỗi API đặt SL/TP lần {attempt+1} cho {symbol}: {e}")
                time.sleep(1)

        # Bước 5: Panic Close nếu không thể đặt SL
        if not sl_placed:
            print(f"🚨 NGUY HIỂM: Thất bại đặt SL sau {max_retries} lần. Đóng lệnh khẩn cấp!")
            try:
                exchange.create_market_order(symbol, sl_side, quantity, params={'reduceOnly': True})
            except Exception as panic_err:
                print(f"❌ LỖI ĐÓNG LỆNH KHẨN CẤP: {panic_err}")
            return None, "0", 0, 0, "Lỗi API SL/TP. Đã tự động đóng lệnh để bảo toàn vốn."

        return order, str(quantity), sl, tp, ""

    except Exception as e:
        return None, "0", 0, 0, str(e)

# ==============================================================================
# ========== TRAILING SL ==========
# ==============================================================================

def manage_trailing_sl():
    if not TRAILING_ENABLED:
        return

    try:
        positions = exchange.fetch_positions()
        if not positions:
            return

        for pos in positions:
            contracts = float(pos.get('contracts', 0) or 0)
            if contracts == 0:
                continue

            sym = pos.get('symbol', '')
            if sym not in SYMBOL_CONFIGS:
                continue

            entry_px = float(pos.get('entryPrice', 0) or 0)
            pos_side = pos.get('side', '').lower()
            if entry_px == 0 or not pos_side:
                continue

            ohlcv = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=5)
            if len(ohlcv) < 2:
                continue
            last_close = ohlcv[-2][4]

            open_orders = exchange.fetch_open_orders(sym)
            current_sl = 0
            sl_order_id = None
            current_tp = 0
            for o in open_orders:
                if o.get('type') in ['stop_market', 'stop'] and o.get('reduceOnly', False):
                    stop_price = o.get('stopPrice') or o.get('triggerPrice') or 0
                    if float(stop_price) > 0 and sl_order_id is None:
                        current_sl = float(stop_price)
                        sl_order_id = o['id']
                elif o.get('type') in ['take_profit_market', 'take_profit'] and o.get('reduceOnly', False):
                    tp_price = o.get('stopPrice') or o.get('triggerPrice') or 0
                    if float(tp_price) > 0:
                        current_tp = float(tp_price)

            if not sl_order_id or current_sl == 0:
                continue

            if current_tp > 0:
                original_risk = abs(current_tp - entry_px) / 2.0
            else:
                original_risk = abs(entry_px - current_sl)

            if original_risk == 0:
                continue

            if pos_side == 'long':
                rr1 = entry_px + original_risk
                rr2 = entry_px + original_risk * 2
            else:
                rr1 = entry_px - original_risk
                rr2 = entry_px - original_risk * 2

            new_sl = None
            trail_step = ""
            if pos_side == 'long':
                if last_close >= rr2 and current_sl < rr1:
                    new_sl = float(exchange.price_to_precision(sym, rr1))
                    trail_step = "Bước 2"
                elif last_close >= rr1 and current_sl < entry_px:
                    new_sl = float(exchange.price_to_precision(sym, entry_px))
                    trail_step = "Bước 1"
            else:
                if last_close <= rr2 and current_sl > rr1:
                    new_sl = float(exchange.price_to_precision(sym, rr1))
                    trail_step = "Bước 2"
                elif last_close <= rr1 and current_sl > entry_px:
                    new_sl = float(exchange.price_to_precision(sym, entry_px))
                    trail_step = "Bước 1"

            if new_sl:
                try:
                    exchange.cancel_order(sl_order_id, sym)
                    sl_side = 'sell' if pos_side == 'long' else 'buy'
                    exchange.create_order(sym, 'stop_market', sl_side, contracts,
                                          params={'stopPrice': new_sl, 'reduceOnly': True})

                    side_text = pos_side.upper()
                    if trail_step == "Bước 1":
                        step_desc = "Giá đạt RR1 → Dời SL về Entry (hòa vốn)"
                    else:
                        step_desc = "Giá đạt RR2 → Dời SL về RR1 (khóa lời)"

                    trail_msg = f"""🛡️ **TRAILING SL** ({trail_step})
📍 {sym} | {side_text}
📊 {step_desc}
💰 Entry: {entry_px:.6f}
🔄 SL cũ: {current_sl:.6f} → SL mới: **{new_sl:.6f}**
📈 Giá hiện tại: {last_close:.6f}"""
                    print(f"🛡️ Trail SL {sym} ({trail_step}) → {new_sl}")
                    bot.send_message(CHAT_ID, trail_msg, parse_mode='Markdown')
                    logging.info(f"TRAIL {trail_step} {sym} {side_text} | SL: {current_sl} → {new_sl}")
                except Exception as e:
                    print(f"⚠️ Trail SL Error {sym}: {e}")
    except Exception as e:
        print(f"⚠️ Trailing SL Error: {e}")
        logging.error(f"Trailing SL Error: {e}")

# ==============================================================================
# ========== DỌN DẸP LỆNH MỒ CÔI ==========
# ==============================================================================

def cleanup_orphan_orders():
    try:
        positions = exchange.fetch_positions()
        active_symbols = set()
        for p in positions:
            if float(p.get('contracts', 0) or 0) != 0:
                active_symbols.add(p.get('symbol', ''))

        for sym in PAIRS:
            if sym in active_symbols:
                continue

            try:
                open_orders = exchange.fetch_open_orders(sym)
                if not open_orders:
                    continue

                orphan_orders = [
                    o for o in open_orders
                    if o.get('reduceOnly', False) and
                    o.get('type') in ['stop_market', 'stop', 'take_profit_market', 'take_profit']
                ]

                if not orphan_orders:
                    continue

                cancelled_count = 0
                for o in orphan_orders:
                    try:
                        exchange.cancel_order(o['id'], sym)
                        cancelled_count += 1
                    except Exception as e:
                        print(f"⚠️ Không hủy được lệnh {o['id']} {sym}: {e}")

                if cancelled_count > 0:
                    msg = f"""🧹 **DỌN LỆNH MỒ CÔI**
📍 {sym}
❌ Đã hủy {cancelled_count} lệnh SL/TP còn sót
💡 Vị thế đã đóng (SL/TP đã trigger)"""
                    print(f"🧹 Cleanup {sym}: hủy {cancelled_count} lệnh mồ côi")
                    bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
                    logging.info(f"CLEANUP {sym}: cancelled {cancelled_count} orphan orders")

            except Exception as e:
                print(f"⚠️ Cleanup error {sym}: {e}")

    except Exception as e:
        print(f"⚠️ Cleanup orphan orders error: {e}")
        logging.error(f"Cleanup orphan orders error: {e}")

# ==============================================================================
# ========== QUÉT THỊ TRƯỜNG ==========
# ==============================================================================

def run_market_scan():
    for sym, cfg in SYMBOL_CONFIGS.items():
        if not cfg.get("Active"):
            continue
        try:
            ohlcv = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=50)
            if not ohlcv:
                continue

            df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
            df[['o', 'h', 'l', 'c']] = df[['o', 'h', 'l', 'c']].astype(float)
            df = df.sort_values('ts').reset_index(drop=True)
            df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()

            s = df.iloc[-2]
            ts = int(s['ts'])

            if ts <= last_candle_ts.get(sym, 0):
                continue
            last_candle_ts[sym] = ts

            max_oc = max(s['o'], s['c'])
            min_oc = min(s['o'], s['c'])
            up_wick = ((s['h'] - max_oc) / max_oc) * 100
            lo_wick = ((min_oc - s['l']) / min_oc) * 100

            side = None
            if (s['c'] > s['o']) and (s['c'] > s['ema20']) and (lo_wick >= cfg['X']) and (up_wick <= cfg['Y']):
                side = "buy"
            elif (s['c'] < s['o']) and (s['c'] < s['ema20']) and (up_wick >= cfg['X']) and (lo_wick <= cfg['Y']):
                side = "sell"

            if not side:
                continue

            positions = exchange.fetch_positions()
            open_positions = sum(1 for p in positions if float(p.get('contracts', 0) or 0) != 0)
            if open_positions >= MAX_POSITIONS:
                print(f"⚠️ Đạt giới hạn {MAX_POSITIONS} vị thế → Không vào {sym}")
                continue

            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get('USDT', 0))
            if usdt_free < TRADE_AMOUNT_USDT:
                print(f"⚠️ Số dư USDT không đủ ({usdt_free:.2f})")
                continue

            total_vol = TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE
            side_text = 'LONG' if side == 'buy' else 'SHORT'
            vn_time = (datetime.fromtimestamp(ts / 1000, tz=timezone.utc) + timedelta(hours=7)).strftime('%H:%M')

            msg_signal = f"""🚨 **EMA WICK SIGNAL** {sym} ({TIMEFRAME})
🕒 {vn_time} (VN Time)
📍 Side: {side_text}
📊 Up Wick: {up_wick:.3f}% | Lo Wick: {lo_wick:.3f}%
💰 Entry ≈ {s['c']:.6f}
Position Size: **{total_vol} USDT** (Leverage {GLOBAL_LEVERAGE}x)"""
            bot.send_message(CHAT_ID, msg_signal, parse_mode='Markdown')

            if TRADING_ENABLED:
                res, sz, sl, tp, err = execute_smart_trade(sym, side, s['c'], s['l'], s['h'])

                if res and not err:
                    actual_entry = res.get('average') or res.get('price') or s['c']
                    if side == 'buy':
                        sl_pct = (actual_entry - sl) / actual_entry * 100
                        tp_pct = (tp - actual_entry) / actual_entry * 100
                    else:
                        sl_pct = (sl - actual_entry) / actual_entry * 100
                        tp_pct = (actual_entry - tp) / actual_entry * 100
                    rr = round(tp_pct / sl_pct, 1) if sl_pct > 0 else 2.0

                    msg = f"""✅ **VÀO LỆNH THÀNH CÔNG** (SL ±0.2% Offset)
Pair: {sym}
Side: {side_text}
Position Size: **{total_vol} USDT** (Leverage {GLOBAL_LEVERAGE}x)
Margin: {TRADE_AMOUNT_USDT} USDT
Qty: {sz}
Entry: {actual_entry:.6f}
SL: {sl:.6f} **(-{sl_pct:.3f}%)**
TP: {tp:.6f} **(+{tp_pct:.3f}%)** → RR 1:{rr}"""
                    bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
                    logging.info(f"OPEN {side_text} {sym} | Entry: {actual_entry:.6f} | SL: -{sl_pct:.3f}% | TP: +{tp_pct:.3f}%")
                else:
                    msg = f"❌ LỖI: {err if err else 'Fail'} | {side_text} {sym}"
                    bot.send_message(CHAT_ID, msg)
                    logging.error(msg)

        except Exception as e:
            print(f"Lỗi quét {sym}: {e}")
            logging.error(f"Scan error {sym}: {e}")

# ==============================================================================
# ========== VÒNG LẶP CHÍNH ==========
# ==============================================================================

def main_loop():
    last_processed_minute = -1
    logging.info("Bot trading loop started")
    bot.send_message(CHAT_ID, f"""🤖 **EMA Wick Bot (Binance)**
Max Positions: {MAX_POSITIONS} | Leverage: {GLOBAL_LEVERAGE}x | Position Size: {TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE} USDT
SL Mode: ±0.2% Offset + Trailing SL
Auto Trade: {'🟢 ON' if TRADING_ENABLED else '🔴 OFF'}
Pairs: {len(PAIRS)} cặp""", parse_mode='Markdown')

    while True:
        try:
            if TRADING_ENABLED:
                now = datetime.now(timezone.utc) + timedelta(hours=7)
                if now.minute % 5 == 0 and now.minute != last_processed_minute:
                    time.sleep(5)
                    run_market_scan()
                    if TRAILING_ENABLED:
                        manage_trailing_sl()
                    cleanup_orphan_orders()
                    last_processed_minute = now.minute
        except Exception as e:
            print(f"Lỗi vòng lặp: {e}")
            logging.error(f"Loop error: {e}")
        time.sleep(1)

# ==============================================================================
# ========== TELEGRAM COMMANDS ==========
# ==============================================================================

@bot.message_handler(commands=['status'])
def status(message):
    if message.chat.id == CHAT_ID:
        try:
            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get('USDT', 0))
            usdt_total = float(balance['total'].get('USDT', 0))
        except:
            usdt_free = 0
            usdt_total = 0
        mode = "🧪 TESTNET" if USE_TESTNET else "🔴 LIVE"
        msg = f"""✅ **Bot đang chạy** ({mode})
📊 Pairs: {len(PAIRS)} | TF: {TIMEFRAME}
⚡ Leverage: {GLOBAL_LEVERAGE}x
💰 Margin/lệnh: {TRADE_AMOUNT_USDT} USDT → Position: {TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE} USDT
💵 Số dư: {usdt_free:.2f} USDT (khả dụng) / {usdt_total:.2f} USDT (tổng)
🤖 Auto Trade: {'🟢 ON' if TRADING_ENABLED else '🔴 OFF'}
🛡️ Trailing SL: {'🟢 ON' if TRAILING_ENABLED else '🔴 OFF'}"""
        bot.reply_to(message,
