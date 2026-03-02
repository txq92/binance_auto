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
GLOBAL_LEVERAGE = 25
TIMEFRAME = "5m"
MAX_POSITIONS = 20
TRADING_ENABLED = True
TRAILING_ENABLED = True
USE_TESTNET = True

SYMBOL_CONFIGS = {
    "BTC/USDT":   {"X": 0.15, "Y": 0.05, "Active": True},
    "ETH/USDT":   {"X": 0.3,  "Y": 0.05, "Active": True},
    "SOL/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "BNB/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "XRP/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "DOGE/USDT":  {"X": 0.35, "Y": 0.05, "Active": True},
    "ADA/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "AVAX/USDT":  {"X": 0.35, "Y": 0.05, "Active": True},
    "DOT/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
    "LINK/USDT":  {"X": 0.35, "Y": 0.05, "Active": True},
    "NEAR/USDT":  {"X": 0.35, "Y": 0.05, "Active": True},
    "FIL/USDT":   {"X": 0.35, "Y": 0.05, "Active": True},
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
# ========== LOGIC VÀO LỆNH & STOP LOSS (GIỮ NGUYÊN NGHIỆP VỤ) ==========
# ==============================================================================

def execute_smart_trade(symbol, side, entry_price, low, high):
    """
    Giữ nguyên logic tính toán SL/TP từ binace_co.py gốc:
    - SL = Low ± 0.2% (offset)
    - TP = R:R 1:2 từ SL
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

        # LOGIC STOP LOSS (±0.2%) - GIỮ NGUYÊN
        if side == "buy":
            sl_raw = low * (1 - 0.002)
        else:
            sl_raw = high * (1 + 0.002)
        sl = float(exchange.price_to_precision(symbol, sl_raw))

        # Tính TP (R:R 1:2) - GIỮ NGUYÊN
        risk = abs(entry_price - sl)
        if side == "buy":
            tp = float(exchange.price_to_precision(symbol, entry_price + (risk * 2)))
        else:
            tp = float(exchange.price_to_precision(symbol, entry_price - (risk * 2)))

        # Đặt lệnh Market
        order = exchange.create_market_order(symbol, side, quantity)
        actual_entry = order.get('price') or exchange.fetch_ticker(symbol)['last']

        # Đặt SL & TP
        sl_side = 'sell' if side == 'buy' else 'buy'
        tp_side = 'sell' if side == 'buy' else 'buy'

        exchange.create_order(symbol, 'stop_market', sl_side, quantity,
                              params={'stopPrice': sl, 'reduceOnly': True})
        exchange.create_order(symbol, 'take_profit_market', tp_side, quantity,
                              params={'stopPrice': tp, 'reduceOnly': True})

        # Tính % SL & % TP
        if side == 'buy':
            sl_percent = (actual_entry - sl) / actual_entry * 100
            tp_percent = (tp - actual_entry) / actual_entry * 100
        else:
            sl_percent = (sl - actual_entry) / actual_entry * 100
            tp_percent = (actual_entry - tp) / actual_entry * 100

        rr = round(tp_percent / sl_percent, 1) if sl_percent > 0 else 2.0
        position_value = TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE

        return order, str(quantity), sl, tp, ""

    except Exception as e:
        return None, "0", 0, 0, str(e)

# ==============================================================================
# ========== TRAILING SL (ĐÃ FIX BUG + THÔNG BÁO CHI TIẾT) ==========
# ==============================================================================

def manage_trailing_sl():
    """
    Trailing SL logic (đã fix bug tính risk):
    - Dùng TP order để tính ngược original risk → trailing đúng cả 2 bước
    - Khi giá đạt RR1 → dời SL về entry (Bước 1)
    - Khi giá đạt RR2 → dời SL về RR1 (Bước 2)
    """
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
            pos_side = pos.get('side', '').lower()  # 'long' or 'short'
            if entry_px == 0 or not pos_side:
                continue

            # Lấy nến gần nhất
            ohlcv = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=5)
            if len(ohlcv) < 2:
                continue
            last_close = ohlcv[-2][4]  # close nến trước

            # Tìm SL & TP order hiện tại
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

            # === FIX: Tính original risk từ TP (R:R 1:2 → risk = |TP - entry| / 2) ===
            if current_tp > 0:
                original_risk = abs(current_tp - entry_px) / 2.0
            else:
                # Fallback: dùng current_sl nếu không tìm thấy TP
                original_risk = abs(entry_px - current_sl)

            if original_risk == 0:
                continue

            if pos_side == 'long':
                rr1 = entry_px + original_risk
                rr2 = entry_px + original_risk * 2
            else:
                rr1 = entry_px - original_risk
                rr2 = entry_px - original_risk * 2

            # Logic trailing
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
                    # Cancel SL cũ, đặt SL mới
                    exchange.cancel_order(sl_order_id, sym)
                    sl_side = 'sell' if pos_side == 'long' else 'buy'
                    exchange.create_order(sym, 'stop_market', sl_side, contracts,
                                          params={'stopPrice': new_sl, 'reduceOnly': True})

                    # Thông báo chi tiết
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
# ========== DỌN DẸP LỆNH MỒ CÔI (SL/TP CÒN SÓT) ==========
# ==============================================================================

def cleanup_orphan_orders():
    """
    Khi SL trigger → TP vẫn còn mở (và ngược lại).
    Hàm này tìm các symbol không còn vị thế nhưng vẫn có lệnh SL/TP mở,
    rồi hủy chúng để tránh gây rối cho lệnh mới.
    """
    try:
        # Lấy tất cả positions
        positions = exchange.fetch_positions()
        # Tìm các symbol ĐANG có vị thế
        active_symbols = set()
        for p in positions:
            if float(p.get('contracts', 0) or 0) != 0:
                active_symbols.add(p.get('symbol', ''))

        # Kiểm tra từng pair, nếu không có vị thế mà vẫn có lệnh → hủy
        for sym in PAIRS:
            if sym in active_symbols:
                continue  # Có vị thế → không động vào

            try:
                open_orders = exchange.fetch_open_orders(sym)
                if not open_orders:
                    continue

                # Lọc chỉ các lệnh SL/TP (reduceOnly)
                orphan_orders = [
                    o for o in open_orders
                    if o.get('reduceOnly', False) and
                    o.get('type') in ['stop_market', 'stop', 'take_profit_market', 'take_profit']
                ]

                if not orphan_orders:
                    continue

                # Hủy tất cả lệnh mồ côi
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
# ========== QUÉT THỊ TRƯỜNG (GIỮ NGUYÊN LOGIC EMA20 + WICK) ==========
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

            s = df.iloc[-2]  # Nến đã đóng gần nhất
            ts = int(s['ts'])

            # Bỏ qua nến đã xử lý
            if ts <= last_candle_ts.get(sym, 0):
                continue
            last_candle_ts[sym] = ts

            # === LOGIC TÍN HIỆU - GIỮ NGUYÊN ===
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

            # Kiểm tra số lượng vị thế
            positions = exchange.fetch_positions()
            open_positions = sum(1 for p in positions if float(p.get('contracts', 0) or 0) != 0)
            if open_positions >= MAX_POSITIONS:
                print(f"⚠️ Đạt giới hạn {MAX_POSITIONS} vị thế → Không vào {sym}")
                continue

            # Kiểm tra số dư
            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get('USDT', 0))
            if usdt_free < TRADE_AMOUNT_USDT:
                print(f"⚠️ Số dư USDT không đủ ({usdt_free:.2f})")
                continue

            total_vol = TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE
            side_text = 'LONG' if side == 'buy' else 'SHORT'
            vn_time = (datetime.fromtimestamp(ts / 1000, tz=timezone.utc) + timedelta(hours=7)).strftime('%H:%M')

            # Gửi tín hiệu qua Telegram
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
                    actual_entry = res.get('price') or s['c']
                    # Tính % SL & TP
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
                    time.sleep(5)  # Chờ nến đóng xong
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
        bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['trade'])
def trade_control(message):
    if message.chat.id != CHAT_ID:
        return
    global TRADING_ENABLED
    text = message.text.lower()
    if 'on' in text:
        TRADING_ENABLED = True
        bot.reply_to(message, "✅ AUTO TRADE BẬT")
    elif 'off' in text:
        TRADING_ENABLED = False
        bot.reply_to(message, "⛔ AUTO TRADE TẮT")
    else:
        bot.reply_to(message, f"Trạng thái: {'🟢 ON' if TRADING_ENABLED else '🔴 OFF'}")

@bot.message_handler(commands=['amo'])
def set_amount(message):
    if message.chat.id != CHAT_ID:
        return
    global TRADE_AMOUNT_USDT
    parts = message.text.strip().split()
    if len(parts) >= 2:
        try:
            new_val = float(parts[1])
            if new_val <= 0:
                bot.reply_to(message, "❌ Giá trị phải > 0")
                return
            TRADE_AMOUNT_USDT = new_val
            bot.reply_to(message, f"✅ Đã set vốn = **{TRADE_AMOUNT_USDT} USDT**\nPosition Size = **{TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE} USDT** (Leverage {GLOBAL_LEVERAGE}x)", parse_mode='Markdown')
        except ValueError:
            bot.reply_to(message, "❌ Sai định dạng. VD: /amo 20")
    else:
        bot.reply_to(message, f"💰 Vốn hiện tại: **{TRADE_AMOUNT_USDT} USDT**\nPosition Size: **{TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE} USDT**\n\nĐể thay đổi: `/amo 20`", parse_mode='Markdown')

@bot.message_handler(commands=['leve'])
def set_leverage(message):
    if message.chat.id != CHAT_ID:
        return
    global GLOBAL_LEVERAGE
    parts = message.text.strip().split()
    if len(parts) >= 2:
        try:
            new_val = int(parts[1])
            if new_val < 1 or new_val > 125:
                bot.reply_to(message, "❌ Leverage phải từ 1 đến 125")
                return
            GLOBAL_LEVERAGE = new_val
            # Cập nhật leverage trên sàn cho tất cả pairs
            for sym in PAIRS:
                try:
                    exchange.set_leverage(GLOBAL_LEVERAGE, sym)
                except:
                    pass
            bot.reply_to(message, f"✅ Đã set leverage = **{GLOBAL_LEVERAGE}x**\nPosition Size = **{TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE} USDT** (Vốn {TRADE_AMOUNT_USDT} USDT)", parse_mode='Markdown')
        except ValueError:
            bot.reply_to(message, "❌ Sai định dạng. VD: /leve 10")
    else:
        bot.reply_to(message, f"⚡ Leverage hiện tại: **{GLOBAL_LEVERAGE}x**\nPosition Size: **{TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE} USDT**\n\nĐể thay đổi: `/leve 10`", parse_mode='Markdown')

@bot.message_handler(commands=['pos'])
def show_positions(message):
    if message.chat.id != CHAT_ID:
        return
    try:
        positions = exchange.fetch_positions()
        active = [p for p in positions if float(p.get('contracts', 0) or 0) != 0]

        if not active:
            bot.reply_to(message, "📭 Hiện không có vị thế nào đang mở.")
            return

        total_pnl = sum(float(p.get('unrealizedPnl', 0) or 0) for p in active)
        total_emoji = "🟢" if total_pnl >= 0 else "🔴"

        active.sort(key=lambda p: float(p.get('unrealizedPnl', 0) or 0), reverse=True)

        msg = f"📊 **Có {len(active)} VỊ THẾ ĐANG MỞ** (Tổng PNL: {total_emoji} {total_pnl:+.4f} USDT)\n\n"

        for p in active:
            symbol = p.get('symbol', 'Unknown').replace(':USDT', '').replace('USDT', '')
            side = p.get('side', 'UNKNOWN').upper()
            qty = float(p.get('contracts', 0) or 0)
            entry = float(p.get('entryPrice', 0) or 0)
            pnl = float(p.get('unrealizedPnl', 0) or 0)

            notional = qty * entry
            lev = p.get('leverage')
            leverage = int(lev) if lev is not None else GLOBAL_LEVERAGE
            margin = notional / leverage if leverage > 0 else 1
            pnl_percent = (pnl / margin * 100) if margin > 0 else 0

            ts = p.get('timestamp') or p.get('updateTime')
            time_str = datetime.fromtimestamp(ts / 1000).strftime('%H:%M') if ts else "N/A"

            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_str = f"{pnl_emoji} **{pnl:+.4f} USDT** ({pnl_percent:+.2f}%)"

            msg += f"`{symbol}` | **{side}** | USDT: {notional:.2f} | Entry: {entry:.6f} | {time_str} | PNL: {pnl_str}\n"

        bot.reply_to(message, msg, parse_mode='Markdown')

    except Exception as e:
        bot.reply_to(message, f"❌ Lỗi lấy positions: {e}")
        print(f"DEBUG Positions Error: {e}")

@bot.message_handler(commands=['closed'])
def show_closed_trades(message):
    if message.chat.id != CHAT_ID:
        return
    try:
        since = int((time.time() - 86400) * 1000)
        all_trades = []

        for symbol in PAIRS:
            trades = exchange.fetch_my_trades(symbol, since=since, limit=100)
            for t in trades:
                rpnl = float(t['info'].get('realizedPnl', 0) or 0)
                if rpnl != 0:
                    all_trades.append(t)

        if not all_trades:
            bot.reply_to(message, "📭 Không có lệnh nào đã đóng trong 24 giờ qua.")
            return

        all_trades.sort(key=lambda x: x['timestamp'], reverse=True)

        msg = f"📜 **LỆNH ĐÃ ĐÓNG (24h qua)** - {len(all_trades)} lệnh\n\n"
        for t in all_trades[:20]:
            ts = datetime.fromtimestamp(t['timestamp'] / 1000).strftime('%H:%M')
            symbol = t['symbol']
            side = t['side'].upper()
            qty = float(t['amount'])
            price = float(t['price'])
            pnl = float(t['info'].get('realizedPnl', 0) or 0)
            fee = float(t.get('fee', {}).get('cost', 0) or 0)
            msg += f"`{ts}` | {symbol} | **{side}** | {qty:.6f} @ {price:.6f} | PNL: **{pnl:+.4f}** USDT (phí {fee:.4f})\n"

        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Lỗi lấy lịch sử lệnh: {e}")

@bot.message_handler(commands=['stats', 'thongke', 'daily'])
def stats_command(message):
    if message.chat.id != CHAT_ID:
        return
    try:
        since = int((time.time() - 86400) * 1000)
        total_pnl = 0.0
        num_closed = 0
        wins = 0
        total_volume = 0.0

        for symbol in PAIRS:
            my_trades = exchange.fetch_my_trades(symbol, since=since, limit=500)
            for t in my_trades:
                rpnl = float(t['info'].get('realizedPnl', 0) or 0)
                total_pnl += rpnl
                qty = float(t.get('amount', 0))
                total_volume += qty * float(t.get('price', 0))
                if rpnl != 0:
                    num_closed += 1
                    if rpnl > 0:
                        wins += 1

        winrate = (wins / num_closed * 100) if num_closed > 0 else 0

        msg = f"""📊 **THỐNG KÊ 24 GIỜ**
Lệnh đã đóng: {num_closed}
Winrate: {winrate:.1f}%
PNL: {total_pnl:+.4f} USDT
Volume: {total_volume:.2f} USDT"""
        bot.reply_to(message, msg, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Lỗi lấy thống kê: {e}")

@bot.message_handler(commands=['config'])
def show_config(message):
    if message.chat.id != CHAT_ID:
        return
    msg = "⚙️ **CẤU HÌNH SYMBOL**\n\n"
    for sym, cfg in SYMBOL_CONFIGS.items():
        status = "🟢" if cfg.get("Active") else "🔴"
        msg += f"{status} `{sym}` | X: {cfg['X']}% | Y: {cfg['Y']}%\n"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['ip'])
def show_ip(message):
    if message.chat.id != CHAT_ID:
        return
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except:
        hostname = "N/A"
        local_ip = "N/A"
    try:
        ext_ip = req_lib.get('https://api.ipify.org', timeout=5).text
    except:
        ext_ip = "N/A"
    msg = f"""🌐 **Thông tin mạng**
🖥️ Hostname: `{hostname}`
🏠 Local IP: `{local_ip}`
🌍 External IP: `{ext_ip}`"""
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['slmove'])
def slmove_control(message):
    if message.chat.id != CHAT_ID:
        return
    global TRAILING_ENABLED
    text = message.text.lower()
    if 'on' in text:
        TRAILING_ENABLED = True
        bot.reply_to(message, "✅ TRAILING SL đã BẬT\n🛡️ Bot sẽ tự động dời SL khi giá đạt RR1/RR2")
    elif 'off' in text:
        TRAILING_ENABLED = False
        bot.reply_to(message, "⛔ TRAILING SL đã TẮT\n⚠️ SL sẽ giữ nguyên vị trí ban đầu")
    else:
        status = '🟢 ON' if TRAILING_ENABLED else '🔴 OFF'
        bot.reply_to(message, f"""🛡️ **Trailing SL: {status}**

Bước 1: Giá đạt RR1 → Dời SL về Entry (hòa vốn)
Bước 2: Giá đạt RR2 → Dời SL về RR1 (khóa lời)

Dùng: `/slmove on` hoặc `/slmove off`""", parse_mode='Markdown')

@bot.message_handler(commands=['help'])
def help_command(message):
    if message.chat.id == CHAT_ID:
        bot.reply_to(message, """**EMA Wick Bot (Binance) - Danh sách lệnh**

/status     - Trạng thái bot
/trade on   - Bật tự động trade
/trade off  - Tắt tự động trade
/slmove on  - Bật trailing SL
/slmove off - Tắt trailing SL
/amo 20     - Set vốn (USDT)
/leve 10    - Set leverage
/pos        - Xem vị thế đang mở
/closed     - Xem lệnh đã đóng + PNL 24h
/stats      - Thống kê 24 giờ
/config     - Xem cấu hình symbol (X, Y)
/ip         - Xem IP máy chủ bot
/help       - Hiển thị hướng dẫn""", parse_mode='Markdown')

# ==============================================================================
# ========== KHỞI ĐỘNG ==========
# ==============================================================================
threading.Thread(target=main_loop, daemon=True).start()
print("🚀 EMA Wick Bot đang chạy... (Log: bot_wick_log.txt)")
bot.polling(none_stop=True)