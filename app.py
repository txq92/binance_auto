import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import traceback
import os
import threading
import gradio as gr
from dotenv import load_dotenv
import hmac
import hashlib
import json
import base64
import math

# ==============================================================================
# ========== CẤU HÌNH & BIẾN TOÀN CỤC ==========
# ==============================================================================
if os.path.exists(".env"):
    load_dotenv(".env")

OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
OKX_BASE_URL = "https://www.okx.com"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

GLOBAL_RUNNING = False
TRADE_AMOUNT_USDT = 10.0  
GLOBAL_LEVERAGE = 25       
TIMEFRAME = "5m"
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
LAST_PROCESSED_MINUTE = -1 

# Cache thông số sàn
MARKET_DATA_CACHE = {}

SYMBOL_CONFIGS = {
    "XAG-USDT-SWAP": {"X": 0.5, "Y": 0.05, "Active": False},
    "BTC-USDT-SWAP": {"X": 0.15, "Y": 0.05, "Active": True},
    "ETH-USDT-SWAP": {"X": 0.3, "Y": 0.05, "Active": True},
    "SOL-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "BNB-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "XRP-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "DOGE-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "ADA-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "AVAX-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "SHIB-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "DOT-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "LINK-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "TRX-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "UNI-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "ATOM-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "ICP-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "ETC-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "FIL-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "NEAR-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "APT-USDT-SWAP": {"X": 0.35, "Y": 0.05, "Active": True},
    "XAU-USDT-SWAP": {"X": 0.1, "Y": 0.05, "Active": False},
}

# ==============================================================================
# ========== HÀM API CORE ==========
# ==============================================================================

def okx_request(method, endpoint, body=None):
    try:
        ts = datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'
        body_str = json.dumps(body) if body else ""
        message = ts + method + endpoint + body_str
        mac = hmac.new(bytes(OKX_SECRET_KEY, 'utf-8'), bytes(message, 'utf-8'), hashlib.sha256)
        sign = base64.b64encode(mac.digest()).decode()
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY, 'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json'
        }
        res = requests.request(method, OKX_BASE_URL + endpoint, headers=headers, data=body_str, timeout=10)
        return res.json()
    except Exception as e:
        print(f"❌ API Error: {e}")
        return None

def get_market_rules(symbol):
    if symbol in MARKET_DATA_CACHE: return MARKET_DATA_CACHE[symbol]
    try:
        url = f"{OKX_BASE_URL}/api/v5/public/instruments?instType=SWAP&instId={symbol}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') == '0' and res.get('data'):
            inst = res['data'][0]
            data = {
                "lotSz": float(inst['lotSz']),
                "tickSz": float(inst['tickSz']),
                "prec": len(inst['tickSz'].split('.')[-1]) if '.' in inst['tickSz'] else 0,
                "minSz": float(inst['minSz']),
                "ctVal": float(inst['ctVal'])
            }
            MARKET_DATA_CACHE[symbol] = data
            return data
    except Exception as e:
        print(f"⚠️ Rules Error {symbol}: {e}")
    return None

def check_existing_position(symbol):
    res = okx_request("GET", f"/api/v5/account/positions?instId={symbol}")
    if res and res.get('code') == '0' and res.get('data'):
        for pos in res['data']:
            if pos['pos'] != '0': return pos['posSide']
    return None

# ==============================================================================
# ========== LOGIC VÀO LỆNH & STOP LOSS MỚI ==========
# ==============================================================================

