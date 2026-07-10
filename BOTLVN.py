#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EAGoldSuper MT5 Bot (XAUUSDc) with friendly PyQt6 GUI.

Run:
  python3 BOTLVN.py
  python3 BOTLVN.py --worker '{"json":"cfg"}'
"""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal
    _HAS_GUI = True
except ImportError:
    _HAS_GUI = False

    class _QtWidgetStubs:
        QDialog = object
        QFrame = object
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


def _import_worker_deps():
    global mt5, pd, np
    try:
        import MetaTrader5 as _mt5
        import numpy as _np
        import pandas as _pd
        mt5 = _mt5
        np = _np
        pd = _pd
        return True
    except Exception as exc:
        print(f"ERROR: missing worker dependencies: {exc}")
        print("Install: pip install MetaTrader5 pandas numpy")
        return False


mt5 = None
pd = None
np = None

_WORKER_MODE = "--worker" in sys.argv
if _WORKER_MODE:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    if not _import_worker_deps():
        sys.exit(1)


_stop = threading.Event()
_send_lock = threading.Lock()
BOT_BUILD = "2026-07-09-mode3-dca-m5-v24"
MODE2_MEAN_REV_LOCK_MINUTES = 90

MODE_LVN_1 = "mode1_lvn_adaptive"
MODE_SCALP_M1_2 = "mode2_m1_pullback"
MODE_DCA_M5_3 = "mode3_dca_m5"
MODE_LABELS = {
    MODE_LVN_1: "Mode 1 - LVN Profile Pro",
    MODE_SCALP_M1_2: "Mode 2 - M5 Six-Strategy Selector",
    MODE_DCA_M5_3: "Mode 3 - DCA M5 Safe",
}
MODE_MAGIC_OFFSETS = {
    MODE_LVN_1: 11,
    MODE_SCALP_M1_2: 22,
    MODE_DCA_M5_3: 33,
}
DEFAULT_ACTIVE_MODES = [MODE_LVN_1, MODE_SCALP_M1_2]
MODE2_STRATEGY_LABELS = {
    "trend_pullback": "Trend Pullback",
    "breakout": "Breakout",
    "mean_reversion": "Mean Reversion",
    "reversal_pa": "Reversal Price Action",
    "orderflow_proxy": "Orderflow/CHOCH (proxy)",
    "session_scalp": "Session-based scalp",
}
MODE_SHORT = {
    MODE_LVN_1: "m1",
    MODE_SCALP_M1_2: "m2",
    MODE_DCA_M5_3: "m3",
}
MODE_SHORT_INV = {v: k for k, v in MODE_SHORT.items()}
MODE2_STRATEGY_SHORT = {
    "trend_pullback": "tp",
    "breakout": "bo",
    "mean_reversion": "mr",
    "reversal_pa": "rp",
    "orderflow_proxy": "of",
    "session_scalp": "ss",
}
MODE2_STRATEGY_SHORT_INV = {v: k for k, v in MODE2_STRATEGY_SHORT.items()}
MODE1_FIXED_PROFILE = "balanced"
MODE1_PROFILE_RULES = {
    "safe": {"min_score": 2, "rr_min": 1.25, "tp2_r": 1.40, "lot_factor": 1.0},
    "balanced": {"min_score": 2, "rr_min": 1.20, "tp2_r": 1.30, "lot_factor": 1.0},
    "aggressive": {"min_score": 1, "rr_min": 1.20, "tp2_r": 1.20, "lot_factor": 0.5},
}
MODE1_SL_MAX = 5.0
MODE2_MIN_SCORE = 0
MODE2_RR_MIN = 1.2
MODE2_SL_MAX = 6.0
MODE2_SETUP_LOCK_MINUTES = 45
MODE3_DCA_MAX_LAYERS = 3
MODE3_DCA_SPACING_ATR = (0.7, 0.9)
MODE3_DCA_LOT_FACTORS = (1.0, 1.3, 1.6)
MODE3_DCA_BASKET_SL_PCT = 1.5
MODE3_DCA_BASKET_TP_PCT = 1.0

_today_mode_cache = {}
_setup_lock_cache = {}
_closed_deal_log_cache = {}
_mode2_be_cache = {}
_mode3_dca_cache = {}
_mode2_market_diag_cache = {}
MODE2_BE_RULES = {
    # be_r: move to BE+buffer at this R
    # lock_r / lock_gain_r: lock profit at this R (SL = entry +/- lock_gain_r * R)
    "trend_pullback": {"be_r": 0.9, "lock_r": 1.4, "lock_gain_r": 0.30},
    "breakout": {"be_r": 1.2, "lock_r": 1.8, "lock_gain_r": 0.50},
    "mean_reversion": {"be_r": 0.7, "lock_r": 1.1, "lock_gain_r": 0.25},
    "reversal_pa": {"be_r": 1.0, "lock_r": 1.5, "lock_gain_r": 0.35},
    "orderflow_proxy": {"be_r": 0.9, "lock_r": 1.4, "lock_gain_r": 0.40},
    "session_scalp": {"be_r": 0.7, "lock_r": 1.0, "lock_gain_r": 0.20},
}


def _mode2_reason_bucket(reason_text):
    s = str(reason_text or "").lower()
    if "outside" in s or "ngoài phiên" in s:
        return "outside-session"
    if "trend direction chưa đạt" in s or "trend direction" in s:
        return "trend-gate"
    if "volume chưa đạt" in s:
        return "volume-weak"
    if "thân nến breakout yếu" in s:
        return "breakout-body-weak"
    if "rsi trung tính" in s:
        return "rsi-neutral"
    if "rsi chưa vào vùng cực trị" in s:
        return "rsi-not-extreme"
    if "chưa chạm biên bollinger" in s:
        return "bb-not-touch"
    if "thị trường chưa đủ sideway" in s:
        return "not-sideway"
    if "s/r mạnh" in s or "s/r" in s:
        return "sr-not-touch"
    if "chưa có mẫu nến đảo chiều" in s:
        return "no-reversal-pattern"
    if "chưa có quét đỉnh/đáy" in s:
        return "no-sweep"
    if "chưa có phá cấu trúc choch" in s:
        return "no-choch"
    if "không có sl hợp lý" in s:
        return "invalid-sl"
    if "không đủ rr" in s:
        return "rr-too-low"
    if "impulse" in s:
        return "impulse-block"
    return "other"


def update_mode2_market_diag(mode2_sig):
    """Collect compact market-state telemetry for later log-based diagnosis."""
    if not isinstance(mode2_sig, dict):
        return None
    t_now = int(mode2_sig.get("m5_time", 0) or 0)
    if t_now <= 0:
        return None
    day_key = datetime.utcfromtimestamp(t_now).strftime("%Y-%m-%d")
    diag = _mode2_market_diag_cache.setdefault(
        day_key,
        {
            "last_bar_time": 0,
            "bars": 0,
            "trend": {"buy": 0, "sell": 0, "flat": 0},
            "session": {"london_ny": 0, "off": 0},
            "vol": {"low": 0, "ok": 0, "high": 0},
            "atr": {"calm": 0, "normal": 0, "volatile": 0},
            "reason": {},
            "strategy": {},
        },
    )
    if int(diag.get("last_bar_time", 0)) == t_now:
        return None
    diag["last_bar_time"] = t_now
    diag["bars"] += 1
    ctx = mode2_sig.get("market_ctx", {}) if isinstance(mode2_sig.get("market_ctx"), dict) else {}
    trend_key = "flat"
    if bool(ctx.get("trend_buy", False)):
        trend_key = "buy"
    elif bool(ctx.get("trend_sell", False)):
        trend_key = "sell"
    diag["trend"][trend_key] = int(diag["trend"].get(trend_key, 0)) + 1
    ses_key = "london_ny" if bool(ctx.get("in_london_ny", False)) else "off"
    diag["session"][ses_key] = int(diag["session"].get(ses_key, 0)) + 1
    vol_ratio = float(ctx.get("vol_ratio", 1.0) or 1.0)
    if vol_ratio < 0.85:
        v_key = "low"
    elif vol_ratio > 1.20:
        v_key = "high"
    else:
        v_key = "ok"
    diag["vol"][v_key] = int(diag["vol"].get(v_key, 0)) + 1
    atr_rank = float(mode2_sig.get("atr_rank", 0.5) or 0.5)
    if atr_rank < 0.35:
        a_key = "calm"
    elif atr_rank < 0.75:
        a_key = "normal"
    else:
        a_key = "volatile"
    diag["atr"][a_key] = int(diag["atr"].get(a_key, 0)) + 1

    status_map = mode2_sig.get("strategy_status", {}) if isinstance(mode2_sig.get("strategy_status"), dict) else {}
    for sid, st in status_map.items():
        if not isinstance(st, dict):
            continue
        sdiag = diag["strategy"].setdefault(str(sid), {"buy": 0, "sell": 0, "wait": 0, "reasons": {}})
        state = str(st.get("state", "WAIT") or "WAIT").upper()
        if state == "BUY":
            sdiag["buy"] += 1
        elif state == "SELL":
            sdiag["sell"] += 1
        else:
            sdiag["wait"] += 1
            bucket = _mode2_reason_bucket(st.get("reason", ""))
            sdiag["reasons"][bucket] = int(sdiag["reasons"].get(bucket, 0)) + 1
            diag["reason"][bucket] = int(diag["reason"].get(bucket, 0)) + 1
    return diag


def format_mode2_market_summary(diag, top_n=4):
    if not isinstance(diag, dict) or int(diag.get("bars", 0)) <= 0:
        return "-"
    bars = max(1, int(diag.get("bars", 0)))
    trend = diag.get("trend", {})
    atr = diag.get("atr", {})
    ses = diag.get("session", {})
    rs = diag.get("reason", {})
    top_reasons = sorted(rs.items(), key=lambda kv: kv[1], reverse=True)[: max(1, int(top_n))]
    top_txt = ", ".join(f"{k}:{v}" for k, v in top_reasons) if top_reasons else "-"
    return (
        f"bars={bars} | trend buy/sell/flat={trend.get('buy',0)}/{trend.get('sell',0)}/{trend.get('flat',0)} "
        f"| atr calm/normal/volatile={atr.get('calm',0)}/{atr.get('normal',0)}/{atr.get('volatile',0)} "
        f"| session on/off={ses.get('london_ny',0)}/{ses.get('off',0)} | top_block={top_txt}"
    )


def send(obj):
    with _send_lock:
        print(json.dumps(obj, ensure_ascii=False), flush=True)


def log(msg, level="info"):
    send(
        {
            "type": "log",
            "level": level,
            "msg": msg,
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
    )


def _stdin_watch():
    for line in sys.stdin:
        try:
            cmd = json.loads(line.strip())
            if cmd.get("cmd") == "stop":
                _stop.set()
                return
        except Exception:
            continue


def init_mt5(cfg):
    retries = 8
    wait_s = 5
    kw = {}
    if cfg.get("path"):
        kw["path"] = cfg["path"]

    for attempt in range(1, retries + 1):
        log(f"MT5 initialize {attempt}/{retries}...")
        try:
            ok = mt5.initialize(**kw)
        except Exception as exc:
            ok = False
            log(f"initialize exception: {exc}", "warn")
        if ok:
            break
        log(f"initialize failed: {mt5.last_error()} (retry {wait_s}s)", "warn")
        try:
            mt5.shutdown()
        except Exception:
            pass
        time.sleep(wait_s)
    else:
        log("MT5 initialize failed", "error")
        return False

    if cfg.get("login") and cfg.get("password"):
        login_kw = {
            "login": int(cfg["login"]),
            "password": str(cfg["password"]),
        }
        if cfg.get("server"):
            login_kw["server"] = str(cfg["server"])
        for attempt in range(1, retries + 1):
            log(f"MT5 login {attempt}/{retries}...")
            if mt5.login(**login_kw):
                break
            log(f"login failed: {mt5.last_error()} (retry {wait_s}s)", "warn")
            time.sleep(wait_s)
        else:
            log("MT5 login failed", "error")
            mt5.shutdown()
            return False

    acc = mt5.account_info()
    if acc is None:
        log("account_info failed", "error")
        mt5.shutdown()
        return False
    if float(acc.balance) <= 0:
        log(f"balance invalid: {acc.balance}", "error")
        mt5.shutdown()
        return False

    sym = mt5.symbol_info(cfg["symbol"])
    if sym is None:
        log(f"symbol {cfg['symbol']} not found", "error")
        mt5.shutdown()
        return False
    if not sym.visible:
        mt5.symbol_select(cfg["symbol"], True)

    log(f"Connected #{acc.login} | balance={acc.balance:.2f} {acc.currency}")
    return True


def round_lot(lot, sym):
    step = float(getattr(sym, "volume_step", 0.01) or 0.01)
    vmin = float(getattr(sym, "volume_min", 0.01) or 0.01)
    vmax = float(getattr(sym, "volume_max", 100.0) or 100.0)
    q = round(round(float(lot) / step) * step, 8)
    return max(vmin, min(vmax, q))


def mode_magic(cfg, mode):
    base = int(cfg.get("magic", 700100))
    return base + int(MODE_MAGIC_OFFSETS.get(mode, 0))


def my_positions(cfg, mode=None):
    pos = mt5.positions_get(symbol=cfg["symbol"]) or []
    magic = int(mode_magic(cfg, mode)) if mode else int(cfg.get("magic", 0))
    if magic == 0:
        return list(pos)
    return [p for p in pos if p.magic == magic]


def account_positions_all_modes(cfg):
    pos = mt5.positions_get(symbol=cfg["symbol"]) or []
    mode_magics = {mode_magic(cfg, m) for m in MODE_LABELS}
    return [p for p in pos if int(getattr(p, "magic", 0)) in mode_magics]


def strategy_id_from_comment(comment, mode_id):
    c = str(comment or "")
    if c.startswith("EGS"):
        token = c[3:]
        # New safest format: alnum only, e.g. EGSm2tp
        if len(token) >= 2:
            mshort = token[:2]
            parsed_mode = MODE_SHORT_INV.get(mshort, None)
            if parsed_mode != mode_id:
                return None
            if mode_id == MODE_SCALP_M1_2:
                sshort = token[2:4] if len(token) >= 4 else ""
                return MODE2_STRATEGY_SHORT_INV.get(sshort, "trend_pullback" if not sshort else None)
            return None
    if c.startswith("EGS:"):
        parts = c.split(":")
        if len(parts) >= 2:
            parsed_mode = MODE_SHORT_INV.get(parts[1], None)
            if parsed_mode != mode_id:
                return None
            if len(parts) >= 3 and mode_id == MODE_SCALP_M1_2:
                return MODE2_STRATEGY_SHORT_INV.get(parts[2], None)
            if mode_id == MODE_SCALP_M1_2:
                return "trend_pullback"
            return None
    if not c.startswith("EAGoldSuper:"):
        return None
    parts = c.split(":")
    if len(parts) >= 3 and parts[1] == mode_id:
        return parts[2]
    # Backward compatibility with old Mode 2 comment format.
    if len(parts) == 2 and parts[1] == MODE_SCALP_M1_2 and mode_id == MODE_SCALP_M1_2:
        return "trend_pullback"
    return None


def count_strategy_positions(cfg, mode_id, strategy_id):
    if not strategy_id:
        return 0
    pos = my_positions(cfg, mode_id)
    out = 0
    for p in pos:
        sid = strategy_id_from_comment(getattr(p, "comment", ""), mode_id)
        if sid == strategy_id:
            out += 1
    return out


def is_setup_temporarily_locked(cfg, mode_id, strategy_id, lock_minutes=45, min_losses=2):
    if not strategy_id or mode_id != MODE_SCALP_M1_2:
        return False, 0, 0
    now = time.time()
    key = (id(cfg), mode_id, strategy_id, int(lock_minutes), int(min_losses))
    cached = _setup_lock_cache.get(key)
    if cached and now - float(cached.get("t", 0.0)) < 10.0:
        return bool(cached.get("locked", False)), int(cached.get("losses", 0)), int(cached.get("remaining_sec", 0))
    try:
        start = datetime.combine(datetime.now().date(), datetime.min.time())
        deals = mt5.history_deals_get(start, datetime.now())
        if deals is None:
            _setup_lock_cache[key] = {"t": now, "locked": False, "losses": 0, "remaining_sec": 0}
            return False, 0, 0
        magic = int(mode_magic(cfg, mode_id))
        rows = []
        for d in deals:
            if int(getattr(d, "magic", 0)) != magic:
                continue
            if int(getattr(d, "entry", -1)) != int(mt5.DEAL_ENTRY_OUT):
                continue
            sid = strategy_id_from_comment(getattr(d, "comment", ""), mode_id)
            if sid != strategy_id:
                continue
            pnl = float(getattr(d, "profit", 0.0) or 0.0) + float(getattr(d, "swap", 0.0) or 0.0) + float(getattr(d, "commission", 0.0) or 0.0)
            rows.append((int(getattr(d, "time", 0)), pnl))
        rows.sort(key=lambda x: x[0])
        losses = 0
        last_loss_ts = 0
        for ts, pnl in reversed(rows):
            if pnl < 0:
                losses += 1
                if last_loss_ts == 0:
                    last_loss_ts = ts
            else:
                break
        locked = False
        remaining_sec = 0
        if losses >= int(min_losses) and last_loss_ts > 0:
            lock_total = float(lock_minutes) * 60.0
            elapsed = now - float(last_loss_ts)
            locked = elapsed <= lock_total
            if locked:
                remaining_sec = max(0, int(lock_total - elapsed))
        _setup_lock_cache[key] = {"t": now, "locked": locked, "losses": losses, "remaining_sec": remaining_sec}
        return locked, losses, remaining_sec
    except Exception:
        _setup_lock_cache[key] = {"t": now, "locked": False, "losses": 0, "remaining_sec": 0}
        return False, 0, 0


def mode_open_sides(cfg, mode_id):
    out = set()
    for p in my_positions(cfg, mode_id):
        ptype = int(getattr(p, "type", -1))
        if ptype == int(mt5.POSITION_TYPE_BUY):
            out.add("BUY")
        elif ptype == int(mt5.POSITION_TYPE_SELL):
            out.add("SELL")
    return out


def manage_mode2_break_even(cfg):
    """Dynamic TP1 partial + BE/lock management for Mode 2 positions."""
    if mt5 is None:
        return
    mode_id = MODE_SCALP_M1_2
    positions = my_positions(cfg, mode_id)
    if not positions:
        return
    info = mt5.symbol_info(cfg["symbol"])
    tick = mt5.symbol_info_tick(cfg["symbol"])
    if info is None or tick is None:
        return
    point = float(getattr(info, "point", 0.01) or 0.01)
    digits = int(getattr(info, "digits", 2) or 2)
    min_stop_dist = float(getattr(info, "trade_stops_level", 0.0) or 0.0) * point
    ask = float(getattr(tick, "ask", 0.0) or 0.0)
    bid = float(getattr(tick, "bid", 0.0) or 0.0)

    active_tickets = set()
    now_ts = time.time()
    for p in positions:
        try:
            ticket = int(getattr(p, "ticket", 0) or 0)
            if ticket <= 0:
                continue
            active_tickets.add(ticket)
            side = "BUY" if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY) else "SELL"
            entry = float(getattr(p, "price_open", 0.0) or 0.0)
            sl_cur = float(getattr(p, "sl", 0.0) or 0.0)
            tp_cur = float(getattr(p, "tp", 0.0) or 0.0)
            vol = float(getattr(p, "volume", 0.0) or 0.0)
            sid = strategy_id_from_comment(getattr(p, "comment", ""), mode_id) or "-"
            if entry <= 0 or sl_cur <= 0:
                continue

            st = _mode2_be_cache.setdefault(ticket, {})
            def log_once(diag_key, message, level="info", cooldown=120.0):
                last_key = str(st.get("last_diag_key", ""))
                last_t = float(st.get("last_diag_t", 0.0) or 0.0)
                if last_key == str(diag_key) and (now_ts - last_t) < float(cooldown):
                    return
                st["last_diag_key"] = str(diag_key)
                st["last_diag_t"] = float(now_ts)
                log(message, level)
            init_risk = float(st.get("init_risk", 0.0) or 0.0)
            if init_risk <= 0:
                init_risk = abs(entry - sl_cur)
            if init_risk <= 0:
                continue
            st_stage = str(st.get("stage", "init"))
            tp1 = float(st.get("tp1", 0.0) or 0.0)
            tp1_done = bool(st.get("tp1_done", False))
            if tp1 <= 0:
                tp1 = entry + init_risk if side == "BUY" else entry - init_risk
            st["init_risk"] = init_risk
            st["stage"] = st_stage
            st["tp1"] = tp1
            st["tp1_done"] = tp1_done

            move = (bid - entry) if side == "BUY" else (entry - ask)
            if move <= 0:
                continue

            # 1) Expert-style partial at TP1 (close 50% of volume once).
            if not tp1_done:
                tp1_hit = (bid >= tp1) if side == "BUY" else (ask <= tp1)
                if tp1_hit:
                    vol_min = float(getattr(info, "volume_min", 0.01) or 0.01)
                    close_vol = round_lot(vol * 0.5, info)
                    if close_vol >= vol:
                        close_vol = round_lot(max(vol_min, vol - vol_min), info)
                    if close_vol >= vol_min and close_vol < vol:
                        close_req = {
                            "action": mt5.TRADE_ACTION_DEAL,
                            "symbol": cfg["symbol"],
                            "position": ticket,
                            "volume": close_vol,
                            "type": mt5.ORDER_TYPE_SELL if side == "BUY" else mt5.ORDER_TYPE_BUY,
                            "price": bid if side == "BUY" else ask,
                            "deviation": int(cfg.get("deviation", 25)),
                            "magic": int(getattr(p, "magic", 0) or 0),
                            "comment": "EGSTP1",
                            "type_time": mt5.ORDER_TIME_GTC,
                        }
                        fill_modes = [int(getattr(info, "filling_mode", -1)), mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
                        sent = False
                        for fm in fill_modes:
                            if not isinstance(fm, int) or fm < 0:
                                continue
                            close_req["type_filling"] = fm
                            res = mt5.order_send(close_req)
                            if res is not None and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                                sent = True
                                break
                        if sent:
                            tp1_done = True
                            st["tp1_done"] = True
                            st["last_diag_key"] = ""
                            st["last_diag_t"] = 0.0
                            log(
                                f"[Mode 2 - TP1/{MODE2_STRATEGY_LABELS.get(sid, sid)}] ticket={ticket} {side} "
                                f"close50%={close_vol:.2f}/{vol:.2f} @tp1={tp1:.2f}",
                                "info",
                            )
                        else:
                            log_once(
                                "tp1-send-fail",
                                f"[Mode 2 - TP1/{MODE2_STRATEGY_LABELS.get(sid, sid)}] ticket={ticket} {side} "
                                f"partial close failed | last_error={mt5.last_error()}",
                                "warn",
                            )
                    else:
                        log_once(
                            "tp1-skip-volume",
                            f"[Mode 2 - TP1/{MODE2_STRATEGY_LABELS.get(sid, sid)}] ticket={ticket} {side} "
                            f"skip partial: volume too small ({vol:.2f})",
                            "info",
                        )

            rules = MODE2_BE_RULES.get(sid, {"be_r": 1.0, "lock_r": 1.5, "lock_gain_r": 0.30})
            move_r = move / max(1e-9, init_risk)
            spread = max(0.0, ask - bid)
            be_lock = max(0.08 * init_risk, min_stop_dist * 1.1, spread * 1.5, point * 8.0)

            target_stage = None
            if move_r >= float(rules.get("lock_r", 1.5)):
                lock_gain_r = max(0.05, float(rules.get("lock_gain_r", 0.30)))
                lock_gain = lock_gain_r * init_risk
                new_sl = (entry + lock_gain) if side == "BUY" else (entry - lock_gain)
                target_stage = "lock"
            elif move_r >= float(rules.get("be_r", 1.0)):
                new_sl = (entry + be_lock) if side == "BUY" else (entry - be_lock)
                target_stage = "be"
            else:
                continue

            # Don't downgrade stage (e.g., lock -> be).
            if st_stage == "lock" and target_stage != "lock":
                continue

            if side == "BUY":
                max_allowed = bid - max(min_stop_dist * 1.05, point * 2.0)
                new_sl = min(new_sl, max_allowed)
                if new_sl <= sl_cur + point * 0.5:
                    log_once(
                        f"{target_stage}-skip-tight-{side}",
                        f"[Mode 2 - {target_stage.upper()}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] "
                        f"ticket={ticket} skip SL move: broker distance/tight spread",
                        "info",
                    )
                    continue
            else:
                min_allowed = ask + max(min_stop_dist * 1.05, point * 2.0)
                new_sl = max(new_sl, min_allowed)
                if new_sl >= sl_cur - point * 0.5:
                    log_once(
                        f"{target_stage}-skip-tight-{side}",
                        f"[Mode 2 - {target_stage.upper()}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] "
                        f"ticket={ticket} skip SL move: broker distance/tight spread",
                        "info",
                    )
                    continue

            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": cfg["symbol"],
                "position": ticket,
                "sl": round(new_sl, digits),
                "tp": round(tp_cur, digits) if tp_cur > 0 else 0.0,
                "magic": int(getattr(p, "magic", 0) or 0),
                "comment": "EGSBE",
            }
            res = mt5.order_send(req)
            if res is None or getattr(res, "retcode", None) != mt5.TRADE_RETCODE_DONE:
                log_once(
                    f"{target_stage}-sltp-fail",
                    f"[Mode 2 - {target_stage.upper()}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] "
                    f"ticket={ticket} SLTP modify failed ret={getattr(res, 'retcode', None)} "
                    f"comment={getattr(res, 'comment', '')} last_error={mt5.last_error()}",
                    "warn",
                )
                continue

            st["stage"] = target_stage
            st["tp1"] = tp1
            st["tp1_done"] = tp1_done
            st["last_diag_key"] = ""
            st["last_diag_t"] = 0.0
            log(
                f"[Mode 2 - {target_stage.upper()}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] "
                f"ticket={ticket} {side} vol={vol:.2f} move={move:.2f} ({move_r:.2f}R) "
                f"| SL {sl_cur:.2f} -> {new_sl:.2f}",
                "info",
            )
        except Exception:
            continue

    # Cleanup stale cache entries from closed positions.
    stale = [k for k in _mode2_be_cache.keys() if k not in active_tickets]
    for k in stale:
        _mode2_be_cache.pop(k, None)


def normalize_mode_settings(cfg):
    modes = cfg.get("modes", {})
    if not isinstance(modes, dict):
        modes = {}
    for mode in MODE_LABELS:
        mcfg = modes.get(mode, {})
        if not isinstance(mcfg, dict):
            mcfg = {}
        mcfg.setdefault("enabled", mode in DEFAULT_ACTIVE_MODES)
        if mode == MODE_SCALP_M1_2:
            strat_cfg = mcfg.get("strategies", {})
            if not isinstance(strat_cfg, dict):
                strat_cfg = {}
            for sid in MODE2_STRATEGY_LABELS:
                strat_cfg.setdefault(sid, True)
            mcfg["strategies"] = strat_cfg
        modes[mode] = mcfg
    cfg["modes"] = modes
    cfg["active_modes"] = [m for m in MODE_LABELS if modes.get(m, {}).get("enabled", False)]
    if not cfg["active_modes"]:
        cfg["active_modes"] = list(DEFAULT_ACTIVE_MODES)
        for m in cfg["active_modes"]:
            cfg["modes"][m]["enabled"] = True
    return cfg


def atr_series(df, period=14):
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    hl = high - low
    hcp = (high - close.shift()).abs()
    lcp = (low - close.shift()).abs()
    tr = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi_series(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_down = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def in_news_blackout(cfg, ts_utc):
    windows = cfg.get("news_blackout_windows_utc", [])
    if not isinstance(windows, list) or not windows:
        return False
    dt = datetime.utcfromtimestamp(int(ts_utc))
    now_m = dt.hour * 60 + dt.minute
    for w in windows:
        if not isinstance(w, dict):
            continue
        sh = str(w.get("start", "")).strip()
        eh = str(w.get("end", "")).strip()
        try:
            sm = int(sh.split(":")[0]) * 60 + int(sh.split(":")[1])
            em = int(eh.split(":")[0]) * 60 + int(eh.split(":")[1])
        except Exception:
            continue
        if sm <= em and sm <= now_m <= em:
            return True
        if sm > em and (now_m >= sm or now_m <= em):
            return True
    return False


def build_lvn_levels(df, bins=24, lvn_count=4):
    low = float(df["low"].min())
    high = float(df["high"].max())
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        return []
    edges = np.linspace(low, high, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    vol = np.zeros(bins, dtype=float)
    close_v = df["close"].to_numpy(dtype=float)
    volume = df["tick_volume"].to_numpy(dtype=float)
    idx = np.clip(np.digitize(close_v, edges) - 1, 0, bins - 1)
    for i, b in enumerate(idx):
        vol[b] += volume[i]
    ranked = np.argsort(vol)[: max(1, int(lvn_count))]
    return sorted(float(centers[i]) for i in ranked)


def build_volume_profile_summary(df, bins=36, value_area=0.70):
    if df is None or len(df) < 30:
        return None
    low = float(df["low"].min())
    high = float(df["high"].max())
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        return None
    bins = max(16, int(bins))
    edges = np.linspace(low, high, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    vol = np.zeros(bins, dtype=float)
    close_v = df["close"].to_numpy(dtype=float)
    volume = df["tick_volume"].to_numpy(dtype=float)
    idx = np.clip(np.digitize(close_v, edges) - 1, 0, bins - 1)
    for j, b in enumerate(idx):
        vol[b] += max(1.0, float(volume[j]))
    total = float(vol.sum())
    if total <= 0:
        return None
    poc_i = int(np.argmax(vol))
    poc = float(centers[poc_i])
    target = total * float(value_area)
    picked = {poc_i}
    acc = float(vol[poc_i])
    left = poc_i - 1
    right = poc_i + 1
    while acc < target and (left >= 0 or right < bins):
        lvol = float(vol[left]) if left >= 0 else -1.0
        rvol = float(vol[right]) if right < bins else -1.0
        if rvol >= lvol:
            if right < bins:
                picked.add(right)
                acc += float(vol[right])
                right += 1
            else:
                picked.add(left)
                acc += float(vol[left])
                left -= 1
        else:
            if left >= 0:
                picked.add(left)
                acc += float(vol[left])
                left -= 1
            else:
                picked.add(right)
                acc += float(vol[right])
                right += 1
    sel = sorted(picked)
    vah = float(centers[max(sel)])
    val = float(centers[min(sel)])
    hvn_idx = np.argsort(vol)[-max(3, bins // 8):]
    lvn_idx = np.argsort(vol)[: max(3, bins // 8)]
    hvn = sorted(float(centers[k]) for k in hvn_idx)
    lvn = sorted(float(centers[k]) for k in lvn_idx)
    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "hvn": hvn,
        "lvn": lvn,
        "edges": centers.tolist(),
        "vol": vol.tolist(),
    }


def nearest_level(levels, px):
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x - px))


def compute_mode1_lvn_signal(cfg):
    bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M5, 0, 900)
    if bars is None or len(bars) < 320:
        return None
    df = pd.DataFrame(bars).iloc[:-1].reset_index(drop=True)  # closed bars only
    if len(df) < 320:
        return None

    atr = atr_series(df, 14)
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    ema200 = df["close"].ewm(span=200, adjust=False).mean()
    rsi = rsi_series(df["close"].astype(float), 14)

    bars_m15 = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M15, 0, 320)
    bars_h1 = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_H1, 0, 260)
    if bars_m15 is None or len(bars_m15) < 120 or bars_h1 is None or len(bars_h1) < 100:
        return None
    d15 = pd.DataFrame(bars_m15).iloc[:-1].reset_index(drop=True)
    dh1 = pd.DataFrame(bars_h1).iloc[:-1].reset_index(drop=True)
    e20_15 = d15["close"].ewm(span=20, adjust=False).mean()
    e50_15 = d15["close"].ewm(span=50, adjust=False).mean()
    e200_15 = d15["close"].ewm(span=200, adjust=False).mean()
    e20_h1 = dh1["close"].ewm(span=20, adjust=False).mean()
    e50_h1 = dh1["close"].ewm(span=50, adjust=False).mean()
    e200_h1 = dh1["close"].ewm(span=200, adjust=False).mean()

    i = len(df) - 1
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    prev2 = df.iloc[i - 2]
    t_now = int(row["time"])
    close = float(row["close"])
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    prev_close = float(prev["close"])
    prev_open = float(prev["open"])
    prev_high = float(prev["high"])
    prev_low = float(prev["low"])
    a = float(atr.iloc[i]) if float(atr.iloc[i]) > 0 else 0.0
    if a <= 0:
        return None

    vp_lookback = max(24, min(int(cfg.get("mode1_vp_lookback", 36)), 48))
    hist = df.iloc[max(0, i - vp_lookback): i]
    vp = build_volume_profile_summary(hist, bins=int(cfg.get("mode1_vp_bins", 40)), value_area=0.70)
    if not vp:
        return {
            "side": None,
            "reason": "NO TRADE | Không có LVN rõ",
            "m5_time": t_now,
            "close": close,
            "atr": a,
            "lvn": close,
            "buy_price_hint": None,
            "sell_price_hint": None,
            "atr_rank": 0.5,
            "trend_strength": 0.0,
            "strategy_id": "no-trade",
        }

    poc = float(vp["poc"])
    vah = float(vp["vah"])
    val = float(vp["val"])
    hvn_levels = [float(x) for x in (vp.get("hvn") or [])]
    lvn_levels = [float(x) for x in (vp.get("lvn") or [])]
    if not lvn_levels:
        return {
            "side": None,
            "reason": "NO TRADE | Không có LVN rõ",
            "m5_time": t_now,
            "close": close,
            "atr": a,
            "lvn": close,
            "buy_price_hint": None,
            "sell_price_hint": None,
            "atr_rank": 0.5,
            "trend_strength": 0.0,
            "strategy_id": "no-trade",
        }

    atr_hist = atr.iloc[max(0, i - 288): i + 1].dropna().to_numpy(dtype=float)
    atr_rank = float((atr_hist <= a).sum()) / float(len(atr_hist)) if len(atr_hist) >= 8 else 0.5
    trend_strength = abs(float(e20_15.iloc[-1]) - float(e50_15.iloc[-1])) / max(1e-9, a)

    lvn_main = nearest_level(lvn_levels, close)
    if lvn_main is None:
        lvn_main = close
    # Fallback to short-window LVN when full profile LVN drifts too far.
    lvn_dist_atr = abs(close - float(lvn_main)) / max(1e-9, a)
    if lvn_dist_atr > 0.6:
        short_window = max(12, min(int(cfg.get("mode1_lvn_short_window", 18)), 24))
        short_hist = df.iloc[max(0, i - short_window): i]
        short_levels = build_lvn_levels(
            short_hist,
            bins=int(cfg.get("mode1_lvn_short_bins", 24)),
            lvn_count=int(cfg.get("mode1_lvn_short_count", 4)),
        )
        short_near = nearest_level(short_levels, close)
        if isinstance(short_near, (int, float)):
            lvn_main = float(short_near)
    lvn_dist_atr = abs(close - float(lvn_main)) / max(1e-9, a)
    cfg_cap = float(cfg.get("mode1_lvn_max_dist_atr", 0.9))
    if atr_rank < 0.35:
        regime_cap = 0.6
        regime_label = "calm"
    elif atr_rank < 0.75:
        regime_cap = 0.8
        regime_label = "normal"
    else:
        regime_cap = 1.0
        regime_label = "volatile"
    max_lvn_dist_atr = min(cfg_cap, regime_cap)
    if lvn_dist_atr > max_lvn_dist_atr:
        watch_buy = float(lvn_main + 0.06 * a)
        watch_sell = float(lvn_main - 0.06 * a)
        summary = (
            "NO TRADE\n\nLý do:\n- "
            f"LVN drift xa giá hiện tại (close={close:.2f}, LVN={lvn_main:.2f}, lệch={abs(close-lvn_main):.2f} ~ {lvn_dist_atr:.2f} ATR, cap={max_lvn_dist_atr:.2f} [{regime_label}])\n- "
            "Mode 1 tạm đứng ngoài, ưu tiên Mode 2 trong pha thị trường này"
        )
        return {
            "side": None,
            "reason": summary,
            "m5_time": t_now,
            "close": close,
            "atr": a,
            "lvn": float(lvn_main),
            "buy_price_hint": watch_buy,
            "sell_price_hint": watch_sell,
            "atr_rank": atr_rank,
            "trend_strength": trend_strength,
            "strategy_id": "no-trade",
        }

    # Session ranges (UTC for VN timezone behavior).
    ts = pd.to_datetime(df["time"], unit="s")
    day_key = datetime.utcfromtimestamp(t_now).strftime("%Y-%m-%d")
    day_mask = ts.dt.strftime("%Y-%m-%d") == day_key
    asia_mask = day_mask & (ts.dt.hour < 7)
    london_mask = day_mask & (ts.dt.hour >= 7) & (ts.dt.hour < 12)
    asia_hi = float(df.loc[asia_mask, "high"].max()) if asia_mask.any() else close
    asia_lo = float(df.loc[asia_mask, "low"].min()) if asia_mask.any() else close
    london_hi = float(df.loc[london_mask, "high"].max()) if london_mask.any() else close
    london_lo = float(df.loc[london_mask, "low"].min()) if london_mask.any() else close

    sr_sup = min(float(d15["low"].iloc[-40:].min()), float(dh1["low"].iloc[-20:].min()))
    sr_res = max(float(d15["high"].iloc[-40:].max()), float(dh1["high"].iloc[-20:].max()))
    swing_lo = float(df["low"].iloc[i - 16:i].min())
    swing_hi = float(df["high"].iloc[i - 16:i].max())

    rsi_now = float(rsi.iloc[i])
    rsi_prev = float(rsi.iloc[i - 1])
    body = abs(close - open_)
    rng = max(1e-9, high - low)

    trend_h1_up = float(dh1["close"].iloc[-1]) > float(e50_h1.iloc[-1]) > float(e200_h1.iloc[-1]) and float(e20_h1.iloc[-1]) > float(e50_h1.iloc[-1])
    trend_h1_dn = float(dh1["close"].iloc[-1]) < float(e50_h1.iloc[-1]) < float(e200_h1.iloc[-1]) and float(e20_h1.iloc[-1]) < float(e50_h1.iloc[-1])
    trend_m15_up = float(d15["close"].iloc[-1]) > float(e50_15.iloc[-1]) > float(e200_15.iloc[-1]) and float(e20_15.iloc[-1]) > float(e50_15.iloc[-1])
    trend_m15_dn = float(d15["close"].iloc[-1]) < float(e50_15.iloc[-1]) < float(e200_15.iloc[-1]) and float(e20_15.iloc[-1]) < float(e50_15.iloc[-1])
    h1_state = "trend tăng" if trend_h1_up else ("trend giảm" if trend_h1_dn else "đi ngang")
    m15_state = "đồng thuận H1" if (trend_h1_up and trend_m15_up) or (trend_h1_dn and trend_m15_dn) else "ngược/không đồng thuận H1"

    # v2-loose+: split hard/soft filters to avoid over-blocking.
    no_trade = []
    soft_no_trade = []
    hard_no_trade = []
    twist = abs(float(ema20.iloc[i]) - float(ema50.iloc[i])) < 0.08 * a and abs(float(ema50.iloc[i]) - float(ema200.iloc[i])) < 0.12 * a
    atr_low = a < float(np.nanpercentile(atr.iloc[max(0, i - 250):i + 1], 25))
    range_hi_12 = float(df["high"].iloc[i - 12:i].max())
    range_lo_12 = float(df["low"].iloc[i - 12:i].min())
    narrow = (range_hi_12 - range_lo_12) < 1.3 * a
    between_range = abs(close - (range_hi_12 + range_lo_12) / 2.0) <= 0.18 * max(1e-9, (range_hi_12 - range_lo_12))
    near_key = min(
        abs(close - lvn_main),
        abs(close - poc),
        abs(close - vah),
        abs(close - val),
        abs(close - sr_sup),
        abs(close - sr_res),
    ) <= 0.55 * a
    if not near_key and between_range:
        soft_no_trade.append("Giá đang ở giữa range")
    if twist:
        soft_no_trade.append("EMA đang xoắn")
    if atr_low or narrow:
        soft_no_trade.append("ATR quá thấp")
    if (prev_high - prev_low) > 2.8 * a and abs(close - prev_close) > 0.8 * a:
        soft_no_trade.append("Giá vừa spike mạnh")
    if in_news_blackout(cfg, t_now):
        hard_no_trade.append("Gần tin mạnh")
    tick = mt5.symbol_info_tick(cfg["symbol"])
    if tick is not None:
        spread = abs(float(getattr(tick, "ask", 0.0)) - float(getattr(tick, "bid", 0.0)))
        if spread > 0.45 * a:
            hard_no_trade.append("Spread quá cao")
        elif spread > 0.35 * a:
            soft_no_trade.append("Spread cao hơn 35% ATR M5")

    if hard_no_trade or len(soft_no_trade) >= 3:
        no_trade = list(hard_no_trade) + list(soft_no_trade)

    # Session preference (VN): London 14:00-17:00, NY 19:30-23:00.
    minute_utc = datetime.utcfromtimestamp(t_now).hour * 60 + datetime.utcfromtimestamp(t_now).minute
    in_london_ny = (7 * 60 <= minute_utc <= 10 * 60) or (12 * 60 + 30 <= minute_utc <= 16 * 60)

    candidates = []
    profile_name = str(MODE1_FIXED_PROFILE).lower().strip()
    if profile_name not in MODE1_PROFILE_RULES:
        profile_name = "balanced"
    p_rule = MODE1_PROFILE_RULES[profile_name]
    score_gate = int(p_rule["min_score"])
    rr_min = float(p_rule["rr_min"])
    tp2_r = float(p_rule["tp2_r"])
    lot_factor_default = float(p_rule["lot_factor"])

    def hvn_poc_above(px):
        pool = [x for x in ([poc] + hvn_levels + [vah, sr_res, london_hi, asia_hi]) if isinstance(x, (int, float)) and x > px]
        return min(pool) if pool else None

    def hvn_poc_below(px):
        pool = [x for x in ([poc] + hvn_levels + [val, sr_sup, london_lo, asia_lo]) if isinstance(x, (int, float)) and x < px]
        return max(pool) if pool else None

    def add_candidate(setup_name, sig_side, entry, sl, tp1, tp2, base_reason, cancel_rule, confidence, factors, lot_factor=1.0):
        if not all(isinstance(x, (int, float)) for x in [entry, sl, tp1, tp2]):
            return
        stop = abs(entry - sl)
        if stop <= 0:
            return
        if stop < 2.0:
            sl = entry - 2.0 if sig_side == "BUY" else entry + 2.0
            stop = abs(entry - sl)
        if stop > float(MODE1_SL_MAX):
            return
        rr = abs(tp2 - entry) / max(1e-9, stop)
        score = int(sum(1 for x in factors if x))
        near_lvn_soft = abs(float(entry) - float(lvn_main)) <= 0.20 * a
        soft_rr_ok = (
            rr >= 1.10
            and near_lvn_soft
            and int(confidence) >= 8
            and score >= max(4, score_gate)
        )
        if rr < rr_min and not soft_rr_ok:
            return
        if sig_side == "BUY" and tp1 <= entry:
            return
        if sig_side == "SELL" and tp1 >= entry:
            return
        # v2-loose scoring: keep only a light quality gate.
        if score < score_gate:
            return
        reason_detail = (
            f"Chiến lược: {setup_name}\n"
            f"Bối cảnh H1: {h1_state}\n"
            f"Bối cảnh M15: {m15_state}\n"
            f"Vùng LVN chính: {lvn_main:.2f}\n"
            f"Vị trí giá so với POC/VAH/VAL: close={close:.2f} | POC={poc:.2f} | VAH={vah:.2f} | VAL={val:.2f}\n"
            f"Hướng lệnh: {sig_side}\n"
            f"Entry: {entry:.2f} | SL: {sl:.2f} | TP1: {tp1:.2f} | TP2: {tp2:.2f}\n"
            f"RR dự kiến: {rr:.2f}\n"
            f"Lý do vào lệnh: {base_reason}\n"
            f"Yếu tố xác nhận: {score}/6\n"
            f"RR gate: {'soft>=1.10 gần LVN' if (rr < rr_min and soft_rr_ok) else f'chuẩn>={rr_min:.2f}'}\n"
            f"Điều kiện hủy kèo: {cancel_rule}\n"
            f"Cách quản lý lệnh: TP1 chốt 50%, dời BE tại 1R, giữ TP2 nếu còn động lượng\n"
            f"Mức độ tự tin: {int(confidence)}/10"
        )
        candidates.append(
            {
                "setup": setup_name,
                "side": sig_side,
                "entry": float(entry),
                "sl": float(sl),
                "tp1": float(tp1),
                "tp2": float(tp2),
                "rr": float(rr),
                "confidence": int(confidence),
                "cancel_rule": str(cancel_rule),
                "reason": reason_detail,
                "score": score,
                "lot_factor": float(lot_factor),
            }
        )

    if not no_trade:
        # Setup 1: LVN Rejection BUY
        buy_reject = low <= lvn_main + 0.20 * a and close > lvn_main and ((min(open_, close) - low) >= 0.40 * rng or ((prev_close < prev_open) and (close > open_) and (open_ <= prev_close) and (close >= prev_open)))
        support_near = min(abs(lvn_main - sr_sup), abs(lvn_main - val), abs(lvn_main - asia_lo), abs(lvn_main - london_lo)) <= 0.45 * a
        if buy_reject and support_near and rsi_now <= 52 and rsi_now >= 30 and rsi_now >= rsi_prev:
            entry = close
            sl = min(low, swing_lo) - 0.30 * a
            tp1_raw = hvn_poc_above(entry)
            tp1 = tp1_raw if isinstance(tp1_raw, (int, float)) else entry + abs(entry - sl)
            tp2 = max(vah, entry + tp2_r * abs(entry - sl))
            factors = [support_near, low < min(prev_low, float(prev2["low"])), buy_reject, isinstance(tp1_raw, (int, float)), (abs(tp2 - entry) / max(1e-9, abs(entry - sl)) >= rr_min), in_london_ny]
            add_candidate(
                "LVN Rejection",
                "BUY",
                entry,
                sl,
                tp1,
                tp2,
                "Giá quét LVN và đóng lại trên LVN với nến xác nhận tăng.",
                "Hủy nếu nến M5 đóng lại dưới LVN hoặc RSI rơi dưới 40",
                8,
                factors,
                lot_factor_default,
            )

        # Setup 2: LVN Rejection SELL
        sell_reject = high >= lvn_main - 0.20 * a and close < lvn_main and ((high - max(open_, close)) >= 0.40 * rng or ((prev_close > prev_open) and (close < open_) and (open_ >= prev_close) and (close <= prev_open)))
        resistance_near = min(abs(lvn_main - sr_res), abs(lvn_main - vah), abs(lvn_main - asia_hi), abs(lvn_main - london_hi)) <= 0.45 * a
        if sell_reject and resistance_near and rsi_now >= 48 and rsi_now <= 70 and rsi_now <= rsi_prev:
            entry = close
            sl = max(high, swing_hi) + 0.30 * a
            tp1_raw = hvn_poc_below(entry)
            tp1 = tp1_raw if isinstance(tp1_raw, (int, float)) else entry - abs(entry - sl)
            tp2 = min(val, entry - tp2_r * abs(entry - sl))
            factors = [resistance_near, high > max(prev_high, float(prev2["high"])), sell_reject, isinstance(tp1_raw, (int, float)), (abs(tp2 - entry) / max(1e-9, abs(entry - sl)) >= rr_min), in_london_ny]
            add_candidate(
                "LVN Rejection",
                "SELL",
                entry,
                sl,
                tp1,
                tp2,
                "Giá quét LVN và đóng lại dưới LVN với nến xác nhận giảm.",
                "Hủy nếu nến M5 đóng lại trên LVN hoặc RSI vượt 60",
                8,
                factors,
                lot_factor_default,
            )

        # Setup 3: LVN Breakout Retest BUY
        prev_rng = max(1e-9, prev_high - prev_low)
        breakout_up = prev_close > lvn_main + 0.10 * a and abs(prev_close - prev_open) >= 0.58 * prev_rng and float(df["tick_volume"].iloc[i - 1]) >= 1.1 * max(1.0, float(df["tick_volume"].iloc[max(0, i - 40):i - 1].mean()))
        retest_up = low <= lvn_main + 0.18 * a and close > lvn_main and close > open_
        if breakout_up and retest_up and not trend_h1_dn:
            entry = close
            sl = min(low, swing_lo, lvn_main) - 0.28 * a
            tp1_raw = hvn_poc_above(entry)
            tp1 = tp1_raw if isinstance(tp1_raw, (int, float)) else entry + abs(entry - sl)
            tp2 = max(vah, entry + min(2.0, max(1.3, tp2_r + 0.1)) * abs(entry - sl))
            factors = [abs(lvn_main - sr_sup) <= 0.7 * a, low < prev_low, retest_up, isinstance(tp1_raw, (int, float)), (abs(tp2 - entry) / max(1e-9, abs(entry - sl)) >= rr_min), in_london_ny]
            add_candidate(
                "LVN Breakout Retest",
                "BUY",
                entry,
                sl,
                tp1,
                tp2,
                "Breakout qua LVN bằng nến thân lớn, retest giữ LVN và xác nhận tăng.",
                "Hủy nếu nến M5 đóng lại dưới LVN",
                8,
                factors,
                lot_factor_default,
            )

        # Setup 4: LVN Breakout Retest SELL
        breakout_dn = prev_close < lvn_main - 0.10 * a and abs(prev_close - prev_open) >= 0.58 * prev_rng and float(df["tick_volume"].iloc[i - 1]) >= 1.1 * max(1.0, float(df["tick_volume"].iloc[max(0, i - 40):i - 1].mean()))
        retest_dn = high >= lvn_main - 0.18 * a and close < lvn_main and close < open_
        if breakout_dn and retest_dn and not trend_h1_up:
            entry = close
            sl = max(high, swing_hi, lvn_main) + 0.28 * a
            tp1_raw = hvn_poc_below(entry)
            tp1 = tp1_raw if isinstance(tp1_raw, (int, float)) else entry - abs(entry - sl)
            tp2 = min(val, entry - min(2.0, max(1.3, tp2_r + 0.1)) * abs(entry - sl))
            factors = [abs(lvn_main - sr_res) <= 0.7 * a, high > prev_high, retest_dn, isinstance(tp1_raw, (int, float)), (abs(tp2 - entry) / max(1e-9, abs(entry - sl)) >= rr_min), in_london_ny]
            add_candidate(
                "LVN Breakout Retest",
                "SELL",
                entry,
                sl,
                tp1,
                tp2,
                "Breakdown qua LVN bằng nến thân lớn, retest thất bại và xác nhận giảm.",
                "Hủy nếu nến M5 đóng lại trên LVN",
                8,
                factors,
                lot_factor_default,
            )

        # Setup 5: LVN Fast Continuation (nới để không bỏ sóng mạnh)
        cont_buy = close > lvn_main + 0.05 * a and body >= 0.40 * rng and (float(ema20.iloc[i]) > float(ema50.iloc[i]) or close > float(ema20.iloc[i])) and rsi_now > 48
        cont_sell = close < lvn_main - 0.05 * a and body >= 0.40 * rng and (float(ema20.iloc[i]) < float(ema50.iloc[i]) or close < float(ema20.iloc[i])) and rsi_now < 52
        if cont_buy and body <= 2.8 * a:
            entry = close
            sl = min(low, lvn_main) - 0.20 * a
            stop = abs(entry - sl)
            tp1 = entry + stop
            tp2 = entry + max(1.3, min(1.5, tp2_r)) * stop
            factors = [close > lvn_main, body >= 0.55 * rng, rsi_now > 50, close > float(ema20.iloc[i]), in_london_ny, (tp2 > tp1)]
            add_candidate(
                "LVN Fast Continuation",
                "BUY",
                entry,
                sl,
                tp1,
                tp2,
                "Breakout/continuation qua LVN với nến M5 thân khá mạnh.",
                "Hủy nếu nến M5 đóng ngược lại dưới LVN",
                7,
                factors,
                min(0.7, lot_factor_default),
            )
        if cont_sell and body <= 2.8 * a:
            entry = close
            sl = max(high, lvn_main) + 0.20 * a
            stop = abs(entry - sl)
            tp1 = entry - stop
            tp2 = entry - max(1.3, min(1.5, tp2_r)) * stop
            factors = [close < lvn_main, body >= 0.55 * rng, rsi_now < 50, close < float(ema20.iloc[i]), in_london_ny, (tp2 < tp1)]
            add_candidate(
                "LVN Fast Continuation",
                "SELL",
                entry,
                sl,
                tp1,
                tp2,
                "Breakdown/continuation qua LVN với nến M5 thân khá mạnh.",
                "Hủy nếu nến M5 đóng ngược lại trên LVN",
                7,
                factors,
                min(0.7, lot_factor_default),
            )

    if not candidates:
        reasons = list(no_trade)
        if not reasons:
            dist_atr = abs(close - lvn_main) / max(1e-9, a)
            near_lvn = dist_atr <= 0.25
            reasons = [
                (
                    f"chưa chạm vùng LVN đủ gần (close={close:.2f}, LVN={lvn_main:.2f}, lệch={abs(close-lvn_main):.2f} ~ {dist_atr:.2f} ATR)"
                    if not near_lvn
                    else f"đã vào vùng LVN (close={close:.2f}, LVN={lvn_main:.2f}, lệch={abs(close-lvn_main):.2f} ~ {dist_atr:.2f} ATR) nhưng chưa có nến xác nhận"
                ),
                "chưa có nến reject/retest/continuation xác nhận tại LVN",
                f"RR chưa đạt ngưỡng tối thiểu {rr_min:.2f}",
            ]
        watch_buy = float(lvn_main + 0.06 * a)
        watch_sell = float(lvn_main - 0.06 * a)
        summary = "NO TRADE\n\nLý do:\n- " + "\n- ".join(reasons[:4])
        return {
            "side": None,
            "reason": summary,
            "m5_time": t_now,
            "close": close,
            "atr": a,
            "lvn": float(lvn_main),
            "buy_price_hint": watch_buy,
            "sell_price_hint": watch_sell,
            "atr_rank": atr_rank,
            "trend_strength": trend_strength,
            "strategy_id": "no-trade",
        }

    # Prioritize setup quality by RR, then confidence and shorter stop.
    candidates.sort(
        key=lambda x: (
            -float(x["rr"]),
            -int(x["confidence"]),
            abs(float(x["entry"]) - float(x["sl"])),
        )
    )
    best = candidates[0]
    if best["setup"] == "LVN Rejection":
        sid = "lvn_rejection"
    elif best["setup"] == "LVN Breakout Retest":
        sid = "lvn_breakout_retest"
    else:
        sid = "lvn_fast_continuation"
    return {
        "side": best["side"],
        "reason": best["reason"],
        "m5_time": t_now,
        "close": close,
        "atr": a,
        "lvn": float(lvn_main),
        "buy_price_hint": best["entry"] if best["side"] == "BUY" else None,
        "sell_price_hint": best["entry"] if best["side"] == "SELL" else None,
        "atr_rank": atr_rank,
        "trend_strength": trend_strength,
        "entry": best["entry"],
        "sl": best["sl"],
        "tp1": best["tp1"],
        "tp2": best["tp2"],
        "rr": best["rr"],
        "confidence": best["confidence"],
        "cancel_rule": best["cancel_rule"],
        "strategy_id": sid,
        "tp": best["tp2"],
        "lot_factor": float(best.get("lot_factor", lot_factor_default)),
    }


def compute_lvn_signal(cfg):
    """Backward-compatible alias for mode 1 LVN signal."""
    return compute_mode1_lvn_signal(cfg)


def get_active_modes(cfg):
    cfg = normalize_mode_settings(cfg)
    return [m for m in MODE_LABELS if cfg.get("modes", {}).get(m, {}).get("enabled", False)]


def compute_mode2_m1_scalp_signal(cfg):
    bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M5, 0, 700)
    if bars is None or len(bars) < 260:
        return None
    df = pd.DataFrame(bars).iloc[:-1].reset_index(drop=True)
    if len(df) < 260:
        return None

    atr = atr_series(df, 14)
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    ema200 = df["close"].ewm(span=200, adjust=False).mean()
    rsi = rsi_series(df["close"].astype(float), 14)
    bb_mid = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std(ddof=0)
    bb_up = bb_mid + 2.0 * bb_std
    bb_dn = bb_mid - 2.0 * bb_std

    bars_m15 = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M15, 0, 320)
    bars_h1 = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_H1, 0, 260)
    if bars_m15 is None or len(bars_m15) < 120 or bars_h1 is None or len(bars_h1) < 100:
        return None
    d15 = pd.DataFrame(bars_m15).iloc[:-1].reset_index(drop=True)
    dh1 = pd.DataFrame(bars_h1).iloc[:-1].reset_index(drop=True)
    e20_15 = d15["close"].ewm(span=20, adjust=False).mean()
    e50_15 = d15["close"].ewm(span=50, adjust=False).mean()
    e200_15 = d15["close"].ewm(span=200, adjust=False).mean()
    e20_h1 = dh1["close"].ewm(span=20, adjust=False).mean()
    e50_h1 = dh1["close"].ewm(span=50, adjust=False).mean()
    e200_h1 = dh1["close"].ewm(span=200, adjust=False).mean()

    i = len(df) - 1
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    prev2 = df.iloc[i - 2]
    a = float(atr.iloc[i]) if float(atr.iloc[i]) > 0 else 0.0
    if a <= 0:
        return None
    close = float(row["close"])
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    prev_close = float(prev["close"])
    prev_open = float(prev["open"])
    prev_high = float(prev["high"])
    prev_low = float(prev["low"])
    t_now = int(row["time"])
    minute_utc = datetime.utcfromtimestamp(t_now).hour * 60 + datetime.utcfromtimestamp(t_now).minute

    atr_hist = atr.iloc[max(0, i - 288): i + 1].dropna().to_numpy(dtype=float)
    atr_rank = float((atr_hist <= a).sum()) / float(len(atr_hist)) if len(atr_hist) >= 8 else 0.5
    trend_strength = abs(float(e20_15.iloc[-1]) - float(e50_15.iloc[-1])) / max(1e-9, a)
    rsi_now = float(rsi.iloc[i])
    rsi_prev = float(rsi.iloc[i - 1])
    bb_mid_now = float(bb_mid.iloc[i]) if np.isfinite(float(bb_mid.iloc[i])) else close
    bb_up_now = float(bb_up.iloc[i]) if np.isfinite(float(bb_up.iloc[i])) else close + a
    bb_dn_now = float(bb_dn.iloc[i]) if np.isfinite(float(bb_dn.iloc[i])) else close - a
    body = abs(close - open_)
    rng = max(1e-9, high - low)

    vol_now = float(df["tick_volume"].iloc[i])
    vol_avg = float(df["tick_volume"].iloc[max(0, i - 40):i].mean())
    range_hi_20 = float(df["high"].iloc[i - 20:i].max())
    range_lo_20 = float(df["low"].iloc[i - 20:i].min())
    range_hi_60 = float(df["high"].iloc[i - 60:i].max())
    range_lo_60 = float(df["low"].iloc[i - 60:i].min())
    range_mid_60 = (range_hi_60 + range_lo_60) / 2.0
    range_h_12 = float(df["high"].iloc[i - 12:i].max())
    range_l_12 = float(df["low"].iloc[i - 12:i].min())
    range_w_12 = range_h_12 - range_l_12
    range_w_40 = range_hi_60 - range_lo_60

    swing_hi = float(df["high"].iloc[i - 15:i].max())
    swing_lo = float(df["low"].iloc[i - 15:i].min())
    support = float(df["low"].iloc[i - 80:i].min())
    resistance = float(df["high"].iloc[i - 80:i].max())
    sup_big = min(float(d15["low"].iloc[-40:].min()), float(dh1["low"].iloc[-20:].min()))
    res_big = max(float(d15["high"].iloc[-40:].max()), float(dh1["high"].iloc[-20:].max()))

    trend_buy_15 = float(d15["close"].iloc[-1]) > float(e50_15.iloc[-1]) > float(e200_15.iloc[-1]) and float(e20_15.iloc[-1]) > float(e50_15.iloc[-1])
    trend_sell_15 = float(d15["close"].iloc[-1]) < float(e50_15.iloc[-1]) < float(e200_15.iloc[-1]) and float(e20_15.iloc[-1]) < float(e50_15.iloc[-1])
    trend_buy_h1 = float(dh1["close"].iloc[-1]) > float(e50_h1.iloc[-1]) > float(e200_h1.iloc[-1]) and float(e20_h1.iloc[-1]) > float(e50_h1.iloc[-1])
    trend_sell_h1 = float(dh1["close"].iloc[-1]) < float(e50_h1.iloc[-1]) < float(e200_h1.iloc[-1]) and float(e20_h1.iloc[-1]) < float(e50_h1.iloc[-1])
    # v2-loose: M15 is primary; H1 only reduces confidence when opposite.
    trend_buy = trend_buy_15 or (trend_buy_h1 and not trend_sell_15)
    trend_sell = trend_sell_15 or (trend_sell_h1 and not trend_buy_15)
    trend_flat = not trend_buy and not trend_sell
    m15_ema20_now = float(e20_15.iloc[-1])
    m15_ema20_prev = float(e20_15.iloc[-2])
    m15_slope_up = (m15_ema20_now - m15_ema20_prev) > 0.02 * a
    m15_slope_dn = (m15_ema20_prev - m15_ema20_now) > 0.02 * a
    in_london_ny = (7 * 60 <= minute_utc <= 10 * 60) or (12 * 60 + 30 <= minute_utc <= 16 * 60)
    vol_ratio = vol_now / max(1.0, vol_avg)
    up_impulse = (
        close > float(ema20.iloc[i]) > float(ema50.iloc[i])
        and close > range_hi_20 + 0.08 * a
        and rsi_now >= 60
        and body >= 0.55 * rng
    )
    down_impulse = (
        close < float(ema20.iloc[i]) < float(ema50.iloc[i])
        and close < range_lo_20 - 0.08 * a
        and rsi_now <= 40
        and body >= 0.55 * rng
    )

    mode2_cfg = cfg.get("modes", {}).get(MODE_SCALP_M1_2, {})
    strats = mode2_cfg.get("strategies", {}) if isinstance(mode2_cfg, dict) else {}
    candidates = []
    strategy_status = {}
    side = None
    reason = "NO TRADE"

    # Always expose "watch levels" so GUI can track potential entry zones
    # even while state is NO TRADE.
    watch_levels = {
        "trend_pullback": {"buy": float(ema20.iloc[i]), "sell": float(ema20.iloc[i])},
        "breakout": {"buy": float(range_hi_20), "sell": float(range_lo_20)},
        "mean_reversion": {"buy": float(bb_dn_now), "sell": float(bb_up_now)},
        "reversal_pa": {"buy": float(min(support, sup_big)), "sell": float(max(resistance, res_big))},
        "orderflow_proxy": {
            "buy": float(df["high"].iloc[i - 6:i].max()),
            "sell": float(df["low"].iloc[i - 6:i].min()),
        },
        "session_scalp": {"buy": float(range_lo_20), "sell": float(range_hi_20)},
    }

    def strat_on(sid):
        return bool(strats.get(sid, True)) if isinstance(strats, dict) else True

    def set_wait_status(sid, why, buy_h=None, sell_h=None):
        dflt = watch_levels.get(sid, {})
        buy_v = buy_h if isinstance(buy_h, (int, float)) else dflt.get("buy")
        sell_v = sell_h if isinstance(sell_h, (int, float)) else dflt.get("sell")
        strategy_status[sid] = {
            "state": "WAIT",
            "reason": str(why),
            "buy_hint": float(buy_v) if isinstance(buy_v, (int, float)) else None,
            "sell_hint": float(sell_v) if isinstance(sell_v, (int, float)) else None,
        }

    def why_missing(parts, fallback):
        clean = [str(x) for x in parts if str(x).strip()]
        if not clean:
            return str(fallback)
        return "NO TRADE | thiếu: " + ", ".join(clean[:4])

    def required_score(sid):
        base = int(MODE2_MIN_SCORE)
        if in_london_ny and sid in ("trend_pullback", "breakout", "session_scalp"):
            return max(4, base - 1)
        return base

    def build_trade(sid, sig_side, entry, sl, tp1, tp2, why, cancel_rule, confidence, priority, confirm_count, lot_factor=1.0):
        req_score = required_score(sid)
        if int(confirm_count) < int(req_score):
            set_wait_status(
                sid,
                f"NO TRADE | điểm {int(confirm_count)}/10 < ngưỡng {int(req_score)}/10",
                buy_h=entry if sig_side == "BUY" else None,
                sell_h=entry if sig_side == "SELL" else None,
            )
            return
        if not all(isinstance(x, (int, float)) for x in [entry, sl, tp1, tp2]):
            set_wait_status(sid, "NO TRADE | Không có SL hợp lý")
            return
        stop = abs(float(entry) - float(sl))
        if stop < 2.0:
            sl = float(entry) - 2.0 if sig_side == "BUY" else float(entry) + 2.0
            stop = abs(float(entry) - float(sl))
        if stop > float(MODE2_SL_MAX):
            # Keep difficult setups tradable by capping stop and reducing size.
            if sid in ("trend_pullback", "orderflow_proxy", "session_scalp"):
                cap_stop = float(MODE2_SL_MAX) * 0.98
                sl = float(entry) - cap_stop if sig_side == "BUY" else float(entry) + cap_stop
                stop = abs(float(entry) - float(sl))
                lot_factor = min(float(lot_factor), 0.70)
                why = f"{why} | cap-SL {cap_stop:.2f}"
            else:
                set_wait_status(sid, "NO TRADE | Không có SL hợp lý (SL quá xa)", buy_h=entry if sig_side == "BUY" else None, sell_h=entry if sig_side == "SELL" else None)
                return
        if sig_side == "BUY":
            tp1 = max(float(tp1), float(entry) + stop * 0.8)
            tp2 = max(float(tp2), float(entry) + stop * 1.3)
            if tp2 <= tp1:
                tp2 = tp1 + 0.15 * stop
            # Ensure TP1/TP2 are visually and functionally separated.
            min_tp_gap = max(0.60 * stop, 0.25 * a)
            if (tp2 - tp1) < min_tp_gap:
                tp2 = tp1 + min_tp_gap
            if tp2 <= float(entry):
                set_wait_status(sid, "NO TRADE | TP không hợp lệ", buy_h=entry)
                return
        else:
            tp1 = min(float(tp1), float(entry) - stop * 0.8)
            tp2 = min(float(tp2), float(entry) - stop * 1.3)
            if tp2 >= tp1:
                tp2 = tp1 - 0.15 * stop
            # Ensure TP1/TP2 are visually and functionally separated.
            min_tp_gap = max(0.60 * stop, 0.25 * a)
            if (tp1 - tp2) < min_tp_gap:
                tp2 = tp1 - min_tp_gap
            if tp2 >= float(entry):
                set_wait_status(sid, "NO TRADE | TP không hợp lệ", sell_h=entry)
                return
        rr = abs(float(tp2) - float(entry)) / max(1e-9, stop)
        if rr < float(MODE2_RR_MIN):
            set_wait_status(sid, "NO TRADE | Không đủ RR (<1:1.2)", buy_h=entry if sig_side == "BUY" else None, sell_h=entry if sig_side == "SELL" else None)
            return
        # v2-loose+: do not hard-block all setups just because entry is near range midpoint.
        trade_reason = (
            f"Chiến lược: {MODE2_STRATEGY_LABELS.get(sid, sid)} | Hướng: {sig_side} | Entry:{entry:.2f} "
            f"SL:{sl:.2f} TP1:{tp1:.2f} TP2:{tp2:.2f} RR:{rr:.2f} | Lý do: {why} | Hủy kèo: {cancel_rule} "
            f"| Quản lý: BE tại 1R, TP1 chốt 50% | Tự tin: {int(confidence)}/10"
        )
        candidates.append(
            {
                "side": sig_side,
                "sid": sid,
                "label": MODE2_STRATEGY_LABELS.get(sid, sid),
                "reason": trade_reason,
                "priority": int(priority),
                "entry": float(entry),
                "sl": float(sl),
                "tp1": float(tp1),
                "tp2": float(tp2),
                "rr": float(rr),
                "confidence": int(confidence),
                "cancel_rule": str(cancel_rule),
                "score": int(confirm_count),
                "sl_dist": float(stop),
                "lot_factor": float(lot_factor),
                "buy_hint": float(entry) if sig_side == "BUY" else None,
                "sell_hint": float(entry) if sig_side == "SELL" else None,
            }
        )
        strategy_status[sid] = {
            "state": sig_side,
            "reason": trade_reason,
            "buy_hint": float(entry) if sig_side == "BUY" else None,
            "sell_hint": float(entry) if sig_side == "SELL" else None,
        }

    # Mandatory anti-noise filters (relaxed in peak sessions).
    no_trade_reasons = []
    soft_noise = []
    hard_noise = []
    ema_twisted = abs(float(ema20.iloc[i]) - float(ema50.iloc[i])) < 0.08 * a and abs(float(ema50.iloc[i]) - float(ema200.iloc[i])) < 0.12 * a
    atr_low = a < float(np.nanpercentile(atr.iloc[max(0, i - 250):i + 1], 25))
    narrow_sideway = range_w_12 < 1.3 * a
    # Only block post-spike if market stalls after extreme candle.
    spike_prev = (prev_high - prev_low) > 2.8 * a and abs(close - prev_close) < 0.25 * a
    if narrow_sideway:
        soft_noise.append("Thị trường đang nhiễu")
    if ema_twisted:
        soft_noise.append("EMA đang xoắn")
    if atr_low:
        soft_noise.append("ATR thấp, thiếu biên độ")
    if spike_prev:
        soft_noise.append("Vừa có nến spike lớn chưa retest")
    if in_news_blackout(cfg, t_now):
        hard_noise.append("Gần tin mạnh")
    tick = mt5.symbol_info_tick(cfg["symbol"])
    if tick is not None:
        spread = abs(float(getattr(tick, "ask", 0.0)) - float(getattr(tick, "bid", 0.0)))
        if spread > 0.45 * a:
            hard_noise.append("Spread quá cao")
        elif spread > 0.35 * a:
            soft_noise.append("Spread cao hơn 35% ATR M5")

    # v2-loose+: keep hard blocks, but only stop when soft warnings stack up.
    if hard_noise or len(soft_noise) >= 3:
        no_trade_reasons = list(hard_noise) + list(soft_noise)
        base_reason = "NO TRADE | " + " ; ".join(no_trade_reasons[:3])
        for sid in MODE2_STRATEGY_LABELS:
            if strat_on(sid):
                set_wait_status(sid, base_reason)
            else:
                set_wait_status(sid, "disabled")
        return {
            "side": None,
            "reason": base_reason,
            "m5_time": t_now,
            "atr": a,
            "lvn": float(ema20.iloc[i]),
            "buy_price_hint": None,
            "sell_price_hint": None,
            "atr_rank": atr_rank,
            "trend_strength": trend_strength,
            "candidates": [],
            "strategy_status": strategy_status,
        }

    # 1) Trend Pullback
    if strat_on("trend_pullback"):
        bull_reject = close > open_ and (min(open_, close) - low) >= 0.35 * rng
        bear_reject = close < open_ and (high - max(open_, close)) >= 0.35 * rng
        ema20_now = float(ema20.iloc[i])
        ema20_prev = float(ema20.iloc[i - 1])
        ema20_prev2 = float(ema20.iloc[i - 2])
        ema20_slope_up = (ema20_now - ema20_prev) > 0.015 * a and (ema20_prev - ema20_prev2) > 0.0
        ema20_slope_dn = (ema20_prev - ema20_now) > 0.015 * a and (ema20_prev2 - ema20_prev) > 0.0
        closes3 = df["close"].iloc[i - 2:i + 1].astype(float)
        hold_above_ema20 = bool((closes3 >= ema20_now - 0.12 * a).all())
        hold_below_ema20 = bool((closes3 <= ema20_now + 0.12 * a).all())
        micro_hi_lookback = float(df["high"].iloc[i - 6:i - 1].max())
        micro_lo_lookback = float(df["low"].iloc[i - 6:i - 1].min())
        choch_up = close > micro_hi_lookback + 0.04 * a
        choch_dn = close < micro_lo_lookback - 0.04 * a
        bull_confirm = (
            bull_reject
            or (close > prev_close and close > ema20_now and body >= 0.42 * rng)
            or (close > open_ and low <= ema20_now + 0.18 * a and close >= ema20_now)
            or (ema20_slope_up and hold_above_ema20 and choch_up)
        )
        bear_confirm = (
            bear_reject
            or (close < prev_close and close < ema20_now and body >= 0.42 * rng)
            or (close < open_ and high >= ema20_now - 0.18 * a and close <= ema20_now)
            or (ema20_slope_dn and hold_below_ema20 and choch_dn)
        )
        # v15: transition-friendly trend gate with EMA20 slope + micro-structure support.
        buy_transition_ok = trend_buy_h1 and ema20_slope_up and hold_above_ema20 and (choch_up or (m15_slope_up and close >= ema20_now))
        sell_transition_ok = trend_sell_h1 and ema20_slope_dn and hold_below_ema20 and (choch_dn or (m15_slope_dn and close <= ema20_now))
        buy_trend_ok = trend_buy_15 or buy_transition_ok or (m15_slope_up and close >= ema20_now)
        sell_trend_ok = trend_sell_15 or sell_transition_ok or (m15_slope_dn and close <= ema20_now)
        near_pull_buy = low <= float(ema20.iloc[i]) + 0.20 * a or low <= float(ema50.iloc[i]) + 0.16 * a or abs(close - support) <= 0.40 * a
        near_pull_sell = high >= float(ema20.iloc[i]) - 0.20 * a or high >= float(ema50.iloc[i]) - 0.16 * a or abs(close - resistance) <= 0.40 * a
        fast_buy = trend_buy and close > float(ema20.iloc[i]) and body >= 0.58 * rng and vol_now >= 1.05 * max(1.0, vol_avg) and rsi_now >= 47
        fast_sell = trend_sell and close < float(ema20.iloc[i]) and body >= 0.58 * rng and vol_now >= 1.05 * max(1.0, vol_avg) and rsi_now <= 53
        if buy_trend_ok and near_pull_buy and bull_confirm and rsi_now >= 44 and rsi_now >= rsi_prev - 0.8:
            entry = close
            micro_lo = float(df["low"].iloc[i - 6:i].min())
            sl_std = min(swing_lo - 0.12 * a, float(ema50.iloc[i]) - 0.18 * a)
            sl_tight = min(micro_lo - 0.08 * a, float(ema20.iloc[i]) - 0.12 * a)
            sl = sl_std if abs(entry - sl_std) <= float(MODE2_SL_MAX) else sl_tight
            tp1 = entry + abs(entry - sl)
            tp2 = min(resistance, entry + 1.9 * abs(entry - sl)) if resistance > entry else entry + 1.9 * abs(entry - sl)
            why = "M15 trend rõ + pullback EMA20/50 + nến xác nhận"
            if buy_transition_ok and not trend_buy_15:
                why = "Trend transition (H1 + EMA20 slope + break micro) + pullback + nến xác nhận"
            if sl == sl_tight:
                why += " | tight-stop micro swing"
            build_trade("trend_pullback", "BUY", entry, sl, tp1, tp2, why, "Hủy nếu nến M5 đóng dưới đáy pullback", 8, 100, 5, 0.85 if sl == sl_tight else 1.0)
        elif in_london_ny and fast_buy:
            entry = close
            sl = min(swing_lo - 0.10 * a, float(ema20.iloc[i]) - 0.15 * a)
            tp1 = entry + abs(entry - sl)
            tp2 = min(resistance, entry + 1.5 * abs(entry - sl)) if resistance > entry else entry + 1.5 * abs(entry - sl)
            build_trade("trend_pullback", "BUY", entry, sl, tp1, tp2, "momentum buy continuation từ vùng EMA20", "Hủy nếu nến M5 đóng lại dưới EMA20", 7, 98, 4, 0.7)
        elif sell_trend_ok and near_pull_sell and bear_confirm and rsi_now <= 56 and rsi_now <= rsi_prev + 0.8:
            entry = close
            micro_hi = float(df["high"].iloc[i - 6:i].max())
            sl_std = max(swing_hi + 0.12 * a, float(ema50.iloc[i]) + 0.18 * a)
            sl_tight = max(micro_hi + 0.08 * a, float(ema20.iloc[i]) + 0.12 * a)
            sl = sl_std if abs(entry - sl_std) <= float(MODE2_SL_MAX) else sl_tight
            tp1 = entry - abs(entry - sl)
            tp2 = max(support, entry - 1.9 * abs(entry - sl)) if support < entry else entry - 1.9 * abs(entry - sl)
            why = "M15 trend rõ + pullback EMA20/50 + nến xác nhận"
            if sell_transition_ok and not trend_sell_15:
                why = "Trend transition (H1 + EMA20 slope + break micro) + pullback + nến xác nhận"
            if sl == sl_tight:
                why += " | tight-stop micro swing"
            build_trade("trend_pullback", "SELL", entry, sl, tp1, tp2, why, "Hủy nếu nến M5 đóng trên đỉnh pullback", 8, 100, 5, 0.85 if sl == sl_tight else 1.0)
        elif in_london_ny and fast_sell:
            entry = close
            sl = max(swing_hi + 0.10 * a, float(ema20.iloc[i]) + 0.15 * a)
            tp1 = entry - abs(entry - sl)
            tp2 = max(support, entry - 1.5 * abs(entry - sl)) if support < entry else entry - 1.5 * abs(entry - sl)
            build_trade("trend_pullback", "SELL", entry, sl, tp1, tp2, "momentum sell continuation từ vùng EMA20", "Hủy nếu nến M5 đóng lại trên EMA20", 7, 98, 4, 0.7)
        else:
            miss = []
            if not (buy_trend_ok or sell_trend_ok):
                miss.append("trend direction chưa đạt (M15/H1+EMA20 slope+micro break)")
            if buy_trend_ok and not near_pull_buy:
                miss.append("BUY chưa pullback về EMA/SR")
            if sell_trend_ok and not near_pull_sell:
                miss.append("SELL chưa pullback về EMA/SR")
            if buy_trend_ok and near_pull_buy and not bull_confirm:
                miss.append("BUY thiếu nến xác nhận")
            if sell_trend_ok and near_pull_sell and not bear_confirm:
                miss.append("SELL thiếu nến xác nhận")
            if 45.0 <= rsi_now <= 55.0 and not (
                (trend_buy_15 and near_pull_buy) or (trend_sell_15 and near_pull_sell)
            ):
                miss.append("RSI trung tính")
            set_wait_status("trend_pullback", why_missing(miss, "NO TRADE | chưa đủ điều kiện Trend Pullback"), buy_h=float(ema20.iloc[i]), sell_h=float(ema20.iloc[i]))
    else:
        set_wait_status("trend_pullback", "disabled")

    # 2) Breakout
    if strat_on("breakout"):
        range_n = 12
        r_hi = float(df["high"].iloc[i - range_n:i].max())
        r_lo = float(df["low"].iloc[i - range_n:i].min())
        r_h = max(1e-9, r_hi - r_lo)
        body_strong = body >= 0.52 * rng
        body_ok = body >= 0.42 * rng
        vol_mult = 0.92 if in_london_ny else 0.80
        vol_avg_base = max(1.0, vol_avg)
        vol_boost = (body_strong and vol_now >= 1.00 * vol_avg_base) or (body_ok and vol_now >= vol_mult * vol_avg_base)
        bo_dist_up = close - r_hi
        bo_dist_dn = r_lo - close
        fake_bo_buy = (high - close) >= 0.55 * rng and bo_dist_up < 0.10 * a
        fake_bo_sell = (close - low) >= 0.55 * rng and bo_dist_dn < 0.10 * a
        breakout_follow_buy = close > prev_close and close >= r_hi + 0.05 * a
        breakout_follow_sell = close < prev_close and close <= r_lo - 0.05 * a
        # Break-and-go continuation branch for strong directional markets with shallow pullback.
        cont_sell = (
            trend_sell
            and close < min(prev_low, r_lo) - 0.06 * a
            and body >= 0.48 * rng
            and close < float(ema20.iloc[i])
            and vol_now >= 0.88 * max(1.0, vol_avg)
            and (close - sup_big) > 0.9 * a
        )
        cont_buy = (
            trend_buy
            and close > max(prev_high, r_hi) + 0.06 * a
            and body >= 0.48 * rng
            and close > float(ema20.iloc[i])
            and vol_now >= 0.88 * max(1.0, vol_avg)
            and (res_big - close) > 0.9 * a
        )
        if trend_buy and r_h >= 1.0 * a and r_h <= 7.5 * a and close > r_hi + 0.03 * a and body_ok and vol_boost and (res_big - close) > 1.3 * a and breakout_follow_buy and not fake_bo_buy:
            entry = close
            sl = r_hi - 0.20 * a
            tp1 = entry + max(r_h, abs(entry - sl))
            tp2 = min(res_big, entry + 1.8 * abs(entry - sl)) if res_big > entry else entry + 1.8 * abs(entry - sl)
            build_trade("breakout", "BUY", entry, sl, tp1, tp2, "tích lũy 5-10 nến + breakout thân mạnh + volume tăng", "Hủy nếu giá đóng lại vào trong range", 8, 90, 5)
        elif trend_sell and r_h >= 1.0 * a and r_h <= 7.5 * a and close < r_lo - 0.03 * a and body_ok and vol_boost and (close - sup_big) > 1.3 * a and breakout_follow_sell and not fake_bo_sell:
            entry = close
            sl = r_lo + 0.20 * a
            tp1 = entry - max(r_h, abs(entry - sl))
            tp2 = max(sup_big, entry - 1.8 * abs(entry - sl)) if sup_big < entry else entry - 1.8 * abs(entry - sl)
            build_trade("breakout", "SELL", entry, sl, tp1, tp2, "tích lũy 5-10 nến + breakout thân mạnh + volume tăng", "Hủy nếu giá đóng lại vào trong range", 8, 90, 5)
        elif trend_buy and r_h >= 1.0 * a and close > r_hi + 0.03 * a and body_ok and (vol_now >= 0.80 * max(1.0, vol_avg)) and (res_big - close) > 1.1 * a:
            entry = close
            sl = r_hi - 0.16 * a
            tp1 = entry + abs(entry - sl)
            tp2 = min(res_big, entry + 1.35 * abs(entry - sl)) if res_big > entry else entry + 1.35 * abs(entry - sl)
            build_trade("breakout", "BUY", entry, sl, tp1, tp2, "breakout continuation volume thấp (lot giảm)", "Hủy nếu đóng lại trong range cũ", 7, 84, 4, 0.7)
        elif trend_sell and r_h >= 1.0 * a and close < r_lo - 0.03 * a and body_ok and (vol_now >= 0.80 * max(1.0, vol_avg)) and (close - sup_big) > 1.1 * a:
            entry = close
            sl = r_lo + 0.16 * a
            tp1 = entry - abs(entry - sl)
            tp2 = max(sup_big, entry - 1.35 * abs(entry - sl)) if sup_big < entry else entry - 1.35 * abs(entry - sl)
            build_trade("breakout", "SELL", entry, sl, tp1, tp2, "breakdown continuation volume thấp (lot giảm)", "Hủy nếu đóng lại trong range cũ", 7, 84, 4, 0.7)
        elif cont_sell:
            entry = close
            sl = max(high, prev_high, float(ema20.iloc[i])) + 0.14 * a
            tp1 = entry - abs(entry - sl)
            tp2 = max(sup_big, entry - 1.45 * abs(entry - sl)) if sup_big < entry else entry - 1.45 * abs(entry - sl)
            build_trade("breakout", "SELL", entry, sl, tp1, tp2, "trend continuation break-and-go (không hồi sâu)", "Hủy nếu nến M5 đóng lại trên EMA20", 7, 86, 4, 0.65)
        elif cont_buy:
            entry = close
            sl = min(low, prev_low, float(ema20.iloc[i])) - 0.14 * a
            tp1 = entry + abs(entry - sl)
            tp2 = min(res_big, entry + 1.45 * abs(entry - sl)) if res_big > entry else entry + 1.45 * abs(entry - sl)
            build_trade("breakout", "BUY", entry, sl, tp1, tp2, "trend continuation break-and-go (không hồi sâu)", "Hủy nếu nến M5 đóng lại dưới EMA20", 7, 86, 4, 0.65)
        elif trend_buy and prev_close > r_hi and close > r_hi and close >= prev_close - 0.15 * a and body >= 0.36 * rng:
            entry = close
            sl = min(low, r_hi) - 0.16 * a
            tp1 = entry + abs(entry - sl)
            tp2 = min(res_big, entry + 1.45 * abs(entry - sl)) if res_big > entry else entry + 1.45 * abs(entry - sl)
            build_trade("breakout", "BUY", entry, sl, tp1, tp2, "follow-through breakout sau nến phá đầu tiên", "Hủy nếu nến M5 đóng lại trong range cũ", 7, 88, 4, 0.85)
        elif trend_sell and prev_close < r_lo and close < r_lo and close <= prev_close + 0.15 * a and body >= 0.36 * rng:
            entry = close
            sl = max(high, r_lo) + 0.16 * a
            tp1 = entry - abs(entry - sl)
            tp2 = max(sup_big, entry - 1.45 * abs(entry - sl)) if sup_big < entry else entry - 1.45 * abs(entry - sl)
            build_trade("breakout", "SELL", entry, sl, tp1, tp2, "follow-through breakdown sau nến phá đầu tiên", "Hủy nếu nến M5 đóng lại trong range cũ", 7, 88, 4, 0.85)
        # Impulse continuation fallback: allow strong directional break without perfect retest.
        elif trend_sell and close < r_lo - 0.25 * a and body >= 0.75 * rng and vol_now >= 1.25 * max(1.0, vol_avg) and (close - sup_big) > 1.8 * a:
            entry = close
            sl = max(high, r_lo) + 0.22 * a
            tp1 = entry - abs(entry - sl)
            tp2 = max(sup_big, entry - 1.7 * abs(entry - sl))
            build_trade("breakout", "SELL", entry, sl, tp1, tp2, "impulse breakdown continuation (không retest chuẩn)", "Hủy nếu nến M5 đóng lại trên đáy range vừa phá", 7, 60, 4, 0.5)
        elif trend_buy and close > r_hi + 0.25 * a and body >= 0.75 * rng and vol_now >= 1.25 * max(1.0, vol_avg) and (res_big - close) > 1.8 * a:
            entry = close
            sl = min(low, r_hi) - 0.22 * a
            tp1 = entry + abs(entry - sl)
            tp2 = min(res_big, entry + 1.7 * abs(entry - sl))
            build_trade("breakout", "BUY", entry, sl, tp1, tp2, "impulse breakout continuation (không retest chuẩn)", "Hủy nếu nến M5 đóng lại dưới đỉnh range vừa phá", 7, 60, 4, 0.5)
        else:
            miss = []
            if r_h < 1.0 * a:
                miss.append("range quá hẹp")
            if r_h > 7.5 * a:
                miss.append("range quá rộng")
            if not body_ok:
                miss.append("thân nến breakout yếu")
            if not vol_boost:
                miss.append(f"volume chưa đạt (strong-body >=1.00x hoặc normal-body >= {vol_mult:.2f}x)")
            if fake_bo_buy or fake_bo_sell:
                miss.append("nguy cơ false-break (đuôi nến ngược chiều lớn)")
            set_wait_status("breakout", why_missing(miss, "NO TRADE | breakout chưa rõ hoặc thiếu retest/volume"), buy_h=r_hi, sell_h=r_lo)
    else:
        set_wait_status("breakout", "disabled")

    # 3) Mean Reversion
    if strat_on("mean_reversion"):
        ema20_now = float(ema20.iloc[i])
        ema50_now = float(ema50.iloc[i])
        ema200_now = float(ema200.iloc[i])
        sideway_ok = (
            range_w_40 >= 1.2 * a
            and range_w_40 <= 8.5 * a
            and trend_strength < 0.85
            and not (trend_buy and close > ema20_now + 0.2 * a)
            and not (trend_sell and close < ema20_now - 0.2 * a)
        )
        breakout_risk = body >= 0.80 * rng and (close > range_hi_20 + 0.12 * a or close < range_lo_20 - 0.12 * a)
        buy_allowed = not down_impulse
        sell_allowed = not up_impulse
        mr_reject_buy = (min(open_, close) - low) >= 0.30 * rng and close >= open_
        mr_reject_sell = (high - max(open_, close)) >= 0.30 * rng and close <= open_
        if sideway_ok and (not breakout_risk) and buy_allowed and low <= bb_dn_now and close > bb_dn_now and rsi_now <= 40 and mr_reject_buy:
            entry = close
            sl = min(low - 0.15 * a, range_lo_20 - 0.10 * a)
            stop = abs(entry - sl)
            tp1_raw = float(bb_mid_now)
            tp1 = min(tp1_raw, entry + 1.25 * stop)
            tp1 = max(tp1, entry + 0.90 * stop)
            tp2 = min(range_hi_20, entry + 1.7 * stop)
            if tp2 <= tp1:
                tp2 = tp1 + 0.35 * stop
            build_trade("mean_reversion", "BUY", entry, sl, tp1, tp2, "sideway + chạm BB dưới + RSI quá bán", "Hủy nếu breakdown thật sự dưới range", 7, 70, 5, 0.75)
        elif sideway_ok and (not breakout_risk) and sell_allowed and high >= bb_up_now and close < bb_up_now and rsi_now >= 60 and mr_reject_sell:
            entry = close
            sl = max(high + 0.15 * a, range_hi_20 + 0.10 * a)
            stop = abs(entry - sl)
            tp1_raw = float(bb_mid_now)
            tp1 = max(tp1_raw, entry - 1.25 * stop)
            tp1 = min(tp1, entry - 0.90 * stop)
            tp2 = max(range_lo_20, entry - 1.7 * stop)
            if tp2 >= tp1:
                tp2 = tp1 - 0.35 * stop
            build_trade("mean_reversion", "SELL", entry, sl, tp1, tp2, "sideway + chạm BB trên + RSI quá mua", "Hủy nếu breakout thật sự khỏi range", 7, 70, 5, 0.75)
        else:
            miss = []
            if not sideway_ok:
                miss.append("thị trường chưa đủ sideway")
            if breakout_risk:
                miss.append("nguy cơ breakout mạnh khỏi range")
            if not buy_allowed:
                miss.append("đang có down-impulse, tránh bắt đáy")
            if not sell_allowed:
                miss.append("đang có up-impulse, tránh bắt đỉnh")
            if not (low <= bb_dn_now or high >= bb_up_now):
                miss.append("chưa chạm biên Bollinger")
            if not (rsi_now <= 40 or rsi_now >= 60):
                miss.append("RSI chưa vào vùng cực trị")
            if (low <= bb_dn_now and rsi_now <= 42) and not mr_reject_buy:
                miss.append("BUY thiếu nến rút chân xác nhận")
            if (high >= bb_up_now and rsi_now >= 58) and not mr_reject_sell:
                miss.append("SELL thiếu nến rút đầu xác nhận")
            set_wait_status("mean_reversion", why_missing(miss, "NO TRADE | chưa đủ điều kiện Mean Reversion"), buy_h=bb_dn_now, sell_h=bb_up_now)
    else:
        set_wait_status("mean_reversion", "disabled")

    # 4) Reversal Price Action
    if strat_on("reversal_pa"):
        bullish_engulf = (prev_close < prev_open) and (close > open_) and (open_ <= prev_close) and (close >= prev_open)
        bearish_engulf = (prev_close > prev_open) and (close < open_) and (open_ >= prev_close) and (close <= prev_open)
        bullish_pin = (min(open_, close) - low) >= 0.5 * rng and close > open_
        bearish_pin = (high - max(open_, close)) >= 0.5 * rng and close < open_
        morning_star = (float(prev2["close"]) < float(prev2["open"])) and (abs(prev_close - prev_open) < 0.4 * a) and close > open_
        evening_star = (float(prev2["close"]) > float(prev2["open"])) and (abs(prev_close - prev_open) < 0.4 * a) and close < open_
        bull_follow2 = (prev_close > prev_open) and (close > prev_close) and close > open_
        bear_follow2 = (prev_close < prev_open) and (close < prev_close) and close < open_
        touch_sup = abs(low - sup_big) <= 0.32 * a or abs(low - support) <= 0.32 * a
        touch_res = abs(high - res_big) <= 0.32 * a or abs(high - resistance) <= 0.32 * a
        rev_buy_ok = not (down_impulse and close < ema20.iloc[i] and trend_sell)
        rev_sell_ok = not (up_impulse and close > ema20.iloc[i] and trend_buy)
        if touch_sup and rev_buy_ok and (bullish_engulf or bullish_pin or morning_star or bull_follow2):
            entry = close
            sl = min(low - 0.12 * a, swing_lo - 0.10 * a)
            tp1 = min(resistance, entry + 1.2 * abs(entry - sl)) if resistance > entry else entry + 1.2 * abs(entry - sl)
            tp2 = min(res_big, entry + 1.8 * abs(entry - sl)) if res_big > entry else entry + 1.8 * abs(entry - sl)
            build_trade("reversal_pa", "BUY", entry, sl, tp1, tp2, "chạm hỗ trợ mạnh + tín hiệu đảo chiều PA", "Hủy nếu nến xác nhận kế tiếp đóng dưới đáy quét", 8, 110, 5)
        elif touch_res and rev_sell_ok and (bearish_engulf or bearish_pin or evening_star or bear_follow2):
            entry = close
            sl = max(high + 0.12 * a, swing_hi + 0.10 * a)
            tp1 = max(support, entry - 1.2 * abs(entry - sl)) if support < entry else entry - 1.2 * abs(entry - sl)
            tp2 = max(sup_big, entry - 1.8 * abs(entry - sl)) if sup_big < entry else entry - 1.8 * abs(entry - sl)
            build_trade("reversal_pa", "SELL", entry, sl, tp1, tp2, "chạm kháng cự mạnh + tín hiệu đảo chiều PA", "Hủy nếu nến xác nhận kế tiếp đóng trên đỉnh quét", 8, 110, 5)
        else:
            miss = []
            if not (touch_sup or touch_res):
                miss.append("chưa chạm vùng S/R mạnh")
            if not (bullish_engulf or bearish_engulf or bullish_pin or bearish_pin or morning_star or evening_star or bull_follow2 or bear_follow2):
                miss.append("chưa có mẫu nến đảo chiều")
            if touch_sup and not rev_buy_ok:
                miss.append("đang down-impulse mạnh, tránh BUY ngược đà")
            if touch_res and not rev_sell_ok:
                miss.append("đang up-impulse mạnh, tránh SELL ngược đà")
            set_wait_status("reversal_pa", why_missing(miss, "NO TRADE | chưa có PA đảo chiều tại vùng mạnh"), buy_h=sup_big, sell_h=res_big)
    else:
        set_wait_status("reversal_pa", "disabled")

    # 5) Orderflow / CHOCH proxy
    if strat_on("orderflow_proxy"):
        micro_hi = float(df["high"].iloc[i - 6:i - 1].max())
        micro_lo = float(df["low"].iloc[i - 6:i - 1].min())
        sweep_low = (low <= min(swing_lo, range_lo_20) + 0.02 * a) and close > min(swing_lo, range_lo_20)
        sweep_high = (high >= max(swing_hi, range_hi_20) - 0.02 * a) and close < max(swing_hi, range_hi_20)
        choch_up = (sweep_low and close > micro_hi - 0.05 * a) or (
            close > micro_hi + 0.04 * a and prev_close <= micro_hi + 0.02 * a and body >= 0.38 * rng
        )
        choch_dn = (sweep_high and close < micro_lo + 0.05 * a) or (
            close < micro_lo - 0.04 * a and prev_close >= micro_lo - 0.02 * a and body >= 0.38 * rng
        )
        flow_buy_ok = not (trend_sell and down_impulse)
        flow_sell_ok = not (trend_buy and up_impulse)
        if choch_up and flow_buy_ok:
            entry = close
            sl = low - 0.12 * a
            tp1 = swing_hi
            tp2 = min(resistance, entry + 2.0 * abs(entry - sl)) if resistance > entry else entry + 2.0 * abs(entry - sl)
            build_trade("orderflow_proxy", "BUY", entry, sl, tp1, tp2, "quét đáy + CHOCH tăng + retest vùng phá cấu trúc", "Hủy nếu phá xuống dưới đáy quét", 7, 80, 5)
        elif choch_dn and flow_sell_ok:
            entry = close
            sl = high + 0.12 * a
            tp1 = swing_lo
            tp2 = max(support, entry - 2.0 * abs(entry - sl)) if support < entry else entry - 2.0 * abs(entry - sl)
            build_trade("orderflow_proxy", "SELL", entry, sl, tp1, tp2, "quét đỉnh + CHOCH giảm + retest vùng phá cấu trúc", "Hủy nếu phá lên trên đỉnh quét", 7, 80, 5)
        elif close > micro_hi + 0.10 * a and prev_close <= micro_hi and close > ema20.iloc[i] and vol_ratio >= 1.05 and flow_buy_ok:
            entry = close
            sl = min(micro_lo, low) - 0.10 * a
            tp1 = entry + abs(entry - sl)
            tp2 = min(resistance, entry + 1.6 * abs(entry - sl)) if resistance > entry else entry + 1.6 * abs(entry - sl)
            build_trade("orderflow_proxy", "BUY", entry, sl, tp1, tp2, "break micro-structure + volume hỗ trợ", "Hủy nếu đóng lại dưới micro range", 6, 76, 4, 0.8)
        elif close < micro_lo - 0.10 * a and prev_close >= micro_lo and close < ema20.iloc[i] and vol_ratio >= 1.05 and flow_sell_ok:
            entry = close
            sl = max(micro_hi, high) + 0.10 * a
            tp1 = entry - abs(entry - sl)
            tp2 = max(support, entry - 1.6 * abs(entry - sl)) if support < entry else entry - 1.6 * abs(entry - sl)
            build_trade("orderflow_proxy", "SELL", entry, sl, tp1, tp2, "break micro-structure + volume hỗ trợ", "Hủy nếu đóng lại trên micro range", 6, 76, 4, 0.8)
        else:
            miss = []
            if not (sweep_low or sweep_high):
                miss.append("chưa có quét đỉnh/đáy")
            if not (choch_up or choch_dn):
                miss.append("chưa có phá cấu trúc CHOCH")
            if not flow_buy_ok or not flow_sell_ok:
                miss.append("đang có impulse mạnh, giảm xác suất đảo chiều")
            set_wait_status("orderflow_proxy", why_missing(miss, "NO TRADE | chưa có CHOCH rõ + retest"), buy_h=micro_hi, sell_h=micro_lo)
    else:
        set_wait_status("orderflow_proxy", "disabled")

    # 6) Session-based scalp (VN: London 14:00-17:00, NY 19:30-23:00).
    if strat_on("session_scalp"):
        in_london = 7 * 60 <= minute_utc <= 10 * 60
        in_ny = 12 * 60 + 30 <= minute_utc <= 16 * 60
        in_session = in_london or in_ny
        day = datetime.utcfromtimestamp(t_now).strftime("%Y-%m-%d")
        dts = pd.to_datetime(df["time"], unit="s")
        day_mask = dts.dt.strftime("%Y-%m-%d") == day
        asia_mask = day_mask & (dts.dt.hour < 7)
        if in_session and asia_mask.any():
            asia_hi = float(df.loc[asia_mask, "high"].max())
            asia_lo = float(df.loc[asia_mask, "low"].min())
            asia_mid = (asia_hi + asia_lo) / 2.0
            session_up_impulse = (
                close > float(ema20.iloc[i]) > float(ema50.iloc[i])
                and close > float(ema200.iloc[i])
                and close > range_hi_20 + 0.08 * a
                and rsi_now >= 60
                and body >= 0.55 * rng
            )
            session_dn_impulse = (
                close < float(ema20.iloc[i]) < float(ema50.iloc[i])
                and close < float(ema200.iloc[i])
                and close < range_lo_20 - 0.08 * a
                and rsi_now <= 40
                and body >= 0.55 * rng
            )
            session_buy_trend_ok = trend_buy or (m15_slope_up and close >= float(ema20.iloc[i]) - 0.05 * a)
            session_sell_trend_ok = trend_sell or (m15_slope_dn and close <= float(ema20.iloc[i]) + 0.05 * a)
            sweep_asia_low = low < asia_lo and close > asia_lo and (min(open_, close) - low) > 0.35 * rng
            sweep_asia_high = high > asia_hi and close < asia_hi and (high - max(open_, close)) > 0.35 * rng
            recent_hi = float(df["high"].iloc[i - 36:i].max())
            recent_lo = float(df["low"].iloc[i - 36:i].min())
            recent_mid = (recent_hi + recent_lo) / 2.0
            sweep_recent_low = low < recent_lo and close > recent_lo and (min(open_, close) - low) > 0.30 * rng
            sweep_recent_high = high > recent_hi and close < recent_hi and (high - max(open_, close)) > 0.30 * rng
            if sweep_asia_low and not session_dn_impulse and session_buy_trend_ok:
                entry = close
                sl = low - 0.12 * a
                stop = abs(entry - sl)
                tp1 = min(float(asia_mid), entry + 1.30 * stop)
                tp1 = max(tp1, entry + 1.00 * stop)
                tp2 = min(asia_hi, entry + 1.8 * stop)
                build_trade("session_scalp", "BUY", entry, sl, tp1, tp2, "quét đáy phiên Á rồi đóng lại trong range", "Hủy nếu đóng dưới đáy quét phiên Á", 8, 120, 5)
            elif sweep_asia_high and not session_up_impulse and session_sell_trend_ok:
                entry = close
                sl = high + 0.12 * a
                stop = abs(entry - sl)
                tp1 = max(float(asia_mid), entry - 1.30 * stop)
                tp1 = min(tp1, entry - 1.00 * stop)
                tp2 = max(asia_lo, entry - 1.8 * stop)
                build_trade("session_scalp", "SELL", entry, sl, tp1, tp2, "quét đỉnh phiên Á rồi đóng lại trong range", "Hủy nếu đóng trên đỉnh quét phiên Á", 8, 120, 5)
            elif sweep_recent_low and not session_dn_impulse and session_buy_trend_ok:
                entry = close
                sl = low - 0.10 * a
                stop = abs(entry - sl)
                tp1 = min(float(recent_mid), entry + 1.25 * stop)
                tp1 = max(tp1, entry + 1.00 * stop)
                tp2 = min(recent_hi, entry + 1.5 * stop)
                build_trade("session_scalp", "BUY", entry, sl, tp1, tp2, "quét đáy range gần nhất trong phiên thanh khoản", "Hủy nếu đóng dưới đáy quét range gần", 7, 116, 4, 0.85)
            elif sweep_recent_high and not session_up_impulse and session_sell_trend_ok:
                entry = close
                sl = high + 0.10 * a
                stop = abs(entry - sl)
                tp1 = max(float(recent_mid), entry - 1.25 * stop)
                tp1 = min(tp1, entry - 1.00 * stop)
                tp2 = max(recent_lo, entry - 1.5 * stop)
                build_trade("session_scalp", "SELL", entry, sl, tp1, tp2, "quét đỉnh range gần nhất trong phiên thanh khoản", "Hủy nếu đóng trên đỉnh quét range gần", 7, 116, 4, 0.85)
            else:
                miss = []
                if session_up_impulse:
                    miss.append("đang up-impulse mạnh, tránh SELL ngược đà")
                if session_dn_impulse:
                    miss.append("đang down-impulse mạnh, tránh BUY ngược đà")
                if not session_buy_trend_ok:
                    miss.append("BUY chưa đồng thuận trend M5/M15")
                if not session_sell_trend_ok:
                    miss.append("SELL chưa đồng thuận trend M5/M15")
                miss.append("chưa có sweep range phiên Á + nến xác nhận")
                set_wait_status("session_scalp", why_missing(miss, "NO TRADE | Session scalp chưa đủ điều kiện"), buy_h=asia_lo, sell_h=asia_hi)
        elif in_session:
            set_wait_status("session_scalp", "NO TRADE | phiên Á quá rộng/thiếu dữ liệu")
        else:
            set_wait_status("session_scalp", "NO TRADE | ngoài phiên London/NY")
    else:
        set_wait_status("session_scalp", "disabled")

    ranked = sorted(
        candidates,
        key=lambda x: (
            -int(x.get("priority", 0)),
            float(x.get("sl_dist", 9999.0)),
            abs(float(x.get("tp1", x.get("entry", 0.0))) - float(x.get("entry", 0.0))),
            -float(x.get("rr", 0.0)),
        ),
    )
    if ranked:
        best = ranked[0]
        side = best["side"]
        reason = best["reason"]
        buy_hint = best.get("buy_hint")
        sell_hint = best.get("sell_hint")
        best_entry = float(best.get("entry", 0.0))
        best_sl = float(best.get("sl", 0.0))
        best_tp1 = float(best.get("tp1", 0.0))
        best_tp2 = float(best.get("tp2", 0.0))
        best_rr = float(best.get("rr", 0.0))
    else:
        base = "NO TRADE | thị trường nhiễu hoặc chưa có nến xác nhận"
        reason = base
        buy_hint = None
        sell_hint = None
        best_entry = 0.0
        best_sl = 0.0
        best_tp1 = 0.0
        best_tp2 = 0.0
        best_rr = 0.0

    return {
        "side": side,
        "reason": reason,
        "m5_time": t_now,
        "close": close,
        "atr": a,
        "lvn": float(ema20.iloc[i]),
        "buy_price_hint": buy_hint,
        "sell_price_hint": sell_hint,
        "atr_rank": atr_rank,
        "trend_strength": trend_strength,
        "candidates": ranked,
        "strategy_status": strategy_status,
        "entry": best_entry,
        "sl": best_sl,
        "tp1": best_tp1,
        "tp2": best_tp2,
        "rr": best_rr,
        "tp": best_tp2 if best_tp2 > 0 else None,
        "market_ctx": {
            "trend_buy": bool(trend_buy),
            "trend_sell": bool(trend_sell),
            "trend_buy_15": bool(trend_buy_15),
            "trend_sell_15": bool(trend_sell_15),
            "trend_buy_h1": bool(trend_buy_h1),
            "trend_sell_h1": bool(trend_sell_h1),
            "trend_flat": bool(trend_flat),
            "in_london_ny": bool(in_london_ny),
            "up_impulse": bool(up_impulse),
            "down_impulse": bool(down_impulse),
            "range12_atr": float(range_w_12 / max(1e-9, a)),
            "range40_atr": float(range_w_40 / max(1e-9, a)),
            "vol_ratio": float(vol_ratio),
            "rsi": float(rsi_now),
        },
    }


def close_position_market(cfg, pos, note="mode-close"):
    info = mt5.symbol_info(cfg["symbol"])
    tick = mt5.symbol_info_tick(cfg["symbol"])
    if info is None or tick is None:
        return False, "symbol/tick unavailable"
    ptype = int(getattr(pos, "type", -1))
    side = "BUY" if ptype == int(mt5.POSITION_TYPE_BUY) else "SELL"
    close_type = mt5.ORDER_TYPE_SELL if side == "BUY" else mt5.ORDER_TYPE_BUY
    price = float(getattr(tick, "bid", 0.0) if side == "BUY" else getattr(tick, "ask", 0.0))
    req_base = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": cfg["symbol"],
        "position": int(getattr(pos, "ticket", 0) or 0),
        "volume": float(getattr(pos, "volume", 0.0) or 0.0),
        "type": close_type,
        "price": price,
        "deviation": int(cfg.get("deviation", 25)),
        "magic": int(getattr(pos, "magic", 0) or 0),
        "comment": "EGSM3EXIT",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    fill_modes = [int(getattr(info, "filling_mode", -1)), mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    last_err = ""
    for fm in fill_modes:
        if not isinstance(fm, int) or fm < 0:
            continue
        req = dict(req_base)
        req["type_filling"] = fm
        res = mt5.order_send(req)
        if res is not None and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
            return True, "ok"
        last_err = f"ret={getattr(res, 'retcode', None)} comment={getattr(res, 'comment', '')} last_error={mt5.last_error()}"
    return False, f"close failed | {note} | {last_err}"


def close_mode_positions(cfg, mode_id, reason="manual"):
    pos = my_positions(cfg, mode_id)
    if not pos:
        return True, "no positions"
    ok_count = 0
    fail_msgs = []
    for p in pos:
        ok, msg = close_position_market(cfg, p, note=reason)
        if ok:
            ok_count += 1
        else:
            fail_msgs.append(msg)
    if fail_msgs:
        return False, f"closed={ok_count}/{len(pos)} | " + " | ".join(fail_msgs[:2])
    return True, f"closed={ok_count}/{len(pos)}"


def _mode3_snapshot(cfg, atr_now):
    pos = my_positions(cfg, MODE_DCA_M5_3)
    if not pos:
        return {
            "layers": 0,
            "side": None,
            "avg_entry": None,
            "floating": 0.0,
            "next_add_price": None,
            "next_lot_factor": MODE3_DCA_LOT_FACTORS[0],
        }
    sides = set()
    entries = []
    volumes = []
    floating = 0.0
    for p in pos:
        ptype = int(getattr(p, "type", -1))
        if ptype == int(mt5.POSITION_TYPE_BUY):
            sides.add("BUY")
        elif ptype == int(mt5.POSITION_TYPE_SELL):
            sides.add("SELL")
        px = float(getattr(p, "price_open", 0.0) or 0.0)
        vol = float(getattr(p, "volume", 0.0) or 0.0)
        if px > 0 and vol > 0:
            entries.append(px)
            volumes.append(vol)
        floating += float(getattr(p, "profit", 0.0) or 0.0)
    if not entries:
        return {
            "layers": len(pos),
            "side": "MIXED" if len(sides) != 1 else list(sides)[0],
            "avg_entry": None,
            "floating": floating,
            "next_add_price": None,
            "next_lot_factor": MODE3_DCA_LOT_FACTORS[min(len(pos), len(MODE3_DCA_LOT_FACTORS) - 1)],
        }
    avg_entry = float(np.average(np.array(entries, dtype=float), weights=np.array(volumes, dtype=float)))
    side = list(sides)[0] if len(sides) == 1 else "MIXED"
    layers = len(pos)
    spacing = MODE3_DCA_SPACING_ATR[0] if layers <= 1 else MODE3_DCA_SPACING_ATR[1]
    if side == "BUY":
        edge = min(entries)
        next_add = edge - float(spacing) * atr_now
    elif side == "SELL":
        edge = max(entries)
        next_add = edge + float(spacing) * atr_now
    else:
        next_add = None
    return {
        "layers": layers,
        "side": side,
        "avg_entry": avg_entry,
        "floating": floating,
        "next_add_price": next_add,
        "next_lot_factor": MODE3_DCA_LOT_FACTORS[min(layers, len(MODE3_DCA_LOT_FACTORS) - 1)],
    }


def compute_mode3_dca_signal(cfg):
    bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M5, 0, 900)
    if bars is None or len(bars) < 320:
        return None
    df = pd.DataFrame(bars).iloc[:-1].reset_index(drop=True)
    if len(df) < 320:
        return None
    atr = atr_series(df, 14)
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    ema200 = df["close"].ewm(span=200, adjust=False).mean()
    rsi = rsi_series(df["close"].astype(float), 14)

    bars_m15 = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M15, 0, 320)
    bars_h1 = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_H1, 0, 260)
    if bars_m15 is None or len(bars_m15) < 120 or bars_h1 is None or len(bars_h1) < 100:
        return None
    d15 = pd.DataFrame(bars_m15).iloc[:-1].reset_index(drop=True)
    dh1 = pd.DataFrame(bars_h1).iloc[:-1].reset_index(drop=True)
    e20_15 = d15["close"].ewm(span=20, adjust=False).mean()
    e50_15 = d15["close"].ewm(span=50, adjust=False).mean()
    e200_15 = d15["close"].ewm(span=200, adjust=False).mean()
    e20_h1 = dh1["close"].ewm(span=20, adjust=False).mean()
    e50_h1 = dh1["close"].ewm(span=50, adjust=False).mean()
    e200_h1 = dh1["close"].ewm(span=200, adjust=False).mean()

    i = len(df) - 1
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    t_now = int(row["time"])
    close = float(row["close"])
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    a = float(atr.iloc[i]) if float(atr.iloc[i]) > 0 else 0.0
    if a <= 0:
        return None

    atr_hist = atr.iloc[max(0, i - 288): i + 1].dropna().to_numpy(dtype=float)
    atr_rank = float((atr_hist <= a).sum()) / float(len(atr_hist)) if len(atr_hist) >= 8 else 0.5
    trend_strength = abs(float(e20_15.iloc[-1]) - float(e50_15.iloc[-1])) / max(1e-9, a)
    body = abs(close - open_)
    rng = max(1e-9, high - low)
    rsi_now = float(rsi.iloc[i])

    trend_h1_up = float(dh1["close"].iloc[-1]) > float(e50_h1.iloc[-1]) > float(e200_h1.iloc[-1]) and float(e20_h1.iloc[-1]) > float(e50_h1.iloc[-1])
    trend_h1_dn = float(dh1["close"].iloc[-1]) < float(e50_h1.iloc[-1]) < float(e200_h1.iloc[-1]) and float(e20_h1.iloc[-1]) < float(e50_h1.iloc[-1])
    trend_m15_up = float(d15["close"].iloc[-1]) > float(e50_15.iloc[-1]) > float(e200_15.iloc[-1]) and float(e20_15.iloc[-1]) > float(e50_15.iloc[-1])
    trend_m15_dn = float(d15["close"].iloc[-1]) < float(e50_15.iloc[-1]) < float(e200_15.iloc[-1]) and float(e20_15.iloc[-1]) < float(e50_15.iloc[-1])
    trend_up = trend_m15_up or (trend_h1_up and close > float(ema20.iloc[i]))
    trend_dn = trend_m15_dn or (trend_h1_dn and close < float(ema20.iloc[i]))

    snapshot = _mode3_snapshot(cfg, a)
    acc = mt5.account_info()
    bal = float(getattr(acc, "balance", 0.0) or 0.0) if acc is not None else 0.0
    basket_sl_money = -bal * float(cfg.get("mode3_dca_basket_sl_pct", MODE3_DCA_BASKET_SL_PCT)) / 100.0 if bal > 0 else -999999.0
    basket_tp_money = bal * float(cfg.get("mode3_dca_basket_tp_pct", MODE3_DCA_BASKET_TP_PCT)) / 100.0 if bal > 0 else 999999.0

    side = None
    reason = "NO TRADE | DCA waiting setup"
    entry = 0.0
    sl = 0.0
    tp1 = 0.0
    tp2 = 0.0
    rr = 0.0
    lot_factor = 1.0
    dca_action = "hold"
    close_reason = ""
    max_layers = int(cfg.get("mode3_dca_max_layers", MODE3_DCA_MAX_LAYERS))
    buy_hint = float(ema20.iloc[i])
    sell_hint = float(ema20.iloc[i])

    if snapshot["layers"] > 0:
        side_now = str(snapshot.get("side"))
        floating = float(snapshot.get("floating", 0.0))
        avg_entry = float(snapshot.get("avg_entry", close) or close)
        next_add = snapshot.get("next_add_price")
        if isinstance(next_add, (int, float)):
            if side_now == "BUY":
                buy_hint = float(next_add)
                sell_hint = float(avg_entry + 0.8 * a)
            elif side_now == "SELL":
                sell_hint = float(next_add)
                buy_hint = float(avg_entry - 0.8 * a)
        if side_now == "MIXED":
            dca_action = "close_all"
            close_reason = "DCA mixed sides detected"
            reason = "NO TRADE | mixed BUY/SELL trong cùng giỏ DCA"
        elif floating <= basket_sl_money:
            dca_action = "close_all"
            close_reason = f"Basket SL hit ({floating:.2f} <= {basket_sl_money:.2f})"
            reason = "NO TRADE | Basket SL kích hoạt"
        elif floating >= basket_tp_money:
            dca_action = "close_all"
            close_reason = f"Basket TP hit ({floating:.2f} >= {basket_tp_money:.2f})"
            reason = "NO TRADE | Basket TP kích hoạt"
        else:
            trend_broken = (side_now == "BUY" and trend_dn and close < float(ema50.iloc[i])) or (side_now == "SELL" and trend_up and close > float(ema50.iloc[i]))
            if trend_broken and floating < 0:
                dca_action = "close_all"
                close_reason = f"Trend break against basket ({side_now})"
                reason = "NO TRADE | Trend break ngược giỏ DCA"
            else:
                can_add = snapshot["layers"] < max_layers and isinstance(next_add, (int, float))
                if side_now == "BUY" and can_add and close <= float(next_add) and trend_up and rsi_now >= 35:
                    side = "BUY"
                    dca_action = "add"
                    lot_factor = float(snapshot.get("next_lot_factor", 1.0))
                    entry = close
                    sl = min(float(df["low"].iloc[i - 24:i].min()), close - 1.8 * a)
                    tp1 = avg_entry + 0.8 * a
                    tp2 = avg_entry + 2.2 * a
                    rr = abs(tp2 - entry) / max(1e-9, abs(entry - sl))
                    reason = (
                        f"DCA ADD BUY lớp {snapshot['layers'] + 1}/{max_layers} | "
                        f"avg={avg_entry:.2f} next_add={float(next_add):.2f} floating={floating:+.2f}"
                    )
                elif side_now == "SELL" and can_add and close >= float(next_add) and trend_dn and rsi_now <= 65:
                    side = "SELL"
                    dca_action = "add"
                    lot_factor = float(snapshot.get("next_lot_factor", 1.0))
                    entry = close
                    sl = max(float(df["high"].iloc[i - 24:i].max()), close + 1.8 * a)
                    tp1 = avg_entry - 0.8 * a
                    tp2 = avg_entry - 2.2 * a
                    rr = abs(tp2 - entry) / max(1e-9, abs(entry - sl))
                    reason = (
                        f"DCA ADD SELL lớp {snapshot['layers'] + 1}/{max_layers} | "
                        f"avg={avg_entry:.2f} next_add={float(next_add):.2f} floating={floating:+.2f}"
                    )
                else:
                    reason = (
                        f"NO TRADE | DCA HOLD {side_now} | layers={snapshot['layers']}/{max_layers} "
                        f"avg={avg_entry:.2f} floating={floating:+.2f}"
                    )
    else:
        pullback_buy = low <= float(ema20.iloc[i]) + 0.15 * a and close > float(ema20.iloc[i]) and close > float(prev["close"]) and close > open_ and rsi_now >= 45
        pullback_sell = high >= float(ema20.iloc[i]) - 0.15 * a and close < float(ema20.iloc[i]) and close < float(prev["close"]) and close < open_ and rsi_now <= 55
        if trend_up and pullback_buy:
            side = "BUY"
            dca_action = "open"
            lot_factor = MODE3_DCA_LOT_FACTORS[0]
            entry = close
            sl = min(float(df["low"].iloc[i - 16:i].min()), close - 1.6 * a)
            tp1 = entry + abs(entry - sl)
            tp2 = entry + 1.8 * abs(entry - sl)
            rr = abs(tp2 - entry) / max(1e-9, abs(entry - sl))
            reason = "DCA OPEN BUY | trend-up + pullback EMA20 + nến xác nhận"
        elif trend_dn and pullback_sell:
            side = "SELL"
            dca_action = "open"
            lot_factor = MODE3_DCA_LOT_FACTORS[0]
            entry = close
            sl = max(float(df["high"].iloc[i - 16:i].max()), close + 1.6 * a)
            tp1 = entry - abs(entry - sl)
            tp2 = entry - 1.8 * abs(entry - sl)
            rr = abs(tp2 - entry) / max(1e-9, abs(entry - sl))
            reason = "DCA OPEN SELL | trend-down + pullback EMA20 + nến xác nhận"
        else:
            reason = "NO TRADE | chưa có trend pullback setup cho DCA M5"
            buy_hint = float(ema20.iloc[i] - 0.15 * a)
            sell_hint = float(ema20.iloc[i] + 0.15 * a)

    return {
        "side": side,
        "reason": reason,
        "m5_time": t_now,
        "close": close,
        "atr": a,
        "lvn": float(ema20.iloc[i]),
        "buy_price_hint": buy_hint,
        "sell_price_hint": sell_hint,
        "atr_rank": atr_rank,
        "trend_strength": trend_strength,
        "strategy_id": "dca_m5",
        "entry": float(entry) if entry else 0.0,
        "sl": float(sl) if sl else 0.0,
        "tp1": float(tp1) if tp1 else 0.0,
        "tp2": float(tp2) if tp2 else 0.0,
        "rr": float(rr),
        "tp": float(tp2) if tp2 else None,
        "lot_factor": float(lot_factor),
        "dca_action": dca_action,
        "close_reason": close_reason,
        "dca_layers": int(snapshot.get("layers", 0)),
        "dca_max_layers": int(max_layers),
        "dca_floating": float(snapshot.get("floating", 0.0)),
        "dca_next_add": snapshot.get("next_add_price"),
        "dca_basket_sl_money": float(basket_sl_money),
        "dca_basket_tp_money": float(basket_tp_money),
    }


def compute_signal_by_modes(cfg):
    """Multi-mode dispatcher."""
    active_modes = get_active_modes(cfg)
    first_payload = None
    for mode in active_modes:
        payload = None
        if mode == MODE_LVN_1:
            payload = compute_mode1_lvn_signal(cfg)
        elif mode == MODE_SCALP_M1_2:
            payload = compute_mode2_m1_scalp_signal(cfg)
        elif mode == MODE_DCA_M5_3:
            payload = compute_mode3_dca_signal(cfg)
        if payload is None:
            continue
        payload["mode"] = mode
        if first_payload is None:
            first_payload = payload
        if payload.get("side") in ("BUY", "SELL"):
            return payload
    return first_payload


def get_today_mode_stats(cfg, mode):
    now = time.time()
    key = (id(cfg), mode)
    cached = _today_mode_cache.get(key)
    if cached and now - cached.get("t", 0) < 5:
        return cached["data"]
    empty = {"pnl_today": 0.0, "closed_today": 0, "wins": 0, "losses": 0}
    try:
        if mt5 is None:
            return empty
        start = datetime.combine(datetime.now().date(), datetime.min.time())
        deals = mt5.history_deals_get(start, datetime.now())
        if deals is None:
            _today_mode_cache[key] = {"t": now, "data": empty}
            return empty
        magic = mode_magic(cfg, mode)
        out = dict(empty)
        for d in deals:
            if int(getattr(d, "magic", 0)) != int(magic):
                continue
            if int(getattr(d, "entry", -1)) == int(mt5.DEAL_ENTRY_OUT):
                pnl = float(getattr(d, "profit", 0.0) or 0.0) + float(getattr(d, "swap", 0.0) or 0.0) + float(getattr(d, "commission", 0.0) or 0.0)
                out["pnl_today"] += pnl
                out["closed_today"] += 1
                if pnl > 0:
                    out["wins"] += 1
                elif pnl < 0:
                    out["losses"] += 1
        _today_mode_cache[key] = {"t": now, "data": out}
        return out
    except Exception:
        return empty


def _deal_reason_text(reason_code):
    code = int(reason_code)
    mapping = {
        int(getattr(mt5, "DEAL_REASON_SL", -10001)): "SL",
        int(getattr(mt5, "DEAL_REASON_TP", -10002)): "TP",
        int(getattr(mt5, "DEAL_REASON_SO", -10003)): "STOP_OUT",
        int(getattr(mt5, "DEAL_REASON_EXPERT", -10004)): "EXPERT",
        int(getattr(mt5, "DEAL_REASON_CLIENT", -10005)): "MANUAL",
        int(getattr(mt5, "DEAL_REASON_MOBILE", -10006)): "MOBILE",
        int(getattr(mt5, "DEAL_REASON_WEB", -10007)): "WEB",
    }
    return mapping.get(code, f"reason#{code}")


def log_recent_closed_deals(cfg, lookback_hours=24, started_ts=None):
    """Emit detailed close diagnostics for win/loss debug."""
    try:
        start = datetime.now() - timedelta(hours=max(1, int(lookback_hours)))
        deals = mt5.history_deals_get(start, datetime.now())
        if deals is None:
            return
        mode_magic_map = {int(mode_magic(cfg, m)): m for m in MODE_LABELS}
        in_by_pos = {}
        for d in deals:
            magic = int(getattr(d, "magic", 0) or 0)
            if magic not in mode_magic_map:
                continue
            if int(getattr(d, "entry", -1)) != int(mt5.DEAL_ENTRY_IN):
                continue
            pos_id = int(getattr(d, "position_id", 0) or 0)
            if pos_id <= 0:
                continue
            d_type = int(getattr(d, "type", -1))
            side = "BUY" if d_type == int(getattr(mt5, "DEAL_TYPE_BUY", -999)) else ("SELL" if d_type == int(getattr(mt5, "DEAL_TYPE_SELL", -998)) else "UNK")
            in_by_pos[pos_id] = {
                "entry": float(getattr(d, "price", 0.0) or 0.0),
                "side": side,
                "comment": str(getattr(d, "comment", "") or ""),
            }

        now = time.time()
        for d in deals:
            magic = int(getattr(d, "magic", 0) or 0)
            mode_id = mode_magic_map.get(magic)
            if mode_id is None:
                continue
            if int(getattr(d, "entry", -1)) != int(mt5.DEAL_ENTRY_OUT):
                continue
            d_ts = int(getattr(d, "time", 0) or 0)
            if isinstance(started_ts, (int, float)) and d_ts > 0 and d_ts < int(started_ts) - 2:
                continue
            ticket = int(getattr(d, "ticket", 0) or 0)
            if ticket <= 0 or ticket in _closed_deal_log_cache:
                continue
            pos_id = int(getattr(d, "position_id", 0) or 0)
            in_leg = in_by_pos.get(pos_id, {})
            entry_price = in_leg.get("entry")
            side = in_leg.get("side", "UNK")
            exit_price = float(getattr(d, "price", 0.0) or 0.0)
            pnl = float(getattr(d, "profit", 0.0) or 0.0) + float(getattr(d, "swap", 0.0) or 0.0) + float(getattr(d, "commission", 0.0) or 0.0)
            outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
            if isinstance(entry_price, (int, float)) and side in ("BUY", "SELL"):
                move = (exit_price - float(entry_price)) if side == "BUY" else (float(entry_price) - exit_price)
                move_txt = f"{move:+.2f}"
                entry_txt = f"{float(entry_price):.2f}"
            else:
                move_txt = "-"
                entry_txt = "-"
            comment = str(getattr(d, "comment", "") or "")
            open_comment = str(in_leg.get("comment", "") or "")
            sid = strategy_id_from_comment(open_comment or comment, mode_id)
            if mode_id == MODE_SCALP_M1_2:
                strat_label = MODE2_STRATEGY_LABELS.get(sid or "", sid or "-")
            elif mode_id == MODE_DCA_M5_3:
                strat_label = "DCA M5"
            else:
                strat_label = str(sid or "Mode1")
            reason_txt = _deal_reason_text(getattr(d, "reason", -1))
            log(
                f"[CLOSE][{MODE_LABELS.get(mode_id, mode_id)}/{strat_label}] {outcome} {side} "
                f"entry={entry_txt} exit={exit_price:.2f} move={move_txt} pnl={pnl:+.2f} "
                f"close_reason={reason_txt} comment={comment or '-'}",
                "info",
            )
            _closed_deal_log_cache[ticket] = now
        # prune old cache entries
        if len(_closed_deal_log_cache) > 3000:
            stale = sorted(_closed_deal_log_cache.items(), key=lambda kv: kv[1])[:1000]
            for tk, _ in stale:
                _closed_deal_log_cache.pop(tk, None)
    except Exception as exc:
        log(f"[CLOSE] diagnostics failed: {exc}", "warn")


def lot_from_risk(cfg, stop_distance):
    acc = mt5.account_info()
    info = mt5.symbol_info(cfg["symbol"])
    if acc is None or info is None:
        return 0.0
    bal = float(acc.balance)
    risk_pct = max(0.01, float(cfg.get("risk_pct", 0.5)))
    risk_money = bal * risk_pct / 100.0

    tick_value = max(
        abs(float(getattr(info, "trade_tick_value", 0.0) or 0.0)),
        abs(float(getattr(info, "trade_tick_value_profit", 0.0) or 0.0)),
        abs(float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0)),
    )
    tick_size = float(getattr(info, "trade_tick_size", 0.01) or 0.01)
    if tick_value <= 0 or tick_size <= 0 or stop_distance <= 0:
        return round_lot(float(cfg.get("fixed_lot_fallback", 0.01)), info)

    value_per_price_unit = tick_value / tick_size
    loss_per_lot = stop_distance * value_per_price_unit
    if loss_per_lot <= 0:
        return round_lot(float(cfg.get("fixed_lot_fallback", 0.01)), info)

    raw_lot = risk_money / loss_per_lot
    return round_lot(raw_lot, info)


def auto_sl_tp_profile(signal):
    """Auto derive SL/TP profile from M5 volatility regime + trend strength."""
    atr_rank = float(signal.get("atr_rank", 0.5))
    trend_strength = float(signal.get("trend_strength", 0.0))

    if atr_rank < 0.35:
        sl_mult, rr = 1.15, 1.55
        regime = "CALM"
    elif atr_rank < 0.75:
        sl_mult, rr = 1.35, 1.85
        regime = "NORMAL"
    else:
        sl_mult, rr = 1.75, 2.20
        regime = "VOLATILE"

    # Favor stronger trend continuation with slightly larger reward target.
    if trend_strength >= 1.20:
        rr = min(2.60, rr + 0.20)
    elif trend_strength <= 0.45 and atr_rank >= 0.75:
        # Very noisy/choppy high-vol regime: avoid over-optimistic TP.
        rr = max(1.90, rr - 0.20)

    return {
        "sl_mult": float(sl_mult),
        "rr": float(rr),
        "regime": regime,
        "atr_rank": atr_rank,
        "trend_strength": trend_strength,
    }


def open_trade(cfg, side, signal):
    info = mt5.symbol_info(cfg["symbol"])
    tick = mt5.symbol_info_tick(cfg["symbol"])
    if info is None or tick is None:
        return False, "symbol/tick unavailable"

    atr_now = max(1e-9, float(signal.get("atr", 0.0)))
    profile = auto_sl_tp_profile(signal)
    sl_atr = max(0.2, float(profile["sl_mult"]))
    rr = max(0.5, float(profile["rr"]))
    # Broker stop-level guard: avoid invalid SL/TP too close to market.
    point = float(getattr(info, "point", 0.01) or 0.01)
    stops_level_pts = float(getattr(info, "trade_stops_level", 0.0) or 0.0)
    min_stop_dist = max(0.0, stops_level_pts * point)

    digits = int(getattr(info, "digits", 2) or 2)
    price = float(tick.ask if side == "BUY" else tick.bid)
    s_sl = signal.get("sl")
    s_tp2 = signal.get("tp2")
    s_tp = signal.get("tp")
    if isinstance(s_sl, (int, float)) and (isinstance(s_tp2, (int, float)) or isinstance(s_tp, (int, float))):
        sl = float(s_sl)
        tp = float(s_tp2 if isinstance(s_tp2, (int, float)) else s_tp)
        if side == "BUY":
            stop_dist = price - sl
            tp_dist = tp - price
            otype = mt5.ORDER_TYPE_BUY
        else:
            stop_dist = sl - price
            tp_dist = price - tp
            otype = mt5.ORDER_TYPE_SELL
        if stop_dist <= 0 or tp_dist <= 0:
            # Price moved after signal; repair SL/TP orientation around current tick.
            sig_entry = float(signal.get("entry", price) or price)
            sig_sl = float(s_sl)
            stop_dist = abs(sig_entry - sig_sl)
            if stop_dist < min_stop_dist * 1.05:
                stop_dist = min_stop_dist * 1.05
            rr_fix = max(1.2, float(signal.get("rr", rr) or rr))
            if side == "BUY":
                sl = price - stop_dist
                tp = price + stop_dist * rr_fix
                tp_dist = tp - price
            else:
                sl = price + stop_dist
                tp = price - stop_dist * rr_fix
                tp_dist = price - tp
        if stop_dist < min_stop_dist * 1.05:
            return False, "SL too close for broker stop-level"
        if tp_dist / max(1e-9, stop_dist) < 1.2:
            # v14: execution-aware RR repair when price drift degrades live RR.
            rr_min_live = 1.2
            if side == "BUY":
                tp = price + stop_dist * rr_min_live
                tp_dist = tp - price
            else:
                tp = price - stop_dist * rr_min_live
                tp_dist = price - tp
            if tp_dist / max(1e-9, stop_dist) < rr_min_live:
                return False, "RR below 1:1.2"
        rr = tp_dist / max(1e-9, stop_dist)
    else:
        stop_dist = sl_atr * atr_now
        if stop_dist < min_stop_dist * 1.05:
            stop_dist = min_stop_dist * 1.05
        if side == "BUY":
            sl = price - stop_dist
            tp = price + stop_dist * rr
            otype = mt5.ORDER_TYPE_BUY
        else:
            sl = price + stop_dist
            tp = price - stop_dist * rr
            otype = mt5.ORDER_TYPE_SELL

    lot = lot_from_risk(cfg, stop_dist)
    lot_factor = float(signal.get("lot_factor", 1.0) or 1.0)
    if lot_factor < 0.05:
        lot_factor = 0.05
    if lot_factor != 1.0:
        lot = round_lot(float(lot) * lot_factor, info)
    if lot <= 0:
        return False, "lot <= 0"

    price = round(price, digits)
    sl = round(sl, digits)
    tp = round(tp, digits)

    mode_id = str(signal.get("mode", MODE_LVN_1))
    strategy_id = str(signal.get("strategy_id", "") or "")
    mode_short = MODE_SHORT.get(mode_id, "m0")
    strat_short = ""
    if mode_id == MODE_SCALP_M1_2 and strategy_id:
        strat_short = MODE2_STRATEGY_SHORT.get(strategy_id, "")
    trade_comment = f"EGS{mode_short}{strat_short}" if strat_short else f"EGS{mode_short}"
    trade_comment = trade_comment[:31]
    req_base = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": cfg["symbol"],
        "volume": lot,
        "type": otype,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": int(cfg.get("deviation", 25)),
        "magic": int(mode_magic(cfg, mode_id)),
        "comment": trade_comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    preferred_fill = int(getattr(info, "filling_mode", -1))
    fill_modes = [preferred_fill, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    # Keep order but avoid duplicates/invalid entries.
    uniq_fill_modes = []
    for fm in fill_modes:
        if isinstance(fm, int) and fm >= 0 and fm not in uniq_fill_modes:
            uniq_fill_modes.append(fm)

    res = None
    attempts = []
    comment_variants = [trade_comment]
    if trade_comment:
        comment_variants.append("")
    for fm in uniq_fill_modes:
        for cm in comment_variants:
            req = dict(req_base)
            req["type_filling"] = fm
            req["comment"] = cm
            ctag = "comment=empty" if not cm else "comment=set"
            chk = mt5.order_check(req)
            if chk is None:
                attempts.append(f"check fill={fm} {ctag} ret=None last_error={mt5.last_error()}")
                continue
            chk_ret = getattr(chk, "retcode", None)
            if chk_ret not in (0, mt5.TRADE_RETCODE_DONE):
                attempts.append(
                    f"check fill={fm} {ctag} ret={chk_ret} comment={getattr(chk, 'comment', '')} last_error={mt5.last_error()}"
                )
                continue
            res = mt5.order_send(req)
            if res is not None and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                break
            attempts.append(
                f"send fill={fm} {ctag} ret={getattr(res, 'retcode', None)} comment={getattr(res, 'comment', '')} last_error={mt5.last_error()}"
            )
        if res is not None and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
            break

    if res is None or getattr(res, "retcode", None) != mt5.TRADE_RETCODE_DONE:
        return False, "order_send failed | " + " | ".join(attempts[-3:])

    # Cache TP1/initial risk for Mode 2 post-entry management (TP1 partial + BE/LOCK).
    try:
        if mode_id == MODE_SCALP_M1_2:
            pos_ticket = int(getattr(res, "order", 0) or 0)
            s_tp1 = signal.get("tp1")
            if pos_ticket > 0 and isinstance(s_tp1, (int, float)):
                _mode2_be_cache[pos_ticket] = {
                    "init_risk": float(stop_dist),
                    "stage": "init",
                    "tp1": float(s_tp1),
                    "tp1_done": False,
                }
    except Exception:
        pass

    send(
        {
            "type": "trade",
            "event": "open",
            "side": side,
            "lot": lot,
            "price": price,
            "sl": sl,
            "tp": tp,
            "ticket": getattr(res, "order", 0),
            "sl_mult": sl_atr,
            "rr": rr,
            "regime": profile["regime"],
            "mode": signal.get("mode", MODE_LVN_1),
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
    )
    log(
        f"OPEN {side} lot={lot:.2f} @ {price:.2f} SL={sl:.2f} TP={tp:.2f} | "
        f"autoSL={sl_atr:.2f}ATR autoRR={rr:.2f} ({profile['regime']})"
    )
    return True, "ok"


def push_status(
    cfg,
    last_signal,
    signal_reason,
    profile_text="-",
    entry_hint="-",
    active_mode_label="-",
    mode_runtime=None,
):
    acc = mt5.account_info()
    if acc is None:
        return
    positions = account_positions_all_modes(cfg)
    floating = float(sum(float(p.profit) for p in positions)) if positions else 0.0
    mode_runtime = mode_runtime or {}
    mode_stats = []
    signal_rows = []
    for mode in MODE_LABELS:
        mpos = my_positions(cfg, mode)
        mfloating = float(sum(float(p.profit) for p in mpos)) if mpos else 0.0
        day = get_today_mode_stats(cfg, mode)
        rt = mode_runtime.get(mode, {})
        mode_stats.append(
            {
                "id": mode,
                "label": MODE_LABELS.get(mode, mode),
                "enabled": bool(cfg.get("modes", {}).get(mode, {}).get("enabled", False)),
                "open_positions": len(mpos),
                "floating": mfloating,
                "pnl_today": float(day.get("pnl_today", 0.0)),
                "closed_today": int(day.get("closed_today", 0)),
                "last_signal": str(rt.get("last_signal", "-")),
                "signal_reason": str(rt.get("signal_reason", "-")),
                "entry_hint": str(rt.get("entry_hint", "-")),
                "buy_hint": rt.get("buy_hint"),
                "sell_hint": rt.get("sell_hint"),
                "profile_text": str(rt.get("profile_text", "-")),
            }
        )
        if mode == MODE_SCALP_M1_2:
            mode_cfg = cfg.get("modes", {}).get(mode, {})
            strat_cfg = mode_cfg.get("strategies", {}) if isinstance(mode_cfg, dict) else {}
            enabled_strats = [sid for sid in MODE2_STRATEGY_LABELS if bool(strat_cfg.get(sid, True))]
            if not enabled_strats:
                enabled_strats = list(MODE2_STRATEGY_LABELS.keys())
            rt_strats = rt.get("strategies", {}) if isinstance(rt, dict) else {}
            for sid in enabled_strats:
                srt = rt_strats.get(sid, {})
                signal_rows.append(
                    {
                        "mode_label": MODE_LABELS.get(mode, mode),
                        "strategy_label": MODE2_STRATEGY_LABELS.get(sid, sid),
                        "state": str(srt.get("last_signal", "WAIT")),
                        "decision": "TRADE" if str(srt.get("last_signal", "WAIT")) in ("BUY", "SELL") else "NO TRADE",
                        "reason": str(srt.get("signal_reason", "no-setup")),
                        "buy_hint": srt.get("buy_hint"),
                        "sell_hint": srt.get("sell_hint"),
                    }
                )
        else:
            signal_rows.append(
                {
                    "mode_label": MODE_LABELS.get(mode, mode),
                    "strategy_label": str(rt.get("strategy_label", "-")),
                    "state": str(rt.get("last_signal", "-")),
                    "decision": "TRADE" if str(rt.get("last_signal", "-")) in ("BUY", "SELL") else "NO TRADE",
                    "reason": str(rt.get("signal_reason", "-")),
                    "buy_hint": rt.get("buy_hint"),
                    "sell_hint": rt.get("sell_hint"),
                }
            )
    send(
        {
            "type": "status",
            "login": int(acc.login),
            "balance": float(acc.balance),
            "equity": float(acc.equity),
            "margin_level": float(acc.margin_level) if float(acc.margin) > 0 else 0.0,
            "currency": str(acc.currency),
            "symbol": cfg["symbol"],
            "floating": floating,
            "open_positions": len(positions),
            "last_signal": last_signal,
            "signal_reason": signal_reason,
            "profile_text": profile_text,
            "entry_hint": entry_hint,
            "active_mode": active_mode_label,
            "mode_stats": mode_stats,
            "signal_rows": signal_rows,
            "positions": [
                {
                    "ticket": int(p.ticket),
                    "side": "BUY" if int(p.type) == mt5.POSITION_TYPE_BUY else "SELL",
                    "lot": float(p.volume),
                    "open_price": float(p.price_open),
                    "current_price": float(p.price_current),
                    "profit": float(p.profit),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "time": datetime.fromtimestamp(int(p.time)).strftime("%H:%M:%S"),
                }
                for p in positions
            ],
        }
    )


def run_worker(cfg):
    cfg = dict(cfg or {})
    cfg.setdefault("symbol", "XAUUSDc")
    cfg.setdefault("magic", 700100)
    cfg.setdefault("max_positions", 1)
    cfg.setdefault("risk_pct", 0.5)
    cfg.setdefault("lvn_window", 96)
    cfg.setdefault("lvn_bins", 32)
    cfg.setdefault("lvn_count", 8)
    cfg.setdefault("mode1_vp_lookback", 36)
    cfg.setdefault("mode1_lvn_max_dist_atr", 0.9)
    cfg.setdefault("mode1_lvn_short_window", 18)
    cfg.setdefault("mode1_lvn_short_bins", 24)
    cfg.setdefault("mode1_lvn_short_count", 4)
    cfg.setdefault("touch_atr", 0.30)
    cfg.setdefault("ema_fast", 20)
    cfg.setdefault("ema_slow", 60)
    cfg.setdefault("atr_regime_window", 288)
    cfg.setdefault("deviation", 25)
    cfg.setdefault("fixed_lot_fallback", 0.01)
    cfg.setdefault("mode3_dca_max_layers", MODE3_DCA_MAX_LAYERS)
    cfg.setdefault("mode3_dca_basket_sl_pct", MODE3_DCA_BASKET_SL_PCT)
    cfg.setdefault("mode3_dca_basket_tp_pct", MODE3_DCA_BASKET_TP_PCT)
    cfg.setdefault("active_modes", list(DEFAULT_ACTIVE_MODES))
    normalize_mode_settings(cfg)

    if not init_mt5(cfg):
        send({"type": "exit", "reason": "mt5 init failed"})
        return

    threading.Thread(target=_stdin_watch, daemon=True).start()
    active_mode_labels = [MODE_LABELS.get(m, m) for m in get_active_modes(cfg)]
    log(
        f"EAGoldSuper started | symbol={cfg['symbol']} | risk={cfg['risk_pct']}% | "
        f"active_modes={', '.join(active_mode_labels)} | auto SL/TP by M5 regime | build={BOT_BUILD}"
    )

    last_status_t = 0.0
    mode_runtime = {}
    for mode in MODE_LABELS:
        mode_runtime[mode] = {
            "last_time": 0,
            "last_signal": "-",
            "signal_reason": "-",
            "profile_text": f"{MODE_LABELS[mode]}: warming up",
            "entry_hint": "-",
            "buy_hint": None,
            "sell_hint": None,
            "strategy_label": "-",
            "strategies": {},
        }
        if mode == MODE_SCALP_M1_2:
            for sid in MODE2_STRATEGY_LABELS:
                mode_runtime[mode]["strategies"][sid] = {
                    "last_time": 0,
                    "last_signal": "WAIT",
                    "signal_reason": "warming up",
                    "entry_hint": "-",
                    "buy_hint": None,
                    "sell_hint": None,
                }
    last_signal = "-"
    signal_reason = "-"
    profile_text = "Auto SL/TP: warming up"
    entry_hint = "-"
    active_mode_label = ", ".join(active_mode_labels)
    last_heartbeat_t = 0.0
    last_deal_diag_t = 0.0
    last_be_manage_t = 0.0
    last_mode2_summary_t = 0.0
    worker_started_ts = int(time.time())
    def _fmt_px(v):
        return f"{float(v):.2f}" if isinstance(v, (int, float)) else "-"

    def _one_line(txt, limit=220):
        s = str(txt or "-").replace("\n", " | ")
        return s if len(s) <= limit else (s[:limit] + "…")

    try:
        while not _stop.is_set():
            now = time.time()
            enabled_modes = get_active_modes(cfg)
            active_mode_label = ", ".join(MODE_LABELS.get(m, m) for m in enabled_modes)
            if now - last_heartbeat_t >= 60.0:
                epoch_now = int(now)
                sec_to_m5_close = (300 - (epoch_now % 300)) % 300
                if sec_to_m5_close == 0:
                    sec_to_m5_close = 300
                mode_bar_state = []
                for m in enabled_modes:
                    lt = int(mode_runtime.get(m, {}).get("last_time", 0) or 0)
                    if lt > 0:
                        ttxt = datetime.utcfromtimestamp(lt).strftime("%H:%M:%S")
                    else:
                        ttxt = "warming"
                    mode_bar_state.append(f"{MODE_LABELS.get(m, m)}@{ttxt}")
                log(
                    f"[HEARTBEAT] worker alive | next M5 close in {sec_to_m5_close}s | "
                    f"modes={' ; '.join(mode_bar_state) if mode_bar_state else '-'}",
                    "info",
                )
                last_heartbeat_t = now
            if now - last_deal_diag_t >= 15.0:
                log_recent_closed_deals(cfg, lookback_hours=24, started_ts=worker_started_ts)
                last_deal_diag_t = now
            if now - last_be_manage_t >= 1.0:
                try:
                    manage_mode2_break_even(cfg)
                except Exception as be_exc:
                    log(f"[Mode 2 - BE] manager error: {be_exc}", "warn")
                last_be_manage_t = now
            cycle_signals = {}
            if MODE_LVN_1 in enabled_modes:
                sig1 = compute_mode1_lvn_signal(cfg)
                if sig1:
                    sig1["mode"] = MODE_LVN_1
                    cycle_signals[MODE_LVN_1] = sig1
            if MODE_SCALP_M1_2 in enabled_modes:
                sig2 = compute_mode2_m1_scalp_signal(cfg)
                if sig2:
                    sig2["mode"] = MODE_SCALP_M1_2
                    cycle_signals[MODE_SCALP_M1_2] = sig2
            if MODE_DCA_M5_3 in enabled_modes:
                sig3 = compute_mode3_dca_signal(cfg)
                if sig3:
                    sig3["mode"] = MODE_DCA_M5_3
                    cycle_signals[MODE_DCA_M5_3] = sig3

            preferred_mode = None
            sig1 = cycle_signals.get(MODE_LVN_1)
            sig2 = cycle_signals.get(MODE_SCALP_M1_2)
            if (
                isinstance(sig1, dict)
                and isinstance(sig2, dict)
                and sig1.get("side") in ("BUY", "SELL")
                and sig2.get("side") in ("BUY", "SELL")
            ):
                s1_stop = abs(float(sig1.get("entry", sig1.get("buy_price_hint", 0.0) or sig1.get("sell_price_hint", 0.0) or 0.0)) - float(sig1.get("sl", 0.0)))
                s2_stop = abs(float(sig2.get("entry", sig2.get("buy_price_hint", 0.0) or sig2.get("sell_price_hint", 0.0) or 0.0)) - float(sig2.get("sl", 0.0)))
                s1_tp1 = abs(float(sig1.get("tp1", sig1.get("entry", 0.0))) - float(sig1.get("entry", 0.0)))
                s2_tp1 = abs(float(sig2.get("tp1", sig2.get("entry", 0.0))) - float(sig2.get("entry", 0.0)))
                if s1_stop > 0 and s2_stop > 0 and abs(s1_stop - s2_stop) > 1e-6:
                    preferred_mode = MODE_LVN_1 if s1_stop < s2_stop else MODE_SCALP_M1_2
                elif s1_tp1 > 0 and s2_tp1 > 0 and abs(s1_tp1 - s2_tp1) > 1e-6:
                    preferred_mode = MODE_LVN_1 if s1_tp1 < s2_tp1 else MODE_SCALP_M1_2
                else:
                    # Tie-break: keep LVN unless mode1 has weak RR.
                    preferred_mode = MODE_LVN_1 if float(sig1.get("rr", 0.0)) >= 1.2 else MODE_SCALP_M1_2
            for mode in enabled_modes:
                if mode == MODE_LVN_1:
                    sig = cycle_signals.get(mode)
                    if not sig:
                        continue
                    mode_label = MODE_LABELS.get(mode, mode)
                    sig_side = sig.get("side")
                    sig_time = int(sig.get("m5_time") or 0)
                    s_reason = str(sig.get("reason", ""))
                    buy_hint = sig.get("buy_price_hint")
                    sell_hint = sig.get("sell_price_hint")
                    buy_txt = f"Giá {buy_hint:.2f} - Buy" if isinstance(buy_hint, (int, float)) else "Buy: chưa hợp lệ"
                    sell_txt = f"Giá {sell_hint:.2f} - Sell" if isinstance(sell_hint, (int, float)) else "Sell: chưa hợp lệ"
                    hint_text = f"{sell_txt} | {buy_txt}"
                    mode_runtime[mode]["last_signal"] = sig_side if sig_side in ("BUY", "SELL") else "WAIT"
                    mode_runtime[mode]["signal_reason"] = s_reason
                    mode_runtime[mode]["entry_hint"] = hint_text
                    mode_runtime[mode]["buy_hint"] = buy_hint if isinstance(buy_hint, (int, float)) else None
                    mode_runtime[mode]["sell_hint"] = sell_hint if isinstance(sell_hint, (int, float)) else None
                    sid = str(sig.get("strategy_id", "") or "")
                    if sid == "lvn_rejection":
                        mode_runtime[mode]["strategy_label"] = "LVN Rejection"
                    elif sid == "lvn_breakout_retest":
                        mode_runtime[mode]["strategy_label"] = "LVN Breakout Retest"
                    elif sid == "lvn_fast_continuation":
                        mode_runtime[mode]["strategy_label"] = "LVN Fast Continuation"
                    elif str(s_reason).startswith("NO TRADE"):
                        mode_runtime[mode]["strategy_label"] = "NO TRADE"
                    else:
                        mode_runtime[mode]["strategy_label"] = "-"
                    prof = auto_sl_tp_profile(sig)
                    mode_runtime[mode]["profile_text"] = (
                        f"{mode_label} | {prof['regime']} | SL={prof['sl_mult']:.2f}ATR | RR={prof['rr']:.2f} | "
                        f"atrRank={prof['atr_rank']:.0%} trend={prof['trend_strength']:.2f}"
                    )

                    if sig_side in ("BUY", "SELL"):
                        last_signal = f"{mode_label}: {sig_side}"
                        signal_reason = s_reason
                        profile_text = mode_runtime[mode]["profile_text"]
                        entry_hint = hint_text

                    if sig_time > 0 and sig_time != int(mode_runtime[mode]["last_time"]):
                        mode_runtime[mode]["last_time"] = sig_time
                        log(
                            f"[{mode_label}] {sig_side if sig_side in ('BUY','SELL') else 'WAIT'} | close={_fmt_px(sig.get('close'))} "
                            f"| watch_sell={_fmt_px(sell_hint)} | watch_buy={_fmt_px(buy_hint)} | reason={_one_line(s_reason)}",
                            "info",
                        )
                        positions = my_positions(cfg, mode)
                        if len(positions) < int(cfg.get("max_positions", 1)) and sig_side in ("BUY", "SELL"):
                            if preferred_mode is not None and preferred_mode != mode:
                                log(f"[{mode_label}] Skip open {sig_side}: ưu tiên {MODE_LABELS.get(preferred_mode, preferred_mode)} cùng thanh M5", "info")
                                continue
                            ok, reason = open_trade(cfg, sig_side, sig)
                            if not ok:
                                log(f"[{mode_label}] Skip open {sig_side}: {reason}", "warn")
                        else:
                            if sig_side in ("BUY", "SELL"):
                                log(f"[{mode_label}] Signal {sig_side} but max_positions reached ({len(positions)})", "info")
                elif mode == MODE_SCALP_M1_2:
                    sig = cycle_signals.get(mode)
                    if not sig:
                        continue
                    mode_label = MODE_LABELS.get(mode, mode)
                    sig_time = int(sig.get("m5_time") or 0)
                    if sig_time > 0 and sig_time != int(mode_runtime[mode]["last_time"]):
                        mode_runtime[mode]["last_time"] = sig_time
                        ctx = sig.get("market_ctx", {}) if isinstance(sig.get("market_ctx"), dict) else {}
                        trend_txt = "FLAT"
                        if bool(ctx.get("trend_buy", False)):
                            trend_txt = "BUY"
                        elif bool(ctx.get("trend_sell", False)):
                            trend_txt = "SELL"
                        session_txt = "London/NY" if bool(ctx.get("in_london_ny", False)) else "Off-session"
                        impulse_txt = "UP" if bool(ctx.get("up_impulse", False)) else ("DOWN" if bool(ctx.get("down_impulse", False)) else "-")
                        log(
                            f"[{mode_label}/Market] close={_fmt_px(sig.get('close'))} | trend={trend_txt} "
                            f"| session={session_txt} | impulse={impulse_txt} | atrRank={float(sig.get('atr_rank', 0.5))*100:.0f}% "
                            f"| range12={float(ctx.get('range12_atr', 0.0)):.2f}ATR | range40={float(ctx.get('range40_atr', 0.0)):.2f}ATR "
                            f"| vol={float(ctx.get('vol_ratio', 1.0)):.2f}x | rsi={float(ctx.get('rsi', 50.0)):.1f}",
                            "info",
                        )
                        diag = update_mode2_market_diag(sig)
                        if now - last_mode2_summary_t >= 1800.0:
                            summary = format_mode2_market_summary(diag)
                            log(f"[{mode_label}/MarketSummary] {summary}", "info")
                            last_mode2_summary_t = now
                    buy_hint = sig.get("buy_price_hint")
                    sell_hint = sig.get("sell_price_hint")
                    buy_txt = f"Giá {buy_hint:.2f} - Buy" if isinstance(buy_hint, (int, float)) else "Buy: chưa hợp lệ"
                    sell_txt = f"Giá {sell_hint:.2f} - Sell" if isinstance(sell_hint, (int, float)) else "Sell: chưa hợp lệ"
                    hint_text = f"{sell_txt} | {buy_txt}"
                    prof = auto_sl_tp_profile(sig)
                    mode_runtime[mode]["profile_text"] = (
                        f"{mode_label} | {prof['regime']} | SL={prof['sl_mult']:.2f}ATR | RR={prof['rr']:.2f} | "
                        f"atrRank={prof['atr_rank']:.0%} trend={prof['trend_strength']:.2f}"
                    )

                    mode2_cfg = cfg.get("modes", {}).get(mode, {})
                    strat_cfg = mode2_cfg.get("strategies", {}) if isinstance(mode2_cfg, dict) else {}
                    enabled_strats = [sid for sid in MODE2_STRATEGY_LABELS if bool(strat_cfg.get(sid, True))]
                    if not enabled_strats:
                        enabled_strats = list(MODE2_STRATEGY_LABELS.keys())
                    open_sides = mode_open_sides(cfg, mode)

                    candidate_map = {}
                    for c in sig.get("candidates", []) or []:
                        sid = str(c.get("sid", ""))
                        if sid:
                            candidate_map[sid] = c
                    status_map = sig.get("strategy_status", {}) if isinstance(sig.get("strategy_status", {}), dict) else {}
                    ranked = sig.get("candidates", []) or []
                    selected_sid = str(ranked[0].get("sid", "")) if ranked else ""
                    selected_side = str(ranked[0].get("side", "")) if ranked else ""

                    active_signals = []
                    for sid in enabled_strats:
                        srt = mode_runtime[mode]["strategies"].setdefault(
                            sid,
                            {
                                "last_time": 0,
                                "last_signal": "WAIT",
                                "signal_reason": "warming up",
                                "entry_hint": "-",
                                "buy_hint": None,
                                "sell_hint": None,
                            },
                        )
                        cand = candidate_map.get(sid)
                        stat = status_map.get(sid, {}) if isinstance(status_map, dict) else {}
                        s_side = str(cand.get("side")) if cand else str(stat.get("state", "WAIT"))
                        s_reason = str(cand.get("reason")) if cand else str(stat.get("reason", "no-setup"))
                        srt["last_signal"] = s_side if s_side in ("BUY", "SELL") else "WAIT"
                        srt["signal_reason"] = s_reason
                        cbuy = cand.get("buy_hint") if cand else stat.get("buy_hint")
                        csell = cand.get("sell_hint") if cand else stat.get("sell_hint")
                        if isinstance(cbuy, (int, float)) or isinstance(csell, (int, float)):
                            c_buy_txt = f"Giá {cbuy:.2f} - Buy" if isinstance(cbuy, (int, float)) else "Buy: -"
                            c_sell_txt = f"Giá {csell:.2f} - Sell" if isinstance(csell, (int, float)) else "Sell: -"
                            srt["entry_hint"] = f"{c_sell_txt} | {c_buy_txt}"
                        else:
                            srt["entry_hint"] = "-"
                        srt["buy_hint"] = cbuy if isinstance(cbuy, (int, float)) else None
                        srt["sell_hint"] = csell if isinstance(csell, (int, float)) else None
                        if sig_time > 0 and sig_time != int(srt.get("last_diag_time", 0)):
                            srt["last_diag_time"] = sig_time
                            log(
                                f"[{mode_label}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] "
                                f"{s_side if s_side in ('BUY','SELL') else 'WAIT'} | close={_fmt_px(sig.get('close'))} "
                                f"| watch_sell={_fmt_px(srt.get('sell_hint'))} | watch_buy={_fmt_px(srt.get('buy_hint'))} "
                                f"| reason={_one_line(s_reason)}",
                                "info",
                            )

                        if s_side in ("BUY", "SELL"):
                            if sid != selected_sid:
                                srt["last_signal"] = "WAIT"
                                srt["signal_reason"] = "NO TRADE | ưu tiên chiến lược khác có xác suất cao hơn"
                                continue
                            if open_sides and selected_side and selected_side not in open_sides:
                                srt["last_signal"] = "WAIT"
                                srt["signal_reason"] = f"blocked opposite: mode2 lock {','.join(sorted(open_sides))}"
                                if sig_time > 0:
                                    srt["last_time"] = sig_time
                                block_key = f"{selected_side}|{','.join(sorted(open_sides))}"
                                should_log_block = (
                                    sig_time > 0
                                    and (
                                        int(srt.get("last_block_time", 0)) != int(sig_time)
                                        or srt.get("last_block_key") != block_key
                                    )
                                )
                                if should_log_block:
                                    srt["last_block_time"] = int(sig_time)
                                    srt["last_block_key"] = block_key
                                    log(
                                        f"[{mode_label}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] Block {selected_side}: direction lock {','.join(sorted(open_sides))}",
                                        "info",
                                    )
                                continue
                            active_signals.append(f"{MODE2_STRATEGY_LABELS.get(sid, sid)}:{s_side}")
                            if sig_time > 0 and sig_time != int(srt.get("last_time", 0)):
                                srt["last_time"] = sig_time
                                current_open = count_strategy_positions(cfg, mode, sid)
                                if current_open < 1:
                                    if preferred_mode is not None and preferred_mode != mode:
                                        srt["last_signal"] = "WAIT"
                                        srt["signal_reason"] = f"NO TRADE | ưu tiên {MODE_LABELS.get(preferred_mode, preferred_mode)} cùng thanh M5"
                                        continue
                                    lock_minutes = MODE2_MEAN_REV_LOCK_MINUTES if sid == "mean_reversion" else MODE2_SETUP_LOCK_MINUTES
                                    min_losses = 1 if sid == "mean_reversion" else 2
                                    locked, loss_streak, remaining_sec = is_setup_temporarily_locked(
                                        cfg,
                                        mode,
                                        sid,
                                        lock_minutes=lock_minutes,
                                        min_losses=min_losses,
                                    )
                                    if locked:
                                        rem_min = max(1, int(np.ceil(float(remaining_sec) / 60.0)))
                                        srt["last_signal"] = "WAIT"
                                        srt["signal_reason"] = (
                                            f"NO TRADE | setup lock {lock_minutes}m sau {loss_streak} lệnh thua liên tiếp (còn ~{rem_min}m)"
                                        )
                                        continue
                                    s_sig = dict(sig)
                                    s_sig["strategy_id"] = sid
                                    s_sig["side"] = s_side
                                    s_sig["reason"] = f"{MODE2_STRATEGY_LABELS.get(sid, sid)} | {s_reason}"
                                    if isinstance(cand, dict):
                                        for k in ("entry", "sl", "tp1", "tp2", "rr", "confidence", "cancel_rule"):
                                            if k in cand:
                                                s_sig[k] = cand[k]
                                        if "tp2" in cand:
                                            s_sig["tp"] = cand["tp2"]
                                    ok, reason = open_trade(cfg, s_side, s_sig)
                                    if not ok:
                                        log(
                                            f"[{mode_label}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] Skip open {s_side}: {reason}",
                                            "warn",
                                        )
                                    else:
                                        open_sides.add(s_side)
                                else:
                                    log(
                                        f"[{mode_label}/{MODE2_STRATEGY_LABELS.get(sid, sid)}] Signal {s_side} but strategy max 1 reached",
                                        "info",
                                    )

                    mode_runtime[mode]["buy_hint"] = buy_hint if isinstance(buy_hint, (int, float)) else None
                    mode_runtime[mode]["sell_hint"] = sell_hint if isinstance(sell_hint, (int, float)) else None
                    mode_runtime[mode]["entry_hint"] = hint_text
                    if active_signals:
                        mode_runtime[mode]["last_signal"] = " | ".join(active_signals)
                        mode_runtime[mode]["signal_reason"] = str((candidate_map.get(selected_sid) or {}).get("reason", sig.get("reason", "-")))
                        last_signal = f"{mode_label}: {mode_runtime[mode]['last_signal']}"
                        signal_reason = mode_runtime[mode]["signal_reason"]
                        profile_text = mode_runtime[mode]["profile_text"]
                        entry_hint = hint_text
                    else:
                        mode_runtime[mode]["last_signal"] = "WAIT"
                        mode_runtime[mode]["signal_reason"] = str(sig.get("reason", "no-setup"))
                        mode_runtime[mode]["entry_hint"] = "Sell: - | Buy: -"
                        mode_runtime[mode]["buy_hint"] = None
                        mode_runtime[mode]["sell_hint"] = None
                elif mode == MODE_DCA_M5_3:
                    sig = cycle_signals.get(mode)
                    if not sig:
                        continue
                    mode_label = MODE_LABELS.get(mode, mode)
                    sig_side = sig.get("side")
                    sig_time = int(sig.get("m5_time") or 0)
                    s_reason = str(sig.get("reason", ""))
                    dca_action = str(sig.get("dca_action", "hold"))
                    buy_hint = sig.get("buy_price_hint")
                    sell_hint = sig.get("sell_price_hint")
                    buy_txt = f"Giá {buy_hint:.2f} - Buy" if isinstance(buy_hint, (int, float)) else "Buy: -"
                    sell_txt = f"Giá {sell_hint:.2f} - Sell" if isinstance(sell_hint, (int, float)) else "Sell: -"
                    hint_text = f"{sell_txt} | {buy_txt}"
                    mode_runtime[mode]["last_signal"] = sig_side if sig_side in ("BUY", "SELL") else "WAIT"
                    mode_runtime[mode]["signal_reason"] = s_reason
                    mode_runtime[mode]["entry_hint"] = hint_text
                    mode_runtime[mode]["buy_hint"] = buy_hint if isinstance(buy_hint, (int, float)) else None
                    mode_runtime[mode]["sell_hint"] = sell_hint if isinstance(sell_hint, (int, float)) else None
                    mode_runtime[mode]["strategy_label"] = "DCA M5 Core"
                    layers = int(sig.get("dca_layers", 0) or 0)
                    max_layers = int(sig.get("dca_max_layers", 0) or 0)
                    floating = float(sig.get("dca_floating", 0.0) or 0.0)
                    next_add = sig.get("dca_next_add")
                    mode_runtime[mode]["profile_text"] = (
                        f"{mode_label} | layers={layers}/{max_layers} | floating={floating:+.2f} | "
                        f"next_add={_fmt_px(next_add)} | basketTP={float(sig.get('dca_basket_tp_money', 0.0)):+.2f} "
                        f"basketSL={float(sig.get('dca_basket_sl_money', 0.0)):+.2f}"
                    )

                    if sig_time > 0 and sig_time != int(mode_runtime[mode]["last_diag_time"] if "last_diag_time" in mode_runtime[mode] else 0):
                        mode_runtime[mode]["last_diag_time"] = sig_time
                        log(
                            f"[{mode_label}] {sig_side if sig_side in ('BUY','SELL') else 'WAIT'} "
                            f"| action={dca_action} | layers={layers}/{max_layers} | close={_fmt_px(sig.get('close'))} "
                            f"| watch_sell={_fmt_px(sell_hint)} | watch_buy={_fmt_px(buy_hint)} | reason={_one_line(s_reason)}",
                            "info",
                        )

                    if dca_action == "close_all":
                        if now - float(mode_runtime[mode].get("last_close_try_t", 0.0) or 0.0) >= 8.0:
                            ok, msg = close_mode_positions(cfg, mode, reason=str(sig.get("close_reason", "mode3-close")))
                            mode_runtime[mode]["last_close_try_t"] = now
                            if ok:
                                log(f"[{mode_label}] CLOSE-ALL executed | {msg} | reason={sig.get('close_reason', '-')}", "warn")
                            else:
                                log(f"[{mode_label}] CLOSE-ALL failed | {msg}", "warn")
                    elif sig_side in ("BUY", "SELL"):
                        if sig_time > 0 and sig_time != int(mode_runtime[mode]["last_time"]):
                            mode_runtime[mode]["last_time"] = sig_time
                            current_layers = len(my_positions(cfg, mode))
                            if current_layers >= int(cfg.get("mode3_dca_max_layers", MODE3_DCA_MAX_LAYERS)):
                                log(f"[{mode_label}] Signal {sig_side} but max DCA layers reached ({current_layers})", "info")
                            else:
                                s_sig = dict(sig)
                                s_sig["strategy_id"] = "dca_m5"
                                ok, reason = open_trade(cfg, sig_side, s_sig)
                                if not ok:
                                    log(f"[{mode_label}] Skip open {sig_side}: {reason}", "warn")

                    if sig_side in ("BUY", "SELL"):
                        last_signal = f"{mode_label}: {sig_side}"
                        signal_reason = s_reason
                        profile_text = mode_runtime[mode]["profile_text"]
                        entry_hint = hint_text

            # Always expose signal state per enabled mode (even WAIT), so GUI
            # never looks blank while waiting for setups.
            if enabled_modes:
                state_parts = []
                reason_parts = []
                hint_parts = []
                for m in enabled_modes:
                    label = MODE_LABELS.get(m, m)
                    m_sig = str(mode_runtime.get(m, {}).get("last_signal", "-"))
                    m_reason = str(mode_runtime.get(m, {}).get("signal_reason", "-"))
                    m_hint = str(mode_runtime.get(m, {}).get("entry_hint", "-"))
                    state_parts.append(f"{label}: {m_sig}")
                    reason_parts.append(f"{label}: {m_reason}")
                    hint_parts.append(f"{label}: {m_hint}")
                last_signal = " | ".join(state_parts)
                signal_reason = " | ".join(reason_parts)
                entry_hint = " || ".join(hint_parts)

            if now - last_status_t >= 1.0:
                push_status(
                    cfg,
                    last_signal,
                    signal_reason,
                    profile_text=profile_text,
                    entry_hint=entry_hint,
                    active_mode_label=active_mode_label,
                    mode_runtime=mode_runtime,
                )
                last_status_t = now

            time.sleep(0.2)
    except Exception as exc:
        log(f"Worker exception: {exc}\n{traceback.format_exc()}", "error")
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
        send({"type": "exit", "reason": "normal"})


class WorkerHandle:
    def __init__(self, cfg, event_queue):
        self._q = event_queue
        self._proc = None
        self._start(cfg)

    def _start(self, cfg):
        cfg_json = json.dumps(cfg, ensure_ascii=False)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--worker", cfg_json]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--worker", cfg_json]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            creationflags=flags,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    obj["_type"] = obj.get("type", "")
                    self._q.put_nowait(obj)
                except json.JSONDecodeError:
                    self._q.put_nowait(
                        {
                            "_type": "log",
                            "level": "error",
                            "msg": line,
                            "ts": datetime.now().strftime("%H:%M:%S"),
                        }
                    )
        except Exception:
            pass

    def is_alive(self):
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
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
            try:
                self._proc.kill()
            except Exception:
                pass


CFG_FILE = Path(__file__).resolve().with_name("eagoldsuper_config.json")


def load_cfg():
    if CFG_FILE.exists():
        try:
            return json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cfg(cfg):
    try:
        CFG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


QSS = """
QWidget { background:#060b16; color:#e7eefc; font-family:'Segoe UI'; font-size:12px; }
QFrame#topbar {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #0e1d39, stop:1 #11243b);
    border:1px solid #2a4169; border-radius:14px;
}
QFrame#sidebar, QFrame#mainpanel {
    background:#0c1528; border:1px solid #24395d; border-radius:12px;
}
QFrame#sectionCard {
    background:#0a1222; border:1px solid #1f3356; border-radius:10px;
}
QFrame#metricCard {
    background:#0a1222; border:1px solid #1f3356; border-radius:10px;
}
QFrame#signalCard {
    background:#0d1a31; border:1px solid #25406a; border-radius:10px;
}
QLabel#title { font-size:21px; font-weight:800; color:#f8fbff; }
QLabel#sub { color:#90afd8; font-size:11px; }
QLabel#sectionTitle { color:#67b2ff; font-size:11px; font-weight:800; letter-spacing:0.7px; }
QLabel#caption { color:#7e93b5; font-size:10px; font-weight:700; }
QLabel#metric { color:#f6faff; font-size:17px; font-weight:800; font-family:'Consolas'; }
QLabel#metricWeak { color:#b8cae8; font-size:13px; font-weight:700; font-family:'Consolas'; }
QLineEdit, QDoubleSpinBox {
    background:#0a1222; border:1px solid #314b74; border-radius:8px; padding:7px 10px; color:#f6fbff;
}
QLineEdit:focus, QDoubleSpinBox:focus { border:1px solid #6ab6ff; }
QPushButton {
    border-radius:8px; padding:8px 14px; font-weight:700; border:1px solid #36537d; background:#152640; color:#dce9ff;
}
QPushButton#start { background:#18a767; border:none; color:#031e13; }
QPushButton#stop { background:#d23f54; border:none; color:#fff; }
QPushButton#save { background:#2f7ef0; border:none; color:#fff; }
QPushButton:hover { border-color:#6ab6ff; }
QTabWidget::pane { border:1px solid #24395d; border-radius:10px; background:#0a1222; }
QTabBar::tab {
    background:#101b31; border:1px solid #24395d; border-bottom:none; border-top-left-radius:8px; border-top-right-radius:8px;
    padding:7px 12px; margin-right:4px; color:#adc4e9; font-weight:700;
}
QTabBar::tab:selected { background:#163058; color:#f1f7ff; }
QPlainTextEdit { background:#060f1e; border:1px solid #223a5e; border-radius:8px; padding:6px; }
QTableWidget { background:#060f1e; border:1px solid #223a5e; border-radius:8px; gridline-color:#192f4f; }
QHeaderView::section { background:#132644; color:#d6e6ff; border:none; padding:7px; font-weight:700; }
"""


class LVNWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EAGoldSuper - MT5 Multi-Mode")
        self.resize(1180, 760)
        self.worker = None
        self.event_q = queue.Queue()
        self.state = {}
        self.cfg = {
            "symbol": "XAUUSDc",
            "magic": 700100,
            "active_modes": list(DEFAULT_ACTIVE_MODES),
            "modes": {m: {"enabled": (m in DEFAULT_ACTIVE_MODES)} for m in MODE_LABELS},
            "risk_pct": 0.5,
            "max_positions": 1,
            "lvn_window": 96,
            "lvn_bins": 32,
            "lvn_count": 8,
            "mode1_vp_lookback": 36,
            "mode1_lvn_max_dist_atr": 0.9,
            "mode1_lvn_short_window": 18,
            "mode1_lvn_short_bins": 24,
            "mode1_lvn_short_count": 4,
            "touch_atr": 0.30,
            "ema_fast": 20,
            "ema_slow": 60,
            "atr_regime_window": 288,
            "deviation": 25,
        }
        self.cfg.update(load_cfg())
        normalize_mode_settings(self.cfg)
        self._build_ui()
        self._apply_cfg_to_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(200)

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        header = QtWidgets.QFrame()
        header.setObjectName("topbar")
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(16, 12, 16, 12)
        h.setSpacing(10)
        title = QtWidgets.QLabel("EAGoldSuper")
        title.setObjectName("title")
        subtitle = QtWidgets.QLabel("Multi-mode trading engine · Mode 1 LVN Profile + Mode 2 selector")
        subtitle.setObjectName("sub")
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(2)
        left.addWidget(title)
        left.addWidget(subtitle)
        h.addLayout(left)
        h.addStretch(1)
        self.lb_runtime_state = QtWidgets.QLabel("OFFLINE")
        self.lb_runtime_state.setObjectName("metricWeak")
        h.addWidget(self.lb_runtime_state)
        self.b_save = QtWidgets.QPushButton("Lưu cấu hình")
        self.b_save.setObjectName("save")
        self.b_start = QtWidgets.QPushButton("Start")
        self.b_start.setObjectName("start")
        self.b_stop = QtWidgets.QPushButton("Stop")
        self.b_stop.setObjectName("stop")
        h.addWidget(self.b_save)
        h.addWidget(self.b_start)
        h.addWidget(self.b_stop)
        layout.addWidget(header)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(10)
        layout.addLayout(body, 1)

        left_panel = QtWidgets.QFrame()
        left_panel.setObjectName("sidebar")
        left_panel.setMinimumWidth(350)
        left_panel.setMaximumWidth(390)
        left_wrap = QtWidgets.QVBoxLayout(left_panel)
        left_wrap.setContentsMargins(12, 12, 12, 12)
        left_wrap.setSpacing(10)

        conn_card = QtWidgets.QFrame()
        conn_card.setObjectName("sectionCard")
        conn_layout = QtWidgets.QFormLayout(conn_card)
        conn_layout.setContentsMargins(10, 10, 10, 10)
        conn_layout.setSpacing(8)
        conn_title = QtWidgets.QLabel("KẾT NỐI MT5")
        conn_title.setObjectName("sectionTitle")
        conn_layout.addRow(conn_title)

        self.ed_login = QtWidgets.QLineEdit()
        self.ed_password = QtWidgets.QLineEdit()
        self.ed_password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.ed_server = QtWidgets.QLineEdit()
        self.ed_path = QtWidgets.QLineEdit()
        self.lb_symbol_fixed = QtWidgets.QLabel("XAUUSDc (fixed)")
        self.lb_symbol_fixed.setObjectName("metricWeak")
        self.lb_strategy = QtWidgets.QLabel("ACTIVE: Mode 1 - LVN Profile Pro | Max position = 1")
        self.lb_strategy.setObjectName("sub")
        self.sp_risk = QtWidgets.QDoubleSpinBox()
        self.sp_risk.setRange(0.01, 10.0)
        self.sp_risk.setSingleStep(0.1)
        self.sp_risk.setDecimals(2)
        self.sp_risk.setSuffix(" %")
        self.lb_auto_profile = QtWidgets.QLabel("Auto profile: warming up...")
        self.lb_auto_profile.setObjectName("sub")

        conn_layout.addRow("MT5 login", self.ed_login)
        conn_layout.addRow("MT5 password", self.ed_password)
        conn_layout.addRow("MT5 server", self.ed_server)
        conn_layout.addRow("Terminal path", self.ed_path)
        conn_layout.addRow("Symbol", self.lb_symbol_fixed)
        left_wrap.addWidget(conn_card)

        risk_card = QtWidgets.QFrame()
        risk_card.setObjectName("sectionCard")
        risk_layout = QtWidgets.QFormLayout(risk_card)
        risk_layout.setContentsMargins(10, 10, 10, 10)
        risk_layout.setSpacing(8)
        risk_title = QtWidgets.QLabel("RỦI RO")
        risk_title.setObjectName("sectionTitle")
        risk_layout.addRow(risk_title)
        risk_layout.addRow("Risk % / lệnh", self.sp_risk)
        risk_layout.addRow("Engine", self.lb_strategy)
        risk_layout.addRow("Auto SL/TP", self.lb_auto_profile)
        self.mode_widgets = {}
        for mode, label in MODE_LABELS.items():
            box = QtWidgets.QHBoxLayout()
            cb = QtWidgets.QCheckBox(label)
            pnl = QtWidgets.QLabel("PnL today: 0.00")
            pnl.setObjectName("sub")
            box.addWidget(cb, 1)
            box.addWidget(pnl, 0, Qt.AlignmentFlag.AlignRight)
            wrap = QtWidgets.QWidget()
            wrap.setLayout(box)
            risk_layout.addRow(wrap)
            self.mode_widgets[mode] = {"checkbox": cb, "pnl": pnl}
        left_wrap.addWidget(risk_card)
        left_wrap.addStretch(1)
        body.addWidget(left_panel, 0)

        right_panel = QtWidgets.QFrame()
        right_panel.setObjectName("mainpanel")
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(10)

        metric_row = QtWidgets.QHBoxLayout()
        metric_row.setSpacing(8)

        def make_metric_card(caption, initial):
            card = QtWidgets.QFrame()
            card.setObjectName("metricCard")
            lay = QtWidgets.QVBoxLayout(card)
            lay.setContentsMargins(10, 8, 10, 8)
            cap = QtWidgets.QLabel(caption)
            cap.setObjectName("caption")
            val = QtWidgets.QLabel(initial)
            val.setObjectName("metric")
            lay.addWidget(cap)
            lay.addWidget(val)
            return card, val

        c1, self.lb_balance = make_metric_card("BALANCE", "-")
        c2, self.lb_equity = make_metric_card("EQUITY", "-")
        c3, self.lb_float = make_metric_card("FLOATING", "-")
        c4, self.lb_positions = make_metric_card("OPEN POSITIONS", "0")
        for c in [c1, c2, c3, c4]:
            metric_row.addWidget(c, 1)
        right_layout.addLayout(metric_row)

        signal_card = QtWidgets.QFrame()
        signal_card.setObjectName("signalCard")
        signal_l = QtWidgets.QVBoxLayout(signal_card)
        signal_l.setContentsMargins(10, 8, 10, 8)
        sig_cap = QtWidgets.QLabel("SIGNAL")
        sig_cap.setObjectName("caption")
        self.lb_signal = QtWidgets.QLabel("Đang chờ dữ liệu...")
        self.lb_signal.setObjectName("metricWeak")
        self.signal_tbl = QtWidgets.QTableWidget(0, 7)
        self.signal_tbl.setHorizontalHeaderLabels(["Mode", "Strategy", "State", "Decision", "Sell", "Buy", "Reason"])
        self.signal_tbl.verticalHeader().setVisible(False)
        self.signal_tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.signal_tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.signal_tbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.signal_tbl.setAlternatingRowColors(False)
        self.signal_tbl.horizontalHeader().setStretchLastSection(True)
        self.signal_tbl.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.signal_tbl.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.signal_tbl.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.signal_tbl.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.signal_tbl.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.signal_tbl.horizontalHeader().setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.signal_tbl.setColumnWidth(0, 190)
        self.signal_tbl.setColumnWidth(1, 185)
        self.signal_tbl.setColumnWidth(2, 85)
        self.signal_tbl.setColumnWidth(3, 95)
        self.signal_tbl.setColumnWidth(4, 100)
        self.signal_tbl.setColumnWidth(5, 100)
        signal_l.addWidget(sig_cap)
        signal_l.addWidget(self.lb_signal)
        signal_l.addWidget(self.signal_tbl)
        right_layout.addWidget(signal_card)

        tabs = QtWidgets.QTabWidget()
        pos_tab = QtWidgets.QWidget()
        pos_l = QtWidgets.QVBoxLayout(pos_tab)
        pos_l.setContentsMargins(6, 6, 6, 6)

        self.tbl = QtWidgets.QTableWidget(0, 6)
        self.tbl.setHorizontalHeaderLabels(["Ticket", "Side", "Lot", "Open", "P/L", "SL/TP"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        pos_l.addWidget(self.tbl)
        tabs.addTab(pos_tab, "Positions")

        log_tab = QtWidgets.QWidget()
        log_l = QtWidgets.QVBoxLayout(log_tab)
        log_l.setContentsMargins(6, 6, 6, 6)
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.document().setMaximumBlockCount(1200)
        log_l.addWidget(self.log_box)
        tabs.addTab(log_tab, "Event Log")
        right_layout.addWidget(tabs, 1)

        body.addWidget(right_panel, 1)

        self.b_save.clicked.connect(self._save_cfg)
        self.b_start.clicked.connect(self._start)
        self.b_stop.clicked.connect(self._stop)

    def _collect_cfg(self):
        mode_cfg = {}
        for mode in MODE_LABELS:
            w = self.mode_widgets.get(mode, {})
            enabled = bool(w.get("checkbox").isChecked()) if w else (mode in DEFAULT_ACTIVE_MODES)
            mode_cfg[mode] = {"enabled": enabled}
        active_modes = [m for m in MODE_LABELS if mode_cfg[m]["enabled"]]
        if not active_modes:
            # Always keep at least one mode active for user safety.
            first = list(MODE_LABELS.keys())[0]
            mode_cfg[first]["enabled"] = True
            active_modes = [first]
        return {
            "login": self.ed_login.text().strip(),
            "password": self.ed_password.text().strip(),
            "server": self.ed_server.text().strip(),
            "path": self.ed_path.text().strip(),
            "symbol": "XAUUSDc",
            "magic": int(self.cfg.get("magic", 700100)),
            "modes": mode_cfg,
            "active_modes": active_modes,
            "risk_pct": float(self.sp_risk.value()),
        }

    def _apply_cfg_to_ui(self):
        c = self.cfg
        self.ed_login.setText(str(c.get("login", "")))
        self.ed_password.setText(str(c.get("password", "")))
        self.ed_server.setText(str(c.get("server", "")))
        self.ed_path.setText(str(c.get("path", "")))
        self.sp_risk.setValue(float(c.get("risk_pct", 0.5)))
        mode_labels = [MODE_LABELS.get(m, m) for m in get_active_modes(c)]
        self.lb_strategy.setText(f"ACTIVE: {', '.join(mode_labels)} | Max position = 1")
        for mode, w in self.mode_widgets.items():
            enabled = bool(c.get("modes", {}).get(mode, {}).get("enabled", mode in DEFAULT_ACTIVE_MODES))
            w["checkbox"].setChecked(enabled)
            w["pnl"].setText("PnL today: 0.00")

    def _save_cfg(self):
        merged = dict(self.cfg)
        merged.update(self._collect_cfg())
        self.cfg = normalize_mode_settings(merged)
        save_cfg(self.cfg)
        self._append_log("Config saved", "info")

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        self._save_cfg()
        self.worker = WorkerHandle(self.cfg, self.event_q)
        self.lb_runtime_state.setText("STARTING...")
        self._append_log("Worker started", "info")

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
            self.lb_runtime_state.setText("OFFLINE")
            self._append_log("Worker stop requested", "warn")

    def _append_log(self, msg, level="info", ts=None):
        prefix = {"error": "ERR", "warn": "WARN"}.get(level, "INFO")
        now = ts or datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText(f"[{now}] [{prefix}] {msg}")

    def _apply_status(self, obj):
        self.state = obj
        bal = float(obj.get("balance", 0.0))
        eq = float(obj.get("equity", 0.0))
        fl = float(obj.get("floating", 0.0))
        cur = str(obj.get("currency", ""))
        self.lb_balance.setText(f"{bal:,.2f} {cur}".strip())
        self.lb_equity.setText(f"{eq:,.2f} {cur}".strip())
        self.lb_float.setText(f"{fl:+,.2f} {cur}".strip())
        self.lb_positions.setText(str(int(obj.get("open_positions", 0))))
        self.lb_signal.setText(f"Signal realtime theo mode (M5 entry | M15/H1 confirm | closed bars) | Active: {obj.get('active_mode', '-')}")
        self.lb_auto_profile.setText(f"Auto profile: {obj.get('profile_text', '-')}")
        self.lb_strategy.setText(f"ACTIVE: {obj.get('active_mode', 'Mode 1 - LVN Profile Pro')} | Max position = 1")
        self.lb_runtime_state.setText("ONLINE")
        signal_rows = obj.get("signal_rows", []) or []
        if not signal_rows:
            for m in obj.get("mode_stats", []) or []:
                signal_rows.append(
                    {
                        "mode_label": str(m.get("label", m.get("id", "-"))),
                        "strategy_label": "-",
                        "state": str(m.get("last_signal", "-")),
                        "decision": "TRADE" if str(m.get("last_signal", "-")) in ("BUY", "SELL") else "NO TRADE",
                        "reason": str(m.get("signal_reason", "-")),
                        "buy_hint": m.get("buy_hint"),
                        "sell_hint": m.get("sell_hint"),
                    }
                )

        self.signal_tbl.setRowCount(len(signal_rows))
        for r, m in enumerate(signal_rows):
            state = str(m.get("state", "-"))
            if state == "BUY":
                state_color = "#22c55e"
            elif state == "SELL":
                state_color = "#ef4444"
            else:
                state_color = "#a5b4cf"
            decision = str(m.get("decision", "NO TRADE"))
            decision_color = "#22c55e" if decision == "TRADE" else "#94a3b8"
            buy_hint = m.get("buy_hint")
            sell_hint = m.get("sell_hint")
            # Always show computed watch levels when available, even in WAIT/NO TRADE.
            buy_txt = f"{float(buy_hint):.2f}" if isinstance(buy_hint, (int, float)) else "-"
            sell_txt = f"{float(sell_hint):.2f}" if isinstance(sell_hint, (int, float)) else "-"
            reason = str(m.get("reason", "-"))
            vals = [str(m.get("mode_label", "-")), str(m.get("strategy_label", "-")), state, decision, sell_txt, buy_txt, reason]
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if c in (4, 5):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                elif c in (2, 3):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # Keep table readable in all themes/states.
                bg = "#0a1324" if (r % 2 == 0) else "#0c1730"
                it.setBackground(QtGui.QColor(bg))
                if c == 2:
                    it.setForeground(QtGui.QColor(state_color))
                elif c == 3:
                    it.setForeground(QtGui.QColor(decision_color))
                else:
                    it.setForeground(QtGui.QColor("#dbe8ff"))
                self.signal_tbl.setItem(r, c, it)
            self.signal_tbl.setRowHeight(r, 28)
        for m in obj.get("mode_stats", []) or []:
            mode = str(m.get("id", ""))
            if mode in self.mode_widgets:
                self.mode_widgets[mode]["pnl"].setText(
                    f"PnL today: {float(m.get('pnl_today', 0.0)):+.2f} | Open: {int(m.get('open_positions', 0))}"
                )

        positions = obj.get("positions", []) or []
        self.tbl.setRowCount(len(positions))
        for r, p in enumerate(positions):
            vals = [
                str(p.get("ticket", "")),
                str(p.get("side", "")),
                f"{float(p.get('lot', 0.0)):.2f}",
                f"{float(p.get('open_price', 0.0)):.2f}",
                f"{float(p.get('profit', 0.0)):+.2f}",
                f"{float(p.get('sl', 0.0)):.2f} / {float(p.get('tp', 0.0)):.2f}",
            ]
            for c, v in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(v)
                if c in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.tbl.setItem(r, c, item)

    def _poll(self):
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.lb_runtime_state.setText("OFFLINE")
            self._append_log("Worker exited", "warn")
        for _ in range(300):
            try:
                obj = self.event_q.get_nowait()
            except queue.Empty:
                break
            kind = obj.get("_type")
            if kind == "log":
                self._append_log(obj.get("msg", ""), obj.get("level", "info"), obj.get("ts"))
            elif kind == "status":
                self._apply_status(obj)
            elif kind == "trade":
                side = obj.get("side", "-")
                lot = float(obj.get("lot", 0.0))
                price = float(obj.get("price", 0.0))
                self._append_log(
                    f"Trade open {side} lot={lot:.2f} @ {price:.2f} | ticket={obj.get('ticket', '')}",
                    "info",
                    obj.get("ts"),
                )
            elif kind == "exit":
                self._append_log(f"Worker exit: {obj.get('reason', 'normal')}", "warn")

    def closeEvent(self, event):
        self._save_cfg()
        if self.worker:
            self.worker.stop()
        event.accept()


def _worker_load_cfg():
    try:
        return json.loads(sys.argv[2])
    except Exception:
        return {}


def _gui_main():
    if not _HAS_GUI:
        print("Missing PyQt6. Install: pip install PyQt6")
        sys.exit(1)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(QSS)
    try:
        app.setFont(QtGui.QFont("Segoe UI", 10))
    except Exception:
        pass
    win = LVNWindow()
    win.show()

    def _exc(et, ev, etb):
        msg = "".join(traceback.format_exception(et, ev, etb))
        try:
            QtWidgets.QMessageBox.critical(None, "Unhandled Error", msg[:1000])
        except Exception:
            pass

    sys.excepthook = _exc
    sys.exit(app.exec())


if __name__ == "__main__":
    if _WORKER_MODE:
        run_worker(_worker_load_cfg())
    else:
        _gui_main()

