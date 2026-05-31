"""
Bybit Autonomous Trading Bot with Remote Control
- Runs on Render cloud 24/7
- Control via Telegram: /start_trading, /stop_trading, /status
- Auto keep-alive to prevent sleeping
"""

import os
import time
import logging
import requests
import hmac
import hashlib
import json
import threading
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TRADE_BUDGET_USDT = 5.0
MAX_PER_TRADE     = 2.0
STOP_LOSS_PCT     = 0.02
TAKE_PROFIT_PCT   = 0.04
SCAN_INTERVAL     = 60
MAX_OPEN_TRADES   = 3
MIN_BALANCE       = 0.5

BYBIT_BASE = "https://api.bybit.com"

# ── Global state ──────────────────────────────────────────────────────────────
TRADING_ENABLED = True  # Can be toggled by Telegram commands
open_trades = {}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")

def listen_telegram_commands():
    """Listen for Telegram commands in a separate thread."""
    global TRADING_ENABLED
    last_update_id = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": last_update_id + 1, "timeout": 30}, timeout=35)
            updates = r.json().get("result", [])
            
            for update in updates:
                last_update_id = update.get("update_id", last_update_id)
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                
                if text == "/start_trading":
                    TRADING_ENABLED = True
                    send_telegram("✅ Trading ENABLED — bot is now buying/selling")
                    log.info("Trading enabled by user command")
                
                elif text == "/stop_trading":
                    TRADING_ENABLED = False
                    send_telegram("⛔ Trading DISABLED — bot will only monitor, not trade")
                    log.info("Trading disabled by user command")
                
                elif text == "/status":
                    status = "🟢 TRADING ON" if TRADING_ENABLED else "🔴 TRADING OFF"
                    balance = get_balance("USDT")
                    msg_text = (
                        f"{status}\n"
                        f"Balance: ${balance:.4f} USDT\n"
                        f"Open trades: {len(open_trades)}\n"
                        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    send_telegram(msg_text)
                    log.info(f"Status checked: {status}")
        
        except Exception as e:
            log.warning(f"Telegram listen error: {e}")
        
        time.sleep(1)

# ── Bybit auth headers ────────────────────────────────────────────────────────
def _get_headers(payload_str: str = "") -> dict:
    ts          = str(int(time.time() * 1000))
    recv_window = "20000"
    sign_str    = ts + BYBIT_API_KEY + recv_window + payload_str
    signature   = hmac.new(
        BYBIT_API_SECRET.encode(),
        sign_str.encode(),
        hashlib.sha256
    ).hexdigest()
    return {
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type":       "application/json",
    }

# ── Bybit market helpers ──────────────────────────────────────────────────────
def get_balance(coin: str = "USDT") -> float:
    try:
        params    = {"accountType": "UNIFIED", "coin": coin}
        query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        headers   = _get_headers(query_str)
        r = requests.get(
            f"{BYBIT_BASE}/v5/account/wallet-balance",
            params=params, headers=headers, timeout=10
        )
        data = r.json()
        if data.get("retCode") != 0:
            return 0.0
        coin_list = data["result"]["list"][0]["coin"]
        for c in coin_list:
            if c["coin"] == coin:
                val = c.get("availableToWithdraw") or c.get("walletBalance") or "0"
                return float(val)
        return 0.0
    except Exception as e:
        log.error(f"get_balance error: {e}")
        return 0.0

STABLECOINS = {
    "USDS", "USDC", "BUSD", "DAI", "TUSD", "USDP", "GUSD",
    "FRAX", "LUSD", "SUSD", "EURC", "FDUSD", "PYUSD", "AEUR",
    "EURT", "EURS", "USDD", "CUSD", "CEUR", "USDJ", "USDX",
}

def get_usdt_pairs() -> list:
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/instruments-info",
            params={"category": "spot"}, timeout=10
        )
        data = r.json()
        pairs = []
        for s in data["result"]["list"]:
            symbol = s["symbol"]
            if not symbol.endswith("USDT"):
                continue
            if s["status"] != "Trading":
                continue
            base = symbol.replace("USDT", "")
            if base in STABLECOINS:
                continue
            pairs.append(symbol)
        return pairs
    except Exception as e:
        log.error(f"get_usdt_pairs error: {e}")
        return []

def get_klines(symbol: str, interval: str = "15", limit: int = 60) -> list:
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "spot", "symbol": symbol,
                    "interval": interval, "limit": limit}, timeout=10
        )
        rows   = r.json()["result"]["list"]
        closes = [float(row[4]) for row in rows]
        closes.reverse()
        return closes
    except Exception as e:
        log.error(f"get_klines {symbol} error: {e}")
        return []

def get_price(symbol: str) -> float:
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "spot", "symbol": symbol}, timeout=10
        )
        return float(r.json()["result"]["list"][0]["lastPrice"])
    except Exception as e:
        log.error(f"get_price {symbol} error: {e}")
        return 0.0

def get_instrument_info(symbol: str) -> dict:
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/instruments-info",
            params={"category": "spot", "symbol": symbol}, timeout=10
        )
        data = r.json()
        lot  = data["result"]["list"][0]["lotSizeFilter"]
        return {
            "min_qty":  float(lot.get("minOrderQty", 0)),
            "qty_step": float(lot.get("basePrecision", 0.0001)),
            "min_amt":  float(lot.get("minOrderAmt", 1)),
        }
    except Exception as e:
        log.error(f"get_instrument_info {symbol} error: {e}")
        return {"min_qty": 0, "qty_step": 0.0001, "min_amt": 1}