def execute_smart_trade(symbol, side, entry_price, low, high):
    try:
        existing_pos = check_existing_position(symbol)
        if existing_pos:
            return None, "0", 0, 0, f"Đã có vị thế {existing_pos}"

        rules = get_market_rules(symbol)
        if not rules: return None, "0", 0, 0, "Không lấy được rules sàn"

        ct_val = rules['ctVal']
        lot_sz = rules['lotSz']
        prec = rules['prec']
        min_sz = rules['minSz']

        # 1. Tính Volume (Sửa lỗi chính xác Volume USDT)
        total_notional_usdt = TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE
        raw_sz = total_notional_usdt / (entry_price * ct_val)
        size = math.floor(raw_sz / lot_sz) * lot_sz
        if size < min_sz: size = min_sz
        sz_str = format(size, 'f').rstrip('0').rstrip('.')

        # 2. LOGIC STOP LOSS MỚI (±0.2%)
        pos_side = "long" if side == "buy" else "short"
        
        if side == "buy":
            # SL = Low - (Low * 0.2%)
            sl_raw = low * (1 - 0.002)
            sl = round(sl_raw, prec)
        else:
            # SL = High + (High * 0.2%)
            sl_raw = high * (1 + 0.002)
            sl = round(sl_raw, prec)

        # 3. Tính TP (Dựa trên R:R 1:2 từ điểm SL mới)
        risk = abs(entry_price - sl)
        if side == "buy":
            tp = round(entry_price + (risk * 2), prec)
        else:
            tp = round(entry_price - (risk * 2), prec)

        # 4. Thực thi lệnh
        okx_request("POST", "/api/v5/account/set-leverage", {
            "instId": symbol, "lever": str(GLOBAL_LEVERAGE), "mgnMode": "isolated", "posSide": pos_side
        })

        body = {
            "instId": symbol, "tdMode": "isolated", "side": side, "posSide": pos_side,
            "ordType": "market", "sz": sz_str,
            "attachAlgoOrds": [
                {"attachAlgoOrdType": "sl", "slTriggerPx": str(sl), "slOrdPx": "-1"},
                {"attachAlgoOrdType": "tp", "tpTriggerPx": str(tp), "tpOrdPx": "-1"}
            ]
        }
        res = okx_request("POST", "/api/v5/trade/order", body)
        return res, sz_str, sl, tp, res.get('msg') if res and res.get('code') != '0' else ""
    except Exception as e:
        return None, "0", 0, 0, str(e)

# ==============================================================================
# ========== HÀM QUÉT & SLACK ==========
# ==============================================================================

def manage_trailing_sl():
    try:
        pos_res = okx_request("GET", "/api/v5/account/positions")
        if not pos_res or pos_res.get('code') != '0': return
        for pos in pos_res.get('data', []):
            if pos['pos'] == '0': continue
            sym, entry_px, pos_side = pos['instId'], float(pos['avgPx']), pos['posSide']
            if sym not in SYMBOL_CONFIGS: continue
            
            c_res = requests.get(f"{OKX_BASE_URL}/api/v5/market/history-candles?instId={sym}&bar={TIMEFRAME}&limit=5").json()
            if not c_res.get('data'): continue
            last_close = float(c_res['data'][1][4])

            algo_res = okx_request("GET", f"/api/v5/trade/orders-algo?instId={sym}&ordType=conditional")
            current_sl, algo_id = 0, ""
            for algo in algo_res.get('data', []):
                if algo.get('slTriggerPx'):
                    current_sl, algo_id = float(algo['slTriggerPx']), algo['algoId']
                    break
            
            if not algo_id: continue
            risk = abs(entry_px - current_sl)
            rr1 = entry_px + risk if pos_side == 'long' else entry_px - risk
            rr2 = entry_px + risk*2 if pos_side == 'long' else entry_px - risk*2
            prec = get_market_rules(sym)['prec']

            new_sl = None
            if pos_side == 'long':
                if last_close >= rr2 and current_sl < rr1: new_sl = round(rr1, prec)
                elif last_close >= rr1 and current_sl < entry_px: new_sl = round(entry_px, prec)
            else:
                if last_close <= rr2 and current_sl > rr1: new_sl = round(rr1, prec)
                elif last_close <= rr1 and current_sl > entry_px: new_sl = round(entry_px, prec)

            if new_sl:
                okx_request("POST", "/api/v5/trade/amend-algos", {"instId": sym, "algoId": algo_id, "newSlTriggerPx": str(new_sl)})
                print(f"🛡️ Trail SL {sym} -> {new_sl}")
    except: pass

