# Liquidity Sweep Strategy

## Overview

A Smart Money Concepts (SMC) mean-reversion strategy on BTC perpetual futures (15m timeframe). The core idea: retail stop-losses cluster just beyond recent swing highs/lows. When price briefly spikes through those levels ("sweeps" the liquidity) and then reverses, Smart Money has filled its orders and the move is likely over. We enter in the direction of the reversal.

---

## Market Structure Foundation

The strategy depends on three SMC concepts computed from the `smartmoneyconcepts` library on a 50-candle swing length:

- **Swing highs / lows** — structural pivots used to locate liquidity pools
- **Liquidity levels** — price levels where clustered stops sit (just above swing highs, just below swing lows)
- **Break of Structure (BOS)** — a close that breaks a prior swing in the opposite direction, confirming that the sweep was genuine and momentum has shifted

---

## Signal Detection

### Bearish sweep → Long setup
- SMC library flags `liq = -1` on the current candle
- Price traded below the previous swing low (sweeping sell-stop liquidity) and closed back above it
- The swept low becomes `liq_level`

### Bullish sweep → Short setup
- SMC library flags `liq = 1` on the current candle
- Price traded above the previous swing high (sweeping buy-stop liquidity) and closed back below it
- The swept high becomes `liq_level`

Signal is raised on **candle close** — no intra-candle entries.

---

## Entry

| | Long | Short |
|---|---|---|
| Trigger | Bearish sweep closed | Bullish sweep closed |
| Entry price | Close of sweep candle | Close of sweep candle |

If **BOS confirmation** is enabled, the entry is deferred until a subsequent candle closes back beyond `liq_level` in the reversal direction (up to `bos_window` candles, default 10). If no BOS occurs within the window, the setup is discarded.

---

## Stop Loss & Take Profit

```
Long:
  SL = liq_level × (1 − sl_buffer)        # e.g. 0.5% below swept low
  TP = entry + (entry − SL) × reward_ratio

Short:
  SL = liq_level × (1 + sl_buffer)        # e.g. 0.5% above swept high
  TP = entry − (SL − entry) × reward_ratio
```

Default parameters: `sl_buffer = 0.5%`, `reward_ratio = 1.5` (1.5R target).

SL is placed just beyond the swept level with a small buffer to avoid being stopped by noise immediately at the level.

---

## Position Sizing

```
risk_amount = current_capital × risk_per_trade_pct   # default 1%
sl_distance = |entry − sl_price|
position_size = risk_amount / sl_distance
```

At most one open position at a time.

---

## Filters

All filters are optional and can be combined independently.

### Direction filter
Restrict to `long`, `short`, or `both` directions. A `dynamic` mode uses the 200-period MA: long only above MA200, short only below.

### HMM Regime filter
A 2-state Hidden Markov Model trained on 4-hour data classifies the macro regime using log-returns and ATR ratio as features. The regime is updated incrementally (forward filter step) every 16 × 15m candles (≈ 4h).

- **Bullish regime** (`regime = True`) → only short setups allowed
- **Bearish regime** (`regime = False`) → only long setups allowed

Counter-trend sweeps in the wrong regime are skipped.

### ATR filter
Only trade when the current ATR exceeds its 14-candle rolling average, i.e. the market is in an expanding / trending volatility state. Quiet, low-momentum environments are filtered out.

### BOS confirmation filter
As described under Entry — requires structure break before committing. Reduces false entries at the cost of a slightly worse entry price.

---

## Trade Management

### Trailing stop
1. **Breakeven**: when floating P&L reaches 1R, SL is moved to entry price (risk-free)
2. **Trail**: from 2R onwards, SL trails the best price in 0.5R steps

### Partial exit (optional, disabled by default)
- At 1R profit, close 50% of the position
- Move SL to entry for the remaining 50%

---

## Circuit Breaker

Three independent safeguards prevent runaway losses:

| Condition | Action |
|---|---|
| N consecutive losses (default 3) | Pause trading for remainder of UTC day |
| Daily P&L < −3% of capital | Pause trading for remainder of UTC day |
| Peak-to-trough drawdown > 10% | Hard stop — requires manual restart |

---

## Execution

