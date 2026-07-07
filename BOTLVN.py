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
from datetime import datetime
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

MODE_LVN_1 = "mode1_lvn_adaptive"
MODE_SCALP_M1_2 = "mode2_m1_pullback"
MODE_LABELS = {
    MODE_LVN_1: "Mode 1 - LVN Adaptive",
    MODE_SCALP_M1_2: "Mode 2 - M1 Scalp Pullback",
}
MODE_MAGIC_OFFSETS = {
    MODE_LVN_1: 11,
    MODE_SCALP_M1_2: 22,
}
DEFAULT_ACTIVE_MODES = [MODE_LVN_1, MODE_SCALP_M1_2]

_today_mode_cache = {}


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


def normalize_mode_settings(cfg):
    modes = cfg.get("modes", {})
    if not isinstance(modes, dict):
        modes = {}
    for mode in MODE_LABELS:
        mcfg = modes.get(mode, {})
        if not isinstance(mcfg, dict):
            mcfg = {}
        mcfg.setdefault("enabled", mode in DEFAULT_ACTIVE_MODES)
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


def nearest_level(levels, px):
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x - px))


def compute_mode1_lvn_signal(cfg):
    bars_needed = max(int(cfg.get("lvn_window", 144)) + 80, 300)
    bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M5, 0, bars_needed)
    if bars is None or len(bars) < bars_needed // 2:
        return None
    df = pd.DataFrame(bars)
    if len(df) < 100:
        return None
    # use closed bar only
    df = df.iloc[:-1].reset_index(drop=True)
    if len(df) < 100:
        return None

    atr = atr_series(df, 14)
    ema_fast = df["close"].ewm(span=int(cfg.get("ema_fast", 20)), adjust=False).mean()
    ema_slow = df["close"].ewm(span=int(cfg.get("ema_slow", 60)), adjust=False).mean()

    i = len(df) - 1
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    a = float(atr.iloc[i]) if float(atr.iloc[i]) > 0 else 0.0
    if a <= 0:
        return None

    lookback = int(cfg.get("lvn_window", 144))
    hist = df.iloc[max(0, i - lookback): i]
    levels = build_lvn_levels(hist, int(cfg.get("lvn_bins", 26)), int(cfg.get("lvn_count", 4)))
    lvl = nearest_level(levels, float(row["close"]))
    if lvl is None:
        return None

    touch_dist = float(cfg.get("touch_atr", 0.30)) * a
    touch_ok = abs(float(row["close"]) - lvl) <= touch_dist
    price_step = 0.01
    try:
        info = mt5.symbol_info(cfg["symbol"])
        if info is not None:
            price_step = max(0.0001, float(getattr(info, "point", 0.01) or 0.01))
    except Exception:
        pass
    # Entry guide cho user:
    #   BUY: can close >= LVN va > close truoc
    #   SELL: can close <= LVN va < close truoc
    # Dong thoi van nam trong vung touch quanh LVN.
    buy_raw = max(float(lvl), float(prev["close"]) + price_step)
    sell_raw = min(float(lvl), float(prev["close"]) - price_step)
    buy_upper = float(lvl) + touch_dist
    sell_lower = float(lvl) - touch_dist
    buy_price_hint = buy_raw if buy_raw <= buy_upper else None
    sell_price_hint = sell_raw if sell_raw >= sell_lower else None
    if not touch_ok:
        return {
            "side": None,
            "reason": f"far-from-lvn close={row['close']:.2f} lvn={lvl:.2f}",
            "m5_time": int(row["time"]),
            "atr": a,
            "lvn": lvl,
            "buy_price_hint": buy_price_hint,
            "sell_price_hint": sell_price_hint,
        }

    side = None
    reason = ""
    if float(ema_fast.iloc[i]) > float(ema_slow.iloc[i]) and float(row["close"]) >= lvl and float(row["close"]) > float(prev["close"]):
        side = "BUY"
        reason = f"up-trend touch-lvn {lvl:.2f}"
    elif float(ema_fast.iloc[i]) < float(ema_slow.iloc[i]) and float(row["close"]) <= lvl and float(row["close"]) < float(prev["close"]):
        side = "SELL"
        reason = f"down-trend touch-lvn {lvl:.2f}"
    else:
        reason = "trend-not-confirmed"

    # Volatility regime by ATR percentile in a rolling M5 window.
    atr_lookback = int(cfg.get("atr_regime_window", 288))
    atr_hist = atr.iloc[max(0, i - atr_lookback): i + 1].dropna().to_numpy(dtype=float)
    if len(atr_hist) >= 8:
        atr_rank = float((atr_hist <= a).sum()) / float(len(atr_hist))
    else:
        atr_rank = 0.5
    trend_strength = abs(float(ema_fast.iloc[i]) - float(ema_slow.iloc[i])) / max(1e-9, a)

    return {
        "side": side,
        "reason": reason,
        "m5_time": int(row["time"]),
        "atr": a,
        "lvn": lvl,
        "buy_price_hint": buy_price_hint,
        "sell_price_hint": sell_price_hint,
        "atr_rank": atr_rank,
        "trend_strength": trend_strength,
    }