def run_market_scan():
    for sym, cfg in SYMBOL_CONFIGS.items():
        if not cfg.get("Active"): continue
        try:
            url = f"{OKX_BASE_URL}/api/v5/market/history-candles?instId={sym}&bar={TIMEFRAME}&limit=50"
            resp = requests.get(url, timeout=10).json()
            data = resp.get('data', [])
            if not data: continue
            df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
            df[['o', 'h', 'l', 'c']] = df[['o', 'h', 'l', 'c']].astype(float)
            df = df.sort_values('ts').reset_index(drop=True)
            df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()
            
            s = df.iloc[-2]
            max_oc, min_oc = max(s['o'], s['c']), min(s['o'], s['c'])
            up_wick, lo_wick = ((s['h'] - max_oc) / max_oc) * 100, ((min_oc - s['l']) / min_oc) * 100
            
            side = None
            if (s['c'] > s['o']) and (s['c'] > s['ema20']) and (lo_wick >= cfg['X']) and (up_wick <= cfg['Y']): side = "buy"
            elif (s['c'] < s['o']) and (s['c'] < s['ema20']) and (up_wick >= cfg['X']) and (lo_wick <= cfg['Y']): side = "sell"

            if side:
                res, sz, sl, tp, err = execute_smart_trade(sym, side, s['c'], s['l'], s['h'])
                total_vol = TRADE_AMOUNT_USDT * GLOBAL_LEVERAGE
                
                if res and res.get('code') == '0':
                    msg = f"✅ OK | {side.upper()} {sym}\nVol: {total_vol} USDT | SL: {sl} | TP: {tp}"
                else:
                    msg = f"❌ LỖI: {err if err else 'Fail'} | {side.upper()} {sym}\nVolume: {total_vol} USDT (Size: {sz})\nSL: {sl} | TP: {tp}"
                
                if SLACK_WEBHOOK_URL: requests.post(SLACK_WEBHOOK_URL, json={"text": msg})
                print(msg)
        except: pass

def main_loop():
    global LAST_PROCESSED_MINUTE
    while True:
        if GLOBAL_RUNNING:
            now = datetime.now(VIETNAM_TZ)
            if now.minute % 5 == 0 and now.minute != LAST_PROCESSED_MINUTE:
                time.sleep(5)
                run_market_scan()
                manage_trailing_sl()
                LAST_PROCESSED_MINUTE = now.minute
        time.sleep(1)

threading.Thread(target=main_loop, daemon=True).start()

# ==============================================================================
# ========== UI GRADIO ==========
# ==============================================================================

def update_settings(amt, lev, run):
    global TRADE_AMOUNT_USDT, GLOBAL_LEVERAGE, GLOBAL_RUNNING
    TRADE_AMOUNT_USDT, GLOBAL_LEVERAGE, GLOBAL_RUNNING = float(amt), int(lev), run
    return f"{'🟢 CHẠY' if run else '🔴 DỪNG'} | Volume: {float(amt)*int(lev)}$ | SL: ±0.2% Offset"

with gr.Blocks(title="OKX Bot RR V5") as demo:
    gr.Markdown("# 🤖 OKX Bot (SL Offset 0.2% + Trailing SL)")
    with gr.Row():
        num_amt = gr.Number(label="Vốn (USDT)", value=10)
        num_lev = gr.Number(label="Đòn bẩy", value=25)
        chk_run = gr.Checkbox(label="Kích hoạt Bot")
    btn = gr.Button("LƯU & CHẠY", variant="primary")
    out = gr.Textbox(label="Trạng thái", interactive=False)
    btn.click(update_settings, [num_amt, num_lev, chk_run], out)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)