- **Timeframe**: 15-minute candles
- **Entry timing**: on candle close (no lookahead)
- **Fee model**: 0.1% per trade, 0.05% slippage
- **Multi-coin**: each symbol runs an independent detector and order manager with a shared account-level circuit breaker

---

## Strategy Edge

The edge comes from one core mechanism:

**Liquidity hunt reversal** — the sweep itself is the institutional trigger. Price prints a false breakout to collect stops, then reverses sharply. Entering on the close of the sweep candle captures the beginning of this reversal before it is widely recognized.

The ATR filter was tested and found to reduce the edge over the long run. The simplest configuration — long-only, no additional filters — is the most robust.

---

## Backtest Results

### BTCUSDT — In-sample (2017-08 → 2022-12, long-only)

| Metric | Value |
|---|---|
| Total trades | 429 (~20/quarter) |
| Win rate | 55.9% |
| Sharpe ratio | 1.87 |
| Profit factor | 1.49 |
| Max drawdown | 14.6% (concentrated in 2018 bear) |
| Total return | +214% (10k → 31k USDT) |

Per-year breakdown:

| Year | Trades | Win rate | P&L |
|---|---|---|---|
| 2017 | 28 | 75.0% | +2 464 USDT |
| 2018 | 83 | 50.6% | +1 664 USDT |
| 2019 | 81 | 55.6% | +3 327 USDT |
| 2020 | 77 | 55.8% | +2 693 USDT |
| 2021 | 84 | 56.0% | +5 848 USDT |
| 2022 | 76 | 55.3% | +5 396 USDT |

### Walk-forward (2017-08 → 2022-12, 17 quarters, train=12m / test=3m)

| Metric | Value |
|---|---|
| Windows with Sharpe > 0 | 76% (13/17) |
| Avg Sharpe | +1.88 |
| Sharpe range | −1.51 → +4.56 |
| Avg win rate | 55.5% |
| Avg max drawdown | 4.2% |
| Avg profit factor | 1.67 |

Losing quarters: 2018-11, 2019-08, 2020-08, 2021-11 — all coincide with sharp BTC downtrends, consistent with a long-only setup.

### BTCUSDT — Out-of-sample (2023-01 → 2024-12, long-only) ✅

| Metric | Value |
|---|---|
| Total trades | 174 |
| Win rate | 60.3% |
| Sharpe ratio | 2.65 |
| Profit factor | 1.80 |
| Max drawdown | 4.3% |
| Total return | +85% (10k → 18 460 USDT) |

The OOS result is stronger than in-sample across all metrics — no evidence of overfitting.

---

## Analysis

### Strengths

- Consistent win rate of 55–60% across 9 years of data (2017–2024)
- OOS outperforms in-sample: Sharpe 2.65 vs 1.87, MDD 4.3% vs 14.6%
- Walk-forward confirms the edge holds in 76% of rolling quarters
- Simple setup: no filters beyond direction — robust and not over-engineered

### Weaknesses

**Long-only loses edge in sustained bear markets** — 2018 was the weakest year (50.6% win rate), barely profitable. The strategy cannot be run unattended through a multi-month bear market without a regime filter or manual pause.

**Break-even math is tight** — with 1.5R target and ~0.15% total cost per trade, break-even win rate is ~43%. The 55–60% average is comfortable, but individual bad quarters at 40–43% win rate are losing quarters.

**Circuit breaker timing risk** — 3 consecutive losses triggers a day-pause. In a weak quarter this could fire early and miss a subsequent recovery.

---

## Known Weaknesses & Open Action Items

| Item | Priority | Status | Notes |
|---|---|---|---|
| ATR filter | High | ❌ Rejected | Tested — reduces long-run edge. Do not enable. |
| Test 2023–2026 | High | ✅ Done | OOS 2023–2024 confirmed, Sharpe 2.65 |
| Extend in-sample to 2017 | High | ✅ Done | in_sample_start = 2017-08-01 |
| Add regime filter (MA200 or HMM) | Medium | Open | Could help skip bear markets for long-only |
| Dynamic reward ratio | Low | Open | Higher target (2R) in high-ATR, lower (1.2R) when quiet |
| Dynamic position sizing | Low | Open | Half-Kelly or vol-scaling; reduces size in losing streaks |
| Evaluate limit entries | Low | Open | Could reduce slippage cost from 0.05% to near-zero |