def compute_lvn_signal(cfg):
    """Backward-compatible alias for mode 1 LVN signal."""
    return compute_mode1_lvn_signal(cfg)


def get_active_modes(cfg):
    cfg = normalize_mode_settings(cfg)
    return [m for m in MODE_LABELS if cfg.get("modes", {}).get(m, {}).get("enabled", False)]


def compute_mode2_m1_scalp_signal(cfg):
    bars = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M1, 0, 500)
    if bars is None or len(bars) < 150:
        return None
    df = pd.DataFrame(bars).iloc[:-1].reset_index(drop=True)  # closed bars only
    if len(df) < 120:
        return None

    atr_m1 = atr_series(df, 14)
    ema9 = df["close"].ewm(span=9, adjust=False).mean()

    bars_m5 = mt5.copy_rates_from_pos(cfg["symbol"], mt5.TIMEFRAME_M5, 0, 260)
    if bars_m5 is None or len(bars_m5) < 100:
        return None
    d5 = pd.DataFrame(bars_m5).iloc[:-1].reset_index(drop=True)
    ema20_m5 = d5["close"].ewm(span=20, adjust=False).mean()
    ema50_m5 = d5["close"].ewm(span=50, adjust=False).mean()
    trend_buy = float(ema20_m5.iloc[-1]) > float(ema50_m5.iloc[-1])
    trend_sell = float(ema20_m5.iloc[-1]) < float(ema50_m5.iloc[-1])

    i = len(df) - 1
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    a = float(atr_m1.iloc[i]) if float(atr_m1.iloc[i]) > 0 else 0.0
    if a <= 0:
        return None
    e9 = float(ema9.iloc[i])
    close = float(row["close"])
    open_ = float(row["open"])
    low = float(row["low"])
    high = float(row["high"])

    side = None
    reason = "no-setup"
    if trend_buy and low <= e9 and close > e9 and close > open_:
        side = "BUY"
        reason = "m1 pullback buy confirm"
    elif trend_sell and high >= e9 and close < e9 and close < open_:
        side = "SELL"
        reason = "m1 pullback sell confirm"

    touch_band = 0.20 * a
    buy_hint = max(e9, float(prev["close"]) + 0.01)
    sell_hint = min(e9, float(prev["close"]) - 0.01)
    buy_hint = buy_hint if buy_hint <= e9 + touch_band else None
    sell_hint = sell_hint if sell_hint >= e9 - touch_band else None

    atr_hist = atr_m1.iloc[max(0, i - 288): i + 1].dropna().to_numpy(dtype=float)
    atr_rank = float((atr_hist <= a).sum()) / float(len(atr_hist)) if len(atr_hist) >= 8 else 0.5
    trend_strength = abs(float(ema20_m5.iloc[-1]) - float(ema50_m5.iloc[-1])) / max(1e-9, a)

    return {
        "side": side,
        "reason": reason,
        "m5_time": int(row["time"]),
        "atr": a,
        "lvn": e9,
        "buy_price_hint": buy_hint,
        "sell_price_hint": sell_hint,
        "atr_rank": atr_rank,
        "trend_strength": trend_strength,
    }


