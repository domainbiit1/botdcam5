# DCA strategy comparison backtest (gold proxy)

Run date: 2026-07-07  
Data source: Yahoo Finance (`GC=F`, 5m, 60d)  
Purpose: compare **entry-direction engines** while keeping DCA basket style consistent.

## Simulator assumptions

- One new DCA order each bar using current directional signal.
- No per-order SL/TP.
- Basket closes only when floating basket PnL reaches `basket_tp`.
- Constant lot per order for fair direction-engine comparison.
- This is a research proxy, not broker-native MT5 XAUUSD tick replay.

## Command

```bash
python3 /workspace/backtest_dca_compare.py --basket-tp 25 --order-cost 0.0
python3 /workspace/backtest_dca_compare.py --basket-tp 25 --order-cost 0.2
```

## Result (`order_cost = 0.0`)

```text
current-st     | net= 24245.95 | maxDD=-29170.40 | maxOpen=2081 | cycles= 332 | winCycle= 99.7% | avgCycleBars=  62.4
balanced       | net= 24245.95 | maxDD=-29170.40 | maxOpen=2081 | cycles= 332 | winCycle= 99.7% | avgCycleBars=  62.4
balanced-both  | net= 43967.66 | maxDD=-14017.29 | maxOpen=1365 | cycles= 516 | winCycle= 99.8% | avgCycleBars=  40.1
safe           | net= 24245.95 | maxDD=-29170.40 | maxOpen=2081 | cycles= 332 | winCycle= 99.7% | avgCycleBars=  62.4
fast           | net= 24245.95 | maxDD=-29170.40 | maxOpen=2081 | cycles= 332 | winCycle= 99.7% | avgCycleBars=  62.4
two-bar-flip   | net= 23320.82 | maxDD=-28212.60 | maxOpen=2062 | cycles= 299 | winCycle= 99.7% | avgCycleBars=  69.3
```

## Result (`order_cost = 0.2`)

```text
current-st     | net= 18810.15 | maxDD=-29482.60 | maxOpen=2081 | cycles= 332 | winCycle= 99.1% | avgCycleBars=  62.4
balanced       | net= 18810.15 | maxDD=-29482.60 | maxOpen=2081 | cycles= 332 | winCycle= 99.1% | avgCycleBars=  62.4
balanced-both  | net= 38531.86 | maxDD=-14285.89 | maxOpen=1365 | cycles= 516 | winCycle= 99.8% | avgCycleBars=  40.1
safe           | net= 18810.15 | maxDD=-29482.60 | maxOpen=2081 | cycles= 332 | winCycle= 99.1% | avgCycleBars=  62.4
fast           | net= 18810.15 | maxDD=-29482.60 | maxOpen=2081 | cycles= 332 | winCycle= 99.1% | avgCycleBars=  62.4
two-bar-flip   | net= 17885.02 | maxDD=-28519.90 | maxOpen=2062 | cycles= 299 | winCycle= 99.0% | avgCycleBars=  69.3
```

## Practical takeaway

- In this proxy test, `balanced-both` (flip accepted only when **both** ST buffer and M15 ADX/DI confirm) gave:
  - higher net points,
  - much smaller max drawdown,
  - lower peak open-position count,
  - faster basket cycle turnover.
- `current-st`, `balanced` (either/or), `safe`, and `fast` behaved nearly identically in this sample.

## Recommendation for your DCA bot

If your main pain is “ôm lệnh quá nhiều và quá lâu”, this test suggests:

1. Keep core DCA mechanics.
2. Tighten direction flip gate to `balanced-both` style (strict flip confirmation only).
3. Re-validate on broker-native XAUUSD history (MT5 export) before production rollout.