def place_order(symbol: str, side: str, qty: str) -> dict:
    try:
        body     = {"category": "spot", "symbol": symbol,
                    "side": side, "orderType": "Market", "qty": qty}
        body_str = json.dumps(body)
        headers  = _get_headers(body_str)
        r = requests.post(
            f"{BYBIT_BASE}/v5/order/create",
            data=body_str, headers=headers, timeout=10
        )
        result = r.json()
        if result.get("retCode") == 0:
            return result["result"]
        log.error(f"Order failed {symbol} {side}: {result.get('retMsg')}")
        return None
    except Exception as e:
        log.error(f"place_order error: {e}")
        return None

# ── Indicators ────────────────────────────────────────────────────────────────
def rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-period + i] - closes[-period + i - 1]
        (gains if d > 0 else losses).append(abs(d))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def sma(closes: list, period: int) -> float:
    if len(closes) < period:
        return 0.0
    return sum(closes[-period:]) / period

def signal(closes: list) -> str:
    if len(closes) < 21:
        return "HOLD"
    r     = rsi(closes)
    ma    = sma(closes, 20)
    price = closes[-1]
    prev  = closes[-2]
    if r < 35 and prev < ma and price >= ma:
        return "BUY"
    if r > 65 and prev > ma and price <= ma:
        return "SELL"
    return "HOLD"

def calc_qty(symbol: str, price: float, budget: float) -> str:
    try:
        info     = get_instrument_info(symbol)
        min_qty  = info["min_qty"]
        qty_step = info["qty_step"] if info["qty_step"] > 0 else 0.0001
        min_amt  = info["min_amt"]
        raw_qty  = budget / price
        if raw_qty < min_qty:
            raw_qty = min_qty
        steps   = int(raw_qty / qty_step)
        qty     = steps * qty_step
        order_value = qty * price
        if order_value < min_amt:
            qty = (min_amt / price) * 1.05
        qty = round(qty, 6)
        if qty <= 0:
            return None
        return str(qty)
    except Exception as e:
        log.error(f"calc_qty error for {symbol}: {e}")
        return None

# ── Bot trading logic ─────────────────────────────────────────────────────────
def check_open_trades():
    for symbol in list(open_trades.keys()):
        trade = open_trades[symbol]
        price = get_price(symbol)
        if price <= 0:
            continue
        hit_stop   = price <= trade["stop"]
        hit_target = price >= trade["target"]
        if hit_stop or hit_target:
            reason = "STOP-LOSS" if hit_stop else "TAKE-PROFIT"
            result = place_order(symbol, "Sell", trade["qty"])
            if result:
                pnl = (price - trade["buy_price"]) * float(trade["qty"])
                msg = (
                    f"{reason} triggered\n"
                    f"Coin: {symbol}\n"
                    f"Sell price: ${price:.6f}\n"
                    f"P&L: ${pnl:+.4f} USDT"
                )
                log.info(msg)
                send_telegram(msg)
                del open_trades[symbol]

def scan_and_trade():
    global TRADING_ENABLED
    
    if not TRADING_ENABLED:
        log.info("Trading disabled — skipping scan")
        return

    if len(open_trades) >= MAX_OPEN_TRADES:
        log.info("Max open trades reached")
        return

    usdt_balance = get_balance("USDT")
    log.info(f"Balance: ${usdt_balance:.4f} | Trading: {'ON' if TRADING_ENABLED else 'OFF'}")

    if usdt_balance < MIN_BALANCE:
        log.info(f"Low balance: ${usdt_balance:.4f}")
        return

    trade_size = min(MAX_PER_TRADE, usdt_balance / 2)
    pairs      = get_usdt_pairs()
    log.info(f"Scanning {len(pairs)} pairs...")

    bought = 0
    for symbol in pairs:
        if symbol in open_trades or len(open_trades) + bought >= MAX_OPEN_TRADES:
            continue
        closes = get_klines(symbol)
        if not closes or signal(closes) != "BUY":
            continue
        price = closes[-1]
        if price <= 0:
            continue
        qty = calc_qty(symbol, price, trade_size)
        if not qty:
            continue
        result = place_order(symbol, "Buy", qty)
        if not result:
            continue
        stop   = round(price * (1 - STOP_LOSS_PCT), 6)
        target = round(price * (1 + TAKE_PROFIT_PCT), 6)
        open_trades[symbol] = {
            "qty": qty, "buy_price": price,
            "stop": stop, "target": target,
        }
        bought += 1
        rsi_val = rsi(closes)
        msg = (
            f"BUY SIGNAL\n"
            f"Coin: {symbol}\n"
            f"Price: ${price:.6f}\n"
            f"Qty: {qty}\n"
            f"RSI: {rsi_val:.1f}\n"
            f"Stop-loss: ${stop:.6f}\n"
            f"Take-profit: ${target:.6f}"
        )
        log.info(msg)
        send_telegram(msg)
        time.sleep(0.5)

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Bybit Bot starting on Render cloud...")
    
    # Start Telegram command listener in background thread
    telegram_thread = threading.Thread(target=listen_telegram_commands, daemon=True)
    telegram_thread.start()
    log.info("Telegram command listener started")
    
    balance = get_balance("USDT")
    send_telegram(
        f"🚀 Bot deployed to cloud!\n"
        f"Balance: ${balance:.4f} USDT\n"
        f"Status: Trading ENABLED\n"
        f"\n"
        f"Commands:\n"
        f"/start_trading - Enable trading\n"
        f"/stop_trading - Disable trading\n"
        f"/status - Check status"
    )
    
    while True:
        try:
            check_open_trades()
            scan_and_trade()
        except Exception as e:
            log.error(f"Error: {e}")
            send_telegram(f"⚠️ Bot error: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