def compute_signal_by_modes(cfg):
    """Multi-mode dispatcher. Currently supports Mode 1 LVN."""
    active_modes = get_active_modes(cfg)
    first_payload = None
    for mode in active_modes:
        payload = None
        if mode == MODE_LVN_1:
            payload = compute_mode1_lvn_signal(cfg)
        elif mode == MODE_SCALP_M1_2:
            payload = compute_mode2_m1_scalp_signal(cfg)
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
    stop_dist = sl_atr * atr_now
    lot = lot_from_risk(cfg, stop_dist)
    if lot <= 0:
        return False, "lot <= 0"

    price = float(tick.ask if side == "BUY" else tick.bid)
    if side == "BUY":
        sl = price - stop_dist
        tp = price + stop_dist * rr
        otype = mt5.ORDER_TYPE_BUY
    else:
        sl = price + stop_dist
        tp = price - stop_dist * rr
        otype = mt5.ORDER_TYPE_SELL

    mode_id = str(signal.get("mode", MODE_LVN_1))
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": cfg["symbol"],
        "volume": lot,
        "type": otype,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": int(cfg.get("deviation", 25)),
        "magic": int(mode_magic(cfg, mode_id)),
        "comment": f"EAGoldSuper:{mode_id}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        req["type_filling"] = mt5.ORDER_FILLING_FOK
        res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"order_send failed ret={getattr(res, 'retcode', None)}"

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
    cfg.setdefault("lvn_window", 144)
    cfg.setdefault("lvn_bins", 26)
    cfg.setdefault("lvn_count", 4)
    cfg.setdefault("touch_atr", 0.30)
    cfg.setdefault("ema_fast", 20)
    cfg.setdefault("ema_slow", 60)
    cfg.setdefault("atr_regime_window", 288)
    cfg.setdefault("deviation", 25)
    cfg.setdefault("fixed_lot_fallback", 0.01)
    cfg.setdefault("active_modes", list(DEFAULT_ACTIVE_MODES))
    normalize_mode_settings(cfg)

    if not init_mt5(cfg):
        send({"type": "exit", "reason": "mt5 init failed"})
        return

    threading.Thread(target=_stdin_watch, daemon=True).start()
    active_mode_labels = [MODE_LABELS.get(m, m) for m in get_active_modes(cfg)]
    log(
        f"EAGoldSuper started | symbol={cfg['symbol']} | risk={cfg['risk_pct']}% | "
        f"active_modes={', '.join(active_mode_labels)} | auto SL/TP by M5 regime"
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
        }
    last_signal = "-"
    signal_reason = "-"
    profile_text = "Auto SL/TP: warming up"
    entry_hint = "-"
    active_mode_label = ", ".join(active_mode_labels)

    try:
        while not _stop.is_set():
            now = time.time()
            enabled_modes = get_active_modes(cfg)
            active_mode_label = ", ".join(MODE_LABELS.get(m, m) for m in enabled_modes)
            for mode in enabled_modes:
                sig = None
                if mode == MODE_LVN_1:
                    sig = compute_mode1_lvn_signal(cfg)
                elif mode == MODE_SCALP_M1_2:
                    sig = compute_mode2_m1_scalp_signal(cfg)
                if not sig:
                    continue
                sig["mode"] = mode
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
                    positions = my_positions(cfg, mode)
                    if len(positions) < int(cfg.get("max_positions", 1)) and sig_side in ("BUY", "SELL"):
                        ok, reason = open_trade(cfg, sig_side, sig)
                        if not ok:
                            log(f"[{mode_label}] Skip open {sig_side}: {reason}", "warn")
                    else:
                        if sig_side in ("BUY", "SELL"):
                            log(f"[{mode_label}] Signal {sig_side} but max_positions reached ({len(positions)})", "info")

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
            "lvn_window": 144,
            "lvn_bins": 26,
            "lvn_count": 4,
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
        subtitle = QtWidgets.QLabel("Multi-mode trading engine · Mode 1 (LVN Adaptive) đang bật")
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
        self.lb_strategy = QtWidgets.QLabel("ACTIVE: Mode 1 - LVN Adaptive | Max position = 1")
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
        self.signal_tbl = QtWidgets.QTableWidget(0, 5)
        self.signal_tbl.setHorizontalHeaderLabels(["Mode", "State", "Sell", "Buy", "Reason"])
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
        self.signal_tbl.setColumnWidth(0, 205)
        self.signal_tbl.setColumnWidth(1, 85)
        self.signal_tbl.setColumnWidth(2, 110)
        self.signal_tbl.setColumnWidth(3, 110)
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
        self.lb_signal.setText(f"Signal realtime theo mode (M5/M1 closed bars) | Active: {obj.get('active_mode', '-')}")
        self.lb_auto_profile.setText(f"Auto profile: {obj.get('profile_text', '-')}")
        self.lb_strategy.setText(f"ACTIVE: {obj.get('active_mode', 'Mode 1 - LVN Adaptive')} | Max position = 1")
        self.lb_runtime_state.setText("ONLINE")
        mode_stats = obj.get("mode_stats", []) or []
        self.signal_tbl.setRowCount(len(mode_stats))
        for r, m in enumerate(mode_stats):
            state = str(m.get("last_signal", "-"))
            if state == "BUY":
                state_color = "#22c55e"
            elif state == "SELL":
                state_color = "#ef4444"
            else:
                state_color = "#a5b4cf"
            buy_hint = m.get("buy_hint")
            sell_hint = m.get("sell_hint")
            buy_txt = f"{float(buy_hint):.2f}" if isinstance(buy_hint, (int, float)) else "-"
            sell_txt = f"{float(sell_hint):.2f}" if isinstance(sell_hint, (int, float)) else "-"
            reason = str(m.get("signal_reason", "-"))
            vals = [str(m.get("label", m.get("id", "-"))), state, sell_txt, buy_txt, reason]
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if c in (2, 3):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                elif c == 1:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # Keep table readable in all themes/states.
                bg = "#0a1324" if (r % 2 == 0) else "#0c1730"
                it.setBackground(QtGui.QColor(bg))
                if c == 1:
                    it.setForeground(QtGui.QColor(state_color))
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

