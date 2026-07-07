#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FollowM1 Supertrend M5 ATR10 Factor3 AutoScale Pro - MT5 bot rieng cho XAUUSD

CHAY:
  python bot.py              <-- mo GUI
  python bot.py --worker '{"cfg":"json"}'  <-- worker mode (GUI tu goi)

YEU CAU:
  pip install MetaTrader5 pandas numpy matplotlib

CONFIG:
  File btcrush_accounts.json se duoc tao tu dong khi save tu GUI.
"""

import sys
import os
import json
import time
import threading
import io
import subprocess
import queue
import traceback
from collections import deque
from itertools import combinations
from pathlib import Path
from datetime import datetime, timedelta, time as dtime

# ── GUI imports (chi can khi mo GUI, khong fail neu worker) ─────────────────
# PyQt6 GUI (khong dung chart -> khong can matplotlib)
try:
    from PyQt6 import QtWidgets, QtCore, QtGui
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal
    _HAS_GUI = True
except ImportError:
    _HAS_GUI = False
    # Worker mode must still run without PyQt6.  These lightweight stubs only
    # let the GUI class declarations load; _gui_main() exits with an install
    # message when a user actually tries to open the interface.
    class _QtWidgetStubs:
        QFrame = object
        QDialog = object
        QMainWindow = object
        QWidget = object
        def __getattr__(self, _name):
            return object
    class _QtCoreStubs:
        QObject = object
        def __getattr__(self, _name):
            return object
    class _QtGuiStubs:
        def __getattr__(self, _name):
            return object
    QtWidgets = _QtWidgetStubs()
    QtCore = _QtCoreStubs()
    QtGui = _QtGuiStubs()
    Qt = object()
    QTimer = object
    def pyqtSignal(*_args, **_kwargs):
        return None

# ── Worker imports (chi can khi worker mode) ──────────────────────────────────
def _import_worker_deps():
    """Import MT5 + pandas + numpy. Goi truoc khi run worker."""
    global mt5, pd, np
    try:
        import MetaTrader5 as _mt5
        import pandas as _pd
        import numpy as _np
        mt5 = _mt5
        pd = _pd
        np = _np
        return True
    except ImportError as e:
        print(f"ERROR: Thieu thu vien worker: {e}")
        print("Chay: pip install MetaTrader5 pandas numpy")
        return False

# Stub global de tranh NameError trong GUI mode
mt5 = None
pd = None
np = None

# ─────────────────────────────────────────────────────────────────────────────
BUILD_TAG = "FOLLOWM1_M5_SUPERTREND_ATR10_FACTOR3_BASKET_TP_ONLY_V2"
# DETECT MODE
# ─────────────────────────────────────────────────────────────────────────────
_WORKER_MODE = "--worker" in sys.argv

if _WORKER_MODE:
    # Worker mode: chuyen sys.stdout/stderr de gui JSON cho parent
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    if not _import_worker_deps():
        sys.exit(1)

# ── File logger ───────────────────────────────────────────────────────────────
# Khi start bot, tao file log txt trong thu muc logs/
# Ten file: logs/{symbol}_{login_or_pid}_{YYYY-MM-DD}.txt
_log_file = None
_log_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM NOTIFICATION (hardcoded)
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = "8552780845:AAE2qC0K9by_bSJIrhT04eailk01DmWMgvE"
TELEGRAM_CHAT_ID = "-1003527222406"
TELEGRAM_ENABLED = True
import urllib.request, urllib.parse

# === POSITION LIMIT ===
# None = khong co tran so lenh o cap bot. Chi con gioi han ky thuat/margin cua MT5 va broker.
MAX_POSITIONS = None

# Exit policy: KHONG Pair Close, KHONG Smart/Stale Cut, KHONG Quick Close theo Pair Min.
# Bot chỉ đóng toàn bộ giỏ khi tổng PnL đạt Basket TP; cuối tuần/market-close chỉ pause, không tự đóng lệnh.
BASKET_TP_ONLY = True

def _position_limit_label():
    return "NO APP LIMIT" if MAX_POSITIONS is None else str(int(MAX_POSITIONS))

def _position_limit_reached(current_count):
    return MAX_POSITIONS is not None and int(current_count) >= int(MAX_POSITIONS)

def _has_position_slots(current_count, needed=1):
    return MAX_POSITIONS is None or (int(current_count) + int(needed) <= int(MAX_POSITIONS))

def _allowed_open_count(current_count, requested_count):
    requested = max(0, int(requested_count))
    if MAX_POSITIONS is None:
        return requested
    return max(0, min(requested, int(MAX_POSITIONS) - int(current_count)))

# Stats cho hourly/daily report
_tg_stats = {
    "session_start_t": 0,
    "session_start_balance": 0,
    "hourly_last_t": 0,
    "hourly_last_balance": 0,
    "daily_last_t": 0,
    "daily_last_balance": 0,
    # Counters reset moi hour
    "h_opened": 0,
    "h_closed": 0,
    "h_win_count": 0, "h_win_pnl": 0.0,
    "h_loss_count": 0, "h_loss_pnl": 0.0,
    "h_total_lot": 0.0,
    # Counters reset moi day
    "d_opened": 0,
    "d_closed": 0,
    "d_win_count": 0, "d_win_pnl": 0.0,
    "d_loss_count": 0, "d_loss_pnl": 0.0,
    "d_total_lot": 0.0,
    "d_max_dd_pct": 0.0,
    "d_max_dd_time": "",
    "d_uptime_sec": 0,
}

import ssl as _tg_ssl

# SSL context bypass verify (de tranh "self signed cert" do AV/Firewall MITM)
_tg_ssl_ctx = _tg_ssl.create_default_context()
_tg_ssl_ctx.check_hostname = False
_tg_ssl_ctx.verify_mode = _tg_ssl.CERT_NONE

def tg_send(text):
    """Gui tin nhan Telegram. Khong block neu fail. SSL bypass cho AV/Firewall."""
    if not TELEGRAM_ENABLED or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10, context=_tg_ssl_ctx) as resp:
            resp.read()
    except Exception as e:
        # Khong block bot neu Telegram fail
        try:
            log(f"[TELEGRAM] Send fail: {e}", "warn")
        except: pass

def tg_track_trade(event, side, lot, pnl=0.0):
    """Cap nhat counters khi co trade."""
    if event == "open":
        _tg_stats["h_opened"] += 1
        _tg_stats["d_opened"] += 1
        _tg_stats["h_total_lot"] += lot
        _tg_stats["d_total_lot"] += lot
    elif event == "close":
        _tg_stats["h_closed"] += 1
        _tg_stats["d_closed"] += 1
        if pnl > 0:
            _tg_stats["h_win_count"] += 1
            _tg_stats["h_win_pnl"] += pnl
            _tg_stats["d_win_count"] += 1
            _tg_stats["d_win_pnl"] += pnl
        else:
            _tg_stats["h_loss_count"] += 1
            _tg_stats["h_loss_pnl"] += pnl
            _tg_stats["d_loss_count"] += 1
            _tg_stats["d_loss_pnl"] += pnl

def tg_track_dd(dd_pct):
    """Track max drawdown."""
    if dd_pct < _tg_stats["d_max_dd_pct"]:
        _tg_stats["d_max_dd_pct"] = dd_pct
        from datetime import datetime as _dtm
        _tg_stats["d_max_dd_time"] = _dtm.now().strftime("%H:%M")

# ─────────────────────────────────────────────────────────────────────────────
# MT5 HISTORY HELPERS (BOT V3) - Doc Session Net + Total Lot Today tu MT5
# Thay vi tu dem event open/close (se reset khi restart worker), doc thang
# tu mt5.history_deals_get() ke tu 00:00 ngay hom nay → chinh xac 100%.
# ─────────────────────────────────────────────────────────────────────────────
_today_hist_cache = {"t": 0, "data": None}

def get_today_history_stats(cfg, force=False):
    """Doc lich su deal hom nay tu MT5 (ke tu 00:00 local time).

    Cache 5s de giam tai. Filter theo magic va symbol cua cfg.
    Tra ve dict:
      - total_lot : tong lot DA MO trong ngay (entry IN)
      - net       : net realized PnL trong ngay (entry OUT, da gom swap+comm)
      - wins      : so lenh dong lai > 0
      - losses    : so lenh dong lai < 0
      - opened    : so lenh mo trong ngay
      - closed    : so lenh dong trong ngay
    """
    empty = {"total_lot":0.0, "net":0.0, "wins":0, "losses":0, "opened":0, "closed":0}
    if mt5 is None:
        return empty
    now = time.time()
    if (not force) and (now - _today_hist_cache["t"] < 5) and (_today_hist_cache["data"] is not None):
        return _today_hist_cache["data"]

    try:
        from datetime import datetime as _dt, time as _dtime
        today_start = _dt.combine(_dt.now().date(), _dtime.min)
        deals = mt5.history_deals_get(today_start, _dt.now())
    except Exception:
        deals = None

    result = dict(empty)
    if deals is None:
        _today_hist_cache["t"] = now
        _today_hist_cache["data"] = result
        return result

    magic  = cfg.get("magic", 0)
    symbol = cfg.get("symbol")
    for d in deals:
        if magic  and d.magic  != magic:  continue
        if symbol and d.symbol != symbol: continue
        if d.entry == mt5.DEAL_ENTRY_IN:            # lenh mo
            result["opened"]    += 1
            result["total_lot"] += d.volume
        elif d.entry == mt5.DEAL_ENTRY_OUT:         # lenh dong
            result["closed"] += 1
            pnl = d.profit + d.swap + d.commission
            result["net"] += pnl
            if   pnl > 0: result["wins"]   += 1
            elif pnl < 0: result["losses"] += 1

    _today_hist_cache["t"] = now
    _today_hist_cache["data"] = result
    return result

def _tg_get_login(cfg):
    """Lay login: tu cfg neu co, hoac tu mt5.account_info() neu khong."""
    login = cfg.get('login')
    if login:
        return str(login)
    try:
        acc = mt5.account_info()
        if acc:
            return str(acc.login)
    except Exception:
        pass
    return "auto"

def tg_msg_start(cfg, balance):
    from datetime import datetime as _dtm
    _tg_stats["session_start_t"] = time.time()
    _tg_stats["session_start_balance"] = balance
    _tg_stats["hourly_last_t"] = time.time()
    _tg_stats["hourly_last_balance"] = balance
    _tg_stats["daily_last_t"] = time.time()
    _tg_stats["daily_last_balance"] = balance
    
    return (
        f"🤖 <b>BOT STARTED</b>\n\n"
        f"📊 Account: <code>#{_tg_get_login(cfg)}</code>\n"
        f"💰 Balance: <b>{balance:,.0f} USC</b>\n"
        f"📈 Symbol: <code>{cfg['symbol']}</code>\n\n"
        f"⚙️ <b>Setting:</b>\n"
        f"  • Base lot: {cfg.get('base_lot', 0):.2f}\n"
        f"  • Lot step: {cfg.get('lot_step', 0):.2f}\n"
        f"  • Max lot: {cfg.get('max_lot', 0):.2f}\n"
        f"  • DCA: {cfg.get('dca_step', 0)} USD\n"
        f"  • Basket TP: {cfg.get('basket_tp', 0)}\n"
        f"  • Reentry: {cfg.get('reentry_wait', 0)//60}m\n"
        f"  • Batch: {cfg.get('batch_count', 1)}×{cfg.get('batch_delay', 5)}s\n\n"
        f"🛡️ Pause windows: {len(cfg.get('pause_windows', []))}\n"
        f"🛡️ Pause days (FOMC): {len(cfg.get('pause_days', []))}\n"
        f"🛡️ Position cap: {_position_limit_label()}\n"
        f"⏰ {_dtm.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

def tg_msg_hourly(cfg, balance, equity, ml, n_positions):
    """Tao tin hourly va RESET counter hour."""
    last_bal = _tg_stats["hourly_last_balance"]
    diff = balance - last_bal
    diff_sign = "+" if diff >= 0 else ""
    
    h_total = _tg_stats["h_win_count"] + _tg_stats["h_loss_count"]
    win_rate = (_tg_stats["h_win_count"] / h_total * 100) if h_total > 0 else 0
    realize = _tg_stats["h_win_pnl"] + _tg_stats["h_loss_pnl"]
    realize_sign = "+" if realize >= 0 else ""
    
    if ml >= 300:
        status = "✅ HEALTHY"
    elif ml >= 150:
        status = "🟡 OK"
    elif ml >= 100:
        status = "🟠 WARNING"
    else:
        status = "🔴 DANGER"
    
    from datetime import datetime as _dtm
    msg = (
        f"⏰ <b>HOURLY REPORT</b> [{_dtm.now().strftime('%H:%M')}]\n\n"
        f"📊 Account: <code>#{_tg_get_login(cfg)}</code>\n"
        f"💰 Balance: <b>{balance:,.0f}</b> USC ({diff_sign}{diff:,.0f})\n"
        f"📈 Equity: {equity:,.0f} USC\n\n"
        f"📋 <b>Giao dịch 1h qua:</b>\n"
        f"  • Lệnh mở: {_tg_stats['h_opened']}\n"
        f"  • Lệnh đóng: {_tg_stats['h_closed']}\n"
        f"  • Đang giữ: {n_positions}\n\n"
        f"💵 <b>Realize PnL: {realize_sign}{realize:,.2f} USC</b>\n"
        f"  ↗️ Winners: {_tg_stats['h_win_count']} ({_tg_stats['h_win_pnl']:+.2f})\n"
        f"  ↘️ Losers: {_tg_stats['h_loss_count']} ({_tg_stats['h_loss_pnl']:+.2f})\n\n"
        f"📦 Tổng lot trade: {_tg_stats['h_total_lot']:.2f}\n"
        f"🎯 Win rate: {win_rate:.1f}%\n\n"
        f"{status} (ML {ml:.0f}%)"
    )
    
    # RESET counters hourly
    _tg_stats["hourly_last_t"] = time.time()
    _tg_stats["hourly_last_balance"] = balance
    _tg_stats["h_opened"] = 0
    _tg_stats["h_closed"] = 0
    _tg_stats["h_win_count"] = 0
    _tg_stats["h_win_pnl"] = 0.0
    _tg_stats["h_loss_count"] = 0
    _tg_stats["h_loss_pnl"] = 0.0
    _tg_stats["h_total_lot"] = 0.0
    
    return msg

def tg_msg_daily(cfg, balance):
    """Tao tin daily va RESET counter day."""
    from datetime import datetime as _dtm
    start_bal = _tg_stats["daily_last_balance"]
    profit = balance - start_bal
    profit_pct = (profit / start_bal * 100) if start_bal > 0 else 0
    sign = "+" if profit >= 0 else ""
    
    d_total = _tg_stats["d_win_count"] + _tg_stats["d_loss_count"]
    win_rate = (_tg_stats["d_win_count"] / d_total * 100) if d_total > 0 else 0
    net = _tg_stats["d_win_pnl"] + _tg_stats["d_loss_pnl"]
    avg_win = _tg_stats["d_win_pnl"] / _tg_stats["d_win_count"] if _tg_stats["d_win_count"] > 0 else 0
    avg_loss = _tg_stats["d_loss_pnl"] / _tg_stats["d_loss_count"] if _tg_stats["d_loss_count"] > 0 else 0
    rr = (avg_win / abs(avg_loss)) if avg_loss < 0 else 0
    lot_avg = _tg_stats["d_total_lot"] / _tg_stats["d_opened"] if _tg_stats["d_opened"] > 0 else 0
    
    uptime_sec = time.time() - _tg_stats["session_start_t"]
    uptime_h = int(uptime_sec / 3600)
    uptime_m = int((uptime_sec % 3600) / 60)
    uptime_pct = (uptime_sec / 86400 * 100)
    
    if profit > 0:
        result_emoji = "✅"
        result_text = f"PROFIT {sign}{profit_pct:.2f}%"
    elif profit == 0:
        result_emoji = "⏸️"
        result_text = "BREAK EVEN"
    else:
        result_emoji = "❌"
        result_text = f"LOSS {profit_pct:.2f}%"
    
    msg = (
        f"📅 <b>DAILY REPORT</b> {_dtm.now().strftime('%d/%m/%Y')}\n\n"
        f"📊 Account: <code>#{_tg_get_login(cfg)}</code>\n"
        f"💰 Balance start: {start_bal:,.0f} USC\n"
        f"💰 Balance end: <b>{balance:,.0f}</b> USC\n"
        f"📈 Profit ngày: <b>{sign}{profit:,.2f}</b> USC ({sign}{profit_pct:.2f}%)\n\n"
        f"═══════════════════════\n"
        f"📋 <b>TỔNG GIAO DỊCH 24H</b>\n"
        f"═══════════════════════\n\n"
        f"🔢 <b>Số lệnh:</b>\n"
        f"  • Tổng mở: {_tg_stats['d_opened']:,}\n"
        f"  • Tổng đóng: {_tg_stats['d_closed']:,}\n\n"
        f"💵 <b>PnL chi tiết:</b>\n"
        f"  • Winners: {_tg_stats['d_win_count']} lệnh ({_tg_stats['d_win_pnl']:+.2f})\n"
        f"  • Losers: {_tg_stats['d_loss_count']} lệnh ({_tg_stats['d_loss_pnl']:+.2f})\n"
        f"  • Net: <b>{net:+.2f}</b> USC\n\n"
        f"📦 <b>Khối lượng:</b>\n"
        f"  • Tổng lot trade: {_tg_stats['d_total_lot']:.2f}\n"
        f"  • Lot TB/lệnh: {lot_avg:.3f}\n\n"
        f"🎯 <b>Hiệu suất:</b>\n"
        f"  • Win rate: {win_rate:.1f}%\n"
        f"  • Avg win: {avg_win:+.2f}\n"
        f"  • Avg loss: {avg_loss:+.2f}\n"
        f"  • R:R = 1:{rr:.2f}\n\n"
        f"⚙️ DD tối đa: {_tg_stats['d_max_dd_pct']:.1f}%"
    )
    if _tg_stats['d_max_dd_time']:
        msg += f" (lúc {_tg_stats['d_max_dd_time']})"
    msg += (
        f"\n⏰ Uptime: {uptime_h}h {uptime_m}m ({uptime_pct:.1f}%)\n\n"
        f"═══════════════════════\n"
        f"{result_emoji} <b>KẾT QUẢ: {result_text}</b>\n"
        f"═══════════════════════"
    )
    
    # RESET counters daily
    _tg_stats["daily_last_t"] = time.time()
    _tg_stats["daily_last_balance"] = balance
    _tg_stats["d_opened"] = 0
    _tg_stats["d_closed"] = 0
    _tg_stats["d_win_count"] = 0
    _tg_stats["d_win_pnl"] = 0.0
    _tg_stats["d_loss_count"] = 0
    _tg_stats["d_loss_pnl"] = 0.0
    _tg_stats["d_total_lot"] = 0.0
    _tg_stats["d_max_dd_pct"] = 0.0
    _tg_stats["d_max_dd_time"] = ""
    
    return msg

def tg_msg_alert(cfg, alert_type, details):
    """Tin alert: SMART_CUT, H1_REVERSAL, EMERGENCY."""
    from datetime import datetime as _dtm
    ts = _dtm.now().strftime("%H:%M:%S")
    
    if alert_type == "SMART_CUT":
        return (
            f"⚠️ <b>ALERT - SMART CUT</b>\n\n"
            f"📊 Account: <code>#{_tg_get_login(cfg)}</code>\n"
            f"🕐 Thời gian: {ts}\n\n"
            f"{details}\n\n"
            f"💪 Bot đang cứu account!"
        )
    elif alert_type == "H1_REVERSAL":
        return (
            f"🔄 <b>H1 REVERSAL</b>\n\n"
            f"📊 Account: <code>#{_tg_get_login(cfg)}</code>\n"
            f"🕐 Thời gian: {ts}\n\n"
            f"{details}"
        )
    elif alert_type == "EMERGENCY":
        return (
            f"🚨🚨 <b>EMERGENCY STOP</b> 🚨🚨\n\n"
            f"📊 Account: <code>#{_tg_get_login(cfg)}</code>\n"
            f"🕐 Thời gian: {ts}\n\n"
            f"{details}\n\n"
            f"⚠️ Bot đã DỪNG. Cần kiểm tra ngay!"
        )
    return ""

def init_log_file(cfg):
    """Tao file log khi bat dau worker."""
    global _log_file
    try:
        # Folder logs/ cung thu muc voi worker
        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        log_dir = os.path.join(base_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)

        symbol = cfg.get("symbol", "UNKNOWN")
        login = cfg.get("login", os.getpid())
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{symbol}_{login}_{date_str}.txt"
        filepath = os.path.join(log_dir, filename)

        _log_file = open(filepath, "a", encoding="utf-8", buffering=1)  # line buffered
        _log_file.write(f"\n{'='*70}\n")
        _log_file.write(f"START: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        _log_file.write(f"Symbol: {symbol} | Login: {login}\n")
        _log_file.write(f"{'='*70}\n")
        _log_file.flush()
    except Exception as e:
        _log_file = None
        # Khong fail neu khong ghi duoc file

def write_log_file(level, msg):
    """Ghi log vao file (thread-safe)."""
    if _log_file is None: return
    try:
        with _log_lock:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{ts}] [{level.upper():5}] {msg}\n"
            _log_file.write(line)
    except Exception:
        pass

def close_log_file():
    """Dong file khi exit."""
    global _log_file
    if _log_file is not None:
        try:
            with _log_lock:
                _log_file.write(f"END: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                _log_file.write(f"{'='*70}\n\n")
                _log_file.close()
        except Exception:
            pass
        _log_file = None

_send_lock = threading.Lock()

def send(obj):
    """Thread-safe send: nhieu thread cung viet ko bi xen ke."""
    with _send_lock:
        print(json.dumps(obj, ensure_ascii=True), flush=True)

def log(msg, level="info"):
    # Gui qua stdout cho GUI
    send({"type":"log","level":level,"msg":msg,
          "ts":datetime.now().strftime("%H:%M:%S")})
    # Ghi vao file
    write_log_file(level, msg)

def load_cfg():
    try:
        return json.loads(sys.argv[1])
    except Exception as e:
        send({"type":"exit","reason":f"Bad config: {e}"}); sys.exit(1)

# ── MT5 init voi retry ────────────────────────────────────────────────────────
def init_mt5(cfg):
    MAX_RETRIES = 10
    RETRY_WAIT  = 8
    kw = {}
    if cfg.get("path"): kw["path"] = cfg["path"]

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"MT5 initialize attempt {attempt}/{MAX_RETRIES}...")
        try:
            ok = mt5.initialize(**kw)
        except Exception as e:
            ok = False
            log(f"initialize exception: {e}", "warn")
        if ok: break
        log(f"initialize failed: {mt5.last_error()} - retrying in {RETRY_WAIT}s...", "warn")
        try: mt5.shutdown()
        except Exception: pass
        time.sleep(RETRY_WAIT)
    else:
        log(f"MT5 initialize failed after {MAX_RETRIES} attempts", "error")
        return False

    if cfg.get("login") and cfg.get("password"):
        login_kw = {"login": int(cfg["login"]), "password": cfg["password"]}
        if cfg.get("server"): login_kw["server"] = cfg["server"]
        for attempt in range(1, MAX_RETRIES + 1):
            log(f"Login attempt {attempt}/{MAX_RETRIES} for #{cfg['login']}...")
            if mt5.login(**login_kw): break
            log(f"Login failed: {mt5.last_error()} - retrying in {RETRY_WAIT}s...", "warn")
            time.sleep(RETRY_WAIT)
        else:
            log(f"Login failed after {MAX_RETRIES} attempts", "error")
            mt5.shutdown(); return False
        log(f"Logged in #{cfg['login']}")
    else:
        log("No login provided, using current terminal account", "warn")

    acc = mt5.account_info()
    if acc is None:
        log(f"account_info failed: {mt5.last_error()}", "error")
        mt5.shutdown(); return False
    log(f"Connected #{acc.login} | {acc.balance:.2f} {acc.currency}")

    # Check balance > 0
    if acc.balance <= 0:
        log(f"ERROR: Balance = {acc.balance:.2f} - khong the trade. STOP bot.", "error")
        mt5.shutdown()
        return False

    sym = mt5.symbol_info(cfg["symbol"])
    if sym is None:
        log(f"Symbol {cfg['symbol']} not found", "error")
        mt5.shutdown(); return False
    if not sym.visible:
        mt5.symbol_select(cfg["symbol"], True)
    return True

# ── Helpers ───────────────────────────────────────────────────────────────────
def round_lot(lot, sym, cfg):
    """Quantize volume. Follow M1 Supertrend uses no artificial lot cap.

    The only remaining ceiling is the broker's symbol.volume_max, because MT5
    will reject volumes above that technical limit.
    """
    step = float(getattr(sym, "volume_step", 0.01) or 0.01)
    r = round(round(float(lot) / step) * step, 8)
    vmin = float(getattr(sym, "volume_min", 0.01) or 0.01)
    vmax = float(getattr(sym, "volume_max", 100.0) or 100.0)
    if cfg.get("follow_m1_unlimited_max_lot", False):
        cap = vmax
    else:
        try:
            cap = min(vmax, float(cfg.get("max_lot", vmax)))
        except Exception:
            cap = vmax
    return max(vmin, min(vmax, cap, r))

def my_pos(cfg):
    pos = mt5.positions_get(symbol=cfg["symbol"])
    if not pos: return []
    magic = cfg.get("magic", 0)
    return [p for p in pos if p.magic == magic]

def by_side(positions, d):
    t = mt5.POSITION_TYPE_BUY if d == "BUY" else mt5.POSITION_TYPE_SELL
    return [p for p in positions if p.type == t]

def latest(positions):
    return max(positions, key=lambda p: p.time_msc) if positions else None

def recent_open_side_streak(positions, last_n=3):
    """
    Dem 3 lenh CUOI CUA GIO HIEN TAI (positions dang open), khong dung lich su.

    Fix cho Mode Trend M1 sau Pair Close:
      - Pair Close co the dong bot lenh, de lai 4 BUY dang open.
      - Bot phai nhin 3 lenh moi nhat con lai trong gio hien tai la BUY, BUY, BUY
        va CHAN BUY tiep.

    Return: (side, count)
      - side = BUY/SELL neu last_n lenh moi nhat dang open deu cung chieu
      - count = last_n neu bi trung chieu, nguoc lai tra count thuc te lien tiep
    """
    if not positions:
        return None, 0
    try:
        last_n = max(1, int(last_n))
    except Exception:
        last_n = 3

    # Chi lay cac LENH DANG MO hien tai, sort moi nhat truoc.
    # Dung time_msc neu co; fallback time de tranh broker/terminal tra field thieu.
    ordered = sorted(
        positions,
        key=lambda p: (getattr(p, "time_msc", 0), getattr(p, "time", 0), getattr(p, "ticket", 0)),
        reverse=True
    )
    if len(ordered) < last_n:
        return None, len(ordered)

    newest = ordered[:last_n]
    sides = ["BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL" for p in newest]

    # Dung dung y user: chi can 3 lenh cuoi cua GIO hien tai deu cung chieu thi chan.
    if all(s == sides[0] for s in sides):
        return sides[0], last_n

    # Neu khong du 3 lenh cuoi cung chieu, tra count lien tiep de log/debug.
    first_side = sides[0]
    count = 0
    for s in sides:
        if s != first_side:
            break
        count += 1
    return first_side, count

def m1_same_side_guard_blocked(positions, signal_side, cfg):
    """
    Mode Trend M1: chan mo them neu 3 lenh cuoi cua GIO DANG MO cung chieu.

    Mac dinh m1_max_same_side_streak = 3.
    Vi du sau Pair Close con lai: BUY, BUY, BUY, BUY
      -> 3 lenh moi nhat trong gio hien tai deu BUY
      -> signal BUY tiep bi skip, chi cho SELL.
    """
    try:
        max_streak = int(cfg.get("m1_max_same_side_streak", 3))
    except Exception:
        max_streak = 3
    if max_streak <= 0:
        return False, None, 0, max_streak
    side, count = recent_open_side_streak(positions, max_streak)
    return (side == signal_side and count >= max_streak), side, count, max_streak



def m1_imbalance_guard_blocked(positions, signal_side, cfg):
    """
    Mode Trend M1: chan mo them chieu dang lech qua nhieu de Pair Close con luc.

    Vi du nguy hiem user gap:
      BUY=18, SELL=1, signal BUY -> BLOCK
      BUY=18, SELL=1, signal SELL -> ALLOW de can lai gio

    Mac dinh:
      - m1_max_imbalance = 3  : lech >= 3 thi chan chieu dang nhieu
      - m1_hard_imbalance = 6 : log canh bao hard recovery, chi cho chieu doi dien

    Return: (blocked, buy_count, sell_count, diff, max_imb, hard_mode)
      diff la do lech cua CHIEU signal so voi chieu con lai.
    """
    try:
        max_imb = int(cfg.get("m1_max_imbalance", 3))
    except Exception:
        max_imb = 3
    try:
        hard_imb = int(cfg.get("m1_hard_imbalance", 6))
    except Exception:
        hard_imb = 6

    if max_imb <= 0:
        return False, 0, 0, 0, max_imb, False

    buy_count = len(by_side(positions, "BUY"))
    sell_count = len(by_side(positions, "SELL"))

    if signal_side == "BUY":
        diff = buy_count - sell_count
    else:
        diff = sell_count - buy_count

    hard_mode = (hard_imb > 0 and abs(buy_count - sell_count) >= hard_imb)

    # Block khi chieu signal da la chieu dang nhieu va lech >= nguong.
    # Chieu doi dien van duoc vao de can bang gio va tao winner cho Pair Close.
    return (diff >= max_imb), buy_count, sell_count, diff, max_imb, hard_mode


def m1_recovery_side_allowed(positions, signal_side, cfg):
    """
    Cho phep M1 mo chieu DOI DIEN khi gio dang lech qua nguong,
    ke ca 3 lenh moi nhat cung chieu (override SAME-SIDE GUARD).

    Vi du:
      BUY=18, SELL=4, signal=SELL -> ALLOW (SELL la chieu can bang)
      BUY=18, SELL=4, signal=BUY  -> khong phai recovery, de guard chan

    Return: (allowed, buy_count, sell_count, imbalance_abs, recovery_side)
    """
    try:
        max_imb = int(cfg.get("m1_max_imbalance", 3))
    except Exception:
        max_imb = 3
    if max_imb <= 0:
        return False, 0, 0, 0, None

    buy_count = len(by_side(positions, "BUY"))
    sell_count = len(by_side(positions, "SELL"))
    imbalance = buy_count - sell_count

    if imbalance >= max_imb and signal_side == "SELL":
        return True, buy_count, sell_count, abs(imbalance), "SELL"
    if -imbalance >= max_imb and signal_side == "BUY":
        return True, buy_count, sell_count, abs(imbalance), "BUY"
    return False, buy_count, sell_count, abs(imbalance), None



def _recovery_lot_state(positions):
    """
    Tinh lech gio theo TONG LOT, khong chi dem so lenh.

    Return:
      buy_lot, sell_lot, net_lot
      net_lot > 0  => gio lech BUY
      net_lot < 0  => gio lech SELL
    """
    try:
        buy_lot = round(sum(float(p.volume) for p in positions if p.type == mt5.POSITION_TYPE_BUY), 2)
        sell_lot = round(sum(float(p.volume) for p in positions if p.type == mt5.POSITION_TYPE_SELL), 2)
        return buy_lot, sell_lot, round(buy_lot - sell_lot, 2)
    except Exception:
        return 0.0, 0.0, 0.0




def _short_mode_dca_price_spacing_allowed(positions, signal_side, sym, cfg, strategy_mode):
    """
    Ngan DCA ngan han nhap qua gan nhau khi gio dang am.

    Chi ap dung M1/M5/Follow M1/Farm va chi khi basket am. Lenh cung chieu
    moi phai cach lenh cung chieu gan nhat mot khoang gia toi thieu theo huong
    bat loi (BUY khi gia thap hon; SELL khi gia cao hon). Lenh dau tien cua
    mot chieu van duoc phep.
    """
    if strategy_mode == "Trend 1H" or signal_side not in ("BUY", "SELL") or not positions:
        return True, ""
    # Short-mode DCA policy: Price Spacing chi de lai trong code cu de tuong thich,
    # khong duoc phep chan DCA. H1 khong chay vao helper nay.
    if cfg.get("short_mode_only_imbalance_streak", True):
        return True, "price spacing disabled for short modes"
    total_pnl, _, dd_pct = _basket_dd_pct(positions)
    if total_pnl >= 0:
        return True, ""

    # LOT NET GUARD: tu DD 30% tro len, DCA thuong khong duoc lam gio
    # lech them vuot Safe Net. Recovery Force se xu ly phan net vuot band.
    # Guard nay ap dung cho M1 / Follow M1 / M5 / Farm; Trend 1H da return o tren.
    try:
        lot_guard_dd = max(float(cfg.get("short_mode_lot_guard_dd_start_pct", 30.0)), 30.0)
    except Exception:
        lot_guard_dd = 30.0
    # CLEAN SHORT DCA: bo Lot Net Guard / Safe Net. M1/M5/Follow/Farm DCA theo nen
    # o moi DD; chi con price spacing, imbalance, streak va max positions.
    if dd_pct >= lot_guard_dd and not cfg.get("short_mode_clean_dca", True) and not cfg.get("short_mode_only_imbalance_streak", True):
        lot_state = _recovery_safe_net_band(positions, cfg)
        net_lot = float(lot_state.get("net_lot", 0.0))
        safe_net = float(lot_state.get("safe_net", 0.0))
        worsens = ((signal_side == "BUY" and net_lot >= safe_net) or
                   (signal_side == "SELL" and -net_lot >= safe_net))
        if worsens:
            return False, (f"LOT NET GUARD DD={dd_pct:.1f}% | net={net_lot:+.2f} "
                           f">= band ±{safe_net:.2f}; {signal_side} se lam lech hon")
    try:
        min_step = float(cfg.get("short_mode_dca_min_price_step", 1.5))
    except Exception:
        min_step = 1.5
    min_step = max(0.0, min_step)
    if min_step <= 0:
        return True, ""
    same = [p for p in positions if (p.type == mt5.POSITION_TYPE_BUY if signal_side == "BUY" else p.type == mt5.POSITION_TYPE_SELL)]
    if not same:
        return True, "lenh dau tien cua chieu nay"
    try:
        latest = max(same, key=lambda p: getattr(p, "time_msc", 0))
        last_open = float(latest.price_open)
        tick = mt5.symbol_info_tick(sym.name if hasattr(sym, "name") else cfg.get("symbol"))
        cur = float(tick.ask if signal_side == "BUY" else tick.bid) if tick else 0.0
    except Exception:
        return True, "khong lay duoc tick, bo qua price guard"
    if cur <= 0 or last_open <= 0:
        return True, "tick/open khong hop le"
    adverse = (last_open - cur) if signal_side == "BUY" else (cur - last_open)
    if adverse >= min_step:
        return True, f"gia bat loi them {adverse:.2f} >= {min_step:.2f}"
    return False, f"{signal_side} cach lenh cung chieu gan nhat moi {adverse:.2f}/{min_step:.2f}"


def _recovery_safe_net_band(positions, cfg):
    """
    Vung net lot an toan cho recovery cua cac mode ngan han.

    Khong can lot ve 0 lien tuc. Chi can khi net lot vuot vung nay moi bat dau
    can, de tranh vong lap: can lot -> Pair Close -> can lai ngay.

    Band dong = max(2 * Max Lot, 25% gross lot).
    """
    buy_lot, sell_lot, net_lot = _recovery_lot_state(positions)
    gross_lot = round(buy_lot + sell_lot, 2)
    try:
        max_lot = float(cfg.get("max_lot", cfg.get("base_lot", 0.10)))
    except Exception:
        max_lot = 0.10
    try:
        min_mult = float(cfg.get("recovery_safe_net_min_mult", 2.0))
    except Exception:
        min_mult = 2.0
    try:
        gross_ratio = float(cfg.get("recovery_safe_net_gross_ratio", 0.25))
    except Exception:
        gross_ratio = 0.25
    min_mult = max(0.1, min_mult)
    gross_ratio = max(0.0, min(1.0, gross_ratio))
    safe_net = round(max(max_lot * min_mult, gross_lot * gross_ratio, 0.01), 2)
    return {
        "buy_lot": buy_lot,
        "sell_lot": sell_lot,
        "net_lot": net_lot,
        "gross_lot": gross_lot,
        "safe_net": safe_net,
        "net_excess": round(max(0.0, abs(net_lot) - safe_net), 2),
    }




def _post_close_net_projection(positions, closing_positions, cfg):
    """Gia lap lot BUY/SELL con lai sau khi dong mot cum Pair Close."""
    close_tickets = {getattr(p, "ticket", None) for p in closing_positions}
    buy_left = 0.0
    sell_left = 0.0
    for p in positions:
        if getattr(p, "ticket", None) in close_tickets:
            continue
        vol = float(getattr(p, "volume", 0.0) or 0.0)
        if getattr(p, "type", None) == mt5.POSITION_TYPE_BUY:
            buy_left += vol
        elif getattr(p, "type", None) == mt5.POSITION_TYPE_SELL:
            sell_left += vol
    buy_left = round(buy_left, 2)
    sell_left = round(sell_left, 2)
    gross_left = round(buy_left + sell_left, 2)
    net_left = round(buy_left - sell_left, 2)
    try:
        max_lot = float(cfg.get("max_lot", cfg.get("base_lot", 0.10)))
    except Exception:
        max_lot = 0.10
    try:
        min_mult = float(cfg.get("recovery_safe_net_min_mult", 2.0))
    except Exception:
        min_mult = 2.0
    try:
        gross_ratio = float(cfg.get("recovery_safe_net_gross_ratio", 0.25))
    except Exception:
        gross_ratio = 0.25
    try:
        allow_mult = float(cfg.get("post_close_net_guard_allow_mult", 1.0))
    except Exception:
        allow_mult = 1.0
    safe_net = max(max_lot * max(0.1, min_mult), gross_left * max(0.0, min(1.0, gross_ratio)), 0.01)
    allowed_net = round(max(0.01, safe_net * max(0.1, allow_mult)), 2)
    return {
        "buy_left": buy_left,
        "sell_left": sell_left,
        "gross_left": gross_left,
        "net_left": net_left,
        "safe_net": round(safe_net, 2),
        "allowed_net": allowed_net,
    }


def _short_mode_smart_cut_subset(positions, locked_positions, winner_positions, required_pnl, cfg, context="SMART-CUT"):
    """
    Smart Cut / Stale Cut cho cac mode ngan han, KHONG ap dung Trend 1H.

    Muc tieu khi DD sau (chi mode ngan, Trend 1H GIU NGUYEN):
      - Smart Cut duoc phep chap nhan lo trong nguong required_pnl.
      - UU TIEN 1: dong cum lam |net lot| GIAM (xả BUY nhieu hon SELL khi net BUY,
        hoac xả SELL nhieu hon BUY khi net SELL).
      - UU TIEN 2: neu chua co cum giam net hop le, cho phep dong cum GIU NET
        de giam gross lot/margin.
      - CAM dong cum lam |net lot| xau hon truoc khi dong.

    Vi du gio net BUY +1.65, gia dang giam:
      close BUY 1.50 + SELL 0.75 -> net BUY 0.90, uu tien neu lo nam trong budget.
      close BUY 0.75 + SELL 0.75 -> net BUY giu 1.65, chi la phuong an du phong.
      close BUY 0.75 + SELL 1.50 -> net BUY tang, BI chan.
    """
    mode = str(cfg.get("strategy_mode", "Trend 1H"))
    short_modes = {"Trend M1", "Follow M1", "Trend M5", "Farm"}
    default_all = list(locked_positions) + list(winner_positions)

    # CLEAN SHORT MODE: Smart/Stale Cut cua Trend M1 / Follow M1 / Trend M5 / Farm
    # dung logic Smart Cut thuong: khi net cum dat nguong, dong toan bo winners
    # + losers da khoa. KHONG xet Net Guard; DCA theo nen tiep tuc tao cau truc moi.
    # Trend 1H van giu dung hanh vi cu (return default_all).
    if mode in short_modes:
        return default_all

    # Trend 1H giu nguyen hoan toan: Smart Cut cu khong co Net Guard moi.
    if mode not in short_modes or not cfg.get("smart_cut_net_guard_enabled", True):
        return default_all
    if not locked_positions or not winner_positions:
        return default_all

    try:
        required_pnl = float(required_pnl)
    except Exception:
        required_pnl = 0.0

    # Smart/Stale Cut chi vao helper nay khi nguong cho phep am.
    if required_pnl >= 0:
        return None

    before = _post_close_net_projection(positions, [], cfg)
    before_net = float(before["net_left"])
    before_abs = abs(before_net)

    # Dung tolerance bang volume step neu lay duoc, tranh sai so float 0.01.
    try:
        info = mt5.symbol_info(cfg.get("symbol"))
        lot_tol = max(0.01, float(getattr(info, "volume_step", 0.01) or 0.01) / 2.0)
    except Exception:
        lot_tol = 0.01

    # Gioi han candidate de tranh to hop qua lon khi co rat nhieu lenh.
    try:
        max_locked_candidates = max(1, int(cfg.get("smart_cut_net_guard_max_locked_candidates", 8)))
    except Exception:
        max_locked_candidates = 8
    # Cho phep thu toi da 5 loser trong Smart Cut de co du lot giam net,
    # nhung van co the giam qua cfg neu can. Chi ap dung short modes trong helper nay.
    try:
        max_locked_per_cut = max(1, int(cfg.get("smart_cut_net_reduce_max_locked_per_cut",
                                                   cfg.get("smart_cut_net_guard_max_locked_per_cut", 5))))
    except Exception:
        max_locked_per_cut = 5
    try:
        max_winner_candidates = max(1, int(cfg.get("smart_cut_net_guard_max_winner_candidates", 32)))
    except Exception:
        max_winner_candidates = 32

    # Deduplicate va uu tien loser it am hon truoc: Smart Cut uu tien xả nhe,
    # sau do chi sau hon khi khong co cum nhe hop le.
    seen = set()
    locked = []
    for p in sorted(locked_positions, key=lambda x: float(getattr(x, "profit", 0.0) or 0.0), reverse=True):
        ticket = getattr(p, "ticket", None)
        if ticket not in seen:
            locked.append(p)
            seen.add(ticket)
    locked = locked[:max_locked_candidates]

    winners = []
    for p in sorted(winner_positions, key=lambda x: float(getattr(x, "profit", 0.0) or 0.0), reverse=True):
        ticket = getattr(p, "ticket", None)
        if ticket not in seen:
            winners.append(p)
            seen.add(ticket)
    winners = winners[:max_winner_candidates]
    if not locked or not winners:
        return None

    def _pnl(items):
        return sum(float(getattr(p, "profit", 0.0) or 0.0) for p in items)

    def _projection_is_safe(items):
        proj = _post_close_net_projection(positions, items, cfg)
        # Rule duy nhat cua Smart Cut Net Guard:
        # sau dong, rui ro huong (|net lot|) khong duoc xau hon truoc.
        # Cho phep bang nhau de SELL winner + BUY loser van xả duoc khi gia giam.
        return abs(float(proj["net_left"])) <= before_abs + lot_tol, proj

    plans = []
    max_choose = min(max_locked_per_cut, len(locked))
    for n_locked in range(1, max_choose + 1):
        for loser_combo in combinations(locked, n_locked):
            selected = list(loser_combo)
            remaining = list(winners)

            # Greedy: moi winner chon theo thu tu uu tien
            # 1) keo |net| con lai nho nhat;
            # 2) PnL lon hon de gom du nguong cat;
            # 3) giam gross nhieu hon neu cac dieu kien tren bang nhau.
            while remaining:
                ranked = []
                for p in remaining:
                    trial = selected + [p]
                    safe_now, proj = _projection_is_safe(trial)
                    gross_reduced = float(before["gross_left"]) - float(proj["gross_left"])
                    ranked.append((
                        0 if safe_now else 1,
                        abs(float(proj["net_left"])),
                        -float(getattr(p, "profit", 0.0) or 0.0),
                        -gross_reduced,
                        p,
                        proj,
                    ))
                ranked.sort(key=lambda x: x[:4])
                _, _, _, _, pick, _ = ranked[0]
                selected.append(pick)
                remaining = [p for p in remaining if getattr(p, "ticket", None) != getattr(pick, "ticket", None)]

                safe_final, proj_final = _projection_is_safe(selected)
                total_pnl = _pnl(selected)
                if safe_final and total_pnl >= required_pnl:
                    gross_reduced = max(0.0, float(before["gross_left"]) - float(proj_final["gross_left"]))
                    after_abs = abs(float(proj_final["net_left"]))
                    net_reduced = after_abs < before_abs - lot_tol
                    loss_abs = abs(min(0.0, total_pnl))
                    # Smart Cut DD sau: chap nhan lo trong budget de GIAM NET truoc.
                    # Neu khong co cum giam net nao hop le, moi dung cum giu net de giam gross.
                    # Trong cung nhom, cat it lo hon truoc de khong hy sinh qua muc.
                    reduce_first = bool(cfg.get("smart_cut_net_reduce_first", True))
                    score = (
                        0 if (reduce_first and net_reduced) else (1 if reduce_first else 0),
                        loss_abs,
                        after_abs,
                        len(selected),
                        -gross_reduced,
                    )
                    plans.append((score, list(selected), proj_final, total_pnl, net_reduced))
                    break

    if plans:
        plans.sort(key=lambda x: x[0])
        _, chosen, proj, total_pnl, net_reduced = plans[0]
        action = "GIAM NET" if net_reduced else "GIU NET / GIAM GROSS"
        log(f"[SMART CUT NET GUARD-{mode}] {context}: {action} | xả {len(chosen)} lenh | "
            f"PnL={total_pnl:+.2f} >= {required_pnl:+.2f} | "
            f"net {before_net:+.2f} -> {float(proj['net_left']):+.2f} | "
            f"gross {float(before['gross_left']):.2f} -> {float(proj['gross_left']):.2f}", "warn")
        return chosen

    # Không co cum vua dat nguong Smart Cut vua giu net khong xau hon.
    key = (id(cfg), mode, context)
    if not hasattr(_short_mode_smart_cut_subset, "_last_log"):
        _short_mode_smart_cut_subset._last_log = {}
    now_t = time.time()
    last_t = _short_mode_smart_cut_subset._last_log.get(key, 0.0)
    if now_t - last_t >= 15.0:
        _short_mode_smart_cut_subset._last_log[key] = now_t
        log(f"[SMART CUT NET GUARD-{mode}] {context}: chua co cum hop le | "
            f"can PnL>={required_pnl:+.2f}, net truoc={before_net:+.2f}; "
            f"giu lenh cho winner lon hon, khong dong lam |net| xau hon", "warn")
    return None


def _select_short_mode_pair_close_subset(positions, locked_positions, winner_positions, required_pnl, cfg, context="PAIR"):
    """
    Post-Close Net Guard cho Trend M1 / Follow M1 / Trend M5 / Farm.

    Pair Close thuong (required_pnl >= 0): giu guard band cu de tranh xả
    winner xong thanh gio mot chieu.

    Smart/Stale Cut (required_pnl < 0): dung Smart Cut Net Guard rieng:
    duoc phep xả BUY loser + SELL winner (hoac nguoc lai) neu |net lot|
    sau dong khong lon hon truoc dong. Trend 1H bypass hoan toan.
    """
    mode = str(cfg.get("strategy_mode", "Trend 1H"))
    short_modes = {"Trend M1", "Follow M1", "Trend M5", "Farm"}
    all_default = list(locked_positions) + list(winner_positions)
    if mode not in short_modes:
        return all_default
    try:
        required_pnl = float(required_pnl)
    except Exception:
        required_pnl = 0.0

    # Smart/Stale Cut cua mode ngan dung Smart Cut thuong: dong toan bo
    # locked losers + winners khi da dat nguong cat lo dong. Khong dung Net Guard.
    # Trend 1H cung giu dung hanh vi cu (all_default).
    if required_pnl < 0:
        return all_default
    # CLEAN SHORT DCA: Pair Close loi duoc dong theo khoa tang/pair_min, khong
    # giu winner chi de can lot. Trend 1H khong vao helper nay.
    if cfg.get("short_mode_clean_dca", True):
        return all_default
    if not cfg.get("post_close_net_guard_enabled", True):
        return all_default
    if not locked_positions or not winner_positions:
        return all_default

    # De-duplicate ticket phong truong hop list giao nhau bat thuong.
    selected = []
    seen = set()
    for p in locked_positions:
        t = getattr(p, "ticket", None)
        if t not in seen:
            selected.append(p)
            seen.add(t)

    def selected_pnl(items):
        return sum(float(getattr(p, "profit", 0.0) or 0.0) for p in items)

    # Greedy uu tien winner ma khi dong se keo net con lai gan 0 hon;
    # trong cung nhom uu tien PnL lon de van dat nguong Pair Close.
    remaining = [p for p in winner_positions if getattr(p, "ticket", None) not in seen]
    while remaining:
        before = _post_close_net_projection(positions, selected, cfg)
        before_abs = abs(before["net_left"])
        ranked = []
        for p in remaining:
            trial = selected + [p]
            proj = _post_close_net_projection(positions, trial, cfg)
            improves = abs(proj["net_left"]) < before_abs - 1e-9
            in_band = abs(proj["net_left"]) <= proj["allowed_net"] + 1e-9
            ranked.append((0 if in_band else 1,
                           0 if improves else 1,
                           abs(proj["net_left"]),
                           -float(getattr(p, "profit", 0.0) or 0.0),
                           p, proj))
        ranked.sort(key=lambda x: x[:4])
        _, _, _, _, pick, _ = ranked[0]
        selected.append(pick)
        seen.add(getattr(pick, "ticket", None))
        remaining = [p for p in remaining if getattr(p, "ticket", None) != getattr(pick, "ticket", None)]
        proj = _post_close_net_projection(positions, selected, cfg)
        if selected_pnl(selected) >= required_pnl and abs(proj["net_left"]) <= proj["allowed_net"] + 1e-9:
            return selected

    # Khong tim duoc cum vua du PnL vua giu net band -> khong dong de tranh
    # Pair Close xong lai thanh gio mot chieu va recovery lap vo han.
    final_proj = _post_close_net_projection(positions, selected, cfg)
    key = (id(cfg), mode, context)
    if not hasattr(_select_short_mode_pair_close_subset, "_last_log"):
        _select_short_mode_pair_close_subset._last_log = {}
    now_t = time.time()
    last_t = _select_short_mode_pair_close_subset._last_log.get(key, 0.0)
    if now_t - last_t >= 10.0:
        _select_short_mode_pair_close_subset._last_log[key] = now_t
        log(f"[POST-CLOSE NET GUARD-{mode}] {context}: skip Pair Close | "
            f"can PnL>={required_pnl:+.2f}, net sau dong={final_proj['net_left']:+.2f}, "
            f"band=±{final_proj['allowed_net']:.2f} | giu winner de tranh gio lech mot chieu", "warn")
    return None

def _recovery_cooldown_after_pair_close_allowed(cfg, strategy_mode, now_ts=None):
    """Chi khoa RECOVERY sau Pair Close; DCA binh thuong duoi DD hard-stop van duoc phep."""
    if now_ts is None:
        now_ts = time.time()
    try:
        cooldown = float(cfg.get("recovery_after_pair_close_sec", 120.0))
    except Exception:
        cooldown = 120.0
    cooldown = max(0.0, cooldown)
    try:
        last_close = float(getattr(close_pairs, "_last_close_t_by_mode", {}).get(strategy_mode, 0.0))
    except Exception:
        last_close = 0.0
    if last_close <= 0 or cooldown <= 0:
        return True, 0.0
    left = max(0.0, cooldown - (now_ts - last_close))
    return left <= 0, left


def _recovery_spacing_allowed(cfg, sym, strategy_mode, force_side, dd_pct):
    """
    Sau mot nhịp cân, chỉ cân tiếp khi:
      - gia tiep tuc di NGUOC gio cu / theo chieu lenh can them mot khoang, hoac
      - DD xau them mot so diem phan tram.

    Ngăn recovery nhoi moi nen/tick khi gia chi rung nhe.
    """
    try:
        min_price = float(cfg.get("recovery_min_adverse_price_step", 3.0))
    except Exception:
        min_price = 3.0
    try:
        min_dd_worsen = float(cfg.get("recovery_min_dd_worsen_pct", 3.0))
    except Exception:
        min_dd_worsen = 3.0
    min_price = max(0.0, min_price)
    min_dd_worsen = max(0.0, min_dd_worsen)

    if not hasattr(_recovery_spacing_allowed, "_state"):
        _recovery_spacing_allowed._state = {}
    key = (id(cfg), str(strategy_mode))
    prev = _recovery_spacing_allowed._state.get(key)
    if not prev:
        return True, "lan can dau"
    if prev.get("side") != force_side:
        return True, "doi chieu recovery"
    if float(dd_pct) >= float(prev.get("dd_pct", 0.0)) + min_dd_worsen:
        return True, f"DD xau them >= {min_dd_worsen:.1f}%"

    try:
        tick = mt5.symbol_info_tick(sym.name if hasattr(sym, "name") else cfg.get("symbol"))
        price = float(tick.ask if force_side == "BUY" else tick.bid) if tick else 0.0
    except Exception:
        price = 0.0
    last_price = float(prev.get("price", 0.0))
    adverse_move = (price - last_price) if force_side == "BUY" else (last_price - price)
    if price > 0 and last_price > 0 and adverse_move >= min_price:
        return True, f"gia di them {adverse_move:.2f} >= {min_price:.2f}"
    return False, f"doi gia di them {min_price:.2f} hoac DD xau them {min_dd_worsen:.1f}%"


def _mark_recovery_event(cfg, sym, strategy_mode, force_side, dd_pct):
    """Luu moc gia/DD cua nhịp recovery da mo thanh cong."""
    if not hasattr(_recovery_spacing_allowed, "_state"):
        _recovery_spacing_allowed._state = {}
    try:
        tick = mt5.symbol_info_tick(sym.name if hasattr(sym, "name") else cfg.get("symbol"))
        price = float(tick.ask if force_side == "BUY" else tick.bid) if tick else 0.0
    except Exception:
        price = 0.0
    _recovery_spacing_allowed._state[(id(cfg), str(strategy_mode))] = {
        "side": force_side, "price": price, "dd_pct": float(dd_pct), "t": time.time()
    }


def _recovery_force_factor_by_dd(dd_pct, level, cfg):
    """Ty le can tren PHAN NET VUOT VUNG an toan, khong tren toan bo net lot."""
    try: dd_pct = float(dd_pct)
    except Exception: dd_pct = 0.0
    if dd_pct >= 50.0:
        try: return min(0.75, max(0.05, float(cfg.get("recovery_factor_dd_50_plus", 0.75))))
        except Exception: return 0.75
    if dd_pct >= 40.0:
        try: return min(0.50, max(0.05, float(cfg.get("recovery_factor_dd_40_50", 0.50))))
        except Exception: return 0.50
    if dd_pct >= 30.0:
        try: return min(0.30, max(0.05, float(cfg.get("recovery_factor_dd_30_40", 0.30))))
        except Exception: return 0.30
    return 0.0


def _recovery_max_orders_by_dd(dd_pct, cfg):
    """Recovery la nhip nho: 1 lenh duoi 50%, toi da 2 lenh khi emergency >=50%."""
    try: dd_pct = float(dd_pct)
    except Exception: dd_pct = 0.0
    if dd_pct >= 50.0:
        try: return min(2, max(1, int(cfg.get("recovery_max_orders_dd_50_plus", 2))))
        except Exception: return 2
    if dd_pct >= 40.0:
        return 1
    if dd_pct >= 30.0:
        return 1
    return 1


def _recovery_force_plan_by_net(net_abs, sym, cfg, level, dd_pct=0.0, min_lot=None):
    """
    Tra ve ke hoach mo lenh can gio theo tong lot net dang lech.

    Khac voi ban cu:
      - Khong mo full hedge 100% qua som; chi bat dau force tu DD >= 30%.
      - DD cang sau moi mo ti le lon hon.
      - Moi nhip co the mo nhieu lenh nho, moi lenh <= Max Lot.
      - Nhip sau tinh lai net lot, tranh overshoot.
    """
    try: base_lot = float(cfg.get("base_lot", 0.10))
    except Exception: base_lot = 0.10
    try: max_lot = float(cfg.get("max_lot", base_lot))
    except Exception: max_lot = base_lot
    if min_lot is None:
        min_lot = base_lot
    try: min_lot = float(min_lot)
    except Exception: min_lot = base_lot

    try: net_abs = abs(float(net_abs))
    except Exception: net_abs = 0.0
    if net_abs <= 0 or max_lot <= 0:
        return {"force_lots": [], "total_lot": 0.0, "target_lot": 0.0, "factor": 0.0, "max_orders": 0}

    factor = _recovery_force_factor_by_dd(dd_pct, level, cfg)
    # Bao ve factor nam trong 0.00 -> 1.00, khong de config mo qua net lech.
    try: factor = float(factor)
    except Exception: factor = 0.0
    if factor <= 0:
        return {"force_lots": [], "total_lot": 0.0, "target_lot": 0.0, "factor": 0.0, "max_orders": 0}
    factor = max(0.05, min(1.0, factor))

    max_orders = _recovery_max_orders_by_dd(dd_pct, cfg)
    target_total = net_abs * factor

    # Neu da du dieu kien force thi it nhat mo min_lot, nhung khong vuot net_abs va cap nhip.
    cap_total = max_lot * max_orders
    target_total = min(max(target_total, min_lot), net_abs, cap_total)

    lots = []
    remain = target_total
    for _ in range(max_orders):
        if remain <= 0:
            break
        lot_raw = min(max_lot, remain)
        lot = round_lot(lot_raw, sym, cfg)
        if lot <= 0:
            break
        # Neu lot sau khi round vuot phan con lai qua nhieu, van chap nhan vi MT5 co volume_step.
        lots.append(lot)
        remain = round(remain - lot, 4)
        # Neu phan con lai nho hon min_lot thi bo qua, tranh mo lenh linh tinh qua nho.
        if remain < min_lot:
            break

    total_lot = round(sum(lots), 2)
    return {"force_lots": lots, "total_lot": total_lot, "target_lot": round(target_total, 2),
            "factor": factor, "max_orders": max_orders}


def _recovery_force_lot_by_net(net_abs, sym, cfg, level, min_lot=None):
    """Compatibility: tra ve lot dau tien trong plan recovery."""
    plan = _recovery_force_plan_by_net(net_abs, sym, cfg, level, 0.0, min_lot=min_lot)
    lots = plan.get("force_lots") or []
    return lots[0] if lots else 0.0


def _basket_dd_pct(positions):
    """Tinh PnL gio va DD% theo balance. DD% duong khi gio dang am."""
    try:
        acc = mt5.account_info()
        balance = float(acc.balance) if (acc and acc.balance > 0) else 0.0
    except Exception:
        balance = 0.0
    try:
        total_pnl = float(sum(float(p.profit) for p in positions))
    except Exception:
        total_pnl = 0.0
    dd_pct = (abs(total_pnl) / balance * 100.0) if (balance > 0 and total_pnl < 0) else 0.0
    return total_pnl, balance, dd_pct


def _mark_pair_close_event(cfg):
    """Ghi nhan Pair Close: khoa recovery tam thoi va xoa moc recovery cu de tranh vong lap."""
    try:
        mode = str(cfg.get("strategy_mode", ""))
        if not hasattr(close_pairs, "_last_close_t_by_mode"):
            close_pairs._last_close_t_by_mode = {}
        close_pairs._last_close_t_by_mode[mode] = time.time()
        if hasattr(_recovery_spacing_allowed, "_state"):
            _recovery_spacing_allowed._state.pop((id(cfg), mode), None)
    except Exception:
        pass


def recovery_hold_decision(positions, cfg, strategy_mode, now_ts=None):
    """
    Recovery Hold / Drain Mode.

    Khi gio da gan can BUY/SELL lot nhung PnL con am, dung mo lenh thuong/DCA
    de Pair Close co thoi gian gom winner + loser. Hold khong chan Pair Close/Smart Cut.

    Hold chi bat khi DD am du nguong va net lot da gan can.
    Neu Pair Close vua dong xong thi cooldown vai phut de tranh vong lap:
      can lot -> pair close -> lech nhe -> can lai ngay.
    """
    res = {
        "hold": False, "reason": "", "dd_pct": 0.0, "total_pnl": 0.0,
        "buy_lot": 0.0, "sell_lot": 0.0, "net_lot": 0.0,
        "hold_net": 0.0, "exit_net": 0.0, "cooldown_left": 0.0,
    }
    if not cfg.get("recovery_hold_enabled", True):
        return res
    # CLEAN SHORT DCA: khong HOLD khi DD sau; de DCA theo nen tiep tuc tao cau truc
    # moi cho Pair Close / Smart Cut. Trend 1H van khong dung hold nhu cu.
    if (cfg.get("short_mode_clean_dca", True) or cfg.get("short_mode_only_imbalance_streak", True)) and str(strategy_mode or "") != "Trend 1H":
        return res
    if not positions:
        return res

    # Trend 1H chay theo Supertrend H1 + Adaptive DCA rieng, khong dung Recovery Hold/Drain.
    mode = str(strategy_mode or "")
    if mode == "Trend 1H":
        return res

    # Ap dung cho cac mode ngan han neu duoc bat trong config.
    allowed = set(str(cfg.get("recovery_hold_modes", "Trend M5,Follow M1,Trend M1,Farm")).split(','))
    allowed = {x.strip() for x in allowed if x.strip()}
    if mode not in allowed:
        return res

    total_pnl, balance, dd_pct = _basket_dd_pct(positions)
    res.update({"total_pnl": total_pnl, "dd_pct": dd_pct})
    if total_pnl >= 0 or dd_pct <= 0:
        return res

    # Recovery Hold chi bat khi DD da du sau.
    # Luu y: Trend 1H da return o tren, nen logic nay chi ap dung cho M1/M5/Follow M1/Farm.
    try: hold_start_dd = float(cfg.get("recovery_hold_start_dd_pct", 50.0))
    except Exception: hold_start_dd = 50.0
    # Khong HOLD som nua: duoi 50% van de cac mode ngan han co co hoi DCA/thoat lenh.
    hold_start_dd = max(hold_start_dd, 50.0)
    if dd_pct < hold_start_dd:
        return res

    buy_lot, sell_lot, net_lot = _recovery_lot_state(positions)
    net_abs = abs(net_lot)
    res.update({"buy_lot": buy_lot, "sell_lot": sell_lot, "net_lot": net_lot})

    try: max_lot = float(cfg.get("max_lot", cfg.get("base_lot", 0.10)))
    except Exception: max_lot = 0.10
    try: enter_mult = float(cfg.get("recovery_hold_net_mult", 1.0))
    except Exception: enter_mult = 1.0
    try: exit_mult = float(cfg.get("recovery_hold_exit_net_mult", 2.0))
    except Exception: exit_mult = 2.0
    hold_net = round(max(0.01, max_lot * enter_mult), 2)
    exit_net = round(max(hold_net, max_lot * exit_mult), 2)
    res.update({"hold_net": hold_net, "exit_net": exit_net})

    if now_ts is None:
        now_ts = time.time()
    try: cooldown_sec = float(cfg.get("recovery_hold_after_pair_close_sec", 180.0))
    except Exception: cooldown_sec = 180.0
    last_close = 0.0
    try:
        last_close = float(getattr(close_pairs, "_last_close_t_by_mode", {}).get(strategy_mode, 0.0))
    except Exception:
        last_close = 0.0
    cooldown_left = max(0.0, cooldown_sec - (now_ts - last_close)) if last_close > 0 else 0.0
    res["cooldown_left"] = cooldown_left

    # Hold binh thuong: net da gan can.
    if net_abs <= hold_net:
        res["hold"] = True
        res["reason"] = "net lot da gan can -> doi Pair Close xa gio"
        return res

    # Sau Pair Close, neu net chi lech vua phai thi chua can lai ngay; doi cooldown.
    if cooldown_left > 0 and net_abs <= exit_net:
        res["hold"] = True
        res["reason"] = f"vua Pair Close, cooldown {cooldown_left:.0f}s -> tranh can lot lap lai"
        return res

    return res


def h1_recovery_force_decision(positions, sym, cfg):
    """
    Trend 1H Recovery Force theo DD% + NET LOT.

    Chi force khi:
      - Gio dang am du % balance.
      - BUY/SELL lot lech du nguong.
    Muc lot force tinh theo DD% va % net lech, khong can full hedge qua som.
    """
    result = {
        "action": "NONE", "force_side": None, "force_lot": 0.0,
        "dd_pct": 0.0, "total_pnl": 0.0, "balance": 0.0,
        "buy_count": 0, "sell_count": 0,
        "buy_lot": 0.0, "sell_lot": 0.0, "net_lot": 0.0,
        "min_net": 0.0, "level": 0,
        "emergency": False, "force_reason": "",
    }
    # Disabled by design: Trend 1H uses H1 Supertrend + Adaptive DCA, not DDHold/net-lot balancing.
    return result
    if not positions:
        return result

    total_pnl, balance, dd_pct = _basket_dd_pct(positions)
    result.update({"total_pnl": total_pnl, "balance": balance, "dd_pct": dd_pct})
    if balance <= 0 or total_pnl >= 0:
        return result

    try: start_pct = float(cfg.get("h1_recovery_dd_start_pct", 5.0))
    except Exception: start_pct = 5.0
    try: lvl2_pct = float(cfg.get("h1_recovery_dd_level2_pct", 10.0))
    except Exception: lvl2_pct = 10.0
    try: lvl3_pct = float(cfg.get("h1_recovery_dd_level3_pct", 15.0))
    except Exception: lvl3_pct = 15.0
    try: stop_pct = float(cfg.get("h1_recovery_stop_add_pct", 20.0))
    except Exception: stop_pct = 20.0

    if dd_pct < start_pct:
        return result

    buy_count = len(by_side(positions, "BUY"))
    sell_count = len(by_side(positions, "SELL"))
    buy_lot, sell_lot, net_lot = _recovery_lot_state(positions)
    result.update({"buy_count": buy_count, "sell_count": sell_count,
                   "buy_lot": buy_lot, "sell_lot": sell_lot, "net_lot": net_lot})

    try: max_lot = float(cfg.get("max_lot", cfg.get("base_lot", 0.10)))
    except Exception: max_lot = 0.10
    try: net_mult = float(cfg.get("h1_recovery_net_mult", 1.0))
    except Exception: net_mult = 1.0
    min_net = round(max(0.01, max_lot * net_mult), 2)
    result["min_net"] = min_net

    net_abs = abs(net_lot)
    if net_abs < min_net:
        if dd_pct >= stop_pct:
            result["action"] = "STOP"
            result["level"] = 4
        return result

    # Net BUY qua nhieu -> force SELL. Net SELL qua nhieu -> force BUY.
    force_side = "SELL" if net_lot > 0 else "BUY"

    if dd_pct >= stop_pct:
        level = 4
        result["emergency"] = True
    elif dd_pct >= lvl3_pct:
        level = 3
    elif dd_pct >= lvl2_pct:
        level = 2
    else:
        level = 1

    try: base_lot = float(cfg.get("base_lot", 0.10))
    except Exception: base_lot = 0.10
    plan = _recovery_force_plan_by_net(net_abs, sym, cfg, level, dd_pct, min_lot=base_lot)
    force_lots = plan.get("force_lots") or []
    if not force_lots:
        return result

    result.update({"action": "FORCE", "force_side": force_side,
                   "force_lot": force_lots[0], "force_lots": force_lots,
                   "force_total_lot": plan.get("total_lot", sum(force_lots)),
                   "force_orders": len(force_lots), "target_factor": plan.get("factor", 0.0),
                   "target_lot": plan.get("target_lot", 0.0), "level": level,
                   "force_reason": "can bang net lot theo DD%"})
    return result


def m1_recovery_force_decision(positions, sym, cfg):
    """
    Recovery M1/Follow M1 theo DD + NET LOT.

    Chi can khi DD >= 30% VA net lot vuot vung an toan dong.
    Khong can lot ve 0 lien tuc; recovery chi xu ly phan net vuot vung.
    """
    result = {
        "action": "NONE", "force_side": None, "force_lot": 0.0,
        "dd_pct": 0.0, "total_pnl": 0.0, "balance": 0.0,
        "buy_count": 0, "sell_count": 0, "imbalance": 0,
        "buy_lot": 0.0, "sell_lot": 0.0, "net_lot": 0.0,
        "gross_lot": 0.0, "safe_net": 0.0, "net_excess": 0.0,
        "min_imbalance": 0, "min_net": 0.0, "level": 0,
        "emergency": False, "force_reason": "", "wait_reason": "",
    }
    if not cfg.get("m1_recovery_force_enabled", True) or not positions:
        return result

    total_pnl, balance, dd_pct = _basket_dd_pct(positions)
    result.update({"dd_pct": dd_pct, "total_pnl": total_pnl, "balance": balance})
    if balance <= 0 or total_pnl >= 0:
        return result

    try: start_pct = max(float(cfg.get("m1_recovery_dd_start_pct", 30.0)), 30.0)
    except Exception: start_pct = 30.0
    try: lvl2_pct = max(float(cfg.get("m1_recovery_dd_level2_pct", 40.0)), 40.0)
    except Exception: lvl2_pct = 40.0
    try: lvl3_pct = max(float(cfg.get("m1_recovery_dd_level3_pct", 50.0)), 50.0)
    except Exception: lvl3_pct = 50.0
    try: stop_pct = max(float(cfg.get("m1_recovery_stop_add_pct", 50.0)), 50.0)
    except Exception: stop_pct = 50.0
    if dd_pct < start_pct:
        return result

    buy_count = len(by_side(positions, "BUY"))
    sell_count = len(by_side(positions, "SELL"))
    lot_state = _recovery_safe_net_band(positions, cfg)
    net_lot = lot_state["net_lot"]
    net_abs = abs(net_lot)
    safe_net = lot_state["safe_net"]
    net_excess = lot_state["net_excess"]
    result.update({
        "buy_count": buy_count, "sell_count": sell_count, "imbalance": buy_count - sell_count,
        **lot_state, "min_net": safe_net,
    })

    # Chi recovery khi net vuot band. Count imbalance chi log/guard, khong tu mo lenh can.
    if net_excess <= 0:
        if dd_pct >= stop_pct:
            result.update({"action": "STOP", "level": 4,
                           "wait_reason": "net nam trong vung an toan -> hard hold"})
        return result

    force_side = "SELL" if net_lot > 0 else "BUY"
    allowed_pc, pc_left = _recovery_cooldown_after_pair_close_allowed(cfg, str(cfg.get("strategy_mode", "Trend M1")))
    if not allowed_pc:
        result.update({"action": "WAIT", "level": 4 if dd_pct >= stop_pct else 0,
                       "force_side": force_side,
                       "wait_reason": f"vua Pair Close, recovery cooldown {pc_left:.0f}s"})
        return result
    allowed_spacing, spacing_reason = _recovery_spacing_allowed(cfg, sym, str(cfg.get("strategy_mode", "Trend M1")), force_side, dd_pct)
    if not allowed_spacing:
        result.update({"action": "WAIT", "level": 4 if dd_pct >= stop_pct else 0,
                       "force_side": force_side, "wait_reason": spacing_reason})
        return result

    if dd_pct >= stop_pct:
        level = 4; emergency = True
    elif dd_pct >= lvl3_pct:
        level = 3; emergency = False
    elif dd_pct >= lvl2_pct:
        level = 2; emergency = False
    else:
        level = 1; emergency = False
    try: base_lot = float(cfg.get("base_lot", 0.10))
    except Exception: base_lot = 0.10
    plan = _recovery_force_plan_by_net(net_excess, sym, cfg, level, dd_pct, min_lot=base_lot)
    force_lots = plan.get("force_lots") or []
    if not force_lots:
        return result
    result.update({"action": "FORCE", "force_side": force_side,
                   "force_lot": force_lots[0], "force_lots": force_lots,
                   "force_total_lot": plan.get("total_lot", sum(force_lots)),
                   "force_orders": len(force_lots), "target_factor": plan.get("factor", 0.0),
                   "target_lot": plan.get("target_lot", 0.0), "level": level,
                   "emergency": emergency,
                   "force_reason": "can phan net lot vuot vung an toan theo DD%"})
    return result

def m5_recovery_force_decision(positions, sym, cfg, strong_trend_side=None):
    """Recovery Trend M5 theo DD + net vuot vung; trend chi la filter phu truoc DD emergency."""
    result = {
        "action": "NONE", "force_side": None, "force_lot": 0.0,
        "dd_pct": 0.0, "total_pnl": 0.0, "balance": 0.0,
        "buy_count": 0, "sell_count": 0, "reverse_imbalance": 0,
        "buy_lot": 0.0, "sell_lot": 0.0, "net_lot": 0.0,
        "gross_lot": 0.0, "safe_net": 0.0, "net_excess": 0.0,
        "min_imbalance": 0, "min_net": 0.0, "level": 0, "trend_side": strong_trend_side,
        "emergency": False, "force_reason": "", "wait_reason": "",
    }
    if not cfg.get("m5_recovery_force_enabled", True) or not positions:
        return result
    total_pnl, balance, dd_pct = _basket_dd_pct(positions)
    result.update({"dd_pct": dd_pct, "total_pnl": total_pnl, "balance": balance})
    if balance <= 0 or total_pnl >= 0:
        return result

    try: start_pct = max(float(cfg.get("m5_recovery_dd_start_pct", 30.0)), 30.0)
    except Exception: start_pct = 30.0
    try: lvl2_pct = max(float(cfg.get("m5_recovery_dd_level2_pct", 40.0)), 40.0)
    except Exception: lvl2_pct = 40.0
    try: lvl3_pct = max(float(cfg.get("m5_recovery_dd_level3_pct", 50.0)), 50.0)
    except Exception: lvl3_pct = 50.0
    try: stop_pct = max(float(cfg.get("m5_recovery_stop_add_pct", 50.0)), 50.0)
    except Exception: stop_pct = 50.0
    if dd_pct < start_pct:
        return result

    buy_count = len(by_side(positions, "BUY")); sell_count = len(by_side(positions, "SELL"))
    lot_state = _recovery_safe_net_band(positions, cfg)
    net_lot = lot_state["net_lot"]; net_excess = lot_state["net_excess"]
    result.update({"buy_count": buy_count, "sell_count": sell_count, **lot_state,
                   "min_net": lot_state["safe_net"]})

    # Duoi 50%, M5 chi can theo trend ro. Tu 50% uu tien giam net lech.
    if net_excess <= 0:
        if dd_pct >= stop_pct:
            result.update({"action": "STOP", "level": 4,
                           "wait_reason": "net nam trong vung an toan -> hard hold"})
        return result
    natural_side = "SELL" if net_lot > 0 else "BUY"
    if dd_pct < stop_pct:
        # Truoc emergency, M5 chi force khi trend manh dong thuan voi huong can.
        # Sideway/nguoc trend thi de DCA + Pair Close binh thuong tu xu ly.
        if strong_trend_side != natural_side:
            return result
    force_side = natural_side
    force_reason = "emergency can phan net vuot vung" if dd_pct >= stop_pct else "can phan net vuot vung theo DD%"

    allowed_pc, pc_left = _recovery_cooldown_after_pair_close_allowed(cfg, str(cfg.get("strategy_mode", "Trend M5")))
    if not allowed_pc:
        result.update({"action": "WAIT", "level": 4 if dd_pct >= stop_pct else 0,
                       "force_side": force_side, "wait_reason": f"vua Pair Close, recovery cooldown {pc_left:.0f}s"})
        return result
    allowed_spacing, spacing_reason = _recovery_spacing_allowed(cfg, sym, str(cfg.get("strategy_mode", "Trend M5")), force_side, dd_pct)
    if not allowed_spacing:
        result.update({"action": "WAIT", "level": 4 if dd_pct >= stop_pct else 0,
                       "force_side": force_side, "wait_reason": spacing_reason})
        return result

    if dd_pct >= stop_pct:
        level = 4; emergency = True
    elif dd_pct >= lvl3_pct:
        level = 3; emergency = False
    elif dd_pct >= lvl2_pct:
        level = 2; emergency = False
    else:
        level = 1; emergency = False
    try: base_lot = float(cfg.get("base_lot", 0.10))
    except Exception: base_lot = 0.10
    plan = _recovery_force_plan_by_net(net_excess, sym, cfg, level, dd_pct, min_lot=base_lot)
    force_lots = plan.get("force_lots") or []
    if not force_lots:
        return result
    result.update({"action": "FORCE", "force_side": force_side,
                   "force_lot": force_lots[0], "force_lots": force_lots,
                   "force_total_lot": plan.get("total_lot", sum(force_lots)),
                   "force_orders": len(force_lots), "target_factor": plan.get("factor", 0.0),
                   "target_lot": plan.get("target_lot", 0.0), "level": level,
                   "emergency": emergency, "force_reason": force_reason})
    return result

def farm_net_imbalance_guard_blocked(positions, signal_side, cfg, projected_net_lot=0.0):
    """
    Mode Farm: guard theo NET LOT BUY/SELL, khong dem so lenh.

    Farm moi vong mo 1 cap hedge:
      - Farm SELL: BUY nho + SELL lon -> net SELL
      - Farm BUY : SELL nho + BUY lon -> net BUY

    Neu chi dem so lenh thi BUY count/Sell count gan nhu bang nhau,
    nhung tong lot co the lech rat lon. Vi vay Farm phai guard theo:
      net_lot = total_buy_lot - total_sell_lot

    Auto scale theo max_lot:
      max_net  = max_lot * farm_net_guard_max_mult  (mac dinh 1.0)
      hard_net = max_lot * farm_net_guard_hard_mult (mac dinh 2.0)

    Vi du Farm 10k max_lot=0.30:
      max_net  = 0.30 lot
      hard_net = 0.60 lot

    projected_net_lot:
      net lot cua cap Farm SAP MO (vd 0.10). Guard se chan neu mo them
      cung chieu lam net lot vuot max_net, khong doi den khi da vuot moi chan.

    Return:
      (blocked, buy_lot, sell_lot, net_lot, max_net, hard_mode)
    """
    # SHORT DCA POLICY: Farm khong can net lot; mo hedge pair binh thuong moi nen.
    if cfg.get("short_mode_clean_dca", True) or cfg.get("short_mode_only_imbalance_streak", True):
        return False, 0.0, 0.0, 0.0, 0.0, False
    if not cfg.get("farm_net_guard_enabled", True):
        return False, 0.0, 0.0, 0.0, 0.0, False
    if signal_side not in ("BUY", "SELL"):
        return False, 0.0, 0.0, 0.0, 0.0, False

    try:
        max_lot = float(cfg.get("max_lot", 0.30))
    except Exception:
        max_lot = 0.30
    try:
        max_mult = float(cfg.get("farm_net_guard_max_mult", 1.0))
    except Exception:
        max_mult = 1.0
    try:
        hard_mult = float(cfg.get("farm_net_guard_hard_mult", 2.0))
    except Exception:
        hard_mult = 2.0

    buy_lot = round(sum(float(p.volume) for p in positions if p.type == mt5.POSITION_TYPE_BUY), 2)
    sell_lot = round(sum(float(p.volume) for p in positions if p.type == mt5.POSITION_TYPE_SELL), 2)
    # Net guard dong: giu mot vung net cho phep, khong chan/can qua som.
    # Vung toi thieu = 2*MaxLot hoac 25% gross lot hien tai.
    gross_lot = buy_lot + sell_lot
    try: safe_mult = float(cfg.get("recovery_safe_net_min_mult", 2.0))
    except Exception: safe_mult = 2.0
    try: safe_ratio = float(cfg.get("recovery_safe_net_gross_ratio", 0.25))
    except Exception: safe_ratio = 0.25
    dynamic_safe = max(max_lot * max(0.1, safe_mult), gross_lot * max(0.0, min(1.0, safe_ratio)))
    max_net = round(max(0.01, max_lot * max_mult, dynamic_safe), 2)
    hard_net = round(max(max_lot * max(0.01, hard_mult), max_net * 1.5), 2)
    net_lot = round(buy_lot - sell_lot, 2)  # duong = lech BUY, am = lech SELL

    try:
        projected_net_lot = float(projected_net_lot or 0.0)
    except Exception:
        projected_net_lot = 0.0

    if signal_side == "BUY":
        diff_now = net_lot
    else:
        diff_now = -net_lot

    # Neu signal cung chieu voi net dang lech, mo them se lam net lech tang.
    # Chan theo PROJECTED diff de khong vuot nguong max_net sau khi mo cap moi.
    projected_diff = diff_now + projected_net_lot

    hard_mode = abs(net_lot) >= hard_net or projected_diff >= hard_net

    # Block neu Farm signal dang cung chieu voi net lot va sau khi mo se cham/vuot max_net.
    # Chieu doi dien co diff_now am/nhỏ => luon duoc phep de can lai gio.
    blocked = projected_diff >= max_net
    return blocked, buy_lot, sell_lot, net_lot, max_net, hard_mode


def m5_imbalance_guard_blocked(positions, signal_side, cfg, strong_trend_side=None):
    """
    Mode Trend M5: giu BUY/SELL can bang de Pair Close hoat dong tot hon.

    Sideway / trend yeu:
      - dung m5_max_imbalance (mac dinh 8)

    Khi Trend Filter xac nhan trend manh va signal cung chieu trend:
      - noi nguong len m5_max_imbalance_trend (mac dinh 15)
      - giup M5 van bam trend, khong bi khoa qua som

    Vi du sideway max=8:
      BUY=20, SELL=12, signal BUY -> blocked

    Vi du trend BUY manh max_trend=15:
      BUY=20, SELL=12, signal BUY -> allow
      BUY=28, SELL=12, signal BUY -> blocked

    Chi ap dung cho lenh MO MOI / DCA / batch-fill cua Mode Trend M5.
    Khong anh huong Trend 1H va Trend M1.
    """
    try:
        base_max_imb = int(cfg.get("m5_max_imbalance", 8))
    except Exception:
        base_max_imb = 8
    try:
        trend_max_imb = int(cfg.get("m5_max_imbalance_trend", 15))
    except Exception:
        trend_max_imb = 15

    max_imb = base_max_imb
    if strong_trend_side in ("BUY", "SELL") and signal_side == strong_trend_side:
        max_imb = max(base_max_imb, trend_max_imb)

    if max_imb <= 0:
        return False, 0, 0, 0, max_imb

    buy_count = len(by_side(positions, "BUY"))
    sell_count = len(by_side(positions, "SELL"))
    if signal_side == "BUY":
        diff = buy_count - sell_count
    else:
        diff = sell_count - buy_count

    # Block khi chieu signal da lech >= max_imb; neu mo them se lech hon nua.
    return (diff >= max_imb), buy_count, sell_count, diff, max_imb

def get_volatility_step(cfg, default_step=None):
    """
    [ADAPTIVE-STEP] Tính dca_step dựa trên biến động M1 trong 7 phút gần nhất.
    
    Cách hoạt động (7p MATCHED-p50, đổi từ 15p sang 7p để phản ứng nhanh hơn):
      range_7m = max(M1 high) - min(M1 low) trong 7 nến M1 gần nhất
      
      range_7m < 4$   -> step = step_calm    (yên)
      range_7m < 6$   -> step = step_normal  (bình thường)
      range_7m < 9$   -> step = step_active  (động)
      range_7m < 14$  -> step = step_strong  (mạnh)
      range_7m >= 14$ -> step = step_extreme (cực mạnh - FOMC)
    
    Window 7p phản ứng nhanh hơn 15p ~5 lần (3 phút vs 15 phút cảm nhận shock).
    Backtest 48h M1 thực: TOTAL +$1058 vs 15p, Realized +57% (3995 vs 2541).
    
    Cache 30s để giảm tải query MT5.
    Return: step (USD)
    """
    if default_step is None:
        default_step = cfg.get("dca_step", 5.0)
    
    # Nếu adaptive bị tắt -> dùng fixed step
    if not cfg.get("adaptive_step_enabled", True):
        return default_step
    
    # Cache: cấu hình qua cfg (default 30s)
    cache_sec = cfg.get("adaptive_cache_sec", 30)
    now = time.time()
    if not hasattr(get_volatility_step, "_cache"):
        get_volatility_step._cache = {"t": 0, "step": default_step, "range": 0}
    if now - get_volatility_step._cache["t"] < cache_sec:
        return get_volatility_step._cache["step"]
    
    try:
        # Lấy 8 nến M1 gần nhất (7 cho range + 1 buffer)
        bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M1, 0, 8)
        if bars is None or len(bars) < 7:
            return default_step
        
        # Lấy 7 nến M1 gần nhất (window 7 phút)
        df = pd.DataFrame(bars[-7:])
        range_7m = float(df["high"].max() - df["low"].min())
        
        # Bins từ config — 7 bins → 8 tầng (7p MATCHED-p50 v2)
        b1 = cfg.get("adaptive_bin_calm",        4.0)
        b2 = cfg.get("adaptive_bin_normal",      6.0)
        b3 = cfg.get("adaptive_bin_active",      9.0)
        b4 = cfg.get("adaptive_bin_strong",     14.0)
        b5 = cfg.get("adaptive_bin_extreme",    18.0)
        b6 = cfg.get("adaptive_bin_shock",      24.0)
        b7 = cfg.get("adaptive_bin_supershock", 28.0)
        
        s_calm        = cfg.get("adaptive_step_calm",         8.0)
        s_normal      = cfg.get("adaptive_step_normal",      10.0)
        s_active      = cfg.get("adaptive_step_active",      12.0)
        s_strong      = cfg.get("adaptive_step_strong",      14.0)
        s_extreme     = cfg.get("adaptive_step_extreme",     17.0)
        s_shock       = cfg.get("adaptive_step_shock",       20.0)
        s_supershock  = cfg.get("adaptive_step_supershock",  23.0)
        s_max         = cfg.get("adaptive_step_max",         26.0)
        
        if   range_7m < b1: step = s_calm
        elif range_7m < b2: step = s_normal
        elif range_7m < b3: step = s_active
        elif range_7m < b4: step = s_strong
        elif range_7m < b5: step = s_extreme
        elif range_7m < b6: step = s_shock
        elif range_7m < b7: step = s_supershock
        else:               step = s_max
        
        get_volatility_step._cache = {"t": now, "step": step, "range": range_7m}
        return step
    except Exception as e:
        log(f"[ADAPTIVE-STEP] Exception: {e} -> dùng default {default_step}", "warn")
        return default_step

def should_dca(d, pos, tick, cfg):
    """
    Kiểm tra có nên DCA không.
    Step dùng adaptive (theo biến động M1) nếu adaptive_step_enabled=True,
    ngược lại dùng fixed dca_step.
    """
    op = pos.price_open
    step = get_volatility_step(cfg)
    
    if d == "BUY":
        trig = op - step; cur = tick.ask
        return cur <= trig, cur, trig
    else:
        trig = op + step; cur = tick.bid
        return cur >= trig, cur, trig

# ── H1 trend (Supertrend) ─────────────────────────────────────────────────────
def get_h1_trend(cfg):
    """
    Xac dinh H1 trend bang Supertrend (period=10, mult=3.0).
    Tra ve "BUY" / "SELL" / None.
    """
    try:
        period     = cfg.get("st_period", 10)
        multiplier = cfg.get("st_mult", 3.0)
        need_bars  = max(period * 5, 100)

        bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_H1, 0, need_bars)
        if bars is None or len(bars) < period + 10: return None

        df = pd.DataFrame(bars)
        # BO nen cuoi (dang chay) - chi dung nen DA DONG
        df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < period + 10: return None

        high  = df["high"]
        low   = df["low"]
        close = df["close"]

        # True Range
        hl  = high - low
        hcp = (high - close.shift()).abs()
        lcp = (low  - close.shift()).abs()
        tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)

        # ATR (Wilder smoothing - giong TradingView)
        atr = tr.ewm(alpha=1.0/period, adjust=False).mean()

        # Basic bands
        hl2 = (high + low) / 2
        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr

        # Final bands (locked logic)
        n = len(df)
        # .copy() de tao mang ghi duoc
        final_upper = upper_band.to_numpy().copy()
        final_lower = lower_band.to_numpy().copy()
        close_v     = close.to_numpy()

        for i in range(1, n):
            if upper_band.iloc[i] < final_upper[i-1] or close_v[i-1] > final_upper[i-1]:
                final_upper[i] = upper_band.iloc[i]
            else:
                final_upper[i] = final_upper[i-1]

            if lower_band.iloc[i] > final_lower[i-1] or close_v[i-1] < final_lower[i-1]:
                final_lower[i] = lower_band.iloc[i]
            else:
                final_lower[i] = final_lower[i-1]

        # Trend
        trend = [1] * n  # 1=BUY, -1=SELL
        for i in range(1, n):
            prev_trend = trend[i-1]
            if prev_trend == 1:
                if close_v[i] < final_lower[i]:
                    trend[i] = -1
                else:
                    trend[i] = 1
            else:
                if close_v[i] > final_upper[i]:
                    trend[i] = 1
                else:
                    trend[i] = -1

        return "BUY" if trend[-1] == 1 else "SELL"
    except Exception as e:
        log(f"get_h1_trend exception: {e}", "warn")
        return None


def get_m5_closed_signal_info(cfg):
    """
    Trend M5 mode:
      - Lay nen M5 DA DONG gan nhat
      - Nen do  (close < open) -> BUY
      - Nen xanh(close > open) -> SELL
      - Doji/khong du data -> (None, None)

    Return: (signal, candle_time)
      candle_time dung de DCA Trend M5: moi nen M5 dong chi mo 1 batch DCA.
    """
    try:
        bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M5, 0, 3)
        if bars is None or len(bars) < 2:
            return None, None
        # bars[-1] la nen dang chay, bars[-2] la nen da dong gan nhat
        last_closed = bars[-2]
        o = float(last_closed["open"])
        c = float(last_closed["close"])
        candle_time = int(last_closed["time"])
        if c < o:
            return "BUY", candle_time
        if c > o:
            return "SELL", candle_time
        return None, candle_time
    except Exception as e:
        log(f"get_m5_closed_signal_info exception: {e}", "warn")
        return None, None

def get_m5_entry_signal(cfg):
    """Backward-compatible wrapper: chi tra ve huong Trend M5."""
    sig, _ = get_m5_closed_signal_info(cfg)
    return sig

def get_follow_trend_m5_signal_info(cfg):
    """
    Mode Follow M1:
      - Khong dung mau nen do/xanh de dao chieu.
      - Xac dinh trend bang EMA8/EMA21/EMA50 + ADX/DI tren M5 da dong.
      - Vao/DCA theo trend khi co pullback ve EMA8/EMA21 va nen dong xac nhan tiep trend.

    Return: (signal, candle_time, reason)
    """
    try:
        sym = cfg["symbol"]
        fast = int(float(cfg.get("follow_m1_ema_fast", 8)))
        mid  = int(float(cfg.get("follow_m1_ema_mid", 21)))
        slow = int(float(cfg.get("follow_m1_ema_slow", 50)))
        period = int(float(cfg.get("follow_m1_adx_period", 14)))
        adx_min = float(cfg.get("follow_m1_adx_min", 18.0))
        di_gap_min = float(cfg.get("follow_m1_di_gap_min", 3.0))
        max_dist_atr = float(cfg.get("follow_m1_max_dist_atr", 1.8))

        need = max(slow + period + 20, 100)
        bars = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, need + 5)
        if bars is None or len(bars) < slow + period + 10:
            return None, None, "not_enough_bars"

        df = pd.DataFrame(bars).iloc[:-1].reset_index(drop=True)  # bo nen dang chay
        if len(df) < slow + period + 10:
            return None, None, "not_enough_closed_bars"

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        open_ = df["open"].astype(float)

        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_mid = close.ewm(span=mid, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()

        hl = high - low
        hcp = (high - close.shift()).abs()
        lcp = (low - close.shift()).abs()
        tr = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/period, adjust=False).mean()

        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move
        plus_di = 100 * plus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1)
        minus_di = 100 * minus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
        adx = dx.ewm(alpha=1.0/period, adjust=False).mean()

        i = len(df) - 1
        candle_time = int(df.iloc[i]["time"])
        c = float(close.iloc[i]); o = float(open_.iloc[i]); h = float(high.iloc[i]); l = float(low.iloc[i])
        pc = float(close.iloc[i-1])
        ef = float(ema_fast.iloc[i]); em = float(ema_mid.iloc[i]); es = float(ema_slow.iloc[i])
        pef = float(ema_fast.iloc[i-1])
        atr_val = float(atr.iloc[i]) if float(atr.iloc[i]) > 0 else 0.0
        adx_val = float(adx.iloc[i])
        pdi = float(plus_di.iloc[i]); mdi = float(minus_di.iloc[i])
        di_gap = abs(pdi - mdi)

        if atr_val <= 0:
            return None, candle_time, "atr_zero"
        if adx_val < adx_min or di_gap < di_gap_min:
            return None, candle_time, f"sideway adx={adx_val:.1f} gap={di_gap:.1f}"

        dist_mid_atr = abs(c - em) / atr_val
        if dist_mid_atr > max_dist_atr:
            return None, candle_time, f"too_far_ema21 dist={dist_mid_atr:.2f}ATR"

        recent_high = float(high.iloc[max(0, i-7):i].max()) if i >= 2 else h
        recent_low = float(low.iloc[max(0, i-7):i].min()) if i >= 2 else l

        buy_trend = (ef > em > es and pdi > mdi and c > em)
        sell_trend = (ef < em < es and mdi > pdi and c < em)

        if buy_trend:
            pullback = (l <= ef or l <= em or pc <= pef)
            confirm = (c >= ef and (c > o or c > pc))
            breakout = (c > recent_high and adx_val >= adx_min + 4.0)
            if (pullback and confirm) or breakout:
                return "BUY", candle_time, f"EMA{fast}>{mid}>{slow} ADX={adx_val:.1f} DI+={pdi:.1f}>{mdi:.1f} pullback={pullback} breakout={breakout}"
            return None, candle_time, "BUY trend but no pullback-confirm"

        if sell_trend:
            pullback = (h >= ef or h >= em or pc >= pef)
            confirm = (c <= ef and (c < o or c < pc))
            breakout = (c < recent_low and adx_val >= adx_min + 4.0)
            if (pullback and confirm) or breakout:
                return "SELL", candle_time, f"EMA{fast}<{mid}<{slow} ADX={adx_val:.1f} DI-={mdi:.1f}>{pdi:.1f} pullback={pullback} breakout={breakout}"
            return None, candle_time, "SELL trend but no pullback-confirm"

        return None, candle_time, "EMA trend not aligned"
    except Exception as e:
        log(f"get_follow_trend_m5_signal_info exception: {e}", "warn")
        return None, None, "exception"

def get_m1_closed_signal_info(cfg):
    """
    Trend M1 mode:
      - Lay nen M1 DA DONG gan nhat
      - Nen do  (close < open) -> BUY
      - Nen xanh(close > open) -> SELL
      - Doji/khong du data -> (None, None)

    Return: (signal, candle_time)
      candle_time dung de DCA Trend M1: moi nen M1 dong chi mo 1 batch DCA.
    """
    try:
        bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M1, 0, 3)
        if bars is None or len(bars) < 2:
            return None, None
        last_closed = bars[-2]
        o = float(last_closed["open"])
        c = float(last_closed["close"])
        candle_time = int(last_closed["time"])
        if c < o:
            return "BUY", candle_time
        if c > o:
            return "SELL", candle_time
        return None, candle_time
    except Exception as e:
        log(f"get_m1_closed_signal_info exception: {e}", "warn")
        return None, None

def get_m1_entry_signal(cfg):
    """Backward-compatible wrapper: chi tra ve huong Trend M1."""
    sig, _ = get_m1_closed_signal_info(cfg)
    return sig
def get_follow_m1_live_signal_info_legacy(cfg):
    """
    Legacy EMA M1 signal kept only for backward reference; it is not called by this bot:
    Follow M1:
      - DCA theo huong trend M1, khong theo mau nen dao chieu.
      - Trend co ban duoc xac dinh tren M1 DA DONG: EMA nhanh/EMA21 + do doc EMA21.
      - Live EMA Flip: khong doi M1 dong va KHONG doi nen M1 xac nhan.
        Khi gia live pha EMA21 M1 qua vung dem ATR theo chieu nguoc trend hien tai,
        bot doi pha ngay trong nen M1 dang chay.

    Return: (signal, bar_time, reason, fast_flip)
      - bar_time luon la nen M1 DANG CHAY tai thoi diem mo lenh.
        Trend thuong van tinh bang nen M1 da dong, nhung lenh duoc gan vao cua so M1 moi.
      - Live EMA Flip dung cung bar_time nay, vi vay tong cong toi da 1 lenh cho moi nen M1 dang chay.
    """
    try:
        sym = cfg["symbol"]
        fast = int(float(cfg.get("follow_m1_live_ema_fast", cfg.get("follow_m1_ema_fast", 8))))
        mid = int(float(cfg.get("follow_m1_live_ema_mid", cfg.get("follow_m1_ema_mid", 21))))
        slow = int(float(cfg.get("follow_m1_live_ema_slow", cfg.get("follow_m1_ema_slow", 50))))
        atr_period = int(float(cfg.get("follow_m1_live_atr_period", 14)))
        min_sep_atr = float(cfg.get("follow_m1_live_min_ema_sep_atr", 0.08))
        fast_flip_enabled = bool(cfg.get("follow_m1_live_fast_flip_enabled", True))
        # Key moi de ap dung ngay vung dem live EMA 0.15 ATR cho config cu dang co 0.25.
        flip_atr = float(cfg.get("follow_m1_live_fast_flip_live_atr", cfg.get("follow_m1_live_fast_flip_atr", 0.15)))
        flip_atr = max(0.01, flip_atr)

        need = max(slow + atr_period + 20, 100)
        bars = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, need + 5)
        if bars is None or len(bars) < slow + atr_period + 10:
            return None, None, "not_enough_m1_bars", False

        all_df = pd.DataFrame(bars).reset_index(drop=True)
        # Last row is the live M1 candle; indicators are based only on completed candles.
        df = all_df.iloc[:-1].reset_index(drop=True)
        if len(df) < slow + atr_period + 5:
            return None, None, "not_enough_closed_m1", False

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_mid = close.ewm(span=mid, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / max(1, atr_period), adjust=False).mean()

        i = len(df) - 1
        last_closed_time = int(df.iloc[i]["time"])
        live_m1_time = int(all_df.iloc[-1]["time"])
        ef = float(ema_fast.iloc[i])
        em = float(ema_mid.iloc[i])
        es = float(ema_slow.iloc[i])
        pem = float(ema_mid.iloc[i - 1])
        c = float(close.iloc[i])
        atr_v = max(float(atr.iloc[i]), 0.01)
        sep = abs(ef - em) / atr_v

        base = None
        if ef > em and c > em and em >= pem and sep >= min_sep_atr:
            base = "BUY"
        elif ef < em and c < em and em <= pem and sep >= min_sep_atr:
            base = "SELL"

        # Live EMA Flip: chi can gia tick pha EMA21 qua vung dem ATR.
        # Khong doi nen M1 dong / swing M1, de vao nhanh hon khi trend dao manh.
        if fast_flip_enabled and base is not None:
            tick = mt5.symbol_info_tick(sym)
            if tick is not None:
                px = (float(tick.bid) + float(tick.ask)) / 2.0
                lower = em - flip_atr * atr_v
                upper = em + flip_atr * atr_v
                if base == "BUY" and px < lower:
                    return "SELL", live_m1_time, (
                        f"LIVE-EMA-FLIP SELL: px={px:.2f} < EMA{mid} zone={lower:.2f} "
                        f"({flip_atr:.2f}ATR; no M1 confirmation)"
                    ), True
                if base == "SELL" and px > upper:
                    return "BUY", live_m1_time, (
                        f"LIVE-EMA-FLIP BUY: px={px:.2f} > EMA{mid} zone={upper:.2f} "
                        f"({flip_atr:.2f}ATR; no M1 confirmation)"
                    ), True

        if base is None:
            return None, last_closed_time, f"M1 trend unclear EMA{fast}/{mid} sep={sep:.2f}ATR", False
        # Trend duoc tinh tu nen M1 da dong, nhung order mo trong nen M1 dang chay.
        # Dung live_m1_time de Live EMA Flip va trend thuong khong mo 2 lenh trong cung mot M1.
        return base, live_m1_time, (
            f"M1 trend {base}: EMA{fast}={ef:.2f}, EMA{mid}={em:.2f}, "
            f"EMA{slow}={es:.2f}, sep={sep:.2f}ATR"
        ), False
    except Exception as e:
        log(f"get_follow_m1_live_signal_info exception: {e}", "warn")
        return None, None, "exception", False


def get_follow_m1_supertrend_m5_signal_info(cfg):
    """Lấy hướng Supertrend M5 **chỉ từ nến M5 đã đóng**.

    Không được phép lấy high/low/close của cây M5 đang chạy. Vì vậy giá có
    chọc qua line Supertrend rồi rút râu trong cùng cây M5 cũng không thể làm
    bot đổi BUY/SELL. Hướng chỉ có thể thay đổi đúng một lần khi một cây M5
    mới bắt đầu (cây M5 trước đó vừa đóng).

    Return:
      (side, active_m1_time, confirmed_m5_time, reason, closed_bar_flip,
       flip_buffer_ok, flip_buffer_reason)
    """
    try:
        sym = cfg["symbol"]
        period = max(2, int(float(cfg.get("follow_m1_supertrend_m5_period", 10))))
        multiplier = max(0.1, float(cfg.get("follow_m1_supertrend_m5_multiplier", 3.0)))
        need = max(period * 6, 120)

        raw = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, need + 5)
        if raw is None or len(raw) < period + 16:
            return None, None, None, "not_enough_m5_bars", False, False, "no_m5_data"

        # MT5 trả dữ liệu theo thời gian tăng dần, phần tử cuối là bar M5 đang
        # chạy (shift=0). Loại nó theo timestamp, không dựa vào giá live.
        raw_df = pd.DataFrame(raw).sort_values("time").reset_index(drop=True)
        active_m5_time = int(raw_df.iloc[-1]["time"])
        df = raw_df[raw_df["time"].astype("int64") < active_m5_time].reset_index(drop=True)
        if len(df) < period + 12:
            return None, None, None, "not_enough_closed_m5_bars", False, False, "not_enough_closed_m5"

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
        hl2 = (high + low) / 2.0
        upper = hl2 + multiplier * atr
        lower = hl2 - multiplier * atr

        n = len(df)
        final_upper = upper.to_numpy().copy()
        final_lower = lower.to_numpy().copy()
        close_v = close.to_numpy()
        for i in range(1, n):
            if upper.iloc[i] < final_upper[i - 1] or close_v[i - 1] > final_upper[i - 1]:
                final_upper[i] = upper.iloc[i]
            else:
                final_upper[i] = final_upper[i - 1]
            if lower.iloc[i] > final_lower[i - 1] or close_v[i - 1] < final_lower[i - 1]:
                final_lower[i] = lower.iloc[i]
            else:
                final_lower[i] = final_lower[i - 1]

        trend = [1] * n  # 1=BUY, -1=SELL
        for i in range(1, n):
            if trend[i - 1] == 1:
                trend[i] = -1 if close_v[i] < final_lower[i] else 1
            else:
                trend[i] = 1 if close_v[i] > final_upper[i] else -1

        # Cổng vào lệnh vẫn theo cây M1 đang chạy: tối đa một lệnh / phút.
        m1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 2)
        if m1 is None or len(m1) < 1:
            return None, None, None, "not_enough_m1_clock", False, False, "no_m1_clock"
        active_m1_time = int(m1[-1]["time"])
        confirmed_m5_time = int(df.iloc[-1]["time"])
        signal = "BUY" if trend[-1] == 1 else "SELL"
        previous = "BUY" if trend[-2] == 1 else "SELL"
        closed_bar_flip = n >= 2 and trend[-1] != trend[-2]
        # Flip buffer: chi phuc vu gate doi chieu, khong lam cham entry cung chieu.
        # BUY dung final_lower; SELL dung final_upper.
        last_close = float(close_v[-1])
        last_atr = float(atr.iloc[-1]) if float(atr.iloc[-1]) > 0 else 0.0
        st_line = float(final_lower[-1] if signal == "BUY" else final_upper[-1])
        dist = abs(last_close - st_line)
        flip_k = max(0.0, float(cfg.get("follow_m1_flip_buffer_atr", 0.20)))
        need = max(0.0, flip_k * last_atr)
        flip_buffer_ok = bool(last_atr > 0 and dist >= need)
        flip_buffer_reason = f"st_dist={dist:.2f} need={need:.2f} atr={last_atr:.2f}"
        suffix = f" | CLOSED-FLIP {previous}->{signal}" if closed_bar_flip else ""
        return signal, active_m1_time, confirmed_m5_time, (
            f"M5 Supertrend({period},{multiplier:.2f})={signal}; "
            f"confirmed M5={confirmed_m5_time}; active M5={active_m5_time}; order clock=M1{suffix}"
        ), closed_bar_flip, flip_buffer_ok, flip_buffer_reason
    except Exception as e:
        log(f"get_follow_m1_supertrend_m5_signal_info exception: {e}", "warn")
        return None, None, None, "exception", False, False, "exception"


def get_m15_di_confirmation_for_side(cfg, side):
    """Xac nhan huong BUY/SELL tren M15 bang ADX/DI closed bars only."""
    try:
        period = 14
        bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M15, 0, 120)
        if bars is None or len(bars) < period + 20:
            return False, "m15_not_enough_bars"

        df = pd.DataFrame(bars).iloc[:-1].reset_index(drop=True)
        if len(df) < period + 20:
            return False, "m15_not_enough_closed_bars"

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()

        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

        plus_di = 100 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, 1)
        minus_di = 100 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, 1)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
        adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()

        pdi = float(plus_di.iloc[-1])
        mdi = float(minus_di.iloc[-1])
        adx_val = float(adx.iloc[-1])
        gap = abs(pdi - mdi)

        try:
            gap_min = float(cfg.get("follow_m1_flip_m15_di_gap_min", 5.0))
        except Exception:
            gap_min = 5.0
        try:
            adx_min = float(cfg.get("follow_m1_flip_m15_adx_min", 18.0))
        except Exception:
            adx_min = 18.0

        if adx_val < adx_min or gap < gap_min:
            return False, f"m15_weak adx={adx_val:.1f} gap={gap:.1f}"
        if side == "BUY" and pdi > mdi:
            return True, f"m15_buy pdi={pdi:.1f}>{mdi:.1f} adx={adx_val:.1f}"
        if side == "SELL" and mdi > pdi:
            return True, f"m15_sell mdi={mdi:.1f}>{pdi:.1f} adx={adx_val:.1f}"
        return False, f"m15_opposite pdi={pdi:.1f} mdi={mdi:.1f}"
    except Exception as e:
        return False, f"m15_exception {e}"

def get_m1_trend_filter_direction(cfg):
    """
    Trend filter rieng cho Mode M1.
    M1 rat nhieu nen do trend tren M5 bang ADX/DI:
      - ADX M5 >= m1_trend_adx_min
      - |DI+ - DI-| >= m1_trend_di_gap_min
      => BUY/SELL de chi cho vao theo trend.
    Neu khong du manh -> None, Mode M1 quay ve logic goc do->BUY, xanh->SELL.
    """
    try:
        period = 14
        adx_min = float(cfg.get("m1_trend_adx_min", 22.0))
        di_gap_min = float(cfg.get("m1_trend_di_gap_min", 8.0))
        bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M5, 0, 120)
        if bars is None or len(bars) < period + 20:
            return None, 0.0, 0.0
        df = pd.DataFrame(bars).iloc[:-1].reset_index(drop=True)
        if len(df) < period + 20:
            return None, 0.0, 0.0
        high, low, close = df["high"], df["low"], df["close"]
        hl  = high - low
        hcp = (high - close.shift()).abs()
        lcp = (low  - close.shift()).abs()
        tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/period, adjust=False).mean()
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move
        plus_di = 100 * plus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1)
        minus_di = 100 * minus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
        adx = dx.ewm(alpha=1.0/period, adjust=False).mean()
        adx_val = float(adx.iloc[-1])
        plus_val = float(plus_di.iloc[-1])
        minus_val = float(minus_di.iloc[-1])
        gap = abs(plus_val - minus_val)
        if adx_val < adx_min or gap < di_gap_min:
            return None, adx_val, gap
        return ("BUY" if plus_val > minus_val else "SELL"), adx_val, gap
    except Exception as e:
        log(f"get_m1_trend_filter_direction exception: {e}", "warn")
        return None, 0.0, 0.0

# ── Market regime detection (ADX + ATR) ───────────────────────────────────────
def get_market_regime(cfg):
    """
    Phat hien thi truong dang SIDEWAY hay TRENDING.

    Dung ADX(H1, 14) + ATR(H1, 14):
      SIDEWAY:  ADX < adx_max AND ATR < atr_max
      TRENDING: nguoc lai

    Tra ve (regime, adx_val, atr_val, direction):
      regime: "SIDEWAY" / "TRENDING" / None
      direction: "BUY" / "SELL" / None  ([REGIME-DIR] thêm v3.1)
        - "BUY" khi +DI > -DI (phe mua đang thắng)
        - "SELL" khi -DI > +DI (phe bán đang thắng)
        - None khi |+DI - -DI| < di_gap_min (lưỡng lự, treat as no direction)
    """
    try:
        period   = 14
        adx_max  = cfg.get("adx_max", 25.0)
        atr_max  = cfg.get("atr_max", 0)  # 0 = tat check ATR
        di_gap_min = cfg.get("di_gap_min", 3.0)  # gap +DI vs -DI tối thiểu để xác định direction

        bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M15, 0, 100)
        if bars is None or len(bars) < period + 10: return None, 0, 0, None

        df = pd.DataFrame(bars)
        df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < period + 10: return None, 0, 0, None

        high  = df["high"]
        low   = df["low"]
        close = df["close"]

        # True Range
        hl  = high - low
        hcp = (high - close.shift()).abs()
        lcp = (low  - close.shift()).abs()
        tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)

        # ATR (Wilder)
        atr = tr.ewm(alpha=1.0/period, adjust=False).mean()

        # Directional Movement
        up_move   = high.diff()
        down_move = -low.diff()
        plus_dm   = pd.Series(0.0, index=df.index)
        minus_dm  = pd.Series(0.0, index=df.index)
        plus_dm[(up_move > down_move) & (up_move > 0)]   = up_move
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

        # DI smoothed
        plus_di  = 100 * plus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1)
        minus_di = 100 * minus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, 1)

        # ADX
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
        adx = dx.ewm(alpha=1.0/period, adjust=False).mean()

        adx_val = float(adx.iloc[-1])
        atr_val = float(atr.iloc[-1])
        plus_di_val  = float(plus_di.iloc[-1])
        minus_di_val = float(minus_di.iloc[-1])

        # Quyet dinh regime
        is_trending = (adx_val >= adx_max) or (atr_max > 0 and atr_val >= atr_max)
        regime = "TRENDING" if is_trending else "SIDEWAY"

        # [REGIME-DIR] Direction từ +DI vs -DI (chỉ có ý nghĩa khi TRENDING)
        di_gap = abs(plus_di_val - minus_di_val)
        if di_gap < di_gap_min:
            direction = None  # gap nhỏ -> không rõ direction
        elif plus_di_val > minus_di_val:
            direction = "BUY"
        else:
            direction = "SELL"

        return regime, adx_val, atr_val, direction
    except Exception as e:
        log(f"get_market_regime exception: {e}", "warn")
        return None, 0, 0, None

# ── Orders ────────────────────────────────────────────────────────────────────
def open_order(d, lot, cfg):
    """Thread-safe open: serialize with Pair Close so a newly opened ticket is not closed mid-action."""
    with _action_lock:
        tick = mt5.symbol_info_tick(cfg["symbol"])
        if tick is None:
            return None
        otype = mt5.ORDER_TYPE_BUY if d == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if d == "BUY" else tick.bid
        req = {"action":mt5.TRADE_ACTION_DEAL,"symbol":cfg["symbol"],"volume":lot,
               "type":otype,"price":price,"deviation":50,"magic":cfg.get("magic",0),
               "comment":"FollowM1","type_time":mt5.ORDER_TIME_GTC,
               "type_filling":mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            req["type_filling"] = mt5.ORDER_FILLING_FOK
            res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            log(f"ERROR open {d} lot={lot} | {getattr(res,'retcode','?')}", "error")
            return None
        log(f"OPEN {d} {lot} @ {price:.2f} | #{res.order}")
        send({"type":"trade","event":"open","lot":lot,"side":d})
        tg_track_trade("open", d, lot)
        return res

# Lock de tranh dong lenh trong khi main dang dong/mo (race condition)
_action_lock = threading.RLock()  # RLock cho phep nested acquire


def close_pos(pos, cfg):
    """Thread-safe close: avoid race giua main va pair_worker."""
    with _action_lock:
        tick = mt5.symbol_info_tick(cfg["symbol"])
        if tick is None: return False
        # Re-check position van con ton tai (tranh dong trung)
        cur_pos = [p for p in (mt5.positions_get(symbol=cfg["symbol"]) or []) if p.ticket == pos.ticket]
        if not cur_pos:
            return True  # Da bi dong roi (boi thread khac) - coi nhu success
        if pos.type == mt5.POSITION_TYPE_BUY:
            otype, price, side = mt5.ORDER_TYPE_SELL, tick.bid, "BUY"
        else:
            otype, price, side = mt5.ORDER_TYPE_BUY, tick.ask, "SELL"
        req = {"action":mt5.TRADE_ACTION_DEAL,"symbol":cfg["symbol"],"volume":pos.volume,
               "type":otype,"position":pos.ticket,"price":price,"deviation":50,
               "magic":cfg.get("magic",0),"comment":"FollowM1_close",
               "type_time":mt5.ORDER_TIME_GTC,"type_filling":mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            req["type_filling"] = mt5.ORDER_FILLING_FOK
            res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            log(f"ERROR close #{pos.ticket}", "error"); return False
        lvl = "buy" if pos.profit >= 0 else "sell"
        log(f"CLOSE {side} #{pos.ticket} lot={pos.volume} pnl={pos.profit:+.2f}", lvl)
        tg_track_trade("close", side, pos.volume, pos.profit)
        send({"type":"trade","event":"close",
              "ts":datetime.now().strftime("%H:%M:%S"),
              "ticket":pos.ticket,"side":side,"lot":pos.volume,
              "open_price":pos.price_open,"close_price":price,"profit":pos.profit})
        return True

def close_pairs(positions, cfg):
    """Legacy no-op.

    Pair is Close / Pair Min đã bị loại bỏ. Không có caller nào được phép
    đóng từng phần của giỏ qua hàm này; chiến lược thoát lệnh nằm duy nhất ở
    khối BASKET TP trong main loop.
    """
    return 0, None

def _follow_m1_live_dynamic_lock_buffer(pair_min, candidate_positions, cfg):
    """
    Buffer khoa tang CHI cho Follow M1.

    Dung don vi tien te cua tai khoan: lay max giua % Pair Min va chi phi
    truot gia uoc tinh theo tong lot. Cac mode khac (dac biet Trend 1H) tra
    ve dung 1.0 nhu logic cu de khong doi hanh vi cua chung.
    """
    if str(cfg.get("strategy_mode", "Trend 1H")) != "Follow M1":
        return 1.0

    try:
        pair_abs = abs(float(pair_min))
    except Exception:
        pair_abs = 0.0
    try:
        gross_lot = sum(max(0.0, float(getattr(p, "volume", 0.0))) for p in candidate_positions)
    except Exception:
        gross_lot = 0.0
    try:
        pair_pct = max(0.0, float(cfg.get("follow_m1_live_lock_buffer_pair_pct", 0.10)))
    except Exception:
        pair_pct = 0.10
    try:
        slip_ticks = max(0.0, float(cfg.get("follow_m1_live_lock_buffer_slippage_ticks", 2.0)))
    except Exception:
        slip_ticks = 2.0

    tick_value = 0.0
    try:
        info = mt5.symbol_info(cfg["symbol"])
        if info is not None:
            tick_value = max(
                abs(float(getattr(info, "trade_tick_value", 0.0) or 0.0)),
                abs(float(getattr(info, "trade_tick_value_profit", 0.0) or 0.0)),
                abs(float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0)),
            )
    except Exception:
        tick_value = 0.0

    pair_component = pair_abs * pair_pct
    slippage_component = gross_lot * tick_value * slip_ticks
    return round(max(pair_component, slippage_component), 2)


# Pair Close khoa tang cho mode ngan. Trend 1H KHONG dung logic nay.
_SHORT_MULTI_LOSER_TIER_MODES = {"Trend M1", "Follow M1", "Trend M5", "Farm"}


def _uses_short_multi_loser_tiers(cfg):
    """True chi cho mode ngan; Trend 1H giu nguyen khoa tang cu."""
    return str(cfg.get("strategy_mode", "Trend 1H")) in _SHORT_MULTI_LOSER_TIER_MODES



def _select_initial_short_tier_losers(outside_losers, winners_now, pair_min, cfg):
    """
    Chon tang 1 Pair Close cho Follow M1:
      - L1 bat buoc la loser nang nhat cua TOAN BO gio.
      - L2...Ln bat buoc CUNG CHIEU voi L1.
      - Neu gio co tu 2 loser tro len, tang 1 phai gom L1 + it nhat 1 loser
        cung chieu L1. Khong du loser cung chieu thi CHO, khong ghep cheo BUY/SELL.
      - Trong nhom cung chieu, uu tien loser nhe nhat de gom duoc nhieu lenh nhat
        ma van dat Pair Min + buffer.
      - Neu ca gio chi co dung 1 loser thi Pair Close thuong voi L1.

    Tra ve: (selected_losers, lock_buffer, net_after_lock).
    """
    if not outside_losers:
        return [], 0.0, 0.0

    # outside_losers da sort tu am nhat -> L1 luon la loser nang nhat toan gio.
    anchor = outside_losers[0]
    anchor_type = getattr(anchor, "type", None)
    same_side_pool = [p for p in outside_losers if getattr(p, "type", None) == anchor_type]
    winners_pnl = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in winners_now)

    lock_buffer = _follow_m1_live_dynamic_lock_buffer(pair_min, list(winners_now) + [anchor], cfg)
    selected = []
    net_after = 0.0

    # Neu gio co nhieu loser nhung khong co L2 cung chieu L1, khong duoc
    # ghep cross-side. Cho them loser cung chieu hoac thi truong hoi them.
    need_pair = len(outside_losers) >= 2
    if need_pair and len(same_side_pool) < 2:
        net_after = winners_pnl + float(getattr(anchor, "profit", 0.0) or 0.0)
        return [], lock_buffer, net_after

    for _ in range(4):
        threshold = float(pair_min) + float(lock_buffer)
        net_after = winners_pnl + float(getattr(anchor, "profit", 0.0) or 0.0)
        if net_after < threshold:
            return [], lock_buffer, net_after

        selected = [anchor]
        # L2...Ln chi duoc lay trong nhom CUNG CHIEU L1. Chon loser nhe
        # truoc de toi uu so luong loser giai phong trong cung mot tang.
        for candidate in sorted(same_side_pool[1:],
                                key=lambda p: float(getattr(p, "profit", 0.0) or 0.0),
                                reverse=True):
            candidate_pnl = float(getattr(candidate, "profit", 0.0) or 0.0)
            if net_after + candidate_pnl >= threshold:
                selected.append(candidate)
                net_after += candidate_pnl

        if need_pair and len(selected) < 2:
            return [], lock_buffer, net_after

        new_buffer = _follow_m1_live_dynamic_lock_buffer(pair_min, list(winners_now) + list(selected), cfg)
        if abs(new_buffer - lock_buffer) < 0.005:
            lock_buffer = new_buffer
            break
        lock_buffer = new_buffer

    threshold = float(pair_min) + float(lock_buffer)
    net_after = winners_pnl + sum(float(getattr(p, "profit", 0.0) or 0.0) for p in selected)
    if net_after < threshold:
        return [], lock_buffer, net_after
    if need_pair and len(selected) < 2:
        return [], lock_buffer, net_after
    return selected, lock_buffer, net_after


def _select_next_short_tier_loser(outside_losers, net_cum, pair_min, winners_now, locked_pos, cfg):
    """
    Tang 2 tro di: chi them 1 loser, va bat buoc cung chieu L1 cua tang 1.
    Trong nhom cung chieu, uu tien loser am nang nhat co the them vao ma net
    cum van dat Pair Min + buffer. Loser nguoc chieu duoc giu lai, khong bao
    gio chen vao chuoi L1 + L2 + L3... cua Pair Close Follow M1.
    """
    if not locked_pos:
        return None, 0.0, float(net_cum)

    anchor_type = getattr(locked_pos[0], "type", None)
    for candidate in outside_losers:  # da sap am nhat truoc
        if getattr(candidate, "type", None) != anchor_type:
            continue
        buffer = _follow_m1_live_dynamic_lock_buffer(
            pair_min, list(winners_now) + list(locked_pos) + [candidate], cfg
        )
        trial_net = float(net_cum) + float(getattr(candidate, "profit", 0.0) or 0.0)
        if trial_net >= float(pair_min) + float(buffer):
            return candidate, buffer, trial_net
    return None, 0.0, float(net_cum)

def _close_pairs_inner(positions, cfg):
    """Implementation chinh cua close_pairs. Da nam trong _action_lock."""
    losers  = sorted([p for p in positions if p.profit < 0], key=lambda p: p.profit)
    winners = sorted([p for p in positions if p.profit > 0], key=lambda p: p.profit, reverse=True)
    if not losers or not winners: return 0, None

    base_pair_min = cfg.get("pair_min", 1.0)

    # Import time mot lan o dau (dung cho throttle log va deadline checks)
    import time as _time

    # ── FLAG: Bat/tat Smart Cut va Stale Cut ──────────────────────────
    # Smart Cut MOI (chot voi user 30/05/2026):
    #   - CHI hoat dong khi Drawdown >= 40%
    #   - Bo Margin Level - chi tinh theo DD%
    #   - 7 muc deu nhau, moi muc +$50, dai DD 40-70%
    #   - Default: TAT (an toan, bat tay qua GUI checkbox)
    enable_smart_cut = cfg.get("enable_smart_cut", False)  # TAT mac dinh
    enable_stale_cut = cfg.get("enable_stale_cut", False)  # TAT mac dinh

    # ── SMART CUT v3 (theo DD% — pair_min theo % BALANCE) ──────────────────
    # Tham so v3 (chot voi user 30/05/2026):
    #   - Trigger: DD <= -40%
    #   - pair_min = -X% × balance (auto-scale theo size acc)
    #   - 7 muc tu -0.5% den -3.5% balance
    #   Acc $5k -> -$27/-$53/-$80/-$106/-$133/-$160/-$186
    #   Acc $10k -> -$50/-$100/-$150/-$200/-$250/-$300/-$350
    if enable_smart_cut:
        acc = mt5.account_info()
        dd_pct = 0.0
        balance = 0.0
        if acc and acc.balance > 0:
            balance = acc.balance
            float_pnl = (acc.equity - acc.balance)
            dd_pct = (float_pnl / acc.balance) * 100   # se la so AM khi lo

        # Trigger: chi can thiep khi DD <= -40% (lo >= 40%)
        if dd_pct <= -40 and balance > 0:
            if   dd_pct <= -70:  pair_min_pct = -3.5
            elif dd_pct <= -65:  pair_min_pct = -3.0
            elif dd_pct <= -60:  pair_min_pct = -2.5
            elif dd_pct <= -55:  pair_min_pct = -2.0
            elif dd_pct <= -50:  pair_min_pct = -1.5
            elif dd_pct <= -45:  pair_min_pct = -1.0
            else:                pair_min_pct = -0.5  # DD 40-45%

            pair_min = round(balance * pair_min_pct / 100, 2)

            # Throttle log: 1 dong/30s
            now_log = _time.time()
            if not hasattr(close_pairs, '_last_cut_log_t'):
                close_pairs._last_cut_log_t = 0
            if now_log - close_pairs._last_cut_log_t >= 30:
                log(f"[SMART CUT] DD={dd_pct:.1f}% bal={balance:.0f} -> pair_min={pair_min_pct:.1f}%=${pair_min:.2f} (thay vi ${base_pair_min})", "warn")
                close_pairs._last_cut_log_t = now_log
        else:
            pair_min = base_pair_min   # DD < 40% -> chua kich hoat
    else:
        pair_min = base_pair_min   # Smart Cut off -> luon dung pair_min goc

    # ── CACH I: Time-based exit cho lenh stale ──────────────────────────
    if enable_stale_cut:
        now_ts = _time.time()
        stale_hours_1 = cfg.get("stale_hours_1", 6)
        stale_hours_2 = cfg.get("stale_hours_2", 12)
        stale_hours_3 = cfg.get("stale_hours_3", 24)

        oldest_age_h = 0
        for p in positions:
            age_h = (now_ts - p.time) / 3600
            if age_h > oldest_age_h: oldest_age_h = age_h

        if oldest_age_h >= stale_hours_3:
            time_pair_min = -200
            log(f"[STALE] Lenh > {stale_hours_3}h ({oldest_age_h:.1f}h) -> cho cat lo {time_pair_min}", "warn")
        elif oldest_age_h >= stale_hours_2:
            time_pair_min = -50
            log(f"[STALE] Lenh > {stale_hours_2}h ({oldest_age_h:.1f}h) -> cho cat lo {time_pair_min}", "warn")
        elif oldest_age_h >= stale_hours_1:
            time_pair_min = -10
            log(f"[STALE] Lenh > {stale_hours_1}h ({oldest_age_h:.1f}h) -> cho cat lo {time_pair_min}", "warn")
        else:
            time_pair_min = base_pair_min

        # Lay pair_min loi long nhat
        pair_min = min(pair_min, time_pair_min)


    # ── CACH D-V2: LOGIC KHOA TANG (locked losers) ──────────────────────────
    # Logic moi: thay vi dong NGAY khi gom cum >= pair_min, ta:
    #   1. Khoa tang 1: loser nang nhat ghep voi winners >= pair_min
    #   2. Cho gia tang -> khoa tiep tang 2 voi loser nang nhat ke tiep
    #   3. ... cu the cho den khi gia quay dau lam net cum < pair_min
    #   4. Khi net cum < pair_min -> DONG TAT CA (winners + locked losers)
    #
    # Winners luon dong (tat ca lenh duong tai tick do), locked losers co dinh.
    # State luu trong close_pairs._locked_losers (per-process state).

    # Lay state - tickets cua locked losers (kg luu Position obj, vi obj refresh moi tick)
    if not hasattr(close_pairs, "_locked_tickets"):
        close_pairs._locked_tickets = []   # list tickets, theo thu tu khoa
        close_pairs._locked_tang_log_t = 0
    if not hasattr(close_pairs, "_locked_tier_sizes"):
        close_pairs._locked_tier_sizes = []  # chi dung cho mode ngan

    short_tier_mode = _uses_short_multi_loser_tiers(cfg)
    locked_tickets = close_pairs._locked_tickets
    log_throttle_t = close_pairs._locked_tang_log_t

    # Loc locked_tickets: chi giu nhung ticket van con open
    open_ticket_set = {p.ticket for p in positions}
    locked_tickets = [t for t in locked_tickets if t in open_ticket_set]
    if short_tier_mode:
        # Dong bo state ngan: moi tier giu mot nhom loser rieng de rollback
        # khong bao gio tach tung loser ra khoi tang 1.
        close_pairs._locked_tickets = list(locked_tickets)
        tier_sizes = [max(1, int(x)) for x in getattr(close_pairs, "_locked_tier_sizes", [])]
        if sum(tier_sizes) != len(locked_tickets):
            tier_sizes = [len(locked_tickets)] if locked_tickets else []
        close_pairs._locked_tier_sizes = tier_sizes

    # Map ticket -> position object
    pos_by_ticket = {p.ticket: p for p in positions}

    # Tach winners (tat ca lenh duong) va outside_losers (lenh am, chua locked)
    locked_pos = [pos_by_ticket[t] for t in locked_tickets]
    locked_pnl_sum = sum(p.profit for p in locked_pos)
    winners_now = [p for p in positions if p.profit > 0]
    winners_pnl_sum = sum(p.profit for p in winners_now)

    outside_losers = sorted(
        [p for p in positions if p.profit < 0 and p.ticket not in set(locked_tickets)],
        key=lambda p: p.profit  # nang nhat truoc (am nhat)
    )

    # Net cum hien tai
    net_cum = winners_pnl_sum + locked_pnl_sum

    # === CASE A: DA CO TANG (locked_tickets khong rong) ===
    if locked_tickets:
        # Throttle log moi 30s
        now_log = _time.time()
        if now_log - log_throttle_t >= 30:
            log(f"[TANG] Da khoa {len(locked_tickets)} loser, winners={len(winners_now)}(+{winners_pnl_sum:.2f}), "
                f"locked PnL={locked_pnl_sum:+.2f}, net cum={net_cum:+.2f}, pair_min={pair_min}")
            close_pairs._locked_tang_log_t = now_log

        # 1. DONG TAT CA neu net trong khoang [CLOSE_MIN, pair_min) — chot duong an toan
        # Net trong [0, CLOSE_MIN): cho gia hoi (sat nguong, khong dong non)
        # Net <= 0: ROLLBACK tu tang cao xuong thap
        #
        # [MODE TANG CLOSE MIN]
        # Giu nguyen logic KHOA TANG, chi nang san dong non theo tung mode.
        # Ly do: CLOSE_MIN = 1.0 qua mong, de spread/truot gia co the lam lich su nhin nhu dong lo.
        # - Trend 1H giu lenh lau, Pair Min lon -> can dong chac hon.
        # - M5/M1/Farm can Pair Close nhanh hon -> san thap hon H1 de khong lam ket tang.
        strategy_mode_for_tang = cfg.get("strategy_mode", "Trend 1H")
        if pair_min > 0:
            if strategy_mode_for_tang == "Trend 1H":
                tang_close_ratio = float(cfg.get("h1_tang_close_min_ratio", 0.70))
            elif strategy_mode_for_tang == "Trend M5":
                tang_close_ratio = float(cfg.get("m5_tang_close_min_ratio", 0.55))
            elif strategy_mode_for_tang in ("Trend M1", "Follow M1"):
                tang_close_ratio = float(cfg.get("m1_tang_close_min_ratio", 0.45))
            elif strategy_mode_for_tang == "Farm":
                tang_close_ratio = float(cfg.get("farm_tang_close_min_ratio", 0.45))
            else:
                tang_close_ratio = float(cfg.get("tang_close_min_ratio", 0.50))
            tang_close_ratio = max(0.0, min(1.0, tang_close_ratio))
            CLOSE_MIN = max(1.0, pair_min * tang_close_ratio)
        else:
            CLOSE_MIN = 1.0

        # [FIX VONG LAP] Khi pair_min am (Smart/Stale Cut active):
        # Dieu kien dong khac - chap nhan dong cum am theo pair_min cho phep
        if pair_min < 0:
            # Smart/Stale Cut: dong cum khi net >= pair_min (chap nhan lo theo nguong)
            if net_cum >= pair_min:
                log(f"[TANG STALE-CLOSE] Net cum {net_cum:+.2f} >= pair_min {pair_min} "
                    f"(Smart/Stale Cut) -> tim cum xả hop le")
                # Smart/Stale Cut cua short modes: chi dong cum lam |net lot| con lai
                # khong xau hon truoc. Trend 1H bypass helper nay va giu logic cu.
                all_closed = _select_short_mode_pair_close_subset(
                    positions, locked_pos, winners_now, pair_min, cfg, context="SMART-STALE"
                )
                if not all_closed:
                    return 0, None
                locked_ticket_set = {p.ticket for p in locked_pos}
                closed_locked = [p for p in all_closed if p.ticket in locked_ticket_set]
                closed_winners = [p for p in all_closed if p.ticket not in locked_ticket_set]
                actual_net = sum(float(p.profit) for p in all_closed)
                loser_str = " ".join(f"#{p.ticket}({p.profit:+.2f})" for p in closed_locked)
                winner_str = " ".join(f"#{p.ticket}({p.profit:+.2f})" for p in closed_winners)
                log(f"Pair close Smart/Stale: [{loser_str}] + [{winner_str}] = {actual_net:+.2f}")
                ok = True
                for p in all_closed:
                    if not close_pos(p, cfg): ok = False
                if not ok:
                    log(f"[TANG] Co loi close, giu nguyen state", "warn")
                    return 0, None
                close_pairs._locked_tickets = []
                if short_tier_mode:
                    close_pairs._locked_tier_sizes = []
                closed_tickets_set = {p.ticket for p in all_closed}
                deadline = _time.time() + 5.0
                while _time.time() < deadline:
                    current = mt5.positions_get(symbol=cfg.get("symbol"))
                    if current is None: current = []
                    still_open = {p.ticket for p in current} & closed_tickets_set
                    if not still_open: break
                    _time.sleep(0.15)
                ref = max(all_closed, key=lambda p: p.time_msc).price_open
                _mark_pair_close_event(cfg)
                return 1, ref
            else:
                # Net < pair_min am: GIU cum, KHONG khoa them, KHONG unlock
                # Chi cho gia hoi de net >= pair_min thi dong
                return 0, None

        if net_cum < pair_min and net_cum >= CLOSE_MIN:
            log(f"[TANG CLOSE] Net cum {net_cum:+.2f} < pair_min {pair_min} (>= {CLOSE_MIN}) -> "
                f"DONG {len(locked_pos)} locked losers + {len(winners_now)} winners")
            all_closed = _select_short_mode_pair_close_subset(
                positions, locked_pos, winners_now, CLOSE_MIN, cfg, context="TANG-CLOSE")
            if not all_closed:
                return 0, None
            closed_locked_tickets = {p.ticket for p in locked_pos}
            closed_locked = [p for p in all_closed if p.ticket in closed_locked_tickets]
            closed_winners = [p for p in all_closed if p.ticket not in closed_locked_tickets]
            actual_net = sum(p.profit for p in all_closed)
            loser_str  = " ".join(f"#{p.ticket}({p.profit:+.2f})" for p in closed_locked)
            winner_str = " ".join(f"#{p.ticket}({p.profit:+.2f})" for p in closed_winners)
            log(f"Pair close: [{loser_str}] + [{winner_str}] = {actual_net:+.2f}")

            ok = True
            for p in all_closed:
                if not close_pos(p, cfg): ok = False
            if not ok:
                log(f"[TANG] Co loi close, giu nguyen state", "warn")
                return 0, None

            # Reset locked tickets
            close_pairs._locked_tickets = []
            if short_tier_mode:
                close_pairs._locked_tier_sizes = []

            # Race fix: doi MT5 confirm
            closed_tickets_set = {p.ticket for p in all_closed}
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                current = mt5.positions_get(symbol=cfg.get("symbol"))
                if current is None: current = []
                still_open = {p.ticket for p in current} & closed_tickets_set
                if not still_open: break
                _time.sleep(0.15)
            else:
                log(f"[WARN] TANG close timeout 5s, {len(still_open)} ticket chua confirm", "warn")

            ref = max(all_closed, key=lambda p: p.time_msc).price_open
            _mark_pair_close_event(cfg)
            return 1, ref

        # 1b. Net <= 0 (am hoac 0) -> ROLLBACK tu tang cao xuong thap
        # Thay vi bo HET khoa, thu BO TUNG TANG (tu cao nhat) de tim subset duong >= pair_min
        # Khi tim duoc subset duong >= pair_min: DONG subset + winners
        # Cac tang bi BO (tang cao bi rollback): KHONG dong, de lai cho luot sau
        if net_cum <= 0:
            n_locked = len(locked_pos)
            rollback_found = False
            
            # Trend 1H giu rollback cu theo tung loser. Mode ngan rollback theo
            # TANG nguyen ven: tang 1 co the gom nhieu loser va khong duoc tach le.
            if short_tier_mode:
                tier_sizes = list(getattr(close_pairs, "_locked_tier_sizes", []))
                if sum(tier_sizes) != n_locked:
                    tier_sizes = [n_locked]
                    close_pairs._locked_tier_sizes = tier_sizes
                keep_counts = []
                running_count = 0
                for tier_size in tier_sizes:
                    running_count += tier_size
                    keep_counts.append(running_count)
                rollback_keep_counts = list(reversed(keep_counts[:-1]))
            else:
                # Thu giu n_keep tang dau (1 <= n_keep <= n_locked - 1)
                # Bo tu tang cao xuong, dung lai khi tim duoc subset >= pair_min
                rollback_keep_counts = list(range(n_locked - 1, 0, -1))

            for n_keep in rollback_keep_counts:
                subset_locked = locked_pos[:n_keep]
                subset_locked_pnl = sum(p.profit for p in subset_locked)
                subset_net = winners_pnl_sum + subset_locked_pnl
                
                if subset_net >= pair_min:
                    # DONG subset_locked + winners
                    n_dropped = n_locked - n_keep
                    log(f"[TANG ROLLBACK-CLOSE] Net cum {net_cum:+.2f} <= 0 -> "
                        f"Bo {n_dropped} tang cuoi, giu {n_keep} tang dau -> "
                        f"subset net {subset_net:+.2f} >= pair_min {pair_min} -> "
                        f"DONG {n_keep} locked + {len(winners_now)} winners")
                    all_closed = _select_short_mode_pair_close_subset(
                        positions, subset_locked, winners_now, pair_min, cfg, context="TANG-ROLLBACK")
                    if not all_closed:
                        return 0, None
                    dropped_tickets = [p.ticket for p in locked_pos[n_keep:]]
                    subset_ticket_set = {p.ticket for p in subset_locked}
                    closed_locked = [p for p in all_closed if p.ticket in subset_ticket_set]
                    closed_winners = [p for p in all_closed if p.ticket not in subset_ticket_set]
                    actual_net = sum(p.profit for p in all_closed)
                    loser_str = " ".join(f"#{p.ticket}({p.profit:+.2f})" for p in closed_locked)
                    winner_str = " ".join(f"#{p.ticket}({p.profit:+.2f})" for p in closed_winners)
                    dropped_str = " ".join(f"#{t}" for t in dropped_tickets)
                    log(f"Pair close (rollback): [{loser_str}] + [{winner_str}] = {actual_net:+.2f} | "
                        f"De lai (bo khoa): [{dropped_str}]")
                    
                    ok = True
                    for p in all_closed:
                        if not close_pos(p, cfg): ok = False
                    if not ok:
                        log(f"[TANG] Co loi close trong rollback, giu nguyen state", "warn")
                        return 0, None
                    
                    # Reset locked - cac tang bi bo se duoc gom lai luot sau o Case B
                    close_pairs._locked_tickets = []
                    if short_tier_mode:
                        close_pairs._locked_tier_sizes = []
                    
                    closed_tickets_set = {p.ticket for p in all_closed}
                    deadline = _time.time() + 5.0
                    while _time.time() < deadline:
                        current = mt5.positions_get(symbol=cfg.get("symbol"))
                        if current is None: current = []
                        still_open = {p.ticket for p in current} & closed_tickets_set
                        if not still_open: break
                        _time.sleep(0.15)
                    
                    ref = max(all_closed, key=lambda p: p.time_msc).price_open
                    rollback_found = True
                    _mark_pair_close_event(cfg)
                    return 1, ref
            
            # Tat ca subset (tu n_locked-1 ve 1) deu < pair_min
            # -> GIU NGUYEN trang thai khoa, cho gia hoi
            # KHONG unlock (tranh loayhoay khoa-unlock-khoa)
            if not rollback_found:
                now_warn = _time.time()
                if not hasattr(close_pairs, "_rollback_log_t"):
                    close_pairs._rollback_log_t = 0
                if now_warn - close_pairs._rollback_log_t >= 30:
                    log(f"[TANG ROLLBACK-HOLD] Net cum {net_cum:+.2f} <= 0, "
                        f"tat ca {n_locked} subset deu < pair_min {pair_min} -> "
                        f"GIU NGUYEN {n_locked} tang, cho gia hoi")
                    close_pairs._rollback_log_t = now_warn
                return 0, None

        # 1c. Net trong [0, $1): cho gia hoi (sat 0, khong dong, khong bo khoa)
        if net_cum < CLOSE_MIN:
            now_warn = _time.time()
            if not hasattr(close_pairs, "_locked_wait_log_t"):
                close_pairs._locked_wait_log_t = 0
            if now_warn - close_pairs._locked_wait_log_t >= 30:
                log(f"[TANG WAIT] Net cum {net_cum:+.2f} trong [0,{CLOSE_MIN}), cho gia hoi "
                    f"({len(locked_pos)} locked + {len(winners_now)} winners)")
                close_pairs._locked_wait_log_t = now_warn

        # 2. Net >= pair_min -> thu KHOA TANG MOI.
        if outside_losers:
            if short_tier_mode:
                # Tang 2 tro di chi them 1 loser; uu tien loser nang nhat ma van ghep duoc.
                candidate, LOCK_BUFFER, new_net = _select_next_short_tier_loser(
                    outside_losers, net_cum, pair_min, winners_now, locked_pos, cfg
                )
                if candidate is not None:
                    close_pairs._locked_tickets.append(candidate.ticket)
                    close_pairs._locked_tier_sizes.append(1)
                    log(f"[TANG +] Khoa tang {len(close_pairs._locked_tier_sizes)}: "
                        f"+#{candidate.ticket}({candidate.profit:+.2f}) -> net cum {net_cum:+.2f} -> {new_net:+.2f} "
                        f"(buffer={LOCK_BUFFER:.2f}, don vi tai khoan)")
            else:
                # Trend 1H: GIU NGUYEN hoan toan logic cu.
                worst = outside_losers[0]
                new_net = net_cum + worst.profit
                LOCK_BUFFER = _follow_m1_live_dynamic_lock_buffer(
                    pair_min, winners_now + locked_pos + [worst], cfg
                )
                if new_net >= pair_min + LOCK_BUFFER:
                    close_pairs._locked_tickets.append(worst.ticket)
                    log(f"[TANG +] Khoa tang {len(close_pairs._locked_tickets)}: "
                        f"+#{worst.ticket}({worst.profit:+.2f}) -> net cum {net_cum:+.2f} -> {new_net:+.2f} "
                        f"(buffer={LOCK_BUFFER:.2f}, don vi tai khoan)")

        return 0, None  # Da co tang, khong dong, cho tiep

    # === CASE B: CHUA CO TANG (locked rong) - gom tang 1 ===
    if not outside_losers or not winners_now:
        return 0, None

    if short_tier_mode:
        # Mode ngan: Tang 1 phai co L1 (loser nang nhat) va neu gio co >=2
        # loser thi bat buoc ghep them it nhat mot loser khac. Cac loser sau L1
        # duoc chon theo kha nang ghep duoc nhieu lenh nhat, khong theo thu tu lo.
        initial_locked, LOCK_BUFFER, test_net = _select_initial_short_tier_losers(
            outside_losers, winners_now, pair_min, cfg
        )
        if not initial_locked:
            # Log throttle de de debug ma khong spam pair-worker 0.1s.
            now_wait = _time.time()
            if not hasattr(close_pairs, "_initial_tier_wait_log_t"):
                close_pairs._initial_tier_wait_log_t = 0
            if now_wait - close_pairs._initial_tier_wait_log_t >= 30:
                if len(outside_losers) >= 2:
                    l1 = outside_losers[0]
                    same_side_n = sum(1 for p in outside_losers if getattr(p, "type", None) == getattr(l1, "type", None))
                    l1_side = "BUY" if getattr(l1, "type", 0) == getattr(mt5, "POSITION_TYPE_BUY", 0) else "SELL"
                    if same_side_n < 2:
                        log(f"[TANG 1 WAIT] L1 #{l1.ticket}({l1.profit:+.2f}) {l1_side}; "
                            f"chua co L2 loser cung chieu (tong loser={len(outside_losers)}) -> khong ghep cheo BUY/SELL", "warn")
                    else:
                        log(f"[TANG 1 WAIT] Co {len(outside_losers)} loser -> can L1 + it nhat 1 loser CUNG CHIEU L1; "
                            f"winners chua du de khoa cum 2+ loser", "warn")
                else:
                    log(f"[TANG 1 WAIT] Winners chua du de khoa loser duy nhat", "warn")
                close_pairs._initial_tier_wait_log_t = now_wait
            return 0, None

        close_pairs._locked_tickets = [p.ticket for p in initial_locked]
        close_pairs._locked_tier_sizes = [len(initial_locked)]
        loser_str = " ".join(f"#{p.ticket}({p.profit:+.2f})" for p in initial_locked)
        log(f"[TANG 1] Khoa {len(initial_locked)} loser: [{loser_str}] "
            f"+ {len(winners_now)} winners(+{winners_pnl_sum:.2f}) = net {test_net:+.2f} "
            f"(buffer={LOCK_BUFFER:.2f}, don vi tai khoan)")
        return 0, None  # Da khoa, cho gia tang them de khoa tang 2 hoac dong

    # Trend 1H: GIU NGUYEN hoan toan khoa tang cu.
    # Thu khoa tang 1 voi loser nang nhat.
    worst = outside_losers[0]
    LOCK_BUFFER = _follow_m1_live_dynamic_lock_buffer(pair_min, winners_now + [worst], cfg)

    if winners_pnl_sum < pair_min + LOCK_BUFFER:
        return 0, None  # Winners chua du de gom (can >= pair_min + buffer)

    test_net = winners_pnl_sum + worst.profit
    if test_net < pair_min + LOCK_BUFFER:
        return 0, None  # Loser qua nang, cho gia tang them

    # Khoa tang 1
    close_pairs._locked_tickets = [worst.ticket]
    log(f"[TANG 1] Khoa loser nang nhat #{worst.ticket}({worst.profit:+.2f}) "
        f"+ {len(winners_now)} winners(+{winners_pnl_sum:.2f}) = net {test_net:+.2f} "
        f"(buffer={LOCK_BUFFER:.2f}, don vi tai khoan)")

    return 0, None  # Da khoa, cho gia tang them de khoa tang 2 hoac dong

def next_lot(d, sym, cfg, n_same=None, batch_count_override=None):
    """
    Tinh lot cho lenh DCA tiep theo theo TANG LOT THUC TE.

    Fix logic cu: khong tinh lot bang so lenh // batch_count nua, vi khi
    Pair Close/Basket TP dong bot lenh se lam lech so lenh va DCA bi lap lai
    sai tang.

    Logic moi:
      - Neu chua co lenh cung chieu -> base_lot.
      - Lay lot lon nhat dang ton tai cua chieu hien tai.
      - Neu tang lot lon nhat CHUA du batch_count lenh -> mo tiep cung lot do
        de bu du batch.
        VD: chi con 1 lenh 0.10, batch=5 -> mo tiep 0.10 cho den du 5 lenh.
      - Khi tang lot lon nhat da du batch_count -> tang len lot_step tiep theo,
        nhung khong vuot max_lot.
    """
    # Refresh sym de tranh stale data
    fresh = mt5.symbol_info(cfg["symbol"])
    if fresh is not None:
        sym = fresh

    base_lot    = cfg.get("base_lot", 0.01)
    lot_step    = cfg.get("lot_step", 0.0)
    batch_count = max(1, int(batch_count_override if batch_count_override is not None else cfg.get("batch_count", 1)))
    max_lot     = (float(getattr(sym, "volume_max", 100.0) or 100.0)
                   if cfg.get("follow_m1_unlimited_max_lot", False)
                   else cfg.get("max_lot", 100.0))

    same = by_side(my_pos(cfg), d)
    if not same:
        return round_lot(base_lot, sym, cfg)

    # Chuan hoa volume theo round_lot de tranh sai so floating 0.10000000001
    lots = [round_lot(float(p.volume), sym, cfg) for p in same]
    current_max_lot = max(lots)
    current_tier_count = sum(1 for lot in lots if abs(lot - current_max_lot) < 1e-9)

    # Neu tang hien tai chua du batch -> tiep tuc mo cung lot do
    if current_tier_count < batch_count:
        return round_lot(current_max_lot, sym, cfg)

    # Tang hien tai da du batch -> len tang moi, cap max_lot
    next_lot_value = min(current_max_lot + lot_step, max_lot)
    return round_lot(next_lot_value, sym, cfg)




def farm_lots(sym, cfg):
    """
    Farm mode: tinh cap lot hedge theo base_lot.

    Mac dinh:
      base_lot GUI < 0.20 -> dung min 0.20
      extra = base * 0.50
      M1 xanh -> BUY base + SELL base+extra => net SELL extra
      M1 do   -> SELL base + BUY base+extra => net BUY extra

    Vi du base=0.20:
      BUY 0.20 + SELL 0.30 => gross 0.50, net SELL 0.10
    """
    try:
        base_raw = float(cfg.get("base_lot", 0.20))
    except Exception:
        base_raw = 0.20
    try:
        min_base = float(cfg.get("farm_min_base_lot", 0.20))
    except Exception:
        min_base = 0.20
    try:
        extra_ratio = float(cfg.get("farm_extra_ratio", 0.50))
    except Exception:
        extra_ratio = 0.50

    base = max(base_raw, min_base)
    extra = max(base * extra_ratio, 0.0)
    hedge_lot = round_lot(base, sym, cfg)
    strong_lot = round_lot(base + extra, sym, cfg)

    # Neu max_lot qua thap lam lot manh <= hedge, thu tang toi thieu 1 volume_step.
    step = getattr(sym, "volume_step", None) or 0.01
    if strong_lot <= hedge_lot:
        strong_lot = round_lot(hedge_lot + step, sym, cfg)

    net_lot = round(max(0.0, strong_lot - hedge_lot), 2)
    gross_lot = round(hedge_lot + strong_lot, 2)
    return hedge_lot, strong_lot, net_lot, gross_lot, base_raw, min_base, extra_ratio



def farm_recovery_force_decision(positions, sym, cfg):
    """Farm recovery: chi can phan net vuot vung an toan, khong can ve 0 lien tuc."""
    result = {
        "action": "NONE", "force_side": None, "force_lot": 0.0,
        "dd_pct": 0.0, "total_pnl": 0.0, "balance": 0.0,
        "buy_lot": 0.0, "sell_lot": 0.0, "net_lot": 0.0,
        "gross_lot": 0.0, "safe_net": 0.0, "net_excess": 0.0,
        "min_net": 0.0, "level": 0, "emergency": False,
        "force_reason": "", "wait_reason": "",
    }
    if not cfg.get("farm_recovery_force_enabled", True) or not positions:
        return result
    total_pnl, balance, dd_pct = _basket_dd_pct(positions)
    result.update({"dd_pct": dd_pct, "total_pnl": total_pnl, "balance": balance})
    if balance <= 0 or total_pnl >= 0:
        return result
    try: start_pct = max(float(cfg.get("farm_recovery_dd_start_pct", 30.0)), 30.0)
    except Exception: start_pct = 30.0
    try: lvl2_pct = max(float(cfg.get("farm_recovery_dd_level2_pct", 40.0)), 40.0)
    except Exception: lvl2_pct = 40.0
    try: lvl3_pct = max(float(cfg.get("farm_recovery_dd_level3_pct", 50.0)), 50.0)
    except Exception: lvl3_pct = 50.0
    try: stop_pct = max(float(cfg.get("farm_recovery_stop_add_pct", 50.0)), 50.0)
    except Exception: stop_pct = 50.0
    if dd_pct < start_pct:
        return result

    lot_state = _recovery_safe_net_band(positions, cfg)
    net_lot = lot_state["net_lot"]; net_excess = lot_state["net_excess"]
    result.update({**lot_state, "min_net": lot_state["safe_net"]})
    if net_excess <= 0:
        if dd_pct >= stop_pct:
            result.update({"action": "STOP", "level": 4,
                           "wait_reason": "net nam trong vung an toan -> hard hold"})
        return result

    force_side = "SELL" if net_lot > 0 else "BUY"
    allowed_pc, pc_left = _recovery_cooldown_after_pair_close_allowed(cfg, "Farm")
    if not allowed_pc:
        result.update({"action": "WAIT", "level": 4 if dd_pct >= stop_pct else 0,
                       "force_side": force_side, "wait_reason": f"vua Pair Close, recovery cooldown {pc_left:.0f}s"})
        return result
    allowed_spacing, spacing_reason = _recovery_spacing_allowed(cfg, sym, "Farm", force_side, dd_pct)
    if not allowed_spacing:
        result.update({"action": "WAIT", "level": 4 if dd_pct >= stop_pct else 0,
                       "force_side": force_side, "wait_reason": spacing_reason})
        return result

    if dd_pct >= stop_pct:
        level = 4; emergency = True
    elif dd_pct >= lvl3_pct:
        level = 3; emergency = False
    elif dd_pct >= lvl2_pct:
        level = 2; emergency = False
    else:
        level = 1; emergency = False
    _, _, net_extra, _, *_ = farm_lots(sym, cfg)
    if net_extra <= 0:
        return result
    plan = _recovery_force_plan_by_net(net_excess, sym, cfg, level, dd_pct, min_lot=net_extra)
    force_lots = plan.get("force_lots") or []
    if not force_lots:
        return result
    result.update({"action": "FORCE", "force_side": force_side,
                   "force_lot": force_lots[0], "force_lots": force_lots,
                   "force_total_lot": plan.get("total_lot", sum(force_lots)),
                   "force_orders": len(force_lots), "target_factor": plan.get("factor", 0.0),
                   "target_lot": plan.get("target_lot", 0.0), "level": level,
                   "emergency": emergency,
                   "force_reason": "can phan net Farm vuot vung an toan theo DD%"})
    return result

def open_farm_pair(signal_side, sym, cfg, label="FARM"):
    """Open Farm hedge pair atomically; Pair Worker cannot close its first leg before the second leg exists."""
    with _action_lock:
        return _open_farm_pair_impl(signal_side, sym, cfg, label=label)


def _open_farm_pair_impl(signal_side, sym, cfg, label="FARM"):
    """
    Mo cap hedge Farm theo signal M1.

    signal_side la huong NET can trade:
      - signal SELL: mo BUY hedge_lot truoc, sau do SELL strong_lot
      - signal BUY : mo SELL hedge_lot truoc, sau do BUY strong_lot

    Neu lenh thu 2 fail, co gang dong lai lenh hedge vua mo de tranh bi lech sai chieu.
    Return: so lenh mo thanh cong (0 hoac 2; neu rollback fail co the tra 1 va log warning).
    """
    if signal_side not in ("BUY", "SELL"):
        return 0

    before_positions = my_pos(cfg)

    # [FARM RECOVERY FORCE]
    # Khi giỏ Farm đang âm + net lot lệch mạnh, ưu tiên mở 1 lệnh ĐƠN để cân lot.
    # Không mở kèm lệnh ngược chiều, vì lúc này mục tiêu là cứu giỏ cho Pair Close.
    clean_short_dca = bool(cfg.get("short_mode_clean_dca", True))
    rec = (farm_recovery_force_decision(before_positions, sym, cfg)
           if not clean_short_dca else {"action": "NONE"})
    if rec["action"] == "STOP":
        log(f"[FARM RECOVERY STOP] DD={rec['dd_pct']:.1f}% PnL={rec['total_pnl']:+.2f} "
            f">= stop {max(float(cfg.get('farm_recovery_stop_add_pct', 50.0)), 50.0):.1f}% -> chan hedge pair/lenh thuong; net chua lech du de force, "
            f"chi cho Pair Close/Smart Cut xu ly", "warn")
        return -1
    if rec["action"] == "WAIT":
        if float(rec.get("dd_pct", 0.0)) >= max(float(cfg.get("farm_recovery_stop_add_pct", 50.0)), 50.0):
            log(f"[FARM RECOVERY WAIT] DD={rec.get('dd_pct',0):.1f}% | {rec.get('wait_reason','doi recovery')} -> hard hold, khong mo Farm", "warn")
            return -1
        # Duoi hard-stop: bo qua recovery nhung Farm van duoc DCA binh thuong.
    # DD >=50%: tuyet doi khong mo hedge pair thuong. Chi FORCE don can net neu co ke hoach hop le.
    _farm_pnl, _farm_balance, _farm_dd = _basket_dd_pct(before_positions)
    try: _farm_hard_stop = max(float(cfg.get("farm_recovery_stop_add_pct", 50.0)), 50.0)
    except Exception: _farm_hard_stop = 50.0
    if (not clean_short_dca) and _farm_dd >= _farm_hard_stop and rec["action"] != "FORCE":
        log(f"[FARM HARD CONTROL] DD={_farm_dd:.1f}% >= {_farm_hard_stop:.1f}% -> khong mo hedge pair thuong; chi Pair Close/Smart Cut", "warn")
        return -1
    if rec["action"] == "FORCE":
        force_lots = list(rec.get("force_lots") or [rec.get("force_lot", 0.0)])
        force_lots = [float(x) for x in force_lots if float(x) > 0]
        need_slots = len(force_lots)
        if need_slots <= 0:
            return -1
        if not _has_position_slots(len(before_positions), need_slots):
            log(f"[FARM RECOVERY FORCE] Khong du slot: {len(before_positions)}/{_position_limit_label()}, can {need_slots} slot -> skip", "warn")
            return -1
        log(f"[FARM RECOVERY FORCE L{rec['level']}] DD={rec['dd_pct']:.1f}% PnL={rec['total_pnl']:+.2f} | "
            f"BUYlot={rec['buy_lot']:.2f}, SELLlot={rec['sell_lot']:.2f}, net={rec['net_lot']:+.2f} "
            f">= {rec['min_net']:.2f}, factor={rec.get('target_factor',0):.0%}, target={rec.get('target_lot',0):.2f}, "
            f"orders={need_slots}, total={sum(force_lots):.2f} -> open {rec['force_side']} {force_lots} "
            f"de can net, KHONG mo cap hedge", "warn")
        ok_any = False
        for _lot in force_lots:
            if open_order(rec["force_side"], _lot, cfg):
                ok_any = True
                time.sleep(1.0)
            else:
                break
        if ok_any:
            _mark_recovery_event(cfg, sym, "Farm", rec["force_side"], rec.get("dd_pct", 0.0))
        return 1 if ok_any else 0

    # Can du 2 slot vi Farm moi vong mo 2 lenh hedge.
    if not _has_position_slots(len(before_positions), 2):
        log(f"[{label}] Khong du slot: {len(before_positions)}/{_position_limit_label()}, can 2 slot -> skip", "warn")
        return 0

    hedge_lot, strong_lot, net_lot, gross_lot, base_raw, min_base, extra_ratio = farm_lots(sym, cfg)
    if net_lot <= 0:
        log(f"[{label}] Net lot <= 0 (hedge={hedge_lot}, strong={strong_lot}). "
            f"Kiem tra max_lot phai > base_lot. Skip.", "error")
        return 0

    # [FARM NET GUARD] Bao ve rieng cho Farm: tinh theo tong LOT BUY/SELL, khong dem so lenh.
    # Dat o day de moi caller cua open_farm_pair deu duoc bao ve.
    blocked, buy_lot, sell_lot, net_now, max_net, hard_mode = farm_net_imbalance_guard_blocked(before_positions, signal_side, cfg, net_lot)
    if blocked:
        tag = "HARD" if hard_mode else "SOFT"
        log(f"[FARM NET GUARD-{tag}] BUYlot={buy_lot:.2f}, SELLlot={sell_lot:.2f}, "
            f"net={net_now:+.2f}, max_net={max_net:.2f} (scale theo max_lot={float(cfg.get('max_lot', 0)):.2f}) "
            f"-> skip Farm {signal_side}, chi doi chieu doi dien de can net", "warn")
        return -1

    if base_raw < min_base:
        log(f"[{label}] base_lot GUI={base_raw:.2f} < min {min_base:.2f} -> dung hedge base {hedge_lot:.2f}", "warn")

    if signal_side == "SELL":
        # M1 xanh -> net SELL: hedge BUY nho, SELL lon hon
        first_side, first_lot = "BUY", hedge_lot
        second_side, second_lot = "SELL", strong_lot
    else:
        # M1 do -> net BUY: hedge SELL nho, BUY lon hon
        first_side, first_lot = "SELL", hedge_lot
        second_side, second_lot = "BUY", strong_lot

    log(f"[{label}] signal={signal_side} -> open {first_side} {first_lot:.2f} + "
        f"{second_side} {second_lot:.2f} | net {signal_side} {net_lot:.2f}, gross {gross_lot:.2f}", "warn")

    before_tickets = {p.ticket for p in before_positions}
    first_res = open_order(first_side, first_lot, cfg)
    if not first_res:
        return 0

    # Cho terminal cap nhat position thu nhat.
    time.sleep(0.15)
    second_res = open_order(second_side, second_lot, cfg)
    if second_res:
        return 2

    # Lenh thu 2 fail -> rollback lenh hedge vua mo neu tim duoc.
    after_positions = my_pos(cfg)
    new_positions = [p for p in after_positions if p.ticket not in before_tickets]
    if new_positions:
        log(f"[{label}] Lenh {second_side} {second_lot:.2f} fail -> rollback {len(new_positions)} lenh hedge vua mo", "error")
        for p in new_positions:
            close_pos(p, cfg)
        return 0

    log(f"[{label}] Lenh thu 2 fail nhung khong tim thay position moi de rollback", "error")
    return 1


def incomplete_batch_info(d, sym, cfg):
    """
    Kiem tra tang lot lon nhat hien tai da du batch_count chua.

    Neu chua du batch thi tra ve thong tin de FILL NGAY, khong doi
    Adaptive DCA / gia cham trigger.

    Vi du batch=5:
      dang co 3 lenh 0.10 -> missing=2, lot=0.10
      dang co 5 lenh 0.10 -> None, de DCA adaptive mo tang 0.20
    """
    fresh = mt5.symbol_info(cfg["symbol"])
    if fresh is not None:
        sym = fresh

    batch_count = max(1, int(cfg.get("batch_count", 1)))
    same = by_side(my_pos(cfg), d)
    if not same:
        return None

    lots = [round_lot(float(p.volume), sym, cfg) for p in same]
    current_max_lot = max(lots)
    current_tier_count = sum(1 for lot in lots if abs(lot - current_max_lot) < 1e-9)

    if current_tier_count >= batch_count:
        return None

    return {
        "lot": round_lot(current_max_lot, sym, cfg),
        "missing": batch_count - current_tier_count,
        "current_count": current_tier_count,
        "batch_count": batch_count,
    }

def push_status(positions, cached_h1, cfg):
    acc = mt5.account_info()
    if acc is None: return
    # === BOT V3: Doc Session Net + Total Lot tu MT5 history hom nay ===
    hist = get_today_history_stats(cfg)
    
    # [ADAPTIVE-STEP] Lấy step + range_15m hiện tại để gửi lên GUI
    adp_step = None
    adp_range = None
    adp_enabled = bool(cfg.get("adaptive_step_enabled", True))
    if adp_enabled:
        # Gọi 1 lần để đảm bảo cache có sẵn (hàm tự cache 60s)
        try:
            get_volatility_step(cfg)
        except Exception:
            pass
        if hasattr(get_volatility_step, "_cache"):
            c = get_volatility_step._cache
            adp_step = c.get("step")
            adp_range = c.get("range")
    else:
        adp_step = float(cfg.get("dca_step", 5.0))
    
    send({"type":"status",
          "login":acc.login,"balance":acc.balance,"equity":acc.equity,
          "margin":acc.margin,
          "margin_level":acc.margin_level if acc.margin > 0 else 0,
          "currency":acc.currency,
          "symbol":cfg.get("symbol",""),
          "total_pnl":sum(p.profit for p in positions) if positions else 0,
          "total_lot":sum(p.volume for p in positions) if positions else 0,
          # MT5-history based (tu 00:00 hom nay)
          "today_lot":    hist["total_lot"],
          "today_net":    hist["net"],
          "today_wins":   hist["wins"],
          "today_losses": hist["losses"],
          "today_opened": hist["opened"],
          "today_closed": hist["closed"],
          "h1_trend":cached_h1,
          # [ADAPTIVE-STEP] Step + range hiện tại
          "adp_enabled": adp_enabled,
          "adp_step":    adp_step,
          "adp_range":   adp_range,
          "profile":     cfg.get("_follow_m1_live_profile", {}),
          "strategy_mode": cfg.get("strategy_mode", "Follow M1"),
          "positions":[{
              "ticket":p.ticket,"type":"BUY" if p.type==0 else "SELL",
              "lot":p.volume,"open":p.price_open,"current":p.price_current,
              "profit":p.profit,"time":datetime.fromtimestamp(p.time).strftime("%H:%M:%S"),
          } for p in positions]})

# ── Re-entry wait sau khi dong het lenh ──────────────────────────────────────
class ReentryWait:
    """
    Sau khi dong het lenh (khong con lenh nao):
      - Ghi lai ref_price = gia open cua lenh moi nhat vua dong
      - BUY: cho gia ASK <= ref_price trong 10 phut
      - SELL: cho gia BID >= ref_price trong 10 phut
      - Het 10 phut khong khop -> bo qua, vao lenh theo trend H1
    """
    def __init__(self):
        self.active    = False
        self.ref_price = None
        self.deadline  = 0

    def reset(self):
        """Reset trang thai reentry (khi H1 dao chieu)."""
        self.active    = False
        self.ref_price = None
        self.deadline  = 0

    def set(self, ref_price, now, timeout_sec=300):
        self.active    = True
        self.ref_price = ref_price
        self.deadline  = now + timeout_sec

    def check(self, tick, h1_trend, now):
        """
        Tra ve (waiting, reason):
          waiting=True  -> chua duoc mo lenh
          waiting=False -> cho phep mo lenh (gia khop hoac timeout)
        """
        if not self.active: return False, ""

        if now >= self.deadline:
            self.active = False
            return False, "timeout"

        if h1_trend == "BUY":
            if tick.ask <= self.ref_price:
                self.active = False
                return False, f"price reached {tick.ask:.2f} <= {self.ref_price:.2f}"
        elif h1_trend == "SELL":
            if tick.bid >= self.ref_price:
                self.active = False
                return False, f"price reached {tick.bid:.2f} >= {self.ref_price:.2f}"

        remain = int(self.deadline - now)
        cur = tick.ask if h1_trend == "BUY" else tick.bid
        return True, f"waiting | ref={self.ref_price:.2f} cur={cur:.2f} remain={remain}s"

# ── Schedule + Market Close ───────────────────────────────────────────────────
def is_in_pause_window(cfg, now_dt=None):
    """
    Kiem tra co dang trong khung gio PAUSE khong.
    
    Ho tro overnight window: vd [23,55,0,15] = 23:55 hom nay den 00:15 hom sau.
    """
    from datetime import datetime, time as dtime
    if now_dt is None:
        now_dt = datetime.now()
    
    # Khong pause vao T7, CN (de market close logic xu ly)
    weekday = now_dt.weekday()
    if weekday >= 5:
        return False, ""
    
    pause_windows = cfg.get("pause_windows", [])
    cur_time = now_dt.time()
    
    for w in pause_windows:
        if len(w) != 4: continue
        start_t = dtime(w[0], w[1])
        end_t   = dtime(w[2], w[3])
        
        # Window thuong (start < end): vd 19:30 - 22:30
        if start_t < end_t:
            if start_t <= cur_time < end_t:
                return True, f"{w[0]:02d}:{w[1]:02d}-{w[2]:02d}:{w[3]:02d}"
        # Window overnight (start > end): vd 23:55 - 00:15
        else:
            if cur_time >= start_t or cur_time < end_t:
                return True, f"{w[0]:02d}:{w[1]:02d}-{w[2]:02d}:{w[3]:02d} (overnight)"
    
    return False, ""

def is_in_event_pause_window(cfg, now_dt=None):
    """
    Date-aware news pause windows (local machine time, usually VN time).

    cfg["pause_events"] format:
      [
        {"label":"FOMC June", "start":"2026-06-17 23:00", "end":"2026-06-18 05:00"},
      ]

    Dung de thay cho pause ca ngay: FOMC pause truoc tin 2h va sau tin 4h.
    Ho tro window qua ngay moi.
    """
    from datetime import datetime
    if now_dt is None:
        now_dt = datetime.now()

    events = cfg.get("pause_events", []) or []
    for ev in events:
        try:
            start_s = str(ev.get("start", "")).strip()
            end_s   = str(ev.get("end", "")).strip()
            label   = str(ev.get("label", "NEWS")).strip() or "NEWS"
            if not start_s or not end_s:
                continue
            start_dt = datetime.strptime(start_s, "%Y-%m-%d %H:%M")
            end_dt   = datetime.strptime(end_s,   "%Y-%m-%d %H:%M")
            if start_dt <= now_dt < end_dt:
                return True, f"{label} {start_s}->{end_s}"
        except Exception:
            continue
    return False, ""

def is_in_pause_day(cfg, now_dt=None):
    """
    Kiem tra co phai ngay STOP HOAN TOAN khong (NFP/CPI/FOMC).
    
    Cfg "pause_days": list cac ngay format "YYYY-MM-DD" hoac "MM-DD" hoac "DD"
    Vi du: ["2026-06-17", "2026-07-29"] - FOMC
            ["13", "14", "15"]           - CPI moi thang (theo ngay)
            ["2026-06-05"]               - NFP cu the
    """
    from datetime import datetime
    if now_dt is None:
        now_dt = datetime.now()
    
    pause_days = cfg.get("pause_days", [])
    if not pause_days: return False, ""
    
    full_date = now_dt.strftime("%Y-%m-%d")
    md = now_dt.strftime("%m-%d")
    d = now_dt.strftime("%d")
    
    for pd in pause_days:
        pd = str(pd).strip()
        if pd == full_date or pd == md or pd == d:
            return True, pd
    
    return False, ""

def _is_dst_us(dt):
    """
    DST My: tu CN tuan 2 thang 3 -> CN dau thang 11.
    2026: 08/03 (T2) -> 01/11 (CN).
    Don gian: thang 3-10 la DST, thang 11-2 la winter
    (chi sai cau ky cuoi thang 2/dau thang 3 va cuoi thang 10 - khoang 1 tuan moi nam)
    """
    return 3 <= dt.month <= 10

def is_near_gold_close(now_dt=None):
    """
    Exness official: forex/gold dong T6 20:59 UTC (he) hoac 21:59 UTC (dong).
    Doi sang gio VN (GMT+7):
      Mua he (T3-T10):  T7 03:59 sang VN
      Mua dong (T11-T2): T7 04:59 sang VN
    
    Bot canh bao 15 phut truoc gio dong.
    """
    from datetime import datetime
    if now_dt is None:
        now_dt = datetime.now()
    weekday = now_dt.weekday()
    
    is_dst = _is_dst_us(now_dt)
    # Gio dong vang (VN) - khac giua mua he va mua dong
    close_hour = 3 if is_dst else 4   # 03:59 (he) hoac 04:59 (dong)
    warn_hour = close_hour
    warn_minute = 45  # canh bao 15p truoc
    
    # T7: tu warn_time -> 23:59 = gan dong/da dong
    if weekday == 5:
        if now_dt.hour > warn_hour or (now_dt.hour == warn_hour and now_dt.minute >= warn_minute):
            return True
        return False
    
    # CN: van dong
    if weekday == 6: return True
    
    # T2 truoc gio mo: chua mo
    # Gio mo: CN 21:05 UTC = T2 04:05 sang VN (he) hoac T2 05:05 sang VN (dong)
    open_hour = 4 if is_dst else 5
    if weekday == 0 and now_dt.hour < open_hour:
        return True
    
    return False

def is_gold_closed(now_dt=None):
    """
    Kiem tra vang dang DONG hoan toan:
      He:  T7 04:00 -> T2 04:00 VN
      Dong: T7 05:00 -> T2 05:00 VN
    """
    from datetime import datetime
    if now_dt is None:
        now_dt = datetime.now()
    weekday = now_dt.weekday()
    is_dst = _is_dst_us(now_dt)
    close_hour = 4 if is_dst else 5
    open_hour = 4 if is_dst else 5
    
    if weekday == 5 and now_dt.hour >= close_hour: return True
    if weekday == 6: return True
    if weekday == 0 and now_dt.hour < open_hour: return True
    return False

def is_follow_m1_live_weekend_close_hold(now_dt=None):
    """True tu 04:00 sang Thu 7 (gio VN) den luc vang mo lai dau Thu 2.

    Dung cho Follow M1: dong toan bo lenh cua bot dung magic va khoa mo moi
    trong ca cuoi tuan. Khoa lien tuc de bot khong mo lai sau khi da dong luc 04:00.
    """
    from datetime import datetime
    if now_dt is None:
        now_dt = datetime.now()

    weekday = now_dt.weekday()  # T2=0 ... T7=5, CN=6
    if weekday == 5:
        return (now_dt.hour, now_dt.minute) >= (4, 0)
    if weekday == 6:
        return True
    if weekday == 0:
        open_hour = 4 if _is_dst_us(now_dt) else 5
        return now_dt.hour < open_hour
    return False


def is_saturday_4am_vn(now_dt=None):
    """Compatibility helper: True chi trong cua so 04:00-04:05 sang Thu 7 VN."""
    from datetime import datetime
    if now_dt is None:
        now_dt = datetime.now()
    return (now_dt.weekday() == 5 and now_dt.hour == 4 and now_dt.minute <= 5)

def is_in_daily_break(now_dt=None):
    """
    Daily break (rollover) cua vang HANG NGAY:
      Mua he:  20:58 - 22:01 GMT = 03:58 - 05:01 VN
      Mua dong: 21:58 - 23:01 GMT = 04:58 - 06:01 VN

    Trong thoi gian nay spread gian 5-10 lan, de slippage.
    Bot nen PAUSE mo lenh moi (giu lenh cu).

    Chi check trong ngay giao dich (T2-T6 sang T7).
    T7 sau gio dong da co is_gold_closed xu ly roi.
    """
    from datetime import datetime, time as dtime
    if now_dt is None:
        now_dt = datetime.now()
    weekday = now_dt.weekday()
    is_dst = _is_dst_us(now_dt)

    # T7 sang som van co break truoc khi close cuoi tuan
    # CN khong xet (vang dong)
    if weekday == 6: return False

    # Break window theo gio VN
    if is_dst:
        # 03:58 - 05:01 VN
        start = dtime(3, 58)
        end = dtime(5, 1)
    else:
        # 04:58 - 06:01 VN
        start = dtime(4, 58)
        end = dtime(6, 1)

    cur_time = now_dt.time()
    return start <= cur_time <= end

# ── Stop flag ─────────────────────────────────────────────────────────────────
_stop = threading.Event()

def _stdin_watch():
    for line in sys.stdin:
        try:
            if json.loads(line.strip()).get("cmd") == "stop":
                _stop.set(); return
        except Exception: pass

# ── Main loop ─────────────────────────────────────────────────────────────────
def _pair_close_worker(cfg):
    """Legacy no-op: Pair is Close đã bị tắt hoàn toàn."""
    log("[PAIR-WORKER] Disabled — Basket TP only", "info")



# ── FOLLOW M1 AUTO SCALE ─────────────────────────────────────────────────────
# Risk profile is calculated once from MT5 balance when a worker starts.
# Basket TP is then locked for that whole worker session; it does not rescale
# after profits/losses or after any basket close. GUI never exposes lot / TP
# fields for manual editing.
_FOLLOW_M5_REF_BALANCE = 50_000.0
_FOLLOW_M5_REF_BASE_LOT = 0.03
_FOLLOW_M5_REF_PHASE_STEP = 0.02
_FOLLOW_M5_BASKET_TP_RATIO = 0.01  # 1.00% of raw account balance at startup


def _follow_m1_live_is_cent_currency(currency):
    cur = str(currency or "").upper().replace(" ", "")
    return cur in {"USC", "USCENT", "USCENTS", "CENT", "CENTS"}


def follow_m1_live_autoscale_profile(balance, currency=""):
    """Return deterministic Follow M1 risk profile from the MT5 balance.

    Reference point: 50,000 account units reported by MT5 -> 0.03 base / +0.02 phase
    and Basket TP 500 (1% of startup balance). There is no artificial Max Lot:
    order size can keep increasing by phase until the broker's volume_max
    technical limit.
    """
    try:
        raw_balance = max(0.0, float(balance))
    except Exception:
        raw_balance = 0.0

    # Scale by the exact balance MT5 reports so Base Lot and Basket TP use
    # the same account unit and cannot get stuck at 0.01 on USC accounts.
    effective_balance = raw_balance
    lot_scale = effective_balance / _FOLLOW_M5_REF_BALANCE if effective_balance > 0 else 0.0

    # Floors protect very small accounts from unsupported sub-minimum lot values.
    base_lot = max(0.01, _FOLLOW_M5_REF_BASE_LOT * lot_scale)
    phase_step = max(0.01, _FOLLOW_M5_REF_PHASE_STEP * lot_scale)
    # No app-level Max Lot cap. The physical broker limit is added later once
    # symbol_info() is available inside apply_follow_m1_live_autoscale().

    # Basket TP is exactly 1% of the balance reported by MT5 at worker startup.
    # This value is copied into cfg once and remains fixed until this worker stops.
    basket_tp = raw_balance * _FOLLOW_M5_BASKET_TP_RATIO

    return {
        "reference_balance": _FOLLOW_M5_REF_BALANCE,
        "balance_raw": round(raw_balance, 2),
        "balance_effective": round(effective_balance, 2),
        "basket_tp_start_balance": round(raw_balance, 2),
        "basket_tp_ratio": _FOLLOW_M5_BASKET_TP_RATIO,
        "currency": str(currency or ""),
        "is_cent_account": _follow_m1_live_is_cent_currency(currency),
        "base_lot": round(base_lot + 1e-12, 4),
        "phase_step": round(phase_step + 1e-12, 4),
        "max_lot": None,
        "unlimited_max_lot": True,
        "basket_tp": round(basket_tp + 1e-12, 2),
    }


def _follow_m1_live_quantize_lot(value, sym):
    step = float(getattr(sym, "volume_step", 0.01) or 0.01)
    vmin = float(getattr(sym, "volume_min", 0.01) or 0.01)
    vmax = float(getattr(sym, "volume_max", 100.0) or 100.0)
    raw = max(vmin, min(vmax, float(value)))
    quantized = round(round(raw / step) * step, 8)
    return max(vmin, min(vmax, quantized))


def apply_follow_m1_live_autoscale(cfg, sym):
    """Force Follow M1-only config and overwrite all risk settings from balance."""
    acc = mt5.account_info()
    if acc is None:
        return None

    profile = follow_m1_live_autoscale_profile(acc.balance, getattr(acc, "currency", ""))
    base_lot = _follow_m1_live_quantize_lot(profile["base_lot"], sym)
    phase_step = max(float(getattr(sym, "volume_step", 0.01) or 0.01),
                     _follow_m1_live_quantize_lot(profile["phase_step"], sym))
    broker_max_lot = max(base_lot, float(getattr(sym, "volume_max", 100.0) or 100.0))
    broker_max_lot = _follow_m1_live_quantize_lot(broker_max_lot, sym)

    profile["base_lot"] = round(base_lot, 4)
    profile["phase_step"] = round(phase_step, 4)
    profile["max_lot"] = round(broker_max_lot, 4)
    profile["broker_volume_max"] = round(broker_max_lot, 4)
    profile["unlimited_max_lot"] = True

    cfg.update({
        "strategy_mode": "Follow M1",
        "auto_scale_follow_m1_live": True,
        "base_lot": base_lot,
        "lot_step": phase_step,
        # Keep a numeric value for legacy helpers, but it is strictly the
        # broker volume_max—not an AutoScale cap.
        "max_lot": broker_max_lot,
        "follow_m1_unlimited_max_lot": True,
        # Locked once here at worker startup. The main loop only reads cfg["basket_tp"].
        "basket_tp": profile["basket_tp"],
        "basket_tp_start_balance": profile["basket_tp_start_balance"],
        "basket_tp_ratio": profile["basket_tp_ratio"],
        "basket_tp_locked_at_startup": True,
        "basket_tp_only": True,
        "batch_count": 1,
        "batch_delay": 0,
        "dca_cooldown": 0,
        "reentry_wait": 300,
        "adaptive_step_enabled": True,
        "follow_m1_live_phase_lot_enabled": True,
        "follow_m1_live_fast_flip_enabled": False,
        "follow_m1_signal_engine": "M5_SUPERTREND",
        "follow_m1_supertrend_m5_period": 10,
        "follow_m1_supertrend_m5_multiplier": 3.0,
        # Follow M1: khong dung guard Imbalance / Same-side Streak.
        "follow_m1_live_disable_imbalance_streak": True,
        "m1_max_same_side_streak": 0,
        "m1_max_imbalance": 0,
        "m1_hard_imbalance": 0,
        "short_mode_clean_dca": True,
        "short_mode_only_imbalance_streak": True,
        # Dong tat ca position cua dung Magic tu 04:00 sang Thu 7 VN va khoa mo moi den Thu 2.
        "close_saturday_4am": False,
        "enable_smart_cut": False,
        "enable_stale_cut": False,
        "_follow_m1_live_profile": profile,
    })
    log(
        f"[AUTO SCALE] Balance {acc.balance:,.2f} {getattr(acc, 'currency', '')} -> "
        f"Base {base_lot:.2f} | Phase +{phase_step:.2f} | NoCap (broker {broker_max_lot:.2f}) | "
        f"BasketTP {profile['basket_tp']:.2f} (1.00% of startup balance) | "
        f"Close policy: Basket TP only",
        "warn",
    )
    return profile

def run(cfg):
    # Standalone file: hard-force Follow M1, khong co mode nao khac.
    cfg = dict(cfg or {})
    # Xóa khóa cũ nếu file cấu hình trước đây còn lưu Pair Min / Quick Close.
    cfg.pop("pair_min", None)
    cfg.pop("quick_close", None)
    cfg["strategy_mode"] = "Follow M1"
    cfg["auto_scale_follow_m1_live"] = True
    cfg["basket_tp_only"] = True

    # Tao file log truoc tien
    init_log_file(cfg)

    if not init_mt5(cfg):
        send({"type":"exit","reason":"MT5 init failed"})
        close_log_file()
        return

    sym = mt5.symbol_info(cfg["symbol"])
    if sym is None:
        log(f"Symbol {cfg.get('symbol')} khong ton tai sau khi ket noi MT5", "error")
        mt5.shutdown(); close_log_file()
        return

    # Scale trước khi vào vòng lặp; bot chỉ dùng Basket TP để đóng giỏ.
    apply_follow_m1_live_autoscale(cfg, sym)
    reentry = ReentryWait()
    log("[EXIT POLICY] Pair is Close OFF | Quick Close 1/2 OFF | Basket TP only", "warn")

    # Set defaults cho cac key tuy chon
    cfg.setdefault("dca_cooldown", 0)
    cfg.setdefault("dca_step", 2.0)
    cfg.setdefault("basket_tp", 999999)
    cfg.setdefault("reentry_wait", 300)
    cfg.setdefault("base_lot", 0.05)    # 0.05 (truoc 0.10) - chia thanh 2 lenh
    cfg.setdefault("lot_step", 0.0)
    cfg.setdefault("max_lot", 100.0)
    cfg.setdefault("batch_count", 2)    # Mo 2 lenh moi lan (lenh dau + DCA)
    cfg.setdefault("batch_delay", 5)    # Cach 5 giay giua moi lenh trong batch
    cfg.setdefault("strategy_mode", "Trend 1H")  # Trend 1H | Trend M5 | Follow M1 | Trend M1 | Farm
    # Tuong thich config cu: doi ten mode Follow M1 thanh Follow M1.
    if str(cfg.get("strategy_mode", "")) == "Follow M1":
        cfg["strategy_mode"] = "Follow M1"
    # [MODE TANG CLOSE MIN]
    # San dong non theo tung mode de van giu khoa tang nhung tranh dong qua sat loi.
    cfg.setdefault("h1_tang_close_min_ratio", 0.70)    # Trend 1H: dong chac hon
    cfg.setdefault("m5_tang_close_min_ratio", 0.55)    # Trend M5: can bang giua nhanh va an toan
    cfg.setdefault("m1_tang_close_min_ratio", 0.45)    # Trend M1: can Pair Close nhanh hon
    cfg.setdefault("farm_tang_close_min_ratio", 0.45)  # Farm: Pair Min nho, giu toc do dong
    cfg.setdefault("h1_tang_close_min_ratio", 0.70)  # Trend 1H: TANG CLOSE chi dong non khi net >= 70% Pair Min
    cfg.setdefault("m1_max_same_side_streak", 3)  # Trend M1: toi da 3 lenh gan nhat cung chieu, roi doi chieu moi vao tiep
    cfg.setdefault("m1_max_imbalance", 3)  # Trend M1: BUY/SELL lech >= 3 thi chan chieu dang nhieu, chi cho chieu doi dien
    cfg.setdefault("m1_hard_imbalance", 6)  # Trend M1: lech >= 6 coi nhu recovery mode, log canh bao manh hon
    # [M1 RECOVERY FORCE]
    # Khi Trend M1 bi lech BUY/SELL + DD gio lon, khong doi dung mau nen nua.
    # Mo 1 lenh DON theo chieu can bang gio de Pair Close co winner cuu gio.
    cfg.setdefault("m1_recovery_force_enabled", True)
    cfg.setdefault("m1_recovery_dd_start_pct", 30.0)   # DD >= 30% -> force nhe
    cfg.setdefault("m1_recovery_dd_level2_pct", 40.0)  # DD >= 40% -> force manh hon
    cfg.setdefault("m1_recovery_dd_level3_pct", 50.0)  # DD >= 50% -> force manh / emergency
    cfg.setdefault("m1_recovery_stop_add_pct", 50.0)   # DD >= 50% -> emergency immediate / stop lenh thuong neu chua du dieu kien force
    cfg.setdefault("m1_recovery_min_imbalance", cfg.get("m1_hard_imbalance", 6))
    cfg.setdefault("farm_min_base_lot", 0.20)  # Farm: base hedge toi thieu 0.20 lot
    cfg.setdefault("farm_extra_ratio", 0.50)   # Farm: extra net = base_lot * 0.5 (0.20/0.30 -> net 0.10)
    cfg.setdefault("farm_net_guard_enabled", True)  # Farm: guard theo NET LOT BUY/SELL, khong dem so lenh
    cfg.setdefault("farm_net_guard_max_mult", 1.0)  # max_net = max_lot * 1.0 (vd max_lot 0.30 -> max_net 0.30)
    cfg.setdefault("farm_net_guard_hard_mult", 2.0) # hard_net = max_lot * 2.0 (vd max_lot 0.30 -> hard_net 0.60)
    # [FARM RECOVERY FORCE]
    # Khi Farm bi lech net lot + DD gio lon, khong mo cap hedge nua.
    # Mo 1 lenh DON theo chieu can bang net lot de Pair Close co winner cuu gio.
    cfg.setdefault("farm_recovery_force_enabled", True)
    cfg.setdefault("farm_recovery_dd_start_pct", 30.0)  # DD >= 30% -> force nhe
    cfg.setdefault("farm_recovery_dd_level2_pct", 40.0) # DD >= 40% -> force manh hon
    cfg.setdefault("farm_recovery_dd_level3_pct", 50.0) # DD >= 50% -> force manh / emergency
    cfg.setdefault("farm_recovery_stop_add_pct", 50.0)  # DD >= 50% -> emergency immediate / chan hedge pair neu chua du dieu kien force
    cfg.setdefault("farm_recovery_net_mult", 1.0)        # chi force khi |net lot| >= max_lot * mult
    cfg.setdefault("m5_max_imbalance", 8)  # Trend M5 sideway/yeu: BUY/SELL lech >= 8 thi tam dung chieu dang lech
    cfg.setdefault("m5_max_imbalance_trend", 15)  # Trend M1 trend manh: noi nguong cung chieu trend len 15 de bam trend
    # [M5 RECOVERY FORCE]
    # Khac M1: M5 chi force khi M15 trend manh + gio lech NGUOC trend + DD lon.
    # Mo 1 lenh DON theo chieu trend de can gio, khong doi dung mau nen M5.
    cfg.setdefault("m5_recovery_force_enabled", True)
    cfg.setdefault("m5_recovery_dd_start_pct", 30.0)
    cfg.setdefault("m5_recovery_dd_level2_pct", 40.0)
    cfg.setdefault("m5_recovery_dd_level3_pct", 50.0)
    cfg.setdefault("m5_recovery_stop_add_pct", 50.0)  # DD >= 50% -> emergency immediate / stop lenh thuong neu chua du dieu kien force
    cfg.setdefault("m5_recovery_min_imbalance", cfg.get("m5_max_imbalance", 8))
    cfg.setdefault("m5_recovery_net_mult", 1.0)
    # [IMMEDIATE EMERGENCY RECOVERY]
    # Khi DD >= stop_add_pct (mac dinh 50%), M1/M5/Farm duoc mo ngay lenh don can net lot,
    # khong doi nen dong; dung cooldown de khong nhoi theo tick.
    cfg.setdefault("emergency_recovery_immediate_enabled", True)
    cfg.setdefault("emergency_recovery_cooldown_sec", 45.0)
    # [SCALED RECOVERY FORCE]
    # Khi DD sau, bot can gio theo % net lot, khong full hedge 100% ngay lap tuc.
    cfg.setdefault("recovery_factor_dd_30_40", 0.30)     # DD 30-40%: mo ~40% net lech
    cfg.setdefault("recovery_factor_dd_40_50", 0.50)     # DD 40-50%: mo ~60% net lech
    cfg.setdefault("recovery_factor_dd_50_plus", 0.75)   # DD >=50% : mo ~90% net lech
    cfg.setdefault("recovery_max_orders_dd_30_40", 1)    # Moi nhip toi da 2 lenh, moi lenh <= Max Lot
    cfg.setdefault("recovery_max_orders_dd_40_50", 1)    # Moi nhip toi da 3 lenh
    cfg.setdefault("recovery_max_orders_dd_50_plus", 2)  # Moi nhip toi da 5 lenh khi DD rat sau
    cfg.setdefault("recovery_safe_net_min_mult", 2.0)       # band net = it nhat 2*MaxLot
    cfg.setdefault("recovery_safe_net_gross_ratio", 0.25)    # hoac 25% tong gross lot
    cfg.setdefault("recovery_min_adverse_price_step", 3.0)   # can tiep chi khi gia di them 3 gia
    cfg.setdefault("recovery_min_dd_worsen_pct", 3.0)        # hoac DD xau them 3 diem %
    cfg.setdefault("short_mode_dca_min_price_step", 1.5)    # M1/M5/Farm: DCA cung chieu phai cach it nhat 1.5 gia
    cfg.setdefault("recovery_after_pair_close_sec", 120.0)   # Pair Close xong nghi recovery 2 phut
    # [RECOVERY HOLD / DRAIN MODE]
    # Khi gio da gan can lot ma PnL con am, dung DCA/lenh thuong de Pair Close tu xa bot.
    # Ap dung cho cac mode ngan han; KHONG ap dung Trend 1H, KHONG chan Pair Close/Smart Cut.
    cfg.setdefault("recovery_hold_enabled", True)
    cfg.setdefault("recovery_hold_modes", "Trend M5,Follow M1,Trend M1,Farm")
    cfg.setdefault("recovery_hold_net_mult", 1.0)          # net <= 1*MaxLot => HOLD
    cfg.setdefault("recovery_hold_exit_net_mult", 2.0)     # net > 2*MaxLot => cho force can lai
    cfg.setdefault("recovery_hold_start_dd_pct", 50.0)     # Chi HOLD/DRAIN khi DD >= 50%; DD <50 van DCA theo logic mode
    cfg.setdefault("recovery_hold_after_pair_close_sec", 180.0)  # Sau Pair Close nghi 3p neu net chi lech vua
    # [H1 RECOVERY FORCE]
    # Trend 1H KHONG dung logic can lot DDHold; giu Supertrend H1 + Adaptive DCA rieng.
    cfg.setdefault("h1_recovery_force_enabled", False)
    cfg.setdefault("h1_recovery_dd_start_pct", 5.0)
    cfg.setdefault("h1_recovery_dd_level2_pct", 10.0)
    cfg.setdefault("h1_recovery_dd_level3_pct", 15.0)
    cfg.setdefault("h1_recovery_stop_add_pct", 20.0)
    cfg.setdefault("h1_recovery_net_mult", 1.0)
    # [TREND M5 FILTER] Bám trend khi thị trường mạnh: nếu ADX M15 đủ mạnh
    # và DI direction rõ, Mode Trend M5 chỉ vào theo chiều trend.
    # Trend yếu/sideway: giữ logic gốc đỏ->BUY, xanh->SELL.
    cfg.setdefault("m5_trend_filter_enabled", True)
    cfg.setdefault("m5_trend_adx_min", 30.0)
    # [FOLLOW TREND M5] Mode moi: bam trend M5 bang EMA8/21/50 + ADX/DI, vao pullback/xac nhan.
    cfg.setdefault("follow_m1_ema_fast", 8)
    cfg.setdefault("follow_m1_ema_mid", 21)
    cfg.setdefault("follow_m1_ema_slow", 50)
    cfg.setdefault("follow_m1_adx_period", 14)
    cfg.setdefault("follow_m1_adx_min", 18.0)
    cfg.setdefault("follow_m1_di_gap_min", 3.0)
    cfg.setdefault("follow_m1_max_dist_atr", 1.8)
    # [TREND M1 FILTER] M1 nhiễu hơn nên bám trend sớm hơn, đo trend trên M5.
    cfg.setdefault("m1_trend_filter_enabled", False)
    # Follow M1: huong theo Supertrend M5 da dong; clock vao lenh van la M1.
    cfg.setdefault("follow_m1_live_ema_fast", 8)
    cfg.setdefault("follow_m1_live_ema_mid", 21)
    cfg.setdefault("follow_m1_live_ema_slow", 50)
    cfg.setdefault("follow_m1_live_atr_period", 14)
    cfg.setdefault("follow_m1_live_min_ema_sep_atr", 0.08)
    cfg.setdefault("follow_m1_live_fast_flip_enabled", False)
    # Vung dem live EMA: config cu co fast_flip_atr=0.25, key moi nay uu tien 0.15 ATR.
    cfg.setdefault("follow_m1_live_fast_flip_live_atr", 0.15)
    # Lot cua Follow M1 tang theo PHA DAO CHIEU, khong tang theo tung lenh DCA cung chieu.
    # Buffer khoa tang la theo don vi tai khoan va theo Pair Min, khong hard-code $1.
    cfg.setdefault("follow_m1_live_phase_lot_enabled", True)
    cfg.setdefault("follow_m1_unlimited_max_lot", True)
    cfg.setdefault("follow_m1_supertrend_m5_period", 10)
    cfg.setdefault("follow_m1_supertrend_m5_multiplier", 3.0)
    # Follow M1 flip filter (preset CAN BANG):
    # Chi gate khi doi chieu BUY<->SELL, khong lam cham entry cung chieu.
    cfg.setdefault("follow_m1_flip_filter_enabled", True)
    cfg.setdefault("follow_m1_flip_buffer_atr", 0.20)
    cfg.setdefault("follow_m1_flip_m15_di_gap_min", 5.0)
    cfg.setdefault("follow_m1_flip_m15_adx_min", 18.0)
    cfg.setdefault("follow_m1_live_lock_buffer_pair_pct", 0.10)   # 10% |Pair Min|
    cfg.setdefault("follow_m1_live_lock_buffer_slippage_ticks", 2.0)
    cfg.setdefault("m1_trend_adx_min", 22.0)
    cfg.setdefault("m1_trend_di_gap_min", 8.0)
    # [H1-FLIP-HOLD] Khi H1 đảo + giỏ ngược chiều âm > ngưỡng -> giữ + cho DCA chờ hồi
    cfg.setdefault("h1_flip_hold_threshold_pct", 0.0)   # 0.0 = KHÔNG BAO GIỜ chốt lỗ H1-FLIP (mọi PnL âm đều HOLD)
    cfg.setdefault("h1_flip_max_dca", 99999)             # DCA vô hạn trong chế độ hold
    # [ADAPTIVE-STEP] DCA step tự điều chỉnh theo biến động M1 15 phút
    # Phương án Ôn hòa (5/10/17) - nhạy hơn cũ (6/12/20) nhưng vẫn giữ vùng "Yên"
    cfg.setdefault("adaptive_step_enabled", True)
    cfg.setdefault("adaptive_cache_sec",   30)
    # 7p MATCHED-p50 v2: 8 tầng — phản ứng tinh hơn ở vùng biến động cao
    # Bins:  <4 / 4-6 / 6-9 / 9-14 / 14-18 / 18-24 / 24-28 / ≥28
    # Steps: 8  / 10  / 12  / 14   / 17    / 20    / 23    / 26
    cfg.setdefault("adaptive_bin_calm",         4.0)      # <4$    yên
    cfg.setdefault("adaptive_bin_normal",       6.0)      # <6$    bình thường
    cfg.setdefault("adaptive_bin_active",       9.0)      # <9$    động
    cfg.setdefault("adaptive_bin_strong",      14.0)      # <14$   mạnh
    cfg.setdefault("adaptive_bin_extreme",     18.0)      # <18$   rất mạnh
    cfg.setdefault("adaptive_bin_shock",       24.0)      # <24$   cực mạnh
    cfg.setdefault("adaptive_bin_supershock",  28.0)      # <28$   shock
    cfg.setdefault("adaptive_step_calm",        8.0)
    cfg.setdefault("adaptive_step_normal",     10.0)
    cfg.setdefault("adaptive_step_active",     12.0)
    cfg.setdefault("adaptive_step_strong",     14.0)
    cfg.setdefault("adaptive_step_extreme",    17.0)
    cfg.setdefault("adaptive_step_shock",      20.0)
    cfg.setdefault("adaptive_step_supershock", 23.0)
    cfg.setdefault("adaptive_step_max",        26.0)      # ≥28$ siêu shock (FOMC)
    cfg.setdefault("magic", 0)
    cfg.setdefault("st_period", 10)
    cfg.setdefault("st_mult", 3.0)
    cfg.setdefault("adx_max", 999.0)   # 999 = TẮT REGIME guard (cho DCA tự do)
    # CLEAN SHORT MODE (Trend M1 / Follow M1 / Trend M5 / Farm only).
    # DCA theo nen o moi DD; Pair Close khoa tang + Smart Cut xu ly xả gio.
    # Trend 1H khong doc flag nay va giu nguyen logic cu.
    cfg.setdefault("short_mode_clean_dca", True)
    # Follow M1 Supertrend M5: khong co guard Imbalance / Same-side Streak.
    # Cac gate cu nhu Price Spacing, Recovery/Hold, Safe Net van tat cho Follow M1
    # de DCA theo nen M5 va de Pair Close / Smart Cut xu ly gio.
    cfg.setdefault("short_mode_only_imbalance_streak", True)
    if str(cfg.get("strategy_mode", "Trend 1H")) == "Follow M1":
        cfg["short_mode_clean_dca"] = True
        cfg["short_mode_only_imbalance_streak"] = True
        cfg["follow_m1_live_disable_imbalance_streak"] = True
        cfg["m1_max_same_side_streak"] = 0
        cfg["m1_max_imbalance"] = 0
        cfg["m1_hard_imbalance"] = 0
    # Kept only for backwards compatibility when clean mode is manually disabled.
    cfg.setdefault("post_close_net_guard_enabled", False)
    cfg.setdefault("post_close_net_guard_allow_mult", 1.0)
    # Smart/Stale Cut short mode dung Smart Cut thuong, khong Net Guard.
    # Key nay giu lai de tuong thich config cu va mac dinh TAT. Trend 1H giu nguyen.
    cfg.setdefault("smart_cut_net_guard_enabled", False)
    cfg.setdefault("smart_cut_net_guard_max_locked_candidates", 8)
    cfg.setdefault("smart_cut_net_guard_max_locked_per_cut", 3)
    # Smart Cut DD sau cua short modes: thu toi da 5 loser de co the xả
    # BUY nhieu hon SELL (hoac nguoc lai) va GIAM net lot. Trend 1H khong doc key nay.
    cfg.setdefault("smart_cut_net_reduce_first", True)
    cfg.setdefault("smart_cut_net_reduce_max_locked_per_cut", 5)
    cfg.setdefault("smart_cut_net_guard_max_winner_candidates", 32)
    cfg.setdefault("short_mode_lot_guard_dd_start_pct", 30.0)
    cfg.setdefault("recovery_safe_net_min_mult", 2.0)
    cfg.setdefault("recovery_safe_net_gross_ratio", 0.25)
    cfg.setdefault("recovery_after_pair_close_sec", 120.0)
    cfg.setdefault("recovery_min_adverse_price_step", 3.0)
    cfg.setdefault("recovery_min_dd_worsen_pct", 3.0)
    cfg.setdefault("atr_max", 0)       # 0 = tat ATR check
    cfg.setdefault("close_before_market", True)  # Chỉ pause gần giờ đóng, không tự đóng lệnh.
    cfg.setdefault("close_saturday_4am", False)  # Basket TP only: không tự đóng giỏ lúc cuối tuần.
    cfg.setdefault("use_schedule", True)         # True = bat lich TICH CUC
    # BOT V2 - 24/7 KHONG PAUSE theo session; chi pause news event neu khai bao.
    cfg.setdefault("pause_windows", [])

    # === NEWS PAUSE WINDOWS (KHONG STOP CA NGAY) ===
    # FOMC: pause truoc tin 2h va sau tin 4h.
    # Mac dinh theo gio VN/local machine:
    #   FOMC 2:00 PM ET (mua he) = 01:00 VN ngay hom sau -> pause 23:00-05:00.
    #   FOMC 2:00 PM ET (mua dong Dec) = 02:00 VN ngay hom sau -> pause 00:00-06:00.
    cfg.setdefault("pause_events", [
        {"label":"FOMC June",      "start":"2026-06-17 23:00", "end":"2026-06-18 05:00"},
        {"label":"FOMC July",      "start":"2026-07-29 23:00", "end":"2026-07-30 05:00"},
        {"label":"FOMC September", "start":"2026-09-16 23:00", "end":"2026-09-17 05:00"},
        {"label":"FOMC October",   "start":"2026-10-28 23:00", "end":"2026-10-29 05:00"},
        {"label":"FOMC December",  "start":"2026-12-10 00:00", "end":"2026-12-10 06:00"},
    ])

    # Full-day pause mac dinh TAT de tranh bot tat ca ngay vi FOMC.
    # Neu sau nay can stop ca ngay moi bat pause_day_full_enabled=True trong cfg.
    cfg.setdefault("pause_day_full_enabled", False)
    cfg.setdefault("pause_days", [])

    threading.Thread(target=_stdin_watch, daemon=True).start()
    log(f"[BUILD] {BUILD_TAG} | FollowM1=M5-Supertrend(10,3.0,CLOSED-BAR-ONLY)+1order/active-M1+phase-lot(no-app-cap)+dynamic-buffer", "warn")
    log("=== BOT V3 (24/7 + Safety: FOMC pause + NO APP POSITION CAP) ===", "warn")
    # Telegram: bao GUI biet de gom voi cac acc khac (GUI tu gui Telegram)
    try:
        _acc = mt5.account_info()
        if _acc:
            _tg_stats["session_start_t"]       = time.time()
            _tg_stats["session_start_balance"] = _acc.balance
            _tg_stats["hourly_last_t"]         = time.time()
            _tg_stats["hourly_last_balance"]   = _acc.balance
            _tg_stats["daily_last_t"]          = time.time()
            _tg_stats["daily_last_balance"]    = _acc.balance
            send({"type":"tg_event","event":"start",
                  "login":   _acc.login,
                  "balance": _acc.balance,
                  "symbol":  cfg.get("symbol",""),
                  "ts":      time.time()})
    except Exception as _e:
        log(f"[TG] Start fail: {_e}", "warn")

    # [ADAPTIVE-STEP] Banner — 8 tầng MATCHED-p50 v2
    if cfg.get("adaptive_step_enabled", True):
        dca_info = (f"DCA ADAPTIVE("
                    f"{cfg.get('adaptive_step_calm',8)}/{cfg.get('adaptive_step_normal',10)}/"
                    f"{cfg.get('adaptive_step_active',12)}/{cfg.get('adaptive_step_strong',14)}/"
                    f"{cfg.get('adaptive_step_extreme',17)}/{cfg.get('adaptive_step_shock',20)}/"
                    f"{cfg.get('adaptive_step_supershock',23)}/{cfg.get('adaptive_step_max',26)}$ "
                    f"@ r7m {cfg.get('adaptive_bin_calm',4)}/"
                    f"{cfg.get('adaptive_bin_normal',6)}/{cfg.get('adaptive_bin_active',9)}/"
                    f"{cfg.get('adaptive_bin_strong',14)}/{cfg.get('adaptive_bin_extreme',18)}/"
                    f"{cfg.get('adaptive_bin_shock',24)}/{cfg.get('adaptive_bin_supershock',28)}$ "
                    f"cache {cfg.get('adaptive_cache_sec',30)}s)")
    else:
        dca_info = f"DCA fixed {cfg['dca_step']}$"
    
    adx_disp = "OFF" if cfg.get('adx_max',999) >= 100 else cfg.get('adx_max',25)
    sc_disp = "ON(DD>=40%)" if cfg.get("enable_smart_cut", False) else "OFF"
    log(f"Running | MODE={cfg.get('strategy_mode','Trend 1H')} | {cfg['symbol']} | {dca_info} | "
        f"Basket TP {cfg['basket_tp']} | Reentry {cfg['reentry_wait']//60}m | "
        f"Supertrend H1({cfg.get('st_period',10)},{cfg.get('st_mult',3.0)}) | "
        f"REGIME={adx_disp} ATR={cfg.get('atr_max',0) or 'off'} | Emergency=OFF | SmartCut={sc_disp} | "
        f"Exit policy: BASKET TP ONLY")
    if cfg.get("use_schedule", True):
        pw = cfg.get("pause_windows", [])
        pd = cfg.get("pause_days", []) if cfg.get("pause_day_full_enabled", False) else []
        pe = cfg.get("pause_events", [])
        log(f"Schedule ON | Pause windows: {pw} | Pause days(full): {pd if pd else 'OFF'} | News events: {len(pe)}")

    last_log_t = last_status_t = last_dca_t = last_h1_t = 0
    last_m5_dca_bar_time = 0  # Trend M5: moi cua so M5 chi mo toi da 1 lenh
    last_m1_dca_bar_time = 0  # Trend M1/Farm: moi nen M1 da dong chi DCA 1 batch
    last_follow_m1_bar_time = 0  # Follow M1: moi nen M1 dang chay toi da 1 lenh
    # Follow M1: khóa hướng Supertrend theo timestamp của cây M5 ĐÃ ĐÓNG.
    # Trong 5 phút cây M5 đang chạy, hướng này không được thay đổi.
    follow_m1_confirmed_m5_time = None
    follow_m1_confirmed_m5_side = None
    last_immediate_recovery_t = {"Trend 1H": 0.0, "Trend M1": 0.0, "Trend M5": 0.0, "Follow M1": 0.0, "Farm": 0.0}  # DD sau: force ngay, co cooldown

    # [FOLLOW M1 PHASE LOT]
    # Cung mot pha trend: tat ca DCA giu nguyen lot. Chi khi mo THANH CONG
    # o chieu dao nguoc lai moi cong Lot Step. Giỏ rong thi reset Base Lot.
    follow_m1_live_phase = {"initialized": False, "side": None, "lot": None, "index": 0}

    def _follow_m1_live_phase_base_lot():
        return round_lot(float(cfg.get("base_lot", 0.01)), sym, cfg)

    def _follow_m1_live_phase_reset(reason):
        if follow_m1_live_phase["initialized"]:
            log(f"[FOLLOW M1 LOT-PHASE] Reset ve Base Lot {_follow_m1_live_phase_base_lot():.2f} ({reason})", "info")
        follow_m1_live_phase.update({"initialized": False, "side": None, "lot": None, "index": 0})

    def _follow_m1_live_phase_bootstrap(open_positions):
        if follow_m1_live_phase["initialized"] or not open_positions:
            return
        latest_pos = max(open_positions, key=lambda p: getattr(p, "time_msc", getattr(p, "time", 0)))
        side = "BUY" if latest_pos.type == mt5.POSITION_TYPE_BUY else "SELL"
        lot = round_lot(float(latest_pos.volume), sym, cfg)
        base = _follow_m1_live_phase_base_lot()
        try:
            step = max(0.0, float(cfg.get("lot_step", 0.0)))
            idx = max(0, int(round((float(lot) - float(base)) / step))) if step > 0 else 0
        except Exception:
            idx = 0
        follow_m1_live_phase.update({"initialized": True, "side": side, "lot": lot, "index": idx})
        log(f"[FOLLOW M1 LOT-PHASE] Khoi phuc tu gio dang mo: phase={idx}, side={side}, lot={lot:.2f}", "info")

    def _follow_m1_live_phase_preview(signal_side):
        open_positions = my_pos(cfg)
        if not open_positions:
            if follow_m1_live_phase["initialized"]:
                _follow_m1_live_phase_reset("gio da sach")
            return _follow_m1_live_phase_base_lot(), False, "base"

        _follow_m1_live_phase_bootstrap(open_positions)
        active_lot = follow_m1_live_phase["lot"] if follow_m1_live_phase["lot"] is not None else _follow_m1_live_phase_base_lot()
        active_side = follow_m1_live_phase["side"]
        if signal_side == active_side:
            return round_lot(active_lot, sym, cfg), False, f"pha {follow_m1_live_phase['index']} {active_side}"

        try:
            step = max(0.0, float(cfg.get("lot_step", 0.0)))
            if cfg.get("follow_m1_unlimited_max_lot", False):
                max_lot = max(float(active_lot), float(getattr(sym, "volume_max", 100.0) or 100.0))
            else:
                max_lot = max(float(cfg.get("base_lot", active_lot)), float(cfg.get("max_lot", active_lot)))
        except Exception:
            step, max_lot = 0.0, float(active_lot)
        next_phase_lot = round_lot(min(float(active_lot) + step, max_lot), sym, cfg)
        return next_phase_lot, True, f"dao pha {active_side}->{signal_side}"

    def _follow_m1_live_phase_commit(signal_side, opened_lot, is_reversal):
        opened_lot = round_lot(float(opened_lot), sym, cfg)
        if not follow_m1_live_phase["initialized"]:
            follow_m1_live_phase.update({"initialized": True, "side": signal_side, "lot": opened_lot, "index": 0})
            log(f"[FOLLOW M1 LOT-PHASE] Pha 0 {signal_side}: lot={opened_lot:.2f}", "info")
            return
        if is_reversal:
            follow_m1_live_phase["index"] += 1
            follow_m1_live_phase["side"] = signal_side
            follow_m1_live_phase["lot"] = opened_lot
            log(f"[FOLLOW M1 LOT-PHASE] Dao sang pha {follow_m1_live_phase['index']} {signal_side}: "
                f"lot={opened_lot:.2f}", "warn")
        else:
            # Cung pha: giu nguyen state, khong tang Lot Step theo tung lenh DCA.
            follow_m1_live_phase["side"] = signal_side
            follow_m1_live_phase["lot"] = opened_lot
    current_m5_bar_time = None
    fail_count = 0  # Dem so lan open fail lien tiep
    cached_h1         = None
    cached_regime     = None
    cached_regime_dir = None  # [REGIME-DIR] direction từ +DI/-DI
    cached_adx        = 0
    cached_atr        = 0

    # [H1-FLIP-HOLD] State cho logic "H1 đảo + giỏ âm > X% -> giữ + DCA chờ hồi"
    h1_flip_hold_active   = False  # True khi đang ở chế độ "giữ giỏ ngược H1"
    h1_flip_hold_side     = None   # Chiều của giỏ đang giữ (BUY hoặc SELL)
    h1_flip_dca_count     = 0      # Số DCA đã thêm trong chế độ hold

    try:
        while not _stop.is_set():
            now  = time.time()
            tick = mt5.symbol_info_tick(cfg["symbol"])
            if tick is None: time.sleep(0.1); continue

            positions = my_pos(cfg)

            # Cache H1 trend va Market regime moi 60s
            if now - last_h1_t > 60:
                cached_h1 = get_h1_trend(cfg)
                regime, adx_val, atr_val, regime_dir = get_market_regime(cfg)
                cached_regime     = regime
                cached_regime_dir = regime_dir  # [REGIME-DIR]
                cached_adx        = adx_val
                cached_atr        = atr_val
                last_h1_t = now

                # TRENDING - SMART HANDLING:
                # - Dong cac lenh DANG LAI (chot loi)
                # - GIU cac lenh dang LO (cho recovery, khong ban day)
                # - Khong mo lenh moi, khong DCA (logic phia duoi)
                # - Lenh lo se duoc xu ly boi Smart Cut (D+E+I) hoac emergency stop
                #
                # [REGIME-DIR] EXCEPTION: nếu regime_dir CÙNG CHIỀU với H1
                # -> bot tự xử lý bình thường (pair close, basket TP, DCA...)
                # -> chỉ kích hoạt logic đóng winners khi NGƯỢC chiều H1
                if (not BASKET_TP_ONLY and cfg.get("strategy_mode", "Trend 1H") == "Trend 1H"
                        and regime == "TRENDING" and positions):
                    if regime_dir is not None and regime_dir == cached_h1:
                        # [REGIME-DIR] Cùng chiều -> KHÔNG can thiệp, fall through
                        if now - last_log_t > 60:
                            log(f"[REGIME-DIR] TRENDING {regime_dir} cùng chiều H1 "
                                f"(ADX={adx_val:.1f}) -> giữ {len(positions)} lệnh, "
                                f"bot xử lý bình thường", "info")
                            last_log_t = now
                        # KHÔNG continue, để main loop chạy tiếp DCA/pair/basket TP
                    else:
                        # Ngược chiều (hoặc direction không rõ) -> logic cũ
                        winners = [p for p in positions if p.profit > 0]
                        losers = [p for p in positions if p.profit <= 0]

                        if winners:
                            win_pnl = sum(p.profit for p in winners)
                            log(f"[REGIME] TRENDING (ADX={adx_val:.1f}, ATR={atr_val:.1f}, "
                                f"dir={regime_dir}) ngược H1={cached_h1} "
                                f"-> Dong {len(winners)} lenh LAI (+{win_pnl:.2f}), "
                                f"giu {len(losers)} lenh LO de cho recovery", "warn")
                            for p in winners: close_pos(p, cfg)
                            time.sleep(1)
                        elif losers:
                            # Khong co winner - giu tat ca, log canh bao
                            loss_pnl = sum(p.profit for p in losers)
                            if now - last_log_t > 60:
                                log(f"[REGIME] TRENDING (ADX={adx_val:.1f}, ATR={atr_val:.1f}, "
                                    f"dir={regime_dir}) ngược H1={cached_h1} "
                                    f"-> GIU {len(losers)} lenh lo ({loss_pnl:.2f}) cho recovery, "
                                    f"khong mo lenh moi", "warn")
                                last_log_t = now
                        continue

            # Push status moi 3s
            if now - last_status_t > 1:
                push_status(positions, cached_h1, cfg)
                last_status_t = now

            # ──────────────────────────────────────────────────────────────
            # SCHEDULE CHECK + MARKET CLOSE
            # ──────────────────────────────────────────────────────────────
            # Tat ca lich cua Follow M1 su dung gio Viet Nam (UTC+7),
            # khong phu thuoc timezone Windows/VPS.
            from datetime import datetime, timedelta, timezone
            now_dt = (datetime.now(timezone.utc) + timedelta(hours=7)).replace(tzinfo=None)
            
            # 0. FOLLOW M1 WEEKEND HOLD: Basket TP only nên cuối tuần chỉ khóa mở lệnh mới,
            # không tự đóng các vị thế đang giữ.
            if is_follow_m1_live_weekend_close_hold(now_dt):
                if positions and now - last_log_t > 300:
                    log(f"[T7-HOLD] Basket TP only -> giữ {len(positions)} lệnh, không tự đóng cuối tuần", "warn")
                    last_log_t = now
                time.sleep(60); continue

            # 1. DAILY BREAK (chi cho VANG)
            _is_gold = "XAU" in cfg["symbol"].upper()
            if _is_gold and cfg.get("close_before_market", True):
                if is_in_daily_break(now_dt):
                    if now - last_log_t > 300:
                        log(f"[DAILY BREAK] Vang trong gio rollover (spread gian) "
                            f"-> PAUSE mo lenh moi", "warn")
                        last_log_t = now
                    time.sleep(10); continue

            # 1. GOLD MARKET CLOSE (chi cho VANG)
            if _is_gold and cfg.get("close_before_market", True):
                if is_near_gold_close(now_dt):
                    if positions and now - last_log_t > 300:
                        log("[MARKET CLOSE] Basket TP only -> pause mở lệnh mới, giữ nguyên giỏ đang mở", "warn")
                        last_log_t = now

                    if is_gold_closed(now_dt):
                        if now - last_log_t > 300:
                            log(f"[MARKET CLOSE] Thi truong vang dang dong, cho mo cua...", "info")
                            last_log_t = now
                        time.sleep(60); continue
                    else:
                        if now - last_log_t > 60:
                            log(f"[MARKET CLOSE] Trong vung 15p truoc dong, PAUSE", "warn")
                            last_log_t = now
                        time.sleep(5); continue
            
            # 2. NEWS EVENT PAUSE - Pause theo khung gio tin, KHONG stop ca ngay
            # IMPORTANT: Pause chi CHAN LENH MOI / DCA MOI, KHONG dong gio lenh dang am.
            # Pair Close / Smart Cut van duoc phep chay boi pair-worker/main loop truoc block entry.
            if cfg.get("use_schedule", True):
                event_paused, event_label = is_in_event_pause_window(cfg, now_dt)
                if event_paused:
                    if now - last_log_t > 60:
                        log(f"[NEWS PAUSE] Dang pause: {event_label} -> KHONG mo lenh moi/DCA, KHONG dong gio lenh", "warn")
                        last_log_t = now
                    time.sleep(10); continue

                # 2b. Full-day pause chi chay neu bat rieng pause_day_full_enabled=True
                # Pause ca ngay cung KHONG dong gio lenh; chi dung entry/DCA de tranh cat lo cuong buc.
                if cfg.get("pause_day_full_enabled", False):
                    day_paused, day_label = is_in_pause_day(cfg, now_dt)
                    if day_paused:
                        if now - last_log_t > 300:
                            log(f"[SCHEDULE] Pause ca ngay: {day_label} -> KHONG mo lenh moi/DCA, KHONG dong gio lenh", "warn")
                            last_log_t = now
                        time.sleep(60); continue
                
                # 3. SCHEDULE TICH CUC - Khung gio pause hang ngay
                # Khung gio pause hang ngay cung KHONG dong gio lenh; Pair Close/Smart Cut van xu ly.
                win_paused, win_label = is_in_pause_window(cfg, now_dt)
                if win_paused:
                    if now - last_log_t > 60:
                        log(f"[SCHEDULE] Pause window {win_label} -> KHONG mo lenh moi/DCA, KHONG dong gio lenh", "warn")
                        last_log_t = now
                    time.sleep(10); continue

            # EMERGENCY STOP: ĐÃ TẮT theo yêu cầu user (28/05/2026)
            # Bot KHÔNG BAO GIỜ tự cắt lỗ khi DD lớn, để Pair Close tự xử lý
            # if positions:
            #     acc = mt5.account_info()
            #     if acc is not None and acc.balance > 0:
            #         total_pnl = sum(p.profit for p in positions)
            #         dd_pct = (total_pnl / acc.balance) * 100  # % balance
            #         if dd_pct <= -65.0:
            #             log(f"!!! EMERGENCY STOP !!! Drawdown {dd_pct:.1f}% "
            #                 f"(PnL={total_pnl:+.2f} / Balance={acc.balance:.2f})",
            #                 "error")
            #             log(f"Dong tat ca {len(positions)} lenh va STOP bot", "error")
            #             for p in list(positions): close_pos(p, cfg)
            #             time.sleep(2)
            #             _stop.set()
            #             break

            # Lay strategy_mode som de cac logic TP/Pair ben duoi tach rieng mode
            strategy_mode = cfg.get("strategy_mode", "Trend 1H")

            # Không còn ngưỡng đóng sớm. Chỉ Basket TP được phép đóng giỏ.

            # BASKET TP
            if positions:
                total_pnl = sum(p.profit for p in positions)
                if total_pnl >= cfg.get("basket_tp", 999999):
                    if strategy_mode == "Follow M1":
                        # Đóng toàn bộ giỏ trong một action duy nhất khi đạt Basket TP.
                        with _action_lock:
                            fresh_positions = my_pos(cfg)
                            fresh_total = sum(p.profit for p in fresh_positions)
                            if fresh_positions and fresh_total >= cfg.get("basket_tp", 999999):
                                log(f"[TP] BASKET TP! {fresh_total:+.2f} -> dong {len(fresh_positions)} lenh", "buy")
                                for p in list(fresh_positions):
                                    close_pos(p, cfg)
                                positions = my_pos(cfg)
                        positions = my_pos(cfg)
                        if not positions:
                            _follow_m1_live_phase_reset("Basket TP")
                            reentry.reset()
                            log("[Follow M1] Basket TP dong sach gio -> reset phase lot, cho nen M1 moi", "info")
                            time.sleep(1); continue
                    else:
                        log(f"[TP] BASKET TP! {total_pnl:+.2f} -> dong {len(positions)} lenh","buy")
                        # Ref = gia open lenh moi nhat
                        ref = max(positions, key=lambda p: p.time_msc).price_open
                        for p in list(positions): close_pos(p, cfg)
                        time.sleep(0.5)
                        # Verify het lenh truoc khi set reentry
                        if not my_pos(cfg):
                            if strategy_mode == "Trend 1H":
                                wait_sec = cfg.get("reentry_wait", 300)
                                reentry.set(ref, now, wait_sec)
                                log(f"[REENTRY] Cho gia ve {ref:.2f} trong {wait_sec//60} phut", "warn")
                            else:
                                reentry.reset()
                                log(f"[{strategy_mode}] Basket TP dong sach gio -> reset, cho nen moi; khong dung reentry H1", "info")
                        time.sleep(1); continue

            # Pair is Close đã bị loại bỏ. Giữ nguyên toàn bộ vị thế cho đến Basket TP.

            # [RECOVERY HOLD / DRAIN]
            # Neu gio da can lot gan deu va van am, dung lenh thuong/DCA de Pair Close xa bot.
            # Luu y: block nay nam SAU Pair Close, nen Pair Close/Smart Cut khong bi chan.
            if positions:
                hold_rec = recovery_hold_decision(positions, cfg, strategy_mode, now)
                if hold_rec.get("hold"):
                    if now - last_log_t > 15:
                        log(f"[{strategy_mode} RECOVERY HOLD] DD={hold_rec.get('dd_pct',0):.1f}% "
                            f"PnL={hold_rec.get('total_pnl',0):+.2f} | "
                            f"BUYlot={hold_rec.get('buy_lot',0):.2f}, SELLlot={hold_rec.get('sell_lot',0):.2f}, "
                            f"net={hold_rec.get('net_lot',0):+.2f} <= hold {hold_rec.get('hold_net',0):.2f} "
                            f"| {hold_rec.get('reason','')} -> khong DCA/khong force them, chi Pair Close/Smart Cut", "warn")
                        last_log_t = now
                    time.sleep(0.5); continue

            # Strategy mode:
            #   Trend 1H = logic cu: vao lenh theo Supertrend H1
            #   Trend M5 = entry/DCA theo nen M5 da dong gan nhat:
            #              nen do -> BUY, nen xanh -> SELL
            #   Trend M1 = entry/DCA theo nen M1 da dong gan nhat:
            #              nen do -> BUY, nen xanh -> SELL, KHONG bam trend.
            current_m5_bar_time = None
            current_m1_bar_time = None
            current_follow_m1_bar_time = None
            # Follow M1 Fast Flip sets this true only for an intrabar reversal.
            _follow_m1_live_fast = False
            _follow_m1_live_reason = ""
            m5_strong_trend_side = None
            m5_force_precheck_rec = None

            # [IMMEDIATE EMERGENCY RECOVERY]
            # DD >= stop_pct: khong doi nen M1/M5 dong. Tinh net BUY/SELL ngay,
            # mo 1 lenh DON nguoc chieu net lech de Pair Close co winner cuu gio.
            # Co cooldown de tranh nhoi lien tuc theo tick.
            if (positions and strategy_mode in ("Trend M1", "Trend M5", "Follow M1", "Farm")
                    and not cfg.get("short_mode_clean_dca", True) and not cfg.get("short_mode_only_imbalance_streak", True)
                    and cfg.get("emergency_recovery_immediate_enabled", True)):
                try:
                    immediate_cooldown = float(cfg.get("emergency_recovery_cooldown_sec", 45.0))
                except Exception:
                    immediate_cooldown = 45.0
                immediate_cooldown = max(5.0, immediate_cooldown)

                if now - float(last_immediate_recovery_t.get(strategy_mode, 0.0)) >= immediate_cooldown:
                    imm_rec = None
                    imm_stop_pct = 20.0
                    imm_bar_time = None
                    try:
                        if strategy_mode == "Trend 1H":
                            imm_rec = h1_recovery_force_decision(positions, sym, cfg)
                            imm_stop_pct = float(cfg.get("h1_recovery_stop_add_pct", 20.0))
                            imm_bar_time = None
                        elif strategy_mode == "Trend M1":
                            imm_rec = m1_recovery_force_decision(positions, sym, cfg)
                            imm_stop_pct = max(float(cfg.get("m1_recovery_stop_add_pct", 50.0)), 50.0)
                            if strategy_mode == "Follow M1":
                                _, imm_bar_time, _, _, _, _, _ = get_follow_m1_supertrend_m5_signal_info(cfg)
                            else:
                                _, imm_bar_time = get_m1_closed_signal_info(cfg)
                        elif strategy_mode == "Farm":
                            imm_rec = farm_recovery_force_decision(positions, sym, cfg)
                            imm_stop_pct = max(float(cfg.get("farm_recovery_stop_add_pct", 50.0)), 50.0)
                            _, imm_bar_time = get_m1_closed_signal_info(cfg)
                        elif strategy_mode == "Trend M5":
                            _m5_side = None
                            if cfg.get("m5_trend_filter_enabled", True) and not cfg.get("short_mode_only_imbalance_streak", True):
                                try:
                                    _m5_adx_min = float(cfg.get("m5_trend_adx_min", 30.0))
                                except Exception:
                                    _m5_adx_min = 30.0
                                if cached_regime == "TRENDING" and cached_regime_dir in ("BUY", "SELL") and cached_adx >= _m5_adx_min:
                                    _m5_side = cached_regime_dir
                            imm_rec = m5_recovery_force_decision(positions, sym, cfg, _m5_side)
                            imm_stop_pct = max(float(cfg.get("m5_recovery_stop_add_pct", 50.0)), 50.0)
                            _, imm_bar_time = get_m5_closed_signal_info(cfg)
                    except Exception as _e:
                        imm_rec = None
                        if now - last_log_t > 15:
                            log(f"[IMMEDIATE RECOVERY] check exception: {_e}", "warn")
                            last_log_t = now

                    if imm_rec and imm_rec.get("action") == "FORCE" and float(imm_rec.get("dd_pct", 0.0)) >= imm_stop_pct:
                        cur_positions = my_pos(cfg)
                        force_lots = list(imm_rec.get("force_lots") or [imm_rec.get("force_lot", 0.0)])
                        force_lots = [float(x) for x in force_lots if float(x) > 0]
                        need_slots = len(force_lots)
                        if need_slots <= 0:
                            time.sleep(0.1); continue
                        if not _has_position_slots(len(cur_positions), need_slots):
                            log(f"[{strategy_mode} IMMEDIATE RECOVERY] Khong du slot: {len(cur_positions)}/{_position_limit_label()}, "
                                f"can {need_slots} slot -> skip", "warn")
                            last_immediate_recovery_t[strategy_mode] = now
                            time.sleep(0.1); continue

                        log(f"[{strategy_mode} IMMEDIATE RECOVERY L{imm_rec.get('level',4)}] "
                            f"DD={imm_rec.get('dd_pct',0):.1f}% PnL={imm_rec.get('total_pnl',0):+.2f} | "
                            f"BUYlot={imm_rec.get('buy_lot',0):.2f}, SELLlot={imm_rec.get('sell_lot',0):.2f}, "
                            f"net={imm_rec.get('net_lot',0):+.2f}, min_net={imm_rec.get('min_net',0):.2f}, "
                            f"factor={imm_rec.get('target_factor',0):.0%}, target={imm_rec.get('target_lot',0):.2f}, "
                            f"orders={need_slots}, total={sum(force_lots):.2f}, "
                            f"reason={imm_rec.get('force_reason','emergency')} -> open NGAY "
                            f"{imm_rec.get('force_side')} {force_lots}, khong doi nen dong", "warn")

                        ok_any = False
                        for _lot in force_lots:
                            ok = open_order(imm_rec["force_side"], _lot, cfg)
                            if ok:
                                ok_any = True
                                last_dca_t = now
                                fail_count = 0
                                time.sleep(1.0)
                            else:
                                fail_count += 1
                                break
                        last_immediate_recovery_t[strategy_mode] = now
                        if strategy_mode == "Trend M5" and imm_bar_time is not None:
                            last_m5_dca_bar_time = imm_bar_time
                        elif strategy_mode in ("Trend M1", "Farm") and imm_bar_time is not None:
                            last_m1_dca_bar_time = imm_bar_time
                        elif strategy_mode == "Follow M1" and imm_bar_time is not None:
                            last_follow_m1_bar_time = imm_bar_time

                        if ok_any:
                            _mark_recovery_event(cfg, sym, strategy_mode, imm_rec["force_side"], imm_rec.get("dd_pct", 0.0))
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                        else:
                            if fail_count >= 5:
                                acc = mt5.account_info()
                                if acc is None or acc.balance <= 0:
                                    log(f"!!! Balance = {acc.balance if acc else 'N/A'} -> STOP bot", "error")
                                    _stop.set(); break
                                log(f"{strategy_mode} Immediate Recovery open fail {fail_count} lan -> sleep 30s", "warn")
                                time.sleep(30)
                                fail_count = 0
                        time.sleep(0.1); continue

            if strategy_mode == "Trend M5":
                h1, current_m5_bar_time = get_m5_closed_signal_info(cfg)
                if h1 is None:
                    if now - last_log_t > 30:
                        log("Trend M5: nen M5 chua ro/doji, cho...", "warn"); last_log_t = now
                    time.sleep(0.1); continue

                # Trend M5 cu: trend filter chi phu khi clean mode bi tat.
                if cfg.get("m5_trend_filter_enabled", True) and not cfg.get("short_mode_only_imbalance_streak", True):
                    m5_adx_min = float(cfg.get("m5_trend_adx_min", 30.0))
                    strong_trend = (cached_regime == "TRENDING" and
                                    cached_regime_dir in ("BUY", "SELL") and
                                    cached_adx >= m5_adx_min)
                    if strong_trend:
                        m5_strong_trend_side = cached_regime_dir
                    if strong_trend and h1 != cached_regime_dir:
                        m5_force_precheck_rec = m5_recovery_force_decision(my_pos(cfg), sym, cfg, cached_regime_dir)
                        if m5_force_precheck_rec.get("action") == "FORCE":
                            if now - last_log_t > 10:
                                log(f"[M5 TREND-RECOVERY PRECHECK] ADX={cached_adx:.1f}, dir={cached_regime_dir} manh, "
                                    f"signal M5={h1} nguoc trend -> force {cached_regime_dir}", "warn")
                                last_log_t = now
                            h1 = cached_regime_dir
                        else:
                            if now - last_log_t > 30:
                                log(f"[M5 TREND-FILTER] ADX={cached_adx:.1f}, dir={cached_regime_dir} manh "
                                    f"-> bo qua signal M5={h1} nguoc trend", "warn")
                                last_log_t = now
                            if current_m5_bar_time is not None:
                                last_m5_dca_bar_time = current_m5_bar_time
                            time.sleep(0.1); continue

            elif strategy_mode == "Follow M1":
                raw_side, current_follow_m1_bar_time, confirmed_m5_time, _follow_m1_live_reason, closed_m5_flip, st_flip_ok, st_flip_reason = get_follow_m1_supertrend_m5_signal_info(cfg)
                if raw_side is None or confirmed_m5_time is None:
                    if now - last_log_t > 15:
                        log(f"Follow M1: chua co Supertrend M5 da dong ({_follow_m1_live_reason}), cho...", "warn")
                        last_log_t = now
                    time.sleep(0.1); continue

                # HARD LATCH: chỉ được cập nhật hướng khi timestamp của cây M5
                # đã đóng thay đổi. Do đó râu nến/cross tạm thời trong M5 đang
                # chạy không thể tạo flip hoặc thay đổi phase lot.
                if follow_m1_confirmed_m5_time is None:
                    follow_m1_confirmed_m5_time = confirmed_m5_time
                    follow_m1_confirmed_m5_side = raw_side
                    h1 = raw_side
                    _follow_m1_live_fast = False
                    log(f"[FOLLOW M1 M5-ST INIT] Xac nhan {h1} tu M5 da dong #{confirmed_m5_time}; khong doc nen M5 dang chay", "info")
                elif confirmed_m5_time > follow_m1_confirmed_m5_time:
                    previous_side = follow_m1_confirmed_m5_side
                    follow_m1_confirmed_m5_time = confirmed_m5_time
                    if (bool(cfg.get("follow_m1_flip_filter_enabled", True))
                            and previous_side in ("BUY", "SELL")
                            and raw_side in ("BUY", "SELL")
                            and raw_side != previous_side):
                        m15_ok, m15_reason = get_m15_di_confirmation_for_side(cfg, raw_side)
                        if st_flip_ok or m15_ok:
                            follow_m1_confirmed_m5_side = raw_side
                            h1 = raw_side
                            _follow_m1_live_fast = True
                            gate_source = "ST-BUFFER" if st_flip_ok else "M15-DI"
                            log(f"[FOLLOW M1 M5-ST FLIP-OK] {previous_side}->{h1}; "
                                f"M5 #{confirmed_m5_time} da DONG | gate={gate_source} | {st_flip_reason} | {m15_reason}", "warn")
                        else:
                            # Flip yeu -> giu huong cu, khong doi side de tranh whip-saw.
                            follow_m1_confirmed_m5_side = previous_side
                            h1 = previous_side
                            _follow_m1_live_fast = False
                            log(f"[FOLLOW M1 M5-ST FLIP-SKIP] {previous_side}->{raw_side}; "
                                f"M5 #{confirmed_m5_time} da DONG nhung gate fail | {st_flip_reason} | {m15_reason} -> giu {h1}", "warn")
                    else:
                        follow_m1_confirmed_m5_side = raw_side
                        h1 = raw_side
                        _follow_m1_live_fast = (previous_side in ("BUY", "SELL") and previous_side != h1)
                        if _follow_m1_live_fast:
                            log(f"[FOLLOW M1 M5-ST CLOSED FLIP] {previous_side}->{h1}; M5 #{confirmed_m5_time} da DONG xac nhan -> toi da 1 lenh/M1", "warn")
                        else:
                            log(f"[FOLLOW M1 M5-ST NEW CLOSED BAR] M5 #{confirmed_m5_time} da dong, giu {h1}", "info")
                elif confirmed_m5_time == follow_m1_confirmed_m5_time:
                    h1 = follow_m1_confirmed_m5_side
                    _follow_m1_live_fast = False
                else:
                    # Lịch sử MT5 vừa đồng bộ lại/đảo thứ tự: giữ tín hiệu đã
                    # latch thay vì dùng một bar cũ làm đảo chiều giả.
                    h1 = follow_m1_confirmed_m5_side
                    _follow_m1_live_fast = False
                    if now - last_log_t > 30:
                        log(f"[FOLLOW M1 M5-ST LATCH] bo qua M5 cu #{confirmed_m5_time}; giu {h1} tu M5 #{follow_m1_confirmed_m5_time}", "warn")
                        last_log_t = now

                if not _follow_m1_live_fast and now - last_log_t > 15:
                    log(f"[FOLLOW M1 M5-ST] {_follow_m1_live_reason} -> latched={h1}", "info")
                    last_log_t = now

            elif strategy_mode in ("Trend M1", "Farm"):
                h1, current_m1_bar_time = get_m1_closed_signal_info(cfg)
                _m1_logic_note = "do=BUY, xanh=SELL"
                if h1 is None:
                    if now - last_log_t > 15:
                        log(f"{strategy_mode}: nen M1 chua ro/doji, cho...", "warn"); last_log_t = now
                    time.sleep(0.1); continue

            else:
                h1 = cached_h1
                if h1 is None:
                    if now - last_log_t > 30:
                        log("H1 chua ro, cho...","warn"); last_log_t = now
                    time.sleep(0.1); continue

            # [START-WAIT M5/M1]
            # Khi vua START worker, khong vao lenh ngay bang cay nen da dong truoc do.
            # Dat last_*_dca_bar_time = nen da dong gan nhat va CHO NEN MOI DONG.
            # Ap dung cho ca entry dau tien, DCA va batch-fill cua Trend M5/M1/Farm.
            if strategy_mode == "Trend M5" and current_m5_bar_time is not None and last_m5_dca_bar_time == 0:
                last_m5_dca_bar_time = current_m5_bar_time
                if now - last_log_t > 5:
                    log(f"[START-WAIT Trend M5] Bot vua start -> bo qua nen M5 hien tai ({current_m5_bar_time}), cho nen M5 moi", "warn")
                    last_log_t = now
                time.sleep(0.1); continue

            if strategy_mode == "Follow M1" and current_follow_m1_bar_time is not None and last_follow_m1_bar_time == 0:
                last_follow_m1_bar_time = current_follow_m1_bar_time
                if now - last_log_t > 5:
                    log(f"[START-WAIT Follow M1] Bot vua start -> bo qua nen M1 hien tai ({current_follow_m1_bar_time}), cho nen M1 moi", "warn")
                    last_log_t = now
                time.sleep(0.1); continue

            if strategy_mode in ("Trend M1", "Farm") and current_m1_bar_time is not None and last_m1_dca_bar_time == 0:
                last_m1_dca_bar_time = current_m1_bar_time
                if now - last_log_t > 5:
                    log(f"[START-WAIT {strategy_mode}] Bot vua start -> bo qua nen M1 da dong hien tai ({current_m1_bar_time}), cho nen M1 moi dong", "warn")
                    last_log_t = now
                time.sleep(0.1); continue

            # [TREND M5 REVERSE-DCA]
            # Trong mode Trend M5, mỗi nến M5 đóng sẽ lấy MÀU NẾN để quyết định
            # chiều lệnh mới:
            #   - Nến đỏ  -> BUY
            #   - Nến xanh -> SELL
            # Không ép DCA theo chiều giỏ hiện tại. Vì mode này có thể tồn tại
            # cả BUY và SELL cùng lúc, nên phải bỏ qua logic H1-FLIP-HOLD/
            # đóng lệnh ngược chiều kiểu Trend 1H. Pair Close / Basket / Smart Cut
            # vẫn chạy bình thường ở phía trên.

            same = by_side(positions, h1)
            # Lenh nguoc chieu
            opposite = by_side(positions, "SELL" if h1 == "BUY" else "BUY")

            # ═════════════════════════════════════════════════════════════════
            # [H1-FLIP-HOLD] LOGIC MỚI: H1 đảo + giỏ âm > 5% -> GIỮ + DCA chờ hồi
            # ─────────────────────────────────────────────────────────────────
            # User chọn: khi H1 đảo, nếu giỏ ngược chiều đang âm > 5% balance
            # -> KHÔNG đóng, cho DCA thêm tối đa 5 lệnh theo chiều giỏ cũ
            # -> Chờ giá hồi về để thoát hòa/lãi
            # -> Smart Cut (close_pairs) hiện có sẽ tự xử lý nếu DD quá sâu
            # ═════════════════════════════════════════════════════════════════
            if not BASKET_TP_ONLY and strategy_mode == "Trend 1H" and len(opposite) > 0:
                opp_side = "SELL" if h1 == "BUY" else "BUY"
                opp_pnl = sum(p.profit for p in opposite)

                # Tính DD% của giỏ ngược chiều so với balance
                acc = mt5.account_info()
                bal = acc.balance if (acc and acc.balance > 0) else 1.0
                opp_pnl_pct = (opp_pnl / bal) * 100  # âm khi lỗ

                # Ngưỡng kích hoạt chế độ HOLD
                hold_threshold_pct = cfg.get("h1_flip_hold_threshold_pct", 0.0)
                max_hold_dca       = cfg.get("h1_flip_max_dca", 99999)

                if opp_pnl_pct <= hold_threshold_pct:
                    # ── KÍCH HOẠT / DUY TRÌ chế độ HOLD ────────────────────
                    if not h1_flip_hold_active or h1_flip_hold_side != opp_side:
                        # Lần đầu kích hoạt (hoặc đổi chiều giỏ giữ)
                        h1_flip_hold_active = True
                        h1_flip_hold_side   = opp_side
                        h1_flip_dca_count   = 0
                        log(f"[H1-FLIP-HOLD] KÍCH HOẠT - H1 đảo thành {h1}, "
                            f"giỏ {opp_side} đang âm {opp_pnl_pct:.1f}% "
                            f"(PnL={opp_pnl:+.2f}, bal={bal:.0f}) "
                            f"-> GIỮ + cho DCA tối đa {max_hold_dca} lệnh chờ hồi", "warn")

                    # Trong chế độ HOLD: cho phép DCA theo chiều CŨ (opp_side)
                    # nếu chưa đạt limit. Việc DCA sẽ do block DCA phía dưới
                    # xử lý, ở đây chỉ override "h1" để DCA đi theo opp_side.
                    if h1_flip_dca_count < max_hold_dca:
                        if now - last_log_t > 60:
                            log(f"[H1-FLIP-HOLD] Giữ {len(opposite)} lệnh {opp_side} "
                                f"(PnL={opp_pnl:+.2f} = {opp_pnl_pct:.1f}%), "
                                f"DCA {h1_flip_dca_count}/{max_hold_dca}", "info")
                            last_log_t = now
                        # Override: dùng chiều giỏ cũ cho phần còn lại của vòng loop
                        # để DCA logic phía dưới mở thêm lệnh CÙNG CHIỀU giỏ cũ
                        h1   = opp_side
                        same = opposite
                        opposite = []
                        # Fall through xuống DCA/basket TP logic
                    else:
                        # Đã đạt limit DCA -> chỉ giữ, không DCA thêm
                        if now - last_log_t > 60:
                            log(f"[H1-FLIP-HOLD] Đã DCA tối đa {max_hold_dca} lệnh "
                                f"- chỉ giữ {len(opposite)} lệnh {opp_side} "
                                f"chờ hồi (PnL={opp_pnl:+.2f} = {opp_pnl_pct:.1f}%)", "warn")
                            last_log_t = now
                        # KHÔNG đóng, KHÔNG DCA -> ngủ và đợi pair_worker/Smart Cut
                        time.sleep(1); continue
                else:
                    # ── KHÔNG đủ ngưỡng âm -> đóng theo logic CŨ ────────────
                    # (giỏ ngược chiều còn nhẹ, đóng để đi theo trend mới)
                    if h1_flip_hold_active:
                        # Đang HOLD mà PnL đã hồi lên trên ngưỡng -> reset
                        log(f"[H1-FLIP-HOLD] GIẢI PHÓNG - PnL hồi lên {opp_pnl_pct:.1f}% "
                            f"(trên ngưỡng {hold_threshold_pct}%) -> reset state", "info")
                        h1_flip_hold_active = False
                        h1_flip_hold_side   = None
                        h1_flip_dca_count   = 0

                    log(f"[H1 REVERSAL] H1 dao thanh {h1}, dong het {len(opposite)} lenh "
                        f"{opp_side} (PnL={opp_pnl:+.2f} = {opp_pnl_pct:.1f}%) "
                        f"de di theo trend moi (chua đủ ngưỡng hold)", "warn")
                    for p in list(opposite): close_pos(p, cfg)
                    time.sleep(1)
                    reentry.reset()
                    continue
            else:
                # Không còn lệnh ngược chiều -> reset HOLD state
                if h1_flip_hold_active:
                    log(f"[H1-FLIP-HOLD] Giỏ {h1_flip_hold_side} đã đóng hết "
                        f"-> reset state, bot trở về bình thường", "info")
                    h1_flip_hold_active = False
                    h1_flip_hold_side   = None
                    h1_flip_dca_count   = 0

            # Mo lenh DAU TIEN
            # Follow M1 Supertrend M5: even after Pair Close has flattened the
            # basket, never re-enter more than one order in the same active M1.
            if (strategy_mode == "Follow M1" and current_follow_m1_bar_time is not None
                    and current_follow_m1_bar_time <= last_follow_m1_bar_time):
                time.sleep(0.1); continue
            if len(same) == 0:

                # Option B: KHONG mo lenh moi khi TRENDING
                # [REGIME-DIR] EXCEPTION: cho entry nếu regime CÙNG CHIỀU H1
                if strategy_mode == "Trend 1H" and cached_regime == "TRENDING":
                    if cached_regime_dir is not None and cached_regime_dir == h1:
                        # Cùng chiều -> fall through, mở lệnh bình thường
                        if now - last_log_t > 30:
                            log(f"[REGIME-DIR] TRENDING {cached_regime_dir} cùng chiều H1 "
                                f"(ADX={cached_adx:.1f}, ATR={cached_atr:.1f}) "
                                f"-> cho entry", "info")
                            last_log_t = now
                        # KHÔNG continue
                    else:
                        # Ngược chiều hoặc dir không rõ -> chặn entry (logic cũ)
                        if now - last_log_t > 30:
                            log(f"[REGIME] TRENDING (ADX={cached_adx:.1f}, ATR={cached_atr:.1f}, "
                                f"dir={cached_regime_dir}) ngược/lệch H1={h1} "
                                f"-> PAUSE mo lenh moi", "warn")
                            last_log_t = now
                        time.sleep(1); continue

                # Check reentry wait (chi khi khong con lenh nao)
                if strategy_mode == "Trend 1H" and reentry.active and len(positions) == 0:
                    tick_now = mt5.symbol_info_tick(cfg["symbol"])
                    if tick_now is None: time.sleep(1); continue
                    waiting, reason = reentry.check(tick_now, h1, now)
                    if waiting:
                        if now - last_log_t > 15:
                            log(f"[REENTRY] {reason}", "warn")
                            last_log_t = now
                        time.sleep(0.1); continue
                    else:
                        if reason == "timeout":
                            wait_sec = cfg.get("reentry_wait", 300)
                            log(f"[REENTRY] Timeout {wait_sec//60} phut -> mo lenh theo trend", "warn")
                        else:
                            log(f"[REENTRY] Done ({reason}) -> mo lenh", "info")

                # === BOT V3 SAFETY: MAX POSITIONS CHECK ===
                _n_pos = len(my_pos(cfg))
                if _position_limit_reached(_n_pos):
                    if now - last_log_t > 60:
                        log(f"[MAX-POS] Da co {_n_pos} lenh >= {_position_limit_label()} -> SKIP entry moi", "warn")
                        last_log_t = now
                    time.sleep(2); continue

                # BATCH ENTRY: mo nhieu lenh nho cach delay giay
                # [SHORT-MODE DCA PRICE GUARD]
                # Khi gio am, khong nhap them cung chieu neu gia chua di bat loi du xa.
                if strategy_mode in ("Trend M5", "Follow M1", "Trend M1", "Farm") and not cfg.get("short_mode_only_imbalance_streak", True):
                    _spacing_ok, _spacing_reason = _short_mode_dca_price_spacing_allowed(my_pos(cfg), h1, sym, cfg, strategy_mode)
                    if not _spacing_ok:
                        log(f"[DCA PRICE GUARD-{strategy_mode}] {_spacing_reason} -> skip nen nay", "warn")
                        if strategy_mode == "Trend M5":
                            last_m5_dca_bar_time = current_m5_bar_time
                        elif strategy_mode == "Follow M1":
                            last_follow_m1_bar_time = current_follow_m1_bar_time
                        else:
                            last_m1_dca_bar_time = current_m1_bar_time
                        fail_count = 0
                        time.sleep(0.1); continue

                batch_count = cfg.get("batch_count", 2)
                # Short modes bat buoc 1 lenh/nen: tranh Batch N vo tinh nhồi gio.
                if strategy_mode in ("Trend M5", "Follow M1", "Trend M1", "Farm"):
                    batch_count = 1
                batch_delay = cfg.get("batch_delay", 5)
                follow_m1_live_phase_flip = False
                follow_m1_live_phase_note = ""
                if strategy_mode == "Follow M1":
                    lot, follow_m1_live_phase_flip, follow_m1_live_phase_note = _follow_m1_live_phase_preview(h1)
                else:
                    lot = round_lot(cfg.get("base_lot", 0.05), sym, cfg)
                entry_label = "FollowM1" if strategy_mode == "Follow M1" else ("M5" if strategy_mode == "Trend M5" else ("Farm" if strategy_mode == "Farm" else ("M1" if strategy_mode == "Trend M1" else "H1")))
                if strategy_mode == "Farm":
                    hedge_lot, strong_lot, net_lot, gross_lot, _, _, _ = farm_lots(sym, cfg)
                    log(f"Farm={h1}. Mo cap hedge dau {hedge_lot:.2f}/{strong_lot:.2f} "
                        f"| net {net_lot:.2f}, gross {gross_lot:.2f}")
                else:
                    log(f"{entry_label}={h1}. Mo lenh dau (batch {batch_count}x{lot}, cach {batch_delay}s)")

                batch_success = 0
                guard_skip = False
                for bi in range(batch_count):
                    if _stop.is_set(): break
                    # [M5 IMBALANCE GUARD] Neu BUY/SELL lech qua nguong, khong mo them chieu dang lech.
                    if strategy_mode == "Trend M5":
                        cur_positions = my_pos(cfg)
                        blocked, nb, ns, diff, max_imb = m5_imbalance_guard_blocked(cur_positions, h1, cfg, m5_strong_trend_side)
                        if blocked:
                            log(f"[M5 IMBALANCE GUARD] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> bo qua {h1}, cho tin hieu chieu con lai de can bang Pair Close", "warn")
                            break
                    # [M1 IMBALANCE GUARD] Imbalance co uu tien CAO HON Streak.
                    # Khi gio lech BUY/SELL >= nguong, chi chan chieu dang nhieu;
                    # chieu doi dien DUOC phep mo, ke ca 3 lenh moi nhat cung chieu.
                    imbalance_forced_side = False
                    imbalance_nb = imbalance_ns = imbalance_abs = 0
                    if strategy_mode == "Trend M1":
                        cur_positions = my_pos(cfg)
                        blocked, nb, ns, diff, max_imb, hard_mode = m1_imbalance_guard_blocked(cur_positions, h1, cfg)
                        imbalance_nb, imbalance_ns = nb, ns
                        imbalance_abs = abs(nb - ns)
                        if blocked:
                            tag = "HARD" if hard_mode else "SOFT"
                            log(f"[M1 IMBALANCE GUARD-{tag}] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> bo qua {h1}, chi cho chieu doi dien de can bang Pair Close", "warn")
                            break
                        # Neu h1 la chieu doi dien cua gio dang lech >= nguong,
                        # danh dau de bypass Streak o phan ben duoi.
                        if imbalance_abs >= max_imb and (
                            (nb - ns >= max_imb and h1 == "SELL") or
                            (ns - nb >= max_imb and h1 == "BUY")
                        ):
                            imbalance_forced_side = True
                    # [M1 SAME-SIDE GUARD] Chi chan khi gio CHUA lech den nguong Imbalance.
                    if strategy_mode == "Trend M1":
                        cur_positions = my_pos(cfg)
                        blocked, streak_side, streak_count, streak_limit = m1_same_side_guard_blocked(cur_positions, h1, cfg)
                        if blocked:
                            if imbalance_forced_side:
                                log(f"[M1 IMBALANCE PRIORITY] BUY={imbalance_nb}, SELL={imbalance_ns}, "
                                    f"diff={imbalance_abs} -> allow {h1}; bo qua STREAK "
                                    f"{streak_count}x{streak_side} de can bang gio", "warn")
                            else:
                                log(f"[M1 SAME-SIDE GUARD] {streak_count} lenh gan nhat deu {streak_side} "
                                    f">= {streak_limit} -> bo qua {h1}, doi tin hieu nguoc chieu", "warn")
                                break
                    if strategy_mode == "Farm":
                        opened_n = open_farm_pair(h1, sym, cfg, label="FARM-ENTRY")
                        if opened_n > 0:
                            batch_success += opened_n
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                        elif opened_n == -1:
                            guard_skip = True
                        break  # Farm moi nen chi mo 1 cap hedge, khong dung batch_n
                    else:
                        if open_order(h1, lot, cfg):
                            batch_success += 1
                            if strategy_mode == "Follow M1":
                                _follow_m1_live_phase_commit(h1, lot, follow_m1_live_phase_flip)
                            # Push status NGAY de GUI update real-time
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                            # Pair worker thread tu check moi 0.1s -> khong can goi tu day
                    if bi < batch_count - 1:
                        time.sleep(batch_delay)

                if batch_success > 0:
                    last_dca_t = now
                    # Trend M5/M1/Farm: entry da dung nen hien tai, khong DCA tiep tren cung nen nay
                    if strategy_mode == "Trend M5" and current_m5_bar_time is not None:
                        last_m5_dca_bar_time = current_m5_bar_time
                    if strategy_mode == "Follow M1" and current_follow_m1_bar_time is not None:
                        last_follow_m1_bar_time = current_follow_m1_bar_time
                    if strategy_mode in ("Trend M1", "Farm") and current_m1_bar_time is not None:
                        last_m1_dca_bar_time = current_m1_bar_time
                    fail_count = 0
                    log(f"Batch entry: {batch_success}/{batch_count} lenh mo thanh cong")
                elif guard_skip:
                    # Guard skip la dung logic, khong tinh la open fail va khong spam lai cung nen.
                    if strategy_mode == "Trend M5" and current_m5_bar_time is not None:
                        last_m5_dca_bar_time = current_m5_bar_time
                    if strategy_mode == "Follow M1" and current_follow_m1_bar_time is not None:
                        last_follow_m1_bar_time = current_follow_m1_bar_time
                    if strategy_mode in ("Trend M1", "Farm") and current_m1_bar_time is not None:
                        last_m1_dca_bar_time = current_m1_bar_time
                    fail_count = 0
                else:
                    fail_count += 1
                    if fail_count >= 5:
                        acc = mt5.account_info()
                        if acc is None or acc.balance <= 0:
                            log(f"!!! Balance = {acc.balance if acc else 'N/A'} va open fail {fail_count} lan -> STOP bot", "error")
                            _stop.set(); break
                        log(f"Open fail {fail_count} lan lien tiep -> sleep 30s", "warn")
                        time.sleep(30)
                        fail_count = 0
                    else:
                        time.sleep(2)
                time.sleep(0.1); continue

            # DCA
            lat  = latest(same)
            tick = mt5.symbol_info_tick(cfg["symbol"])
            if tick is None: time.sleep(0.1); continue

            # [BATCH-FILL FIX] Neu tang lot hien tai chua du batch, mo NGAY cho du batch.
            # Khong doi Adaptive DCA / trigger gia.
            # VD: batch=5, dang con 3 lenh 0.10 -> mo ngay 2 lenh 0.10,
            # sau khi du 5 lenh moi cho DCA adaptive mo tang 0.20.
            fill = None if strategy_mode in ("Trend M5", "Follow M1", "Trend M1", "Farm") else incomplete_batch_info(h1, sym, cfg)
            if fill and strategy_mode in ("Trend M5", "Follow M1", "Trend M1"):
                _fill_pnl, _fill_bal, _fill_dd = _basket_dd_pct(my_pos(cfg))
                if _fill_dd >= 50.0:
                    log(f"[BATCH-FILL HARD CONTROL] DD={_fill_dd:.1f}% >=50% -> bo batch-fill thuong, chi xu ly recovery/Pair Close", "warn")
                    fill = None
            if fill:
                # [M5 IMBALANCE GUARD] Batch-fill cung bi chan neu chieu fill dang lech qua nguong.
                if strategy_mode == "Trend M5":
                    cur_positions = my_pos(cfg)
                    blocked, nb, ns, diff, max_imb = m5_imbalance_guard_blocked(cur_positions, h1, cfg, m5_strong_trend_side)
                    if blocked:
                        if now - last_log_t > 30:
                            log(f"[M5 IMBALANCE GUARD] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> khong fill them {h1}, doi tin hieu chieu con lai", "warn")
                            last_log_t = now
                        if current_m5_bar_time is not None:
                            last_m5_dca_bar_time = current_m5_bar_time
                        time.sleep(0.1); continue
                # [M1 IMBALANCE GUARD] Batch-fill cung bi chan neu chieu fill dang lech qua nguong.
                if strategy_mode == "Trend M1":
                    cur_positions = my_pos(cfg)
                    blocked, nb, ns, diff, max_imb, hard_mode = m1_imbalance_guard_blocked(cur_positions, h1, cfg)
                    if blocked:
                        if now - last_log_t > 15:
                            tag = "HARD" if hard_mode else "SOFT"
                            log(f"[M1 IMBALANCE GUARD-{tag}] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> khong fill them {h1}, chi cho chieu doi dien", "warn")
                            last_log_t = now
                        if current_m1_bar_time is not None:
                            last_m1_dca_bar_time = current_m1_bar_time
                        time.sleep(0.1); continue
                # [M1 SAME-SIDE GUARD] Batch-fill cung bi chan neu 3 lenh gan nhat da cung chieu.
                if strategy_mode == "Trend M1":
                    cur_positions = my_pos(cfg)
                    blocked, streak_side, streak_count, streak_limit = m1_same_side_guard_blocked(cur_positions, h1, cfg)
                    if blocked:
                        recovery_allowed, nb, ns, imb_abs, recovery_side = m1_recovery_side_allowed(cur_positions, h1, cfg)
                        if recovery_allowed:
                            if now - last_log_t > 15:
                                log(f"[M1 RECOVERY OVERRIDE] BUY={nb}, SELL={ns}, imbalance={imb_abs} "
                                    f"-> allow batch-fill {h1} de can bang gio, bo qua SAME-SIDE GUARD", "warn")
                                last_log_t = now
                        else:
                            if now - last_log_t > 15:
                                log(f"[M1 SAME-SIDE GUARD] {streak_count} lenh gan nhat deu {streak_side} "
                                    f">= {streak_limit} -> khong fill them {h1}, doi tin hieu nguoc chieu", "warn")
                                last_log_t = now
                            if current_m1_bar_time is not None:
                                last_m1_dca_bar_time = current_m1_bar_time
                            time.sleep(0.1); continue
                _n_pos = len(my_pos(cfg))
                if _position_limit_reached(_n_pos):
                    if now - last_log_t > 60:
                        log(f"[MAX-POS FILL] {_n_pos} >= {_position_limit_label()} -> SKIP fill batch", "warn")
                        last_log_t = now
                    time.sleep(1); continue

                can_open = _allowed_open_count(_n_pos, int(fill["missing"]))
                if can_open <= 0:
                    time.sleep(1); continue

                batch_delay = cfg.get("batch_delay", 5)
                lot = fill["lot"]
                log(f"[BATCH-FILL] {h1} tang lot {lot} moi co "
                    f"{fill['current_count']}/{fill['batch_count']} -> mo them {can_open} lenh ngay", "warn")

                fill_success = 0
                for bi in range(can_open):
                    if _stop.is_set(): break
                    if strategy_mode == "Trend M5":
                        cur_positions = my_pos(cfg)
                        blocked, nb, ns, diff, max_imb = m5_imbalance_guard_blocked(cur_positions, h1, cfg, m5_strong_trend_side)
                        if blocked:
                            log(f"[M5 IMBALANCE GUARD] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> dung batch-fill {h1}", "warn")
                            break
                    if strategy_mode == "Trend M1":
                        cur_positions = my_pos(cfg)
                        blocked, nb, ns, diff, max_imb, hard_mode = m1_imbalance_guard_blocked(cur_positions, h1, cfg)
                        if blocked:
                            tag = "HARD" if hard_mode else "SOFT"
                            log(f"[M1 IMBALANCE GUARD-{tag}] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> dung batch-fill {h1}", "warn")
                            break
                        blocked, streak_side, streak_count, streak_limit = m1_same_side_guard_blocked(cur_positions, h1, cfg)
                        if blocked:
                            recovery_allowed, nb, ns, imb_abs, recovery_side = m1_recovery_side_allowed(cur_positions, h1, cfg)
                            if recovery_allowed and not cfg.get("short_mode_clean_dca", True) and not cfg.get("short_mode_only_imbalance_streak", True):
                                log(f"[M1 RECOVERY OVERRIDE] BUY={nb}, SELL={ns}, imbalance={imb_abs} "
                                    f"-> allow batch-fill {h1} de can bang gio, bo qua SAME-SIDE GUARD", "warn")
                            else:
                                log(f"[M1 SAME-SIDE GUARD] {streak_count} lenh gan nhat deu {streak_side} "
                                    f">= {streak_limit} -> dung batch-fill {h1}", "warn")
                                break
                    if open_order(h1, lot, cfg):
                        fill_success += 1
                        try: push_status(my_pos(cfg), cached_h1, cfg)
                        except: pass
                        last_status_t = now
                    if bi < can_open - 1:
                        time.sleep(batch_delay)

                if fill_success > 0:
                    fail_count = 0
                    log(f"[BATCH-FILL] Done {fill_success}/{can_open} lenh lot={lot}")
                else:
                    fail_count += 1
                    if fail_count >= 5:
                        acc = mt5.account_info()
                        if acc is None or acc.balance <= 0:
                            log(f"!!! Balance = {acc.balance if acc else 'N/A'} -> STOP bot", "error")
                            _stop.set(); break
                        log(f"Batch-fill open fail {fail_count} lan -> sleep 30s", "warn")
                        time.sleep(30)
                        fail_count = 0
                    else:
                        time.sleep(2)
                time.sleep(0.1); continue

            # [TREND M5/M1 DCA] Mode Trend M5/M1/Farm: mỗi nến đóng mở 1 batch theo
            # tín hiệu nến vừa đóng, KHÔNG dùng Adaptive DCA:
            #   - Nến đỏ  -> BUY
            #   - Nến xanh -> SELL
            # Batch-fill ở trên vẫn được ưu tiên trước để đủ batch của đúng chiều/tầng lot.
            if strategy_mode in ("Trend M5", "Follow M1", "Trend M1", "Farm"):
                if strategy_mode == "Trend M5":
                    tf_label = "M5"
                    current_bar_time = current_m5_bar_time
                    last_bar_time = last_m5_dca_bar_time
                elif strategy_mode == "Follow M1":
                    tf_label = "FollowM1"
                    current_bar_time = current_follow_m1_bar_time
                    last_bar_time = last_follow_m1_bar_time
                elif strategy_mode == "Farm":
                    tf_label = "Farm"
                    current_bar_time = current_m1_bar_time
                    last_bar_time = last_m1_dca_bar_time
                else:
                    tf_label = "M1"
                    current_bar_time = current_m1_bar_time
                    last_bar_time = last_m1_dca_bar_time

                if current_bar_time is None:
                    time.sleep(0.1); continue
                if current_bar_time <= last_bar_time:
                    if now - last_log_t > (30 if strategy_mode == "Trend M5" else 15):
                        nb  = len(by_side(positions,"BUY"))
                        ns  = len(by_side(positions,"SELL"))
                        tot = sum(p.profit for p in positions)
                        log(f"{tf_label}={h1} | {nb}B+{ns}S | PnL={tot:+.2f} | "
                            f"cho cua so {tf_label} moi de DCA")
                        last_log_t = now
                    time.sleep(0.1); continue

                # [M5 RECOVERY FORCE]
                # M5 co trend filter, nen chi force khi trend manh + gio lech NGUOC trend + DD lon.
                # Chi xu ly 1 lan moi nen M5 dong.
                if strategy_mode == "Trend M5" and not cfg.get("short_mode_clean_dca", True) and not cfg.get("short_mode_only_imbalance_streak", True):
                    cur_positions = my_pos(cfg)
                    rec = m5_force_precheck_rec or m5_recovery_force_decision(cur_positions, sym, cfg, m5_strong_trend_side)
                    if rec["action"] == "STOP":
                        log(f"[M5 RECOVERY STOP] DD={rec['dd_pct']:.1f}% PnL={rec['total_pnl']:+.2f} "
                            f">= stop {max(float(cfg.get('m5_recovery_stop_add_pct', 50.0)), 50.0):.1f}% -> chan lenh M5 thuong; chua du dieu kien emergency force, "
                            f"chi cho Pair Close/Smart Cut xu ly", "warn")
                        last_m5_dca_bar_time = current_bar_time
                        fail_count = 0
                        time.sleep(0.1); continue
                    if rec["action"] == "WAIT" and float(rec.get("dd_pct", 0.0)) >= max(float(cfg.get("m5_recovery_stop_add_pct", 50.0)), 50.0):
                        log(f"[M5 RECOVERY WAIT] DD={rec.get('dd_pct',0):.1f}% | {rec.get('wait_reason','doi recovery')} -> hard hold, chi Pair Close/Smart Cut", "warn")
                        last_m5_dca_bar_time = current_bar_time
                        fail_count = 0
                        time.sleep(0.1); continue
                    if rec["action"] == "FORCE":
                        if rec.get("emergency") and cfg.get("emergency_recovery_immediate_enabled", True):
                            try: _cool = max(5.0, float(cfg.get("emergency_recovery_cooldown_sec", 45.0)))
                            except Exception: _cool = 45.0
                            _remain = _cool - (now - float(last_immediate_recovery_t.get(strategy_mode, 0.0)))
                            if _remain > 0:
                                log(f"[M5 RECOVERY COOLDOWN] vua Immediate Force, con {_remain:.0f}s -> skip nen nay de tranh nhoi", "warn")
                                last_m5_dca_bar_time = current_bar_time
                                fail_count = 0
                                time.sleep(0.1); continue
                        force_lots = list(rec.get("force_lots") or [rec.get("force_lot", 0.0)])
                        force_lots = [float(x) for x in force_lots if float(x) > 0]
                        need_slots = len(force_lots)
                        if need_slots <= 0:
                            last_m5_dca_bar_time = current_bar_time
                            fail_count = 0
                            time.sleep(0.1); continue
                        if not _has_position_slots(len(cur_positions), need_slots):
                            log(f"[M5 RECOVERY FORCE] Khong du slot: {len(cur_positions)}/{_position_limit_label()}, can {need_slots} slot -> skip", "warn")
                            last_m5_dca_bar_time = current_bar_time
                            fail_count = 0
                            time.sleep(0.1); continue
                        log(f"[M5 RECOVERY FORCE L{rec['level']}] DD={rec['dd_pct']:.1f}% PnL={rec['total_pnl']:+.2f} | "
                            f"trend={rec['trend_side']}, BUY={rec['buy_count']}, SELL={rec['sell_count']}, "
                            f"BUYlot={rec.get('buy_lot',0):.2f}, SELLlot={rec.get('sell_lot',0):.2f}, net={rec.get('net_lot',0):+.2f}, "
                            f"factor={rec.get('target_factor',0):.0%}, target={rec.get('target_lot',0):.2f}, "
                            f"orders={need_slots}, total={sum(force_lots):.2f}, reason={rec.get('force_reason','')} -> "
                            f"open {rec['force_side']} {force_lots} de can gio, bo qua signal M5={h1}", "warn")
                        ok_any = False
                        for _lot in force_lots:
                            ok = open_order(rec["force_side"], _lot, cfg)
                            if ok:
                                ok_any = True
                                last_dca_t = now
                                fail_count = 0
                                time.sleep(1.0)
                            else:
                                fail_count += 1
                                break
                        last_m5_dca_bar_time = current_bar_time
                        if ok_any:
                            _mark_recovery_event(cfg, sym, strategy_mode, rec["force_side"], rec.get("dd_pct", 0.0))
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                        else:
                            if fail_count >= 5:
                                acc = mt5.account_info()
                                if acc is None or acc.balance <= 0:
                                    log(f"!!! Balance = {acc.balance if acc else 'N/A'} -> STOP bot", "error")
                                    _stop.set(); break
                                log(f"M5 Recovery Force open fail {fail_count} lan -> sleep 30s", "warn")
                                time.sleep(30)
                                fail_count = 0
                        time.sleep(0.1); continue

                # [M1 RECOVERY FORCE]
                # Khi Trend M1 dang am sau + giỏ lech hard, ep mo chieu can bang gio,
                # khong can doi mau nen M1 dung chieu. Chi xu ly 1 lan moi nen M1 dong.
                if strategy_mode == "Trend M1" and not cfg.get("short_mode_clean_dca", True) and not cfg.get("short_mode_only_imbalance_streak", True):
                    cur_positions = my_pos(cfg)
                    rec = m1_recovery_force_decision(cur_positions, sym, cfg)
                    if rec["action"] == "STOP":
                        log(f"[M1 RECOVERY STOP] DD={rec['dd_pct']:.1f}% PnL={rec['total_pnl']:+.2f} "
                            f">= stop {max(float(cfg.get('m1_recovery_stop_add_pct', 50.0)), 50.0):.1f}% -> chan lenh M1 thuong; chua du dieu kien emergency force, "
                            f"chi cho Pair Close/Smart Cut xu ly", "warn")
                        last_m1_dca_bar_time = current_bar_time
                        fail_count = 0
                        time.sleep(0.1); continue
                    if rec["action"] == "WAIT" and float(rec.get("dd_pct", 0.0)) >= max(float(cfg.get("m1_recovery_stop_add_pct", 50.0)), 50.0):
                        log(f"[M1 RECOVERY WAIT] DD={rec.get('dd_pct',0):.1f}% | {rec.get('wait_reason','doi recovery')} -> hard hold, chi Pair Close/Smart Cut", "warn")
                        last_m1_dca_bar_time = current_bar_time
                        fail_count = 0
                        time.sleep(0.1); continue
                    if rec["action"] == "FORCE":
                        if rec.get("emergency") and cfg.get("emergency_recovery_immediate_enabled", True):
                            try: _cool = max(5.0, float(cfg.get("emergency_recovery_cooldown_sec", 45.0)))
                            except Exception: _cool = 45.0
                            _remain = _cool - (now - float(last_immediate_recovery_t.get(strategy_mode, 0.0)))
                            if _remain > 0:
                                log(f"[M1 RECOVERY COOLDOWN] vua Immediate Force, con {_remain:.0f}s -> skip nen nay de tranh nhoi", "warn")
                                last_m1_dca_bar_time = current_bar_time
                                fail_count = 0
                                time.sleep(0.1); continue
                        force_lots = list(rec.get("force_lots") or [rec.get("force_lot", 0.0)])
                        force_lots = [float(x) for x in force_lots if float(x) > 0]
                        need_slots = len(force_lots)
                        if need_slots <= 0:
                            last_m1_dca_bar_time = current_bar_time
                            fail_count = 0
                            time.sleep(0.1); continue
                        if not _has_position_slots(len(cur_positions), need_slots):
                            log(f"[M1 RECOVERY FORCE] Khong du slot: {len(cur_positions)}/{_position_limit_label()}, can {need_slots} slot -> skip", "warn")
                            last_m1_dca_bar_time = current_bar_time
                            fail_count = 0
                            time.sleep(0.1); continue
                        log(f"[M1 RECOVERY FORCE L{rec['level']}] DD={rec['dd_pct']:.1f}% PnL={rec['total_pnl']:+.2f} | "
                            f"BUY={rec['buy_count']}, SELL={rec['sell_count']}, BUYlot={rec.get('buy_lot',0):.2f}, SELLlot={rec.get('sell_lot',0):.2f}, "
                            f"net={rec.get('net_lot',0):+.2f}, min_net={rec.get('min_net',0):.2f}, "
                            f"factor={rec.get('target_factor',0):.0%}, target={rec.get('target_lot',0):.2f}, "
                            f"orders={need_slots}, total={sum(force_lots):.2f} -> "
                            f"open {rec['force_side']} {force_lots} de can gio, bo qua signal M1={h1}", "warn")
                        ok_any = False
                        for _lot in force_lots:
                            ok = open_order(rec["force_side"], _lot, cfg)
                            if ok:
                                ok_any = True
                                last_dca_t = now
                                fail_count = 0
                                time.sleep(1.0)
                            else:
                                fail_count += 1
                                break
                        last_m1_dca_bar_time = current_bar_time
                        if ok_any:
                            _mark_recovery_event(cfg, sym, strategy_mode, rec["force_side"], rec.get("dd_pct", 0.0))
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                        else:
                            if fail_count >= 5:
                                acc = mt5.account_info()
                                if acc is None or acc.balance <= 0:
                                    log(f"!!! Balance = {acc.balance if acc else 'N/A'} -> STOP bot", "error")
                                    _stop.set(); break
                                log(f"M1 Recovery Force open fail {fail_count} lan -> sleep 30s", "warn")
                                time.sleep(30)
                                fail_count = 0
                        time.sleep(0.1); continue

                # DD >=50%: sau khi da thu Recovery Force o tren, khong cho M1/M5/Follow M1 mo DCA thuong nua.
                # Farm tu xu ly trong open_farm_pair: chi recovery don, khong hedge pair thuong.
                if strategy_mode in ("Trend M5", "Trend M1") and not cfg.get("short_mode_clean_dca", True) and not cfg.get("short_mode_only_imbalance_streak", True):
                    _hard_pnl, _hard_bal, _hard_dd = _basket_dd_pct(my_pos(cfg))
                    if _hard_dd >= 50.0:
                        log(f"[{strategy_mode} HARD CONTROL] DD={_hard_dd:.1f}% >=50% -> khong DCA thuong; chi Pair Close/Smart Cut/recovery", "warn")
                        if strategy_mode == "Trend M5":
                            last_m5_dca_bar_time = current_bar_time
                        elif strategy_mode == "Follow M1":
                            last_follow_m1_bar_time = current_bar_time
                        else:
                            last_m1_dca_bar_time = current_bar_time
                        fail_count = 0
                        time.sleep(0.1); continue

                # [M5 IMBALANCE GUARD] Neu BUY/SELL lech qua nguong, bo qua signal M5 cung chieu dang lech.
                if strategy_mode == "Trend M5":
                    cur_positions = my_pos(cfg)
                    blocked, nb, ns, diff, max_imb = m5_imbalance_guard_blocked(cur_positions, h1, cfg, m5_strong_trend_side)
                    if blocked:
                        log(f"[M5 IMBALANCE GUARD] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                            f"-> skip signal M5={h1}, doi chieu con lai de can bang", "warn")
                        last_m5_dca_bar_time = current_bar_time
                        time.sleep(0.1); continue

                # [M1 / FOLLOW M1] Imbalance uu tien cao hon Streak.
                # Khi gio lech >= nguong, chi chan chieu dang nhieu. Chieu doi dien
                # duoc phep vao, ke ca khi 3 lenh gan nhat cung chieu do.
                if strategy_mode == "Trend M1":
                    cur_positions = my_pos(cfg)
                    imb_blocked, nb, ns, diff, max_imb, hard_mode = m1_imbalance_guard_blocked(cur_positions, h1, cfg)
                    imbalance_forced_side = (
                        max_imb > 0 and (
                            (nb - ns >= max_imb and h1 == "SELL") or
                            (ns - nb >= max_imb and h1 == "BUY")
                        )
                    )
                    if imb_blocked:
                        tag = "HARD" if hard_mode else "SOFT"
                        log(f"[M1 IMBALANCE GUARD-{tag}] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                            f"-> skip signal M1={h1}, chi cho chieu doi dien de can bang", "warn")
                        if strategy_mode == "Follow M1":
                            last_follow_m1_bar_time = current_bar_time
                        else:
                            last_m1_dca_bar_time = current_bar_time
                        time.sleep(0.1); continue

                    streak_blocked, streak_side, streak_count, streak_limit = m1_same_side_guard_blocked(cur_positions, h1, cfg)
                    if streak_blocked:
                        if imbalance_forced_side:
                            log(f"[M1 IMBALANCE PRIORITY] BUY={nb}, SELL={ns}, diff={abs(nb-ns)} "
                                f"-> allow {h1}; bo qua STREAK {streak_count}x{streak_side} de can bang gio", "warn")
                        else:
                            log(f"[M1 SAME-SIDE GUARD] {streak_count} lenh gan nhat deu {streak_side} "
                                f">= {streak_limit} -> skip signal M1={h1}, doi nen nguoc chieu", "warn")
                            if strategy_mode == "Follow M1":
                                last_follow_m1_bar_time = current_bar_time
                            else:
                                last_m1_dca_bar_time = current_bar_time
                            time.sleep(0.1); continue

                _n_pos = len(my_pos(cfg))
                if _position_limit_reached(_n_pos):
                    if now - last_log_t > 60:
                        log(f"[MAX-POS DCA-{tf_label}] {_n_pos} >= {_position_limit_label()} -> SKIP DCA", "warn")
                        last_log_t = now
                    if strategy_mode == "Trend M5":
                        last_m5_dca_bar_time = current_bar_time
                    elif strategy_mode == "Follow M1":
                        last_follow_m1_bar_time = current_bar_time
                    else:
                        last_m1_dca_bar_time = current_bar_time
                    time.sleep(1); continue

                # [SHORT-MODE DCA PRICE GUARD]
                # Khi gio am, khong nhap them cung chieu neu gia chua di bat loi du xa.
                if strategy_mode in ("Trend M5", "Follow M1", "Trend M1", "Farm") and not cfg.get("short_mode_only_imbalance_streak", True):
                    _spacing_ok, _spacing_reason = _short_mode_dca_price_spacing_allowed(my_pos(cfg), h1, sym, cfg, strategy_mode)
                    if not _spacing_ok:
                        log(f"[DCA PRICE GUARD-{strategy_mode}] {_spacing_reason} -> skip nen nay", "warn")
                        if strategy_mode == "Trend M5":
                            last_m5_dca_bar_time = current_bar_time
                        elif strategy_mode == "Follow M1":
                            last_follow_m1_bar_time = current_bar_time
                        else:
                            last_m1_dca_bar_time = current_bar_time
                        fail_count = 0
                        time.sleep(0.1); continue

                batch_count = cfg.get("batch_count", 2)
                # Short modes bat buoc 1 lenh/nen; Farm la 1 hedge pair/nen.
                if strategy_mode in ("Trend M5", "Follow M1", "Trend M1", "Farm"):
                    batch_count = 1
                batch_delay = cfg.get("batch_delay", 5)
                if strategy_mode == "Farm":
                    lot = None
                    batch_count = 1  # Farm moi nen chi mo 1 cap hedge 2 lenh
                    hedge_lot, strong_lot, net_lot, gross_lot, _, _, _ = farm_lots(sym, cfg)
                    log(f"[DCA-Farm] Nen M1 moi dong -> signal={h1}, open hedge pair "
                        f"{hedge_lot:.2f}/{strong_lot:.2f} | net {net_lot:.2f}, gross {gross_lot:.2f} "
                        f"(do=BUY, xanh=SELL, Farm lot)", "warn")
                else:
                    follow_m1_live_phase_flip = False
                    follow_m1_live_phase_note = ""
                    if strategy_mode == "Follow M1":
                        lot, follow_m1_live_phase_flip, follow_m1_live_phase_note = _follow_m1_live_phase_preview(h1)
                        _kind = "M5 Supertrend Flip" if _follow_m1_live_fast else "M5 Supertrend"
                        log(f"[DCA-FollowM1] {_kind} -> signal={h1}, mo batch {batch_count}x{lot} "
                            f"({_follow_m1_live_reason}; {follow_m1_live_phase_note}; toi da 1 lenh/M1)", "warn")
                    else:
                        lot = next_lot(h1, sym, cfg, n_same=len(same), batch_count_override=1 if strategy_mode in ("Trend M5", "Trend M1") else None)
                        log(f"[DCA-{tf_label}] Nen {tf_label} moi dong -> signal={h1}, mo batch {batch_count}x{lot} "
                            f"(do=BUY, xanh=SELL, khong dung Adaptive DCA)", "warn")

                batch_success = 0
                guard_skip = False
                for bi in range(batch_count):
                    if _stop.is_set(): break
                    if _position_limit_reached(len(my_pos(cfg))):
                        log(f"[MAX-POS DCA-{tf_label}] Cham {_position_limit_label()} lenh trong luc mo batch", "warn")
                        break
                    if strategy_mode == "Trend M5":
                        cur_positions = my_pos(cfg)
                        blocked, nb, ns, diff, max_imb = m5_imbalance_guard_blocked(cur_positions, h1, cfg, m5_strong_trend_side)
                        if blocked:
                            log(f"[M5 IMBALANCE GUARD] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> dung batch DCA-{tf_label} {h1}", "warn")
                            break
                    if strategy_mode == "Trend M1":
                        cur_positions = my_pos(cfg)
                        imb_blocked, nb, ns, diff, max_imb, hard_mode = m1_imbalance_guard_blocked(cur_positions, h1, cfg)
                        imbalance_forced_side = (
                            max_imb > 0 and (
                                (nb - ns >= max_imb and h1 == "SELL") or
                                (ns - nb >= max_imb and h1 == "BUY")
                            )
                        )
                        if imb_blocked:
                            tag = "HARD" if hard_mode else "SOFT"
                            log(f"[M1 IMBALANCE GUARD-{tag}] BUY={nb}, SELL={ns}, diff={diff} >= {max_imb} "
                                f"-> dung batch DCA-{tf_label} {h1}", "warn")
                            break
                        streak_blocked, streak_side, streak_count, streak_limit = m1_same_side_guard_blocked(cur_positions, h1, cfg)
                        if streak_blocked and not imbalance_forced_side:
                            log(f"[M1 SAME-SIDE GUARD] {streak_count} lenh gan nhat deu {streak_side} "
                                f">= {streak_limit} -> dung batch DCA-{tf_label} {h1}", "warn")
                            break
                        if streak_blocked and imbalance_forced_side:
                            log(f"[M1 IMBALANCE PRIORITY] BUY={nb}, SELL={ns}, diff={abs(nb-ns)} "
                                f"-> allow batch {h1}; bo qua STREAK {streak_count}x{streak_side}", "warn")
                    if strategy_mode == "Farm":
                        if cfg.get("emergency_recovery_immediate_enabled", True) and not cfg.get("short_mode_only_imbalance_streak", True):
                            _farm_rec = farm_recovery_force_decision(my_pos(cfg), sym, cfg)
                            try: _farm_stop = max(float(cfg.get("farm_recovery_stop_add_pct", 50.0)), 50.0)
                            except Exception: _farm_stop = 50.0
                            if _farm_rec.get("action") == "FORCE" and float(_farm_rec.get("dd_pct", 0.0)) >= _farm_stop:
                                try: _cool = max(5.0, float(cfg.get("emergency_recovery_cooldown_sec", 45.0)))
                                except Exception: _cool = 45.0
                                _remain = _cool - (now - float(last_immediate_recovery_t.get("Farm", 0.0)))
                                if _remain > 0:
                                    log(f"[FARM RECOVERY COOLDOWN] vua Immediate Force, con {_remain:.0f}s -> skip nen nay de tranh nhoi", "warn")
                                    guard_skip = True
                                    break
                        opened_n = open_farm_pair(h1, sym, cfg, label="FARM-DCA")
                        if opened_n > 0:
                            batch_success += opened_n
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                        elif opened_n == -1:
                            guard_skip = True
                        break  # Farm chi mo 1 cap hedge moi nen M1
                    else:
                        if open_order(h1, lot, cfg):
                            batch_success += 1
                            if strategy_mode == "Follow M1":
                                _follow_m1_live_phase_commit(h1, lot, follow_m1_live_phase_flip)
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                    if bi < batch_count - 1:
                        time.sleep(batch_delay)

                # Dù open thành công hay fail, đánh dấu cửa sổ M1/M5 hiện tại đã xử lý.
                # Follow M1 dung cua so M1 rieng, khong dung chung clock Trend M1/M5.
                if strategy_mode == "Trend M5":
                    last_m5_dca_bar_time = current_bar_time
                elif strategy_mode == "Follow M1":
                    last_follow_m1_bar_time = current_bar_time
                else:
                    last_m1_dca_bar_time = current_bar_time
                if batch_success > 0:
                    last_dca_t = now
                    fail_count = 0
                    if strategy_mode == "Trend 1H" and h1_flip_hold_active and h1 == h1_flip_hold_side:
                        h1_flip_dca_count += 1
                elif guard_skip:
                    # Guard skip la dung logic, khong tinh la DCA fail.
                    fail_count = 0
                else:
                    fail_count += 1
                    if fail_count >= 5:
                        acc = mt5.account_info()
                        if acc is None or acc.balance <= 0:
                            log(f"!!! Balance = {acc.balance if acc else 'N/A'} -> STOP bot", "error")
                            _stop.set(); break
                        log(f"DCA-{tf_label} open fail {fail_count} lan -> sleep 30s", "warn")
                        time.sleep(30)
                        fail_count = 0
                    else:
                        time.sleep(2)
                time.sleep(0.1); continue

            # Option B: KHONG DCA them khi TRENDING
            # [REGIME-DIR] EXCEPTION: cho DCA nếu regime CÙNG CHIỀU H1
            if strategy_mode == "Trend 1H" and cached_regime == "TRENDING":
                if cached_regime_dir is not None and cached_regime_dir == h1:
                    # Cùng chiều -> fall through, DCA bình thường
                    if now - last_log_t > 30:
                        log(f"[REGIME-DIR] TRENDING {cached_regime_dir} cùng chiều H1 "
                            f"(ADX={cached_adx:.1f}) -> cho DCA bình thường", "info")
                        last_log_t = now
                    # KHÔNG continue
                else:
                    # Ngược chiều hoặc dir không rõ -> chặn DCA (logic cũ)
                    if now - last_log_t > 30:
                        log(f"[REGIME] TRENDING (ADX={cached_adx:.1f}, dir={cached_regime_dir}) "
                            f"ngược/lệch H1={h1} -> PAUSE DCA", "warn")
                        last_log_t = now
                    time.sleep(1); continue

            ok_dca, cur, trig = should_dca(h1, lat, tick, cfg)
            if ok_dca:
                # === BOT V3 SAFETY: MAX POSITIONS CHECK (DCA) ===
                _n_pos = len(my_pos(cfg))
                if _position_limit_reached(_n_pos):
                    if now - last_log_t > 60:
                        log(f"[MAX-POS DCA] {_n_pos} >= {_position_limit_label()} -> SKIP DCA", "warn")
                        last_log_t = now
                    time.sleep(1); continue
                
                if now - last_dca_t >= cfg.get("dca_cooldown", 0):
                    # [H1 RECOVERY FORCE]
                    # Khi H1 dang am + net BUY/SELL lech du nguong, uu tien mo lenh DON can gio
                    # theo DD% thay vi tiep tuc DCA mot chieu.
                    if False and strategy_mode == "Trend 1H":  # disabled: Trend 1H dung Adaptive DCA rieng, khong can lot DDHold
                        h1_rec = h1_recovery_force_decision(my_pos(cfg), sym, cfg)
                        if h1_rec.get("action") == "STOP":
                            log(f"[H1 RECOVERY STOP] DD={h1_rec.get('dd_pct',0):.1f}% PnL={h1_rec.get('total_pnl',0):+.2f} "
                                f">= stop {float(cfg.get('h1_recovery_stop_add_pct',20.0)):.1f}% nhưng net chưa lệch đủ "
                                f"-> bỏ DCA thường, chỉ Pair Close/Smart Cut", "warn")
                            time.sleep(0.5); continue
                        if h1_rec.get("action") == "FORCE":
                            force_lots = [float(x) for x in (h1_rec.get("force_lots") or [h1_rec.get("force_lot",0.0)]) if float(x) > 0]
                            if force_lots:
                                if not _has_position_slots(len(my_pos(cfg)), len(force_lots)):
                                    log(f"[H1 RECOVERY FORCE] Khong du slot: {len(my_pos(cfg))}/{_position_limit_label()}, can {len(force_lots)} slot -> skip", "warn")
                                    time.sleep(0.5); continue
                                log(f"[H1 RECOVERY FORCE L{h1_rec.get('level',0)}] DD={h1_rec.get('dd_pct',0):.1f}% "
                                    f"PnL={h1_rec.get('total_pnl',0):+.2f} | BUYlot={h1_rec.get('buy_lot',0):.2f}, "
                                    f"SELLlot={h1_rec.get('sell_lot',0):.2f}, net={h1_rec.get('net_lot',0):+.2f}, "
                                    f"factor={h1_rec.get('target_factor',0):.0%}, target={h1_rec.get('target_lot',0):.2f}, "
                                    f"orders={len(force_lots)}, total={sum(force_lots):.2f} -> open {h1_rec.get('force_side')} {force_lots} "
                                    f"de can gio theo DD%, bo qua DCA H1 mot chieu", "warn")
                                ok_any = False
                                for _lot in force_lots:
                                    if open_order(h1_rec["force_side"], _lot, cfg):
                                        ok_any = True
                                        time.sleep(1.0)
                                    else:
                                        break
                                if ok_any:
                                    last_dca_t = now
                                    fail_count = 0
                                    try: push_status(my_pos(cfg), cached_h1, cfg)
                                    except: pass
                                    last_status_t = now
                                time.sleep(0.1); continue
                    # Truyen n_same de tranh goi my_pos lai
                    # DCA BATCH: mo nhieu lenh nho cach delay giay
                    batch_count = cfg.get("batch_count", 2)
                    batch_delay = cfg.get("batch_delay", 5)
                    lot = next_lot(h1, sym, cfg, n_same=len(same))
                    # [ADAPTIVE-STEP] Log range_15m + step đang dùng để theo dõi
                    adp_info = ""
                    if cfg.get("adaptive_step_enabled", True) and hasattr(get_volatility_step, "_cache"):
                        c = get_volatility_step._cache
                        adp_info = f" [range15m={c.get('range',0):.1f}$ step={c.get('step',0):.0f}$]"
                    log(f"[DCA] {h1} #{lat.ticket} open={lat.price_open:.2f} "
                        f"cur={cur:.2f} trig={trig:.2f}{adp_info} batch {batch_count}x{lot}","warn")

                    batch_success = 0
                    for bi in range(batch_count):
                        if _stop.is_set(): break
                        if open_order(h1, lot, cfg):
                            batch_success += 1
                            try: push_status(my_pos(cfg), cached_h1, cfg)
                            except: pass
                            last_status_t = now
                            # Pair worker thread tu check moi 0.1s -> khong can goi tu day
                        if bi < batch_count - 1:
                            time.sleep(batch_delay)

                    if batch_success > 0:
                        last_dca_t = now
                        fail_count = 0
                        # [H1-FLIP-HOLD] Đếm DCA nếu đang ở chế độ hold
                        if strategy_mode == "Trend 1H" and h1_flip_hold_active and h1 == h1_flip_hold_side:
                            h1_flip_dca_count += 1
                            log(f"[H1-FLIP-HOLD] DCA thành công {batch_success} lệnh "
                                f"-> count = {h1_flip_dca_count}/{cfg.get('h1_flip_max_dca', 99999)}", "info")
                    else:
                        fail_count += 1
                        if fail_count >= 5:
                            acc = mt5.account_info()
                            if acc is None or acc.balance <= 0:
                                log(f"!!! Balance = {acc.balance if acc else 'N/A'} -> STOP bot", "error")
                                _stop.set(); break
                            log(f"DCA open fail {fail_count} lan -> sleep 30s", "warn")
                            time.sleep(30)
                            fail_count = 0
                        else:
                            time.sleep(2)
            else:
                if now - last_log_t > 30:
                    nb  = len(by_side(positions,"BUY"))
                    ns  = len(by_side(positions,"SELL"))
                    tot = sum(p.profit for p in positions)
                    log(f"H1={h1} | {nb}B+{ns}S | PnL={tot:+.2f} | "
                        f"open={lat.price_open:.2f} trig={trig:.2f} cur={cur:.2f}")
                    last_log_t = now

            # === TELEGRAM TRACKING: chi track DD, hourly/daily do GUI gom va gui ===
            try:
                _acc_tg = mt5.account_info()
                if _acc_tg and _acc_tg.balance > 0:
                    _dd_pct = (_acc_tg.equity - _acc_tg.balance) / _acc_tg.balance * 100
                    tg_track_dd(_dd_pct)
            except Exception:
                pass

            time.sleep(0.1)

    except Exception as e:
        import traceback as tb
        log(f"EXCEPTION: {e}\n{tb.format_exc()}","error")
    finally:
        # Telegram: bao GUI biet worker da stop (GUI gui aggregated daily neu can)
        try:
            _acc_fin = mt5.account_info()
            if _acc_fin:
                send({"type":"tg_event","event":"stop",
                      "login":   _acc_fin.login,
                      "balance": _acc_fin.balance,
                      "ts":      time.time()})
        except Exception:
            pass
        mt5.shutdown()
        send({"type":"exit","reason":"normal"})
        close_log_file()




MAX_ACCOUNTS = 10
MAGIC_BASE   = 20260600

# ── Đường dẫn worker ─────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__
))
# (WORKER_SCRIPT khong can nua - goi lai chinh bot.py voi --worker)

# ── Config persistence ────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(BASE_DIR, "followm5_autoscale_accounts.json")


# ═══════════════════════════════════════════════════════════════════════════
# ███  PyQt6 GUI  —  MGB Bot v4  (Blue theme · Monitor + Config pages)
# ═══════════════════════════════════════════════════════════════════════════
# Logic trading (worker/run) o tren KHONG doi. GUI nay chi:
#  - spawn WorkerHandle (subprocess) y het ban Tkinter
#  - doc event_q (log/status/trade/tg_event)
#  - 2 trang: MONITOR (bang tat ca account + tong + log) / CONFIG (1 account)
# ═══════════════════════════════════════════════════════════════════════════

QC = {
    "bg":            "#0a0a0a",
    "bg_panel":      "#141414",
    "bg_card":       "#121212",
    "bg_row":        "#0a0a0a",
    "bg_row_alt":    "#121212",
    "bg_input":      "#0d0d0d",
    "bg_hover":      "#1a1a1a",
    "bg_sel":        "#11203a",
    "border":        "#2a2a2a",
    "border_soft":   "#1a1a1a",
    "text":          "#e5e5e5",
    "text_dim":      "#737373",
    "text_mute":     "#525252",
    "text_white":    "#f5f5f5",
    "accent":        "#60a5fa",   # blue accent
    "accent_mid":    "#3b82f6",
    "accent_deep":   "#1e40af",
    "buy":           "#22c55e",   # green profit
    "sell":          "#ef4444",   # red loss
    "warn":          "#fbbf24",   # amber
    "btn_start":     "#16a34a",
    "btn_stop_bg":   "#1a1a1a",
    "btn_stopall":   "#dc2626",
    "sym":           "#fbbf24",
}
QF_UI   = "Segoe UI"
QF_MONO = "Consolas"

def _qss():
    c = QC
    return f"""
    QWidget {{ background:{c['bg']}; color:{c['text']}; font-family:'{QF_UI}'; font-size:12px; }}
    QMainWindow,QDialog {{ background:{c['bg']}; }}
    QLabel {{ background:transparent; }}
    QFrame#card {{ background:{c['bg_card']}; border:1px solid {c['border']}; border-radius:6px; }}
    QFrame#hdr {{ background:{c['bg']}; border:none; border-bottom:1px solid {c['border']}; }}
    QLabel#title {{ color:{c['text_white']}; font-size:14px; font-weight:700; }}
    QLabel#sectTitle {{ color:{c['accent']}; font-size:10px; font-weight:700; }}
    QLabel#kpiCap {{ color:{c['text_mute']}; font-size:9px; }}
    QLabel#dim {{ color:{c['text_dim']}; font-size:11px; }}

    QLineEdit,QComboBox {{
        background:{c['bg_input']}; border:1px solid {c['border']}; border-radius:4px;
        padding:4px 7px; color:{c['text_white']}; font-family:'{QF_MONO}'; font-size:12px;
        selection-background-color:{c['accent_mid']};
    }}
    QLineEdit:focus,QComboBox:focus {{ border:1px solid {c['accent']}; }}
    QComboBox::drop-down {{ border:none; width:16px; }}
    QComboBox QAbstractItemView {{
        background:{c['bg_card']}; border:1px solid {c['border']}; color:{c['text_white']};
        selection-background-color:{c['accent_mid']}; outline:none;
    }}

    QPushButton {{
        background:{c['bg_hover']}; border:1px solid {c['border']}; border-radius:4px;
        padding:5px 11px; color:{c['text']}; font-weight:700; font-size:11px;
    }}
    QPushButton:hover {{ border-color:{c['accent']}; }}
    QPushButton:disabled {{ color:{c['text_mute']}; border-color:{c['border_soft']}; }}
    QPushButton#bAdd   {{ background:{c['accent_deep']}; border:none; color:#fff; }}
    QPushButton#bStart {{ background:{c['btn_start']}; border:none; color:#000; }}
    QPushButton#bStopAll {{ background:{c['btn_stopall']}; border:none; color:#fff; }}
    QPushButton#bConfig {{ background:transparent; border:none; color:{c['accent']}; padding:2px 6px; }}
    QPushButton#bConfig:hover {{ color:{c['text_white']}; }}
    QPushButton#bRowStop {{ background:{c['bg_hover']}; border:1px solid {c['border']}; color:{c['text_dim']}; padding:2px 8px; font-size:10px; }}
    QPushButton#bRowStart {{ background:{c['btn_start']}; border:none; color:#000; padding:2px 8px; font-size:10px; }}
    QPushButton#bRemove {{ background:#7f1d1d; border:none; color:#fca5a5; }}
    QPushButton#bSave {{ background:{c['btn_start']}; border:none; color:#000; }}
    QPushButton#bBack {{ background:transparent; border:none; color:{c['accent']}; }}

    QCheckBox {{ color:{c['text']}; font-size:11px; spacing:6px; }}
    QCheckBox::indicator {{ width:14px; height:14px; border-radius:3px; border:1px solid {c['border']}; background:{c['bg_input']}; }}
    QCheckBox::indicator:checked {{ background:{c['accent_mid']}; border-color:{c['accent_mid']}; }}

    QTableWidget {{
        background:{c['bg_card']}; alternate-background-color:{c['bg_panel']};
        border:none; gridline-color:transparent; color:{c['text']};
        font-family:'{QF_MONO}'; font-size:12px; outline:none;
    }}
    QHeaderView::section {{
        background:{c['bg_panel']}; color:{c['text_dim']}; border:none;
        border-bottom:1px solid {c['border']}; padding:8px 8px;
        font-family:'{QF_MONO}'; font-size:11px; font-weight:700;
    }}
    QTableWidget::item {{
        background:{c['bg_card']}; padding:6px 8px; border:none;
        border-bottom:1px solid {c['border_soft']};
    }}
    QTableWidget#positionsTable::item:alternate {{ background:{c['bg_panel']}; }}
    QTableWidget::item:selected {{ background:{c['bg_sel']}; color:{c['text_white']}; }}

    QScrollBar:vertical {{ background:{c['bg']}; width:9px; margin:0; }}
    QScrollBar::handle:vertical {{ background:{c['border']}; border-radius:4px; min-height:20px; }}
    QScrollBar::handle:vertical:hover {{ background:{c['text_dim']}; }}
    QScrollBar::add-line,QScrollBar::sub-line {{ height:0; }}

    QPlainTextEdit {{
        background:{c['bg_card']}; border:none; color:{c['text']};
        font-family:'{QF_MONO}'; font-size:11px; selection-background-color:{c['accent_mid']};
    }}
    QStackedWidget {{ background:{c['bg']}; }}
    QToolTip {{ background:{c['bg_panel']}; color:{c['text_white']}; border:1px solid {c['border']}; }}
    """

# ── WorkerHandle (giu nguyen tu ban Tk) ──
class WorkerHandle:
    """
    Spawn btcbot_worker.py như subprocess độc lập.
    Đọc stdout (JSON lines) trong daemon thread → push vào event_queue.
    Gửi stop command qua stdin.
    """
    def __init__(self, cfg, event_queue):
        self._q    = event_queue
        self._proc = None
        self._start(cfg)

    def _start(self, cfg):
        cfg_json = json.dumps(cfg, ensure_ascii=False)

        # Goi lai chinh file bot.py voi --worker flag
        if getattr(sys, "frozen", False):
            # Khi build EXE: sys.executable la BTCRushMulti.exe -> goi lai chinh no
            cmd = [sys.executable, "--worker", cfg_json]
        else:
            # Chay tu .py file
            cmd = [sys.executable, os.path.abspath(__file__), "--worker", cfg_json]

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr vào stdout
                text=False,                 # bytes → decode thủ công (tránh codec error)
                creationflags=flags,
            )
        except Exception as e:
            self._emit("error", f"Khong the khoi dong worker process: {e}")
            return

        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        """Đọc stdout subprocess theo dòng, parse JSON, push vào queue."""
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    t = obj.get("type", "")
                    if t == "log":
                        self._q.put_nowait({
                            "_type": "log",
                            "level": obj.get("level", "info"),
                            "msg":   obj.get("msg", ""),
                            "ts":    obj.get("ts", ""),
                        })
                    elif t == "status":
                        obj["_type"] = "status"
                        self._q.put_nowait(obj)
                    elif t == "trade":
                        obj["_type"] = "trade"
                        self._q.put_nowait(obj)
                    elif t == "tg_event":
                        obj["_type"] = "tg_event"
                        self._q.put_nowait(obj)
                    elif t == "exit":
                        self._q.put_nowait({
                            "_type": "log",
                            "level": "warn",
                            "msg":   f"Worker exit: {obj.get('reason', '')}",
                            "ts":    datetime.now().strftime("%H:%M:%S"),
                        })
                except json.JSONDecodeError:
                    # stderr / traceback không phải JSON → log trực tiếp
                    self._q.put_nowait({
                        "_type": "log",
                        "level": "error",
                        "msg":   line,
                        "ts":    datetime.now().strftime("%H:%M:%S"),
                    })
        except Exception:
            pass

    def _emit(self, level, msg):
        try:
            self._q.put_nowait({
                "_type": "log",
                "level": level,
                "msg":   msg,
                "ts":    datetime.now().strftime("%H:%M:%S"),
            })
        except Exception:
            pass

    def stop(self):
        """Gửi {"cmd":"stop"} qua stdin, chờ 5s rồi kill."""
        if self._proc is None:
            return
        try:
            self._proc.stdin.write(b'{"cmd":"stop"}\n')
            self._proc.stdin.flush()
        except Exception:
            pass
        threading.Thread(target=self._wait_kill, daemon=True).start()

    def _wait_kill(self):
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: self._proc.kill()
            except Exception: pass

    def is_alive(self):
        return self._proc is not None and self._proc.poll() is None


def qsave_all_configs(widgets):
    data = {}
    for w in widgets:
        data[str(w.acct_id)] = w.get_cfg_for_save()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"accounts": data}, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def qload_all_configs():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("accounts", {})
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNT — data + worker (KHONG phai widget). 1 account = 1 cau hinh + 1 worker
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# FOLLOW M1 AUTOSCALE PRO GUI — only terminal connection data is editable.
# ═══════════════════════════════════════════════════════════════════════════

def _follow_m1_live_qss():
    return _qss() + f"""
    QFrame#hero {{ background:#0b172a; border-bottom:1px solid #233b5e; }}
    QFrame#metricCard {{ background:#101c31; border:1px solid #253a5a; border-radius:10px; }}
    QFrame#accountCard {{ background:#0e1727; border:1px solid #263b5c; border-radius:12px; }}
    QFrame#accountHeader {{ background:#111f35; border:none; border-bottom:1px solid #263b5c; border-top-left-radius:12px; border-top-right-radius:12px; }}
    QFrame#profileStrip {{ background:#0a2132; border:1px solid #1f5d7e; border-radius:8px; }}
    QLabel#heroTitle {{ color:#f8fbff; font-size:21px; font-weight:800; letter-spacing:1px; }}
    QLabel#heroSub {{ color:#94b4d6; font-size:11px; }}
    QLabel#metricLabel {{ color:#8da2bf; font-size:9px; font-weight:700; letter-spacing:0.8px; }}
    QLabel#metricValue {{ color:#f7fbff; font-family:'{QF_MONO}'; font-size:16px; font-weight:700; }}
    QLabel#accountTitle {{ color:#f5f9ff; font-size:15px; font-weight:800; }}
    QLabel#chip {{ background:#14385b; color:#b8e0ff; border:1px solid #276996; border-radius:9px; padding:3px 8px; font-size:10px; font-weight:700; }}
    QLabel#profileLabel {{ color:#77c5ef; font-size:9px; font-weight:700; letter-spacing:0.8px; }}
    QLabel#profileValue {{ color:#eaf7ff; font-family:'{QF_MONO}'; font-size:13px; font-weight:700; }}
    QLabel#note {{ color:#8ca2bd; font-size:10px; }}
    QPushButton#primary {{ background:#12a56b; border:none; color:#06170f; padding:7px 13px; border-radius:6px; font-weight:800; }}
    QPushButton#danger {{ background:#c93b4a; border:none; color:white; padding:7px 13px; border-radius:6px; font-weight:800; }}
    QPushButton#ghost {{ background:#16243a; border:1px solid #365273; color:#c8def4; padding:6px 11px; border-radius:6px; font-weight:700; }}
    QPushButton#ghost:hover {{ background:#203654; border-color:#67b7ec; }}
    QPlainTextEdit#eventLog {{ background:#080f1b; border:1px solid #1e3554; border-radius:8px; color:#c4d8ec; padding:7px; }}
    QLineEdit {{ min-height:25px; }}
    """


def _format_money(value, currency=""):
    try:
        return f"{float(value):,.2f} {currency}".strip()
    except Exception:
        return "—"


def _profile_from_status(status):
    profile = status.get("profile") or {}
    if profile:
        return profile
    balance = float(status.get("balance", 0.0) or 0.0)
    currency = status.get("currency", "")
    return follow_m1_live_autoscale_profile(balance, currency) if balance > 0 else {}


class FollowM1Account(QtCore.QObject):
    changed = pyqtSignal(int)

    _ALLOWED = {"login", "password", "server", "path", "symbol", "magic"}

    def __init__(self, account_id, log_fn, saved=None):
        super().__init__()
        self.account_id = account_id
        self.log_fn = log_fn
        self.events = queue.Queue()
        self.worker = None
        self.status = {}
        self.cfg = {
            "login": "", "password": "", "server": "", "path": "",
            "symbol": "XAUUSDc", "magic": str(MAGIC_BASE + account_id),
        }
        if isinstance(saved, dict):
            for key in self._ALLOWED:
                if key in saved:
                    self.cfg[key] = saved[key]

    def save_data(self):
        return {key: self.cfg.get(key, "") for key in self._ALLOWED}

    def worker_cfg(self):
        try:
            return {
                "login": (self.cfg.get("login") or "").strip() or None,
                "password": (self.cfg.get("password") or "").strip() or None,
                "server": (self.cfg.get("server") or "").strip() or None,
                "path": (self.cfg.get("path") or "").strip() or None,
                "symbol": (self.cfg.get("symbol") or "XAUUSDc").strip(),
                "magic": int(str(self.cfg.get("magic") or (MAGIC_BASE + self.account_id))),
                "strategy_mode": "Follow M1",
                "auto_scale_follow_m1_live": True,
                # Placeholder values are overwritten after MT5 account_info().
                "base_lot": 0.01, "lot_step": 0.01, "max_lot": 100.0,
                "follow_m1_unlimited_max_lot": True,
                "follow_m1_signal_engine": "M5_SUPERTREND",
                "follow_m1_supertrend_m5_period": 10,
                "follow_m1_supertrend_m5_multiplier": 3.0,
                "basket_tp": 1.0,
                "batch_count": 1, "batch_delay": 0,
                "dca_cooldown": 0, "reentry_wait": 300,
                "adaptive_step_enabled": True,
                "follow_m1_live_phase_lot_enabled": True,
                "follow_m1_live_fast_flip_enabled": False,
                "short_mode_clean_dca": True,
                "short_mode_only_imbalance_streak": True,
                "follow_m1_live_disable_imbalance_streak": True,
                "enable_smart_cut": False,
                "enable_stale_cut": False,
                "m1_max_same_side_streak": 0,
                "m1_max_imbalance": 0,
                "m1_hard_imbalance": 0,
                "close_saturday_4am": False,
            }
        except Exception as exc:
            QtWidgets.QMessageBox.critical(None, "Lỗi cấu hình", str(exc))
            return None

    def start(self):
        if self.is_running():
            return
        cfg = self.worker_cfg()
        if not cfg:
            return
        self.worker = WorkerHandle(cfg, self.events)
        self.log_fn(f"[ACC #{self.account_id}] Khởi động Follow M1 · M5 Supertrend · No Lot Cap", "info")
        self.changed.emit(self.account_id)

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.log_fn(f"[ACC #{self.account_id}] Đã dừng", "warn")
        self.changed.emit(self.account_id)

    def is_running(self):
        return bool(self.worker and self.worker.is_alive())

    def poll(self):
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.log_fn(f"[ACC #{self.account_id}] Worker đã kết thúc", "warn")
            self.changed.emit(self.account_id)
        dirty = False
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("_type")
            if kind == "log":
                message = event.get("msg", "")
                if not message.startswith(f"[ACC #{self.account_id}]"):
                    message = f"[ACC #{self.account_id}] {message}"
                self.log_fn(message, event.get("level", "info"), event.get("ts", ""))
            elif kind == "status":
                self.status = event
                dirty = True
        if dirty:
            self.changed.emit(self.account_id)


class ConnectionDialog(QtWidgets.QDialog):
    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.account = account
        self.setWindowTitle(f"Kết nối MT5 · Account #{account.account_id}")
        self.setModal(True)
        self.resize(520, 330)
        root = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("THIẾT LẬP KẾT NỐI"); title.setObjectName("heroTitle")
        sub = QtWidgets.QLabel("Chỉ thông tin kết nối được chỉnh. Lot và Basket TP tự scale theo balance MT5.")
        sub.setObjectName("heroSub")
        root.addWidget(title); root.addWidget(sub); root.addSpacing(10)

        form = QtWidgets.QFormLayout(); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.fields = {}
        for key, label in [
            ("login", "MT5 Login"), ("password", "Mật khẩu"), ("server", "Server"),
            ("path", "Đường dẫn terminal"), ("symbol", "Symbol vàng"), ("magic", "Magic number"),
        ]:
            edit = QtWidgets.QLineEdit(str(account.cfg.get(key, "")))
            if key == "password":
                edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            if key == "symbol" and not edit.text().strip():
                edit.setText("XAUUSDc")
            form.addRow(label + ":", edit)
            self.fields[key] = edit
        root.addLayout(form)

        note = QtWidgets.QLabel("Profile tham chiếu: 50.000 → Base 0.03 · Pha +0.02 · Không giới hạn lot (theo giới hạn broker) · Basket TP 50")
        note.setObjectName("note"); note.setWordWrap(True); root.addWidget(note)
        buttons = QtWidgets.QHBoxLayout(); buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("HỦY"); cancel.setObjectName("ghost"); cancel.clicked.connect(self.reject)
        save = QtWidgets.QPushButton("LƯU KẾT NỐI"); save.setObjectName("primary"); save.clicked.connect(self._save)
        buttons.addWidget(cancel); buttons.addWidget(save); root.addLayout(buttons)

    def _save(self):
        for key, edit in self.fields.items():
            self.account.cfg[key] = edit.text().strip()
        self.accept()


class AccountCard(QtWidgets.QFrame):
    configureRequested = pyqtSignal(int)
    removeRequested = pyqtSignal(int)
    startStopRequested = pyqtSignal(int)

    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.account = account
        self.setObjectName("accountCard")
        self._build()
        self.refresh()

    @staticmethod
    def _metric(label):
        frame = QtWidgets.QFrame(); frame.setObjectName("metricCard")
        layout = QtWidgets.QVBoxLayout(frame); layout.setContentsMargins(10,7,10,7); layout.setSpacing(1)
        cap = QtWidgets.QLabel(label); cap.setObjectName("metricLabel")
        value = QtWidgets.QLabel("—"); value.setObjectName("metricValue")
        layout.addWidget(cap); layout.addWidget(value)
        return frame, value

    @staticmethod
    def _profile_metric(label):
        box = QtWidgets.QWidget(); layout = QtWidgets.QVBoxLayout(box); layout.setContentsMargins(7,2,7,2); layout.setSpacing(0)
        cap = QtWidgets.QLabel(label); cap.setObjectName("profileLabel")
        val = QtWidgets.QLabel("—"); val.setObjectName("profileValue")
        layout.addWidget(cap); layout.addWidget(val)
        return box, val

    def _build(self):
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        header = QtWidgets.QFrame(); header.setObjectName("accountHeader")
        h = QtWidgets.QHBoxLayout(header); h.setContentsMargins(14,9,12,9); h.setSpacing(8)
        self.title = QtWidgets.QLabel(); self.title.setObjectName("accountTitle")
        self.online = QtWidgets.QLabel(); self.online.setObjectName("chip")
        self.connection = QtWidgets.QLabel(); self.connection.setObjectName("note")
        self.b_start = QtWidgets.QPushButton(); self.b_start.clicked.connect(lambda: self.startStopRequested.emit(self.account.account_id))
        self.b_settings = QtWidgets.QPushButton("KẾT NỐI"); self.b_settings.setObjectName("ghost"); self.b_settings.clicked.connect(lambda: self.configureRequested.emit(self.account.account_id))
        self.b_remove = QtWidgets.QPushButton("✕"); self.b_remove.setObjectName("ghost"); self.b_remove.setFixedWidth(34); self.b_remove.clicked.connect(lambda: self.removeRequested.emit(self.account.account_id))
        h.addWidget(self.title); h.addWidget(self.online); h.addWidget(self.connection); h.addStretch(1); h.addWidget(self.b_settings); h.addWidget(self.b_start); h.addWidget(self.b_remove)
        root.addWidget(header)

        body = QtWidgets.QVBoxLayout(); body.setContentsMargins(12,12,12,12); body.setSpacing(10)
        metric_row = QtWidgets.QHBoxLayout(); metric_row.setSpacing(8)
        # Giữ reference trực tiếp tới từng metric frame. Không lấy lại qua
        # parentWidget(), vì QLabel có thể bị Qt hủy khi frame tạm thời mất reference.
        metric_items = [
            ("balance", self._metric("BALANCE")),
            ("equity", self._metric("EQUITY")),
            ("floating", self._metric("FLOATING")),
            ("dd", self._metric("DRAW DOWN")),
            ("positions_count", self._metric("VỊ THẾ MỞ")),
        ]
        self._metric_frames = []
        for attr, (frame, value) in metric_items:
            setattr(self, attr, value)
            self._metric_frames.append(frame)
            metric_row.addWidget(frame, 1)
        body.addLayout(metric_row)

        profile = QtWidgets.QFrame(); profile.setObjectName("profileStrip")
        pl = QtWidgets.QHBoxLayout(profile); pl.setContentsMargins(10,6,10,6); pl.setSpacing(8)
        profile_title = QtWidgets.QLabel("AUTO SCALE"); profile_title.setObjectName("profileLabel")
        self.base, self.phase, self.cap, self.tp = [None] * 4
        labels = [("BASE LOT", "base"), ("PHA ĐẢO", "phase"), ("LOT CAP", "cap"), ("BASKET TP", "tp")]
        values = []
        for label, attr in labels:
            widget, value = self._profile_metric(label)
            setattr(self, attr, value)
            values.append(widget)
        self.tp_rule = QtWidgets.QLabel("CHỈ ĐÓNG TOÀN BỘ GIỎ KHI ĐỦ BASKET TP"); self.tp_rule.setObjectName("chip")
        pl.addWidget(profile_title); pl.addSpacing(7)
        for widget in values:
            pl.addWidget(widget, 1)
        pl.addWidget(self.tp_rule)
        body.addWidget(profile)

        # Bảng lệnh nhận toàn bộ chiều cao còn lại của card.
        # Không đặt max-height để không tạo vùng trống giữa bảng và Event Log.
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setObjectName("positionsTable")
        self.table.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.table.setHorizontalHeaderLabels(["TICKET", "SIDE", "LOT", "P/L", "OPEN TIME"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        self.table.setMinimumHeight(220)
        self.table.verticalHeader().setDefaultSectionSize(31)
        self.table.verticalHeader().setMinimumSectionSize(31)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed); self.table.setColumnWidth(0, 142)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed); self.table.setColumnWidth(1, 82)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed); self.table.setColumnWidth(2, 76)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed); self.table.setColumnWidth(3, 130)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        body.addWidget(self.table, 1)
        root.addLayout(body, 1)

    def refresh(self):
        status = self.account.status
        running = self.account.is_running()
        symbol = self.account.cfg.get("symbol", "XAUUSDc")
        login = status.get("login") or self.account.cfg.get("login") or "MT5"
        currency = status.get("currency", "")
        balance = float(status.get("balance", 0.0) or 0.0)
        equity = float(status.get("equity", 0.0) or 0.0)
        floating = float(status.get("total_pnl", 0.0) or 0.0)
        dd = ((equity - balance) / balance * 100.0) if balance > 0 else 0.0
        positions = status.get("positions", []) or []

        self.title.setText(f"ACCOUNT #{self.account.account_id}  ·  FOLLOW M1 / M5 SUPERTREND")
        self.connection.setText(f"#{login}  ·  {symbol}  ·  Magic {self.account.cfg.get('magic', '')}")
        if running:
            ml = status.get("margin_level", 0)
            self.online.setText(f"● ONLINE  {ml:.0f}%" if ml else "● ONLINE")
            self.online.setStyleSheet("background:#123d2c;color:#85f0b7;border:1px solid #2a8b5d;border-radius:9px;padding:3px 8px;font-size:10px;font-weight:700;")
            self.b_start.setText("■ DỪNG"); self.b_start.setObjectName("danger")
        else:
            self.online.setText("● OFFLINE")
            self.online.setStyleSheet("background:#2c1d28;color:#ffb1bd;border:1px solid #874457;border-radius:9px;padding:3px 8px;font-size:10px;font-weight:700;")
            self.b_start.setText("▶ CHẠY"); self.b_start.setObjectName("primary")
        self.b_start.style().unpolish(self.b_start); self.b_start.style().polish(self.b_start)

        self.balance.setText(_format_money(balance, currency) if balance else "Chờ MT5")
        self.equity.setText(_format_money(equity, currency) if equity else "—")
        self.floating.setText(f"{floating:+,.2f} {currency}".strip())
        self.floating.setStyleSheet(f"color:{QC['buy'] if floating >= 0 else QC['sell']};font-family:'{QF_MONO}';font-size:16px;font-weight:700;")
        self.dd.setText(f"{dd:+.2f}%")
        self.dd.setStyleSheet(f"color:{QC['sell'] if dd < -2 else QC['warn'] if dd < 0 else QC['buy']};font-family:'{QF_MONO}';font-size:16px;font-weight:700;")
        self.positions_count.setText(str(len(positions)))

        profile = _profile_from_status(status)
        if profile:
            self.base.setText(f"{float(profile.get('base_lot', 0)):.2f}")
            self.phase.setText(f"+{float(profile.get('phase_step', 0)):.2f}")
            if profile.get("unlimited_max_lot", False):
                broker_cap = float(profile.get("broker_volume_max", profile.get("max_lot", 0)) or 0)
                self.cap.setText(f"NO CAP · {broker_cap:.2f}")
            else:
                self.cap.setText(f"{float(profile.get('max_lot', 0)):.2f}")
            self.tp.setText(f"{float(profile.get('basket_tp', 0)):,.2f}")
        else:
            for label in [self.base, self.phase, self.cap, self.tp]:
                label.setText("Tự tính")

        self.table.setRowCount(len(positions))
        align_right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        center = Qt.AlignmentFlag.AlignCenter
        for row, pos in enumerate(positions):
            side = str(pos.get("type", ""))
            pnl = float(pos.get("profit", 0.0) or 0.0)
            data = [
                (str(pos.get("ticket", "")), QC['text'], center),
                (side, QC['buy'] if side == "BUY" else QC['sell'], center),
                (f"{float(pos.get('lot', 0) or 0):.2f}", QC['text'], align_right),
                (f"{pnl:+.2f}", QC['buy'] if pnl >= 0 else QC['sell'], align_right),
                (str(pos.get("time", "")), QC['text_dim'], center),
            ]
            row_bg = QC['bg_card'] if row % 2 == 0 else QC['bg_panel']
            for col, (value, color, alignment) in enumerate(data):
                item = QtWidgets.QTableWidgetItem(value)
                item.setTextAlignment(alignment)
                item.setForeground(QtGui.QColor(color))
                item.setBackground(QtGui.QColor(row_bg))
                self.table.setItem(row, col, item)
            self.table.setRowHeight(row, 31)


class FollowM1Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Follow M1 · M5 Supertrend AutoScale Pro — No Lot Cap")
        self.resize(1460, 930)
        self.accounts = {}
        self.cards = {}
        self.next_id = 1
        self.saved = self._load_saved()
        self._build()
        self.poll_timer = QTimer(self); self.poll_timer.timeout.connect(self._poll); self.poll_timer.start(200)
        self.refresh_timer = QTimer(self); self.refresh_timer.timeout.connect(self.refresh); self.refresh_timer.start(800)
        saved_ids = sorted((int(key) for key in self.saved.keys() if str(key).isdigit()))
        if saved_ids:
            for _ in saved_ids:
                self.add_account()
        else:
            self.add_account()

    def _load_saved(self):
        try:
            data = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
            return data.get("accounts", {}) if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self):
        data = {str(account_id): account.save_data() for account_id, account in self.accounts.items()}
        try:
            Path(CONFIG_FILE).write_text(json.dumps({"accounts": data}, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self.add_log(f"Không thể lưu cấu hình: {exc}", "error")

    def _build(self):
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        hero = QtWidgets.QFrame(); hero.setObjectName("hero")
        h = QtWidgets.QHBoxLayout(hero); h.setContentsMargins(20,12,20,12); h.setSpacing(10)
        left = QtWidgets.QVBoxLayout(); left.setSpacing(1)
        title = QtWidgets.QLabel("FOLLOW M1  /  M5 SUPERTREND  /  AUTOSCALE PRO"); title.setObjectName("heroTitle")
        sub = QtWidgets.QLabel("Supertrend M5 (ATR 10 / Factor 3.0 · CHỈ NẾN ĐÓNG) · 1 lệnh mỗi M1 · No Lot Cap · Pair Close tầng")
        sub.setObjectName("heroSub")
        left.addWidget(title); left.addWidget(sub); h.addLayout(left); h.addStretch(1)
        self.total_balance = self._header_metric("TOTAL BALANCE")
        self.total_float = self._header_metric("FLOATING")
        self.total_running = self._header_metric("RUNNING")
        h.addWidget(self.total_balance); h.addWidget(self.total_float); h.addWidget(self.total_running)
        add = QtWidgets.QPushButton("+ THÊM ACCOUNT"); add.setObjectName("ghost"); add.clicked.connect(self.add_account)
        start_all = QtWidgets.QPushButton("▶ CHẠY TẤT CẢ"); start_all.setObjectName("primary"); start_all.clicked.connect(self.start_all)
        stop_all = QtWidgets.QPushButton("■ DỪNG TẤT CẢ"); stop_all.setObjectName("danger"); stop_all.clicked.connect(self.stop_all)
        h.addWidget(add); h.addWidget(start_all); h.addWidget(stop_all)
        root.addWidget(hero)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.card_host = QtWidgets.QWidget()
        self.card_host.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.card_layout = QtWidgets.QVBoxLayout(self.card_host)
        self.card_layout.setContentsMargins(16,16,16,10)
        self.card_layout.setSpacing(12)
        # Không addStretch() ở đáy: card/table sẽ dùng phần chiều cao còn lại.
        scroll.setWidget(self.card_host)
        splitter.addWidget(scroll)

        log_box = QtWidgets.QFrame(); log_box.setObjectName("metricCard")
        lv = QtWidgets.QVBoxLayout(log_box); lv.setContentsMargins(12,10,12,10); lv.setSpacing(6)
        log_head = QtWidgets.QHBoxLayout(); log_title = QtWidgets.QLabel("LIVE EVENT LOG"); log_title.setObjectName("profileLabel")
        clear = QtWidgets.QPushButton("XÓA LOG"); clear.setObjectName("ghost"); clear.clicked.connect(lambda: self.log.clear())
        log_head.addWidget(log_title); log_head.addStretch(1); log_head.addWidget(clear); lv.addLayout(log_head)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("eventLog"); self.log.setReadOnly(True); self.log.document().setMaximumBlockCount(1500)
        lv.addWidget(self.log)
        splitter.addWidget(log_box); splitter.setSizes([650, 240])
        root.addWidget(splitter, 1)

    def _header_metric(self, label):
        box = QtWidgets.QWidget(); v = QtWidgets.QVBoxLayout(box); v.setContentsMargins(10,0,10,0); v.setSpacing(0)
        cap = QtWidgets.QLabel(label); cap.setObjectName("metricLabel")
        val = QtWidgets.QLabel("—"); val.setObjectName("metricValue")
        v.addWidget(cap); v.addWidget(val)
        return box

    def add_account(self):
        if len(self.accounts) >= MAX_ACCOUNTS:
            QtWidgets.QMessageBox.warning(self, "Giới hạn", f"Tối đa {MAX_ACCOUNTS} account.")
            return
        account_id = self.next_id; self.next_id += 1
        account = FollowM1Account(account_id, self.add_log, self.saved.get(str(account_id)))
        account.changed.connect(lambda _id: self.refresh())
        card = AccountCard(account)
        card.configureRequested.connect(self.configure_account)
        card.startStopRequested.connect(self.toggle_account)
        card.removeRequested.connect(self.remove_account)
        self.accounts[account_id] = account; self.cards[account_id] = card
        # Mỗi card được phép giãn theo chiều dọc. Với một account, bảng lệnh
        # sẽ lấp toàn bộ vùng trống phía trên Event Log.
        self.card_layout.addWidget(card, 1)
        self._save(); self.refresh()

    def configure_account(self, account_id):
        account = self.accounts.get(account_id)
        if not account:
            return
        dialog = ConnectionDialog(account, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._save(); self.refresh()
            self.add_log(f"[ACC #{account_id}] Đã lưu thiết lập kết nối. Khởi động lại bot để áp dụng.", "info")

    def remove_account(self, account_id):
        account = self.accounts.get(account_id)
        if not account:
            return
        if account.is_running():
            QtWidgets.QMessageBox.warning(self, "Đang chạy", "Dừng account trước khi xóa.")
            return
        answer = QtWidgets.QMessageBox.question(self, "Xóa account", f"Xóa Account #{account_id}?")
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        card = self.cards.pop(account_id); self.accounts.pop(account_id)
        card.setParent(None); card.deleteLater()
        self._save(); self.refresh()

    def toggle_account(self, account_id):
        account = self.accounts.get(account_id)
        if not account:
            return
        if account.is_running(): account.stop()
        else: account.start()
        self._save(); self.refresh()

    def start_all(self):
        for account in self.accounts.values():
            if not account.is_running(): account.start()
        self.refresh()

    def stop_all(self):
        for account in self.accounts.values():
            if account.is_running(): account.stop()
        self.refresh()

    def add_log(self, message, level="info", ts=None):
        now = ts or datetime.now().strftime("%H:%M:%S")
        prefix = {"error": "ERR", "warn": "WARN", "buy": "BUY", "sell": "SELL"}.get(level, "INFO")
        self.log.appendPlainText(f"[{now}] [{prefix}] {message}")

    def _poll(self):
        for account in self.accounts.values(): account.poll()

    def refresh(self):
        total_balance = total_float = 0.0; running = 0; currency = ""
        for account_id, account in self.accounts.items():
            self.cards[account_id].refresh()
            status = account.status
            total_balance += float(status.get("balance", 0.0) or 0.0)
            total_float += float(status.get("total_pnl", 0.0) or 0.0)
            currency = currency or status.get("currency", "")
            running += int(account.is_running())
        self._set_header_metric(self.total_balance, _format_money(total_balance, currency) if total_balance else "Chờ MT5")
        self._set_header_metric(self.total_float, f"{total_float:+,.2f} {currency}".strip())
        self._set_header_metric(self.total_running, f"{running} / {len(self.accounts)}")

    @staticmethod
    def _set_header_metric(widget, text):
        labels = widget.findChildren(QtWidgets.QLabel)
        if labels:
            labels[-1].setText(text)

    def closeEvent(self, event):
        self._save()
        for account in self.accounts.values():
            if account.is_running(): account.stop()
        event.accept()


# Backwards-compatible name used by the shared entry point below.
MainWindow = FollowM1Window


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════
def main():
    app=QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(_follow_m1_live_qss())
    try: app.setFont(QtGui.QFont(QF_UI,9))
    except Exception: pass
    win=MainWindow(); win.show()
    def _exc(et,ev,etb):
        msg="".join(traceback.format_exception(et,ev,etb))
        try:
            with open("followm1_autoscale_error.log","a",encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n{datetime.now()}\n{msg}")
        except Exception: pass
        try: QtWidgets.QMessageBox.critical(None,"Error",msg[:600])
        except Exception: pass
    sys.excepthook=_exc
    sys.exit(app.exec())

def _worker_load_cfg():
    try: return json.loads(sys.argv[2])
    except Exception: return {}

def _gui_main():
    if not _HAS_GUI:
        print("ERROR: Thieu PyQt6. Cai: pip install PyQt6"); sys.exit(1)
    main()

if __name__ == "__main__":
    if _WORKER_MODE: run(_worker_load_cfg())
    else: _gui_main()
