# Edge-validatierapport

Gegenereerd: 2026-04-30 17:47 UTC  
Periode: 2019-01-01 → 2022-12-31  
Vensters: train=12m / test=3m  
Filters: regime+long_only

---

## BTCUSDT

**Vensters:** 12  **Totaal trades:** 149  **Gem. trades/venster:** 12.4

**Verdict:** ✅ Robuuste edge


### Per-venster resultaten

| Venster | Trades | Win% | Sharpe | MDD | PF |
|---------|-------:|-----:|-------:|----:|----|
| 2020-01 → 2020-03 | 12 | 41.7% | +0.72 | 4.6% | 1.19 |
| 2020-04 → 2020-06 | 3 | 66.7% | +2.18 ✓ | 1.1% | 3.47 |
| 2020-07 → 2020-09 | 12 | 50.0% | +1.62 ✓ | 3.4% | 1.58 |
| 2020-10 → 2020-12 | 16 | 37.5% | -1.53 ✗ | 8.5% | 0.61 |
| 2021-01 → 2021-03 | 15 | 46.7% | +1.67 ✓ | 3.1% | 1.55 |
| 2021-04 → 2021-06 | 13 | 46.2% | +1.37 ✓ | 3.2% | 1.44 |
| 2021-07 → 2021-09 | 17 | 47.1% | +1.84 ✓ | 2.5% | 1.52 |
| 2021-10 → 2021-12 | 16 | 43.8% | +1.17 ✓ | 6.3% | 1.32 |
| 2022-01 → 2022-03 | 9 | 55.6% | +2.28 ✓ | 2.3% | 2.06 |
| 2022-04 → 2022-06 | 14 | 64.3% | +3.74 ✓ | 2.1% | 2.87 |
| 2022-07 → 2022-09 | 8 | 50.0% | +1.65 ✓ | 2.3% | 1.63 |
| 2022-10 → 2022-12 | 14 | 42.9% | +0.56 | 3.4% | 1.15 |



### Inter-venster variantie

| Metriek | Gemiddeld | Min | Max | Std |
|---------|----------:|----:|----:|----:|
| Sharpe | +1.44 | -1.53 | +3.74 | 1.24 |
| Profit factor | +1.70 | +0.61 | +3.47 | 0.78 |
| Max drawdown | 3.6% | 1.1% | 8.5% | 0.02 |
| Win rate | 49.3% | 37.5% | 66.7% | 0.09 |



> **Lees dit zo:** Een hoge Std op Sharpe betekent dat de edge niet stabiel is over tijd. Streef naar Std < 0.5 bij Sharpe > 1.0.


---

## ETHUSDT

**Vensters:** 12  **Totaal trades:** 144  **Gem. trades/venster:** 12.0

**Verdict:** ✅ Robuuste edge


### Per-venster resultaten

| Venster | Trades | Win% | Sharpe | MDD | PF |
|---------|-------:|-----:|-------:|----:|----|
| 2020-01 → 2020-03 | 16 | 68.8% | +4.62 ✓ | 2.1% | 3.67 |
| 2020-04 → 2020-06 | 16 | 50.0% | +2.15 ✓ | 3.3% | 1.69 |
| 2020-07 → 2020-09 | 10 | 50.0% | +1.67 ✓ | 3.2% | 1.67 |
| 2020-10 → 2020-12 | 11 | 54.5% | +2.41 ✓ | 2.1% | 2.16 |
| 2021-01 → 2021-03 | 19 | 47.4% | +2.01 ✓ | 5.2% | 1.58 |
| 2021-04 → 2021-06 | 13 | 69.2% | +4.32 ✓ | 3.1% | 3.89 |
| 2021-07 → 2021-09 | 16 | 62.5% | +4.23 ✓ | 3.1% | 2.91 |
| 2021-10 → 2021-12 | 13 | 46.2% | +1.45 ✓ | 3.7% | 1.48 |
| 2022-01 → 2022-03 | 9 | 77.8% | +4.46 ✓ | 1.0% | 6.48 |
| 2022-04 → 2022-06 | 7 | 100.0% | +5.83 ✓ | 0.0% | ∞ |
| 2022-07 → 2022-09 | 11 | 54.5% | +2.30 ✓ | 3.3% | 2.01 |
| 2022-10 → 2022-12 | 3 | 33.3% | -0.82 ✗ | 2.2% | 0.85 |



### Inter-venster variantie

| Metriek | Gemiddeld | Min | Max | Std |
|---------|----------:|----:|----:|----:|
| Sharpe | +2.88 | -0.82 | +5.83 | 1.84 |
| Profit factor | +2.58 | +0.85 | +6.48 | 1.60 |
| Max drawdown | 2.7% | 0.0% | 5.2% | 0.01 |
| Win rate | 59.5% | 33.3% | 100.0% | 0.18 |



> **Lees dit zo:** Een hoge Std op Sharpe betekent dat de edge niet stabiel is over tijd. Streef naar Std < 0.5 bij Sharpe > 1.0.


---

## Cross-coin signaalcorrelatie

Fractie van candles waarop twee coins *tegelijk* een open trade hadden t.o.v. alle candles waarop minstens één coin open was. Bij hoge overlap is het effectieve risico hoger dan de per-coin limieten suggereren.


| Paar | Overlap (beide open) | Interpretatie |
|------|---------------------:|---------------|
| BTCUSDT / ETHUSDT | 24.1% | Matig — gedeeltelijk gecorreleerd |
