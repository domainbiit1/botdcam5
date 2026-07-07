#!/usr/bin/env python3
"""Compare DCA basket behavior across direction engines.

This is a research backtest to compare *entry direction logic* while keeping
the DCA mechanic simple and consistent across strategies:
- one new order per bar using current directional signal
- no per-order SL/TP
- basket closes only when total floating PnL reaches basket TP threshold

Data source: Yahoo Finance gold futures proxy (GC=F), 5m bars.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class Position:
    side: int  # +1 buy, -1 sell
    entry: float
    opened_at: pd.Timestamp


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hcp = (df["High"] - df["Close"].shift()).abs()
    lcp = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    out = df.copy()
    out["ATR"] = atr(out, period)
    hl2 = (out["High"] + out["Low"]) / 2.0
    out["UPPER"] = hl2 + mult * out["ATR"]
    out["LOWER"] = hl2 - mult * out["ATR"]

    n = len(out)
    f_upper = out["UPPER"].to_numpy().copy()
    f_lower = out["LOWER"].to_numpy().copy()
    close = out["Close"].to_numpy()
    for i in range(1, n):
        if out["UPPER"].iloc[i] < f_upper[i - 1] or close[i - 1] > f_upper[i - 1]:
            f_upper[i] = out["UPPER"].iloc[i]
        else:
            f_upper[i] = f_upper[i - 1]
        if out["LOWER"].iloc[i] > f_lower[i - 1] or close[i - 1] < f_lower[i - 1]:
            f_lower[i] = out["LOWER"].iloc[i]
        else:
            f_lower[i] = f_lower[i - 1]

    trend = np.ones(n, dtype=int)  # +1 buy, -1 sell
    for i in range(1, n):
        if trend[i - 1] == 1:
            trend[i] = -1 if close[i] < f_lower[i] else 1
        else:
            trend[i] = 1 if close[i] > f_upper[i] else -1

    out["ST_FUPPER"] = f_upper
    out["ST_FLOWER"] = f_lower
    out["ST_TREND"] = trend
    out["ST_LINE"] = np.where(trend == 1, f_lower, f_upper)
    return out


def m15_adx_di_from_m5(df5: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    ohlc = df5[["Open", "High", "Low", "Close", "Volume"]].resample("15min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()
    ohlc["ATR15"] = atr(ohlc, period)

    high = ohlc["High"]
    low = ohlc["Low"]
    close = ohlc["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(0.0, index=ohlc.index)
    minus_dm = pd.Series(0.0, index=ohlc.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    atr15 = ohlc["ATR15"].replace(0, 1e-9)
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr15
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr15
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()

    return pd.DataFrame(
        {"PLUS_DI": plus_di, "MINUS_DI": minus_di, "ADX": adx}
    ).dropna()


def apply_flip_filter(
    st: pd.DataFrame,
    m15: pd.DataFrame,
    buffer_k: float,
    di_gap_min: float,
    adx_min: float,
) -> pd.Series:
    out = pd.Series(index=st.index, dtype="int64")
    prev = None
    for ts, row in st.iterrows():
        raw = int(row["ST_TREND"])
        if prev is None or raw == prev:
            out.loc[ts] = raw
            prev = raw
            continue
        # flip candidate
        dist = abs(float(row["Close"]) - float(row["ST_LINE"]))
        need = buffer_k * max(1e-9, float(row["ATR"]))
        st_ok = dist >= need

        m15_row = m15.loc[:ts].iloc[-1] if (m15.index <= ts).any() else None
        di_ok = False
        if m15_row is not None:
            pdi = float(m15_row["PLUS_DI"])
            mdi = float(m15_row["MINUS_DI"])
            adx = float(m15_row["ADX"])
            gap = abs(pdi - mdi)
            if adx >= adx_min and gap >= di_gap_min:
                di_ok = (raw == 1 and pdi > mdi) or (raw == -1 and mdi > pdi)

        if st_ok or di_ok:
            out.loc[ts] = raw
            prev = raw
        else:
            out.loc[ts] = prev
    return out


def apply_flip_filter_two_bar(st: pd.DataFrame) -> pd.Series:
    """Accept a flip only if raw Supertrend keeps opposite side for 2 bars."""
    out = pd.Series(index=st.index, dtype="int64")
    prev = None
    for idx in range(len(st)):
        ts = st.index[idx]
        raw = int(st.iloc[idx]["ST_TREND"])
        if prev is None or raw == prev:
            out.loc[ts] = raw
            prev = raw
            continue
        if idx >= 1 and int(st.iloc[idx - 1]["ST_TREND"]) == raw:
            out.loc[ts] = raw
            prev = raw
        else:
            out.loc[ts] = prev
    return out


def apply_flip_filter_both(
    st: pd.DataFrame,
    m15: pd.DataFrame,
    buffer_k: float,
    di_gap_min: float,
    adx_min: float,
) -> pd.Series:
    """Accept flip only when both ST-buffer and M15 ADX/DI confirm."""
    out = pd.Series(index=st.index, dtype="int64")
    prev = None
    for ts, row in st.iterrows():
        raw = int(row["ST_TREND"])
        if prev is None or raw == prev:
            out.loc[ts] = raw
            prev = raw
            continue
        dist = abs(float(row["Close"]) - float(row["ST_LINE"]))
        need = buffer_k * max(1e-9, float(row["ATR"]))
        st_ok = dist >= need
        di_ok = False
        m15_row = m15.loc[:ts].iloc[-1] if (m15.index <= ts).any() else None
        if m15_row is not None:
            pdi = float(m15_row["PLUS_DI"])
            mdi = float(m15_row["MINUS_DI"])
            adx = float(m15_row["ADX"])
            gap = abs(pdi - mdi)
            if adx >= adx_min and gap >= di_gap_min:
                di_ok = (raw == 1 and pdi > mdi) or (raw == -1 and mdi > pdi)
        if st_ok and di_ok:
            out.loc[ts] = raw
            prev = raw
        else:
            out.loc[ts] = prev
    return out


def run_dca_basket(
    df: pd.DataFrame,
    signal: pd.Series,
    basket_tp: float = 25.0,
    order_cost: float = 0.0,
) -> Dict[str, float]:
    positions: List[Position] = []
    realized = 0.0
    equity_curve: List[float] = []
    max_open = 0
    cycle_pnls: List[float] = []
    cycle_bars: List[int] = []
    trades_opened = 0
    active_cycle_start = None

    for ts, row in df.iterrows():
        px = float(row["Close"])
        sig = int(signal.loc[ts])

        floating = sum((px - p.entry) * p.side for p in positions)
        if positions and floating >= basket_tp:
            cycle_realized = floating - order_cost * len(positions)
            realized += cycle_realized
            cycle_pnls.append(cycle_realized)
            if active_cycle_start is not None:
                cycle_bars.append(max(1, int((ts - active_cycle_start) / pd.Timedelta(minutes=5))))
            positions = []
            active_cycle_start = None
            floating = 0.0

        # add one DCA order every bar with current direction signal
        if sig in (1, -1):
            if not positions:
                active_cycle_start = ts
            positions.append(Position(side=sig, entry=px, opened_at=ts))
            trades_opened += 1
            realized -= order_cost

        floating = sum((px - p.entry) * p.side for p in positions)
        equity = realized + floating
        equity_curve.append(equity)
        max_open = max(max_open, len(positions))

    # force-close remaining positions at end of dataset for terminal equity
    if positions:
        px = float(df.iloc[-1]["Close"])
        floating = sum((px - p.entry) * p.side for p in positions)
        terminal = floating - order_cost * len(positions)
        realized += terminal
        cycle_pnls.append(terminal)
        if active_cycle_start is not None:
            cycle_bars.append(
                max(1, int((df.index[-1] - active_cycle_start) / pd.Timedelta(minutes=5)))
            )
        positions = []
        equity_curve.append(realized)

    eq = np.array(equity_curve, dtype=float)
    peaks = np.maximum.accumulate(eq)
    drawdown = eq - peaks
    avg_cycle = float(np.mean(cycle_pnls)) if cycle_pnls else 0.0
    win_cycles = sum(1 for x in cycle_pnls if x > 0)

    return {
        "bars": int(len(df)),
        "trades_opened": int(trades_opened),
        "cycles": int(len(cycle_pnls)),
        "win_cycle_rate": (100.0 * win_cycles / len(cycle_pnls)) if cycle_pnls else 0.0,
        "net_points": float(realized),
        "max_drawdown_points": float(drawdown.min()) if len(drawdown) else 0.0,
        "max_open_positions": int(max_open),
        "avg_cycle_points": avg_cycle,
        "avg_cycle_bars": float(np.mean(cycle_bars)) if cycle_bars else 0.0,
        "median_cycle_bars": float(np.median(cycle_bars)) if cycle_bars else 0.0,
    }


def fmt(name: str, m: Dict[str, float]) -> str:
    return (
        f"{name:14} | net={m['net_points']:9.2f} | maxDD={m['max_drawdown_points']:9.2f}"
        f" | maxOpen={m['max_open_positions']:4d} | cycles={m['cycles']:4d}"
        f" | winCycle={m['win_cycle_rate']:5.1f}% | avgCycleBars={m['avg_cycle_bars']:6.1f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare DCA direction strategies on gold data")
    parser.add_argument("--symbol", default="GC=F")
    parser.add_argument("--period", default="60d")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--basket-tp", type=float, default=25.0)
    parser.add_argument("--order-cost", type=float, default=0.0)
    args = parser.parse_args()

    data = yf.download(args.symbol, period=args.period, interval=args.interval, auto_adjust=False, progress=False)
    if data is None or data.empty:
        raise SystemExit("No data downloaded")
    data = data.dropna().copy()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data.columns = [str(c) for c in data.columns]
    data = data[np.isfinite(data["Open"]) & np.isfinite(data["High"]) & np.isfinite(data["Low"]) & np.isfinite(data["Close"])]
    if len(data) < 500:
        raise SystemExit("Not enough bars")

    st = supertrend(data, period=10, mult=3.0).dropna().copy()
    m15 = m15_adx_di_from_m5(st, period=14)

    sig_current = st["ST_TREND"].astype(int)
    sig_balanced = apply_flip_filter(st, m15, buffer_k=0.20, di_gap_min=5.0, adx_min=18.0)
    sig_balanced_both = apply_flip_filter_both(st, m15, buffer_k=0.20, di_gap_min=5.0, adx_min=18.0)
    sig_safe = apply_flip_filter(st, m15, buffer_k=0.25, di_gap_min=7.0, adx_min=22.0)
    sig_fast = apply_flip_filter(st, m15, buffer_k=0.12, di_gap_min=3.0, adx_min=14.0)
    sig_two_bar = apply_flip_filter_two_bar(st)

    metrics = {
        "current-st": run_dca_basket(st, sig_current, basket_tp=args.basket_tp, order_cost=args.order_cost),
        "balanced": run_dca_basket(st, sig_balanced, basket_tp=args.basket_tp, order_cost=args.order_cost),
        "balanced-both": run_dca_basket(st, sig_balanced_both, basket_tp=args.basket_tp, order_cost=args.order_cost),
        "safe": run_dca_basket(st, sig_safe, basket_tp=args.basket_tp, order_cost=args.order_cost),
        "fast": run_dca_basket(st, sig_fast, basket_tp=args.basket_tp, order_cost=args.order_cost),
        "two-bar-flip": run_dca_basket(st, sig_two_bar, basket_tp=args.basket_tp, order_cost=args.order_cost),
    }

    print(
        f"Data: {args.symbol} {args.interval} {args.period} | bars={len(st)}"
        f" | from={st.index[0]} to={st.index[-1]}"
    )
    print(f"Basket TP (points): {args.basket_tp} | order_cost: {args.order_cost}")
    for name, m in metrics.items():
        print(fmt(name, m))


if __name__ == "__main__":
    main()

