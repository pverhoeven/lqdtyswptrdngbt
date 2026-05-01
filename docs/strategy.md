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

The edge comes from two compounding factors:

1. **Liquidity hunt reversal** — the sweep itself is the institutional trigger. Price prints a false breakout to collect stops, then reverses sharply. Entering on the close of the sweep candle captures the beginning of this reversal before it is widely recognized.

2. **Volatility expansion filter (ATR)** — by only trading when ATR is above its rolling average, the strategy selects environments where the reversal has momentum behind it and avoids low-volatility chop where sweeps frequently fail to follow through.

Together these two conditions select setups where a stop-hunt has just occurred in a market that has the energy to reverse — the highest-probability subset of all liquidity sweeps.

---

## Backtest Results (2019–2022, long-only, no BOS/ATR filters)

### BTCUSDT

| Metric | Value |
|---|---|
| Total trades | 230 (~19 per quarter) |
| Win rate | 55.9% (range: 42.9%–66.7%) |
| Avg Sharpe | +1.92 (std 1.70) |
| Profit Factor | 1.63 (range: 0.60–2.44) |
| Max Drawdown | 3.8% (peak: 9.6% in Q4 2020) |

**Verdict**: Edge present, but inconsistent — high Sharpe variance signals regime dependence.

### ETHUSDT

| Metric | Value |
|---|---|
| Total trades | 242 (~20 per quarter) |
| Win rate | 59.6% (range: 44.4%–73.7%) |
| Avg Sharpe | +2.79 (std 1.52) |
| Profit Factor | 2.11 (range: 1.04–3.67) |
| Max Drawdown | 2.7% (peak: 4.6% in Q4 2021) |

**Verdict**: Stronger edge than BTC across all metrics. Signal overlap between BTC and ETH is only 32.2%, making them partially independent — useful for diversification.

---

## Analysis

### Strengths

- Both coins show average Sharpe > 1.5 and PF > 1.5: statistically significant edge
- ETH outperforms BTC in win rate, Sharpe, PF, and drawdown
- MDD stays under 10% in all quarters (BTC Q4 2020 is the outlier at 9.6%)
- Some quarters show very strong mean-reversion: ETH Q3 2021 hit 73.7% win rate
- Low signal overlap (32.2%) reduces portfolio-level correlation risk

### Weaknesses

**High Sharpe variance** — the strategy works well in trending, volatile markets (2021 bull run) and poorly in sideways/consolidating ones (Q4 2020, Q4 2022). Bad quarters: BTC Q4 2020 (Sharpe −1.71), Q4 2022 (Sharpe −0.71).

**Break-even math is tight** — with a 1.5R target and 0.15% total cost per trade (0.1% fees + 0.05% slippage), the break-even win rate is ~43%. The 55–60% average is sufficient, but quarters at 42.9% win rate are losing quarters. A single bad quarter can erase multiple good ones.

**Circuit breaker timing risk** — 3 consecutive losses triggers a day-pause. In a quarter with only 42.9% win rate, three early losses could cause the CB to fire and miss a subsequent recovery. Raising the threshold slightly (e.g. to 4–5) may reduce unnecessary pauses.

---

## Known Weaknesses & Open Action Items

| Item | Priority | Notes |
|---|---|---|
| Add ATR filter | High | Should reduce bad-quarter frequency; already implemented, just needs enabling |
| Test 2023–2026 | High | Post-2022 market structure differs (ETF flows, higher institutional volume) |
| Add regime filter (MA200 or HMM) | Medium | Mean-reversion works better in bearish/ranging regimes |
| Dynamic reward ratio | Low | Higher target (2R) in high-ATR environments, lower (1.2R) when quiet |
| Dynamic position sizing | Low | Half-Kelly or vol-scaling; reduces size in losing streaks |
| Evaluate limit entries | Low | Could reduce slippage cost from 0.05% to near-zero |
