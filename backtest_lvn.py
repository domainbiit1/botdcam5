#!/usr/bin/env python3
"""Simple LVN backtest on gold (GC=F) using Yahoo Finance data.

Strategy:
- Build a rolling M5 volume profile over `window` bars.
- Extract LVN bins (lowest-volume nodes) from the profile.
- Enter when price trades near nearest LVN with trend filter confirmation.
- Use fixed ATR-multiple SL and RR-based TP per trade.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import List, Dict, Tuple

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class Trade:
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    sl: float
    tp: float
    pnl: float
    ret_r: float
    bars_held: int
    reason: str


PRESETS = {
    "safe": {
        "window": 180,
        "bins": 30,
        "lvn_count": 4,
        "touch_atr": 0.35,
        "sl_atr": 1.6,
        "rr": 1.6,
        "ema_fast": 20,
        "ema_slow": 80,
    },
    "balanced": {
        "window": 144,
        "bins": 26,
        "lvn_count": 4,
        "touch_atr": 0.30,
        "sl_atr": 1.4,
        "rr": 1.8,
        "ema_fast": 20,
        "ema_slow": 60,
    },
    "fast": {
        "window": 96,
        "bins": 22,
        "lvn_count": 5,
        "touch_atr": 0.25,
        "sl_atr": 1.2,
        "rr": 1.6,
        "ema_fast": 12,
        "ema_slow": 40,
    },
}


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hcp = (df["High"] - df["Close"].shift()).abs()
    lcp = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def rolling_lvn_levels(window_df: pd.DataFrame, bins: int, lvn_count: int) -> List[float]:
    low = float(window_df["Low"].min())
    high = float(window_df["High"].max())
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        return []

    edges = np.linspace(low, high, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    vol = np.zeros(bins, dtype=float)

    closes = window_df["Close"].to_numpy(dtype=float)
    volumes = window_df["Volume"].to_numpy(dtype=float)
    idx = np.clip(np.digitize(closes, edges) - 1, 0, bins - 1)
    for i, b in enumerate(idx):
        vol[b] += volumes[i]

    ranked = np.argsort(vol)[: max(1, lvn_count)]
    levels = sorted(float(centers[i]) for i in ranked)
    return levels


def nearest_level(levels: List[float], price: float) -> float | None:
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x - price))


def backtest(df: pd.DataFrame, cfg: Dict[str, float]) -> Tuple[List[Trade], Dict[str, float]]:
    work = df.copy()
    work["ATR"] = atr(work, 14)
    work["EMA_FAST"] = work["Close"].ewm(span=int(cfg["ema_fast"]), adjust=False).mean()
    work["EMA_SLOW"] = work["Close"].ewm(span=int(cfg["ema_slow"]), adjust=False).mean()

    trades: List[Trade] = []
    i = int(cfg["window"]) + 2

    while i < len(work) - 2:
        row = work.iloc[i]
        prev = work.iloc[i - 1]
        a = float(row["ATR"])
        if not math.isfinite(a) or a <= 0:
            i += 1
            continue

        hist = work.iloc[i - int(cfg["window"]): i]
        levels = rolling_lvn_levels(hist, int(cfg["bins"]), int(cfg["lvn_count"]))
        lvn = nearest_level(levels, float(row["Close"]))
        if lvn is None:
            i += 1
            continue

        dist = abs(float(row["Close"]) - lvn)
        if dist > float(cfg["touch_atr"]) * a:
            i += 1
            continue

        side = None
        if row["EMA_FAST"] > row["EMA_SLOW"] and row["Close"] > lvn and row["Close"] > prev["Close"]:
            side = "BUY"
        elif row["EMA_FAST"] < row["EMA_SLOW"] and row["Close"] < lvn and row["Close"] < prev["Close"]:
            side = "SELL"

        if side is None:
            i += 1
            continue

        entry = float(row["Close"])
        risk = float(cfg["sl_atr"]) * a
        if side == "BUY":
            sl = entry - risk
            tp = entry + risk * float(cfg["rr"])
        else:
            sl = entry + risk
            tp = entry - risk * float(cfg["rr"])

        exit_price = entry
        exit_time = work.index[i]
        reason = "eod"
        j = i + 1
        while j < len(work):
            bar = work.iloc[j]
            hi = float(bar["High"])
            lo = float(bar["Low"])
            if side == "BUY":
                hit_sl = lo <= sl
                hit_tp = hi >= tp
                if hit_sl and hit_tp:
                    exit_price, reason = sl, "sl"
                    exit_time = work.index[j]
                    break
                if hit_sl:
                    exit_price, reason = sl, "sl"
                    exit_time = work.index[j]
                    break
                if hit_tp:
                    exit_price, reason = tp, "tp"
                    exit_time = work.index[j]
                    break
            else:
                hit_sl = hi >= sl
                hit_tp = lo <= tp
                if hit_sl and hit_tp:
                    exit_price, reason = sl, "sl"
                    exit_time = work.index[j]
                    break
                if hit_sl:
                    exit_price, reason = sl, "sl"
                    exit_time = work.index[j]
                    break
                if hit_tp:
                    exit_price, reason = tp, "tp"
                    exit_time = work.index[j]
                    break
            j += 1

        pnl = (exit_price - entry) if side == "BUY" else (entry - exit_price)
        ret_r = pnl / max(1e-9, risk)
        trades.append(
            Trade(
                side=side,
                entry_time=work.index[i],
                exit_time=exit_time,
                entry=entry,
                exit=exit_price,
                sl=sl,
                tp=tp,
                pnl=pnl,
                ret_r=ret_r,
                bars_held=max(1, j - i),
                reason=reason,
            )
        )
        i = min(len(work) - 2, j + 1)

    if not trades:
        return trades, {"trades": 0}

    pnl = np.array([t.pnl for t in trades], dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    equity = np.cumsum(pnl)
    peaks = np.maximum.accumulate(equity)
    drawdowns = equity - peaks

    summary = {
        "trades": int(len(trades)),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl <= 0).sum()),
        "win_rate": float((pnl > 0).mean() * 100.0),
        "net_points": float(pnl.sum()),
        "avg_points": float(pnl.mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if losses.size and abs(losses.sum()) > 0 else float("inf"),
        "max_drawdown_points": float(drawdowns.min()),
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])),
        "tp_hits": int(sum(1 for t in trades if t.reason == "tp")),
        "sl_hits": int(sum(1 for t in trades if t.reason == "sl")),
    }
    return trades, summary


def format_summary(name: str, summary: Dict[str, float]) -> str:
    if summary.get("trades", 0) == 0:
        return f"{name:9} | trades=0"
    return (
        f"{name:9} | trades={summary['trades']:4d} | win={summary['win_rate']:5.1f}%"
        f" | net={summary['net_points']:8.2f} | pf={summary['profit_factor']:5.2f}"
        f" | maxDD={summary['max_drawdown_points']:8.2f} | TP/SL={summary['tp_hits']}/{summary['sl_hits']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="LVN backtest on GC=F (gold futures)")
    parser.add_argument("--symbol", default="GC=F")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--period", default="60d")
    parser.add_argument("--preset", choices=["safe", "balanced", "fast", "all"], default="all")
    args = parser.parse_args()

    data = yf.download(args.symbol, period=args.period, interval=args.interval, auto_adjust=False, progress=False)
    if data is None or data.empty:
        raise SystemExit("No data downloaded from Yahoo Finance")

    data = data.dropna().copy()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data.columns = [str(c) for c in data.columns]
    # keep market-session rows only where OHLCV are finite
    data = data[np.isfinite(data["Open"]) & np.isfinite(data["High"]) & np.isfinite(data["Low"]) & np.isfinite(data["Close"])]
    if len(data) < 300:
        raise SystemExit("Not enough bars for backtest")

    print(f"Data: {args.symbol} {args.interval} {args.period} | bars={len(data)} | from={data.index[0]} to={data.index[-1]}")
    presets = PRESETS.keys() if args.preset == "all" else [args.preset]
    for name in presets:
        _, summary = backtest(data, PRESETS[name])
        print(format_summary(name, summary))


if __name__ == "__main__":
    main()

