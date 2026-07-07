# LVN Backtest (auto-fetched gold data)

Run timestamp (UTC): 2026-07-07  
Source: Yahoo Finance (`yfinance`)  
Symbol: `GC=F` (Gold Futures proxy)  
Timeframe: `5m`  
Lookback: `60d`

## Command

```bash
python3 /workspace/backtest_lvn.py --preset all
```

## Output

```text
Data: GC=F 5m 60d | bars=13565 | from=2026-04-26 18:10:00-04:00 to=2026-07-07 15:50:00-04:00
safe      | trades= 189 | win= 37.6% | net=    0.83 | pf= 1.00 | maxDD= -177.24 | TP/SL=71/118
balanced  | trades= 213 | win= 35.2% | net=  -22.68 | pf= 0.98 | maxDD= -205.15 | TP/SL=75/138
fast      | trades= 325 | win= 39.7% | net=  135.32 | pf= 1.10 | maxDD= -190.74 | TP/SL=129/196
```

## Quick read

- `safe`: near breakeven in this sample
- `balanced`: slightly negative in this sample
- `fast`: strongest net and PF in this sample, but with higher trade count

## Notes

- This is a lightweight research backtest (single-position, bar-based SL/TP handling).
- `GC=F` is used as a public gold proxy, not broker-specific XAUUSD tick data.
- For production decisions, re-run on broker-native XAUUSD data with spread/slippage/session constraints.
