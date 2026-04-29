# Edge-validatierapport

Gegenereerd: 2026-04-28 16:02 UTC  
Periode: 2019-01-01 → 2022-12-31  
Vensters: train=12m / test=3m

---

## BTCUSDT

**Vensters:** 12  **Totaal trades:** 230  **Gem. trades/venster:** 19.2

**Verdict:** ✅ Robuuste edge


### Per-venster resultaten

| Venster | Trades | Win% | Sharpe | MDD | PF |
|---------|-------:|-----:|-------:|----:|----|
| 2020-01 → 2020-03 | 18 | 66.7% | +4.01 ✓ | 2.2% | 2.44 |
| 2020-04 → 2020-06 | 17 | 52.9% | +1.49 ✓ | 5.5% | 1.39 |
| 2020-07 → 2020-09 | 17 | 64.7% | +3.14 ✓ | 3.4% | 2.14 |
| 2020-10 → 2020-12 | 21 | 42.9% | -1.71 ✗ | 9.6% | 0.60 |
| 2021-01 → 2021-03 | 22 | 54.5% | +2.22 ✓ | 4.1% | 1.59 |
| 2021-04 → 2021-06 | 19 | 52.6% | +1.59 ✓ | 3.2% | 1.41 |
| 2021-07 → 2021-09 | 20 | 55.0% | +2.15 ✓ | 2.2% | 1.56 |
| 2021-10 → 2021-12 | 21 | 57.1% | +2.43 ✓ | 3.2% | 1.67 |
| 2022-01 → 2022-03 | 15 | 53.3% | +1.51 ✓ | 2.2% | 1.46 |
| 2022-04 → 2022-06 | 22 | 63.6% | +3.59 ✓ | 2.2% | 2.18 |
| 2022-07 → 2022-09 | 17 | 64.7% | +3.34 ✓ | 2.2% | 2.24 |
| 2022-10 → 2022-12 | 21 | 42.9% | -0.71 ✗ | 5.2% | 0.85 |



### Inter-venster variantie

| Metriek | Gemiddeld | Min | Max | Std |
|---------|----------:|----:|----:|----:|
| Sharpe | +1.92 | -1.71 | +4.01 | 1.70 |
| Profit factor | +1.63 | +0.60 | +2.44 | 0.56 |
| Max drawdown | 3.8% | 2.2% | 9.6% | 0.02 |
| Win rate | 55.9% | 42.9% | 66.7% | 0.08 |



> **Lees dit zo:** Een hoge Std op Sharpe betekent dat de edge niet stabiel is over tijd. Streef naar Std < 0.5 bij Sharpe > 1.0.


---

## ETHUSDT

**Vensters:** 12  **Totaal trades:** 242  **Gem. trades/venster:** 20.2

**Verdict:** ✅ Robuuste edge


### Per-venster resultaten

| Venster | Trades | Win% | Sharpe | MDD | PF |
|---------|-------:|-----:|-------:|----:|----|
| 2020-01 → 2020-03 | 24 | 54.2% | +2.12 ✓ | 3.2% | 1.53 |
| 2020-04 → 2020-06 | 20 | 55.0% | +2.05 ✓ | 3.3% | 1.53 |
| 2020-07 → 2020-09 | 17 | 70.6% | +4.25 ✓ | 2.1% | 3.04 |
| 2020-10 → 2020-12 | 19 | 57.9% | +2.60 ✓ | 2.2% | 1.82 |
| 2021-01 → 2021-03 | 25 | 56.0% | +2.62 ✓ | 3.2% | 1.68 |
| 2021-04 → 2021-06 | 17 | 70.6% | +4.64 ✓ | 2.1% | 3.11 |
| 2021-07 → 2021-09 | 19 | 73.7% | +5.12 ✓ | 1.1% | 3.67 |
| 2021-10 → 2021-12 | 18 | 44.4% | +0.22 | 4.6% | 1.04 |
| 2022-01 → 2022-03 | 15 | 73.3% | +4.50 ✓ | 1.1% | 3.56 |
| 2022-04 → 2022-06 | 21 | 57.1% | +2.46 ✓ | 2.1% | 1.69 |
| 2022-07 → 2022-09 | 21 | 52.4% | +1.77 ✓ | 3.6% | 1.43 |
| 2022-10 → 2022-12 | 26 | 50.0% | +1.10 ✓ | 4.3% | 1.21 |



### Inter-venster variantie

| Metriek | Gemiddeld | Min | Max | Std |
|---------|----------:|----:|----:|----:|
| Sharpe | +2.79 | +0.22 | +5.12 | 1.52 |
| Profit factor | +2.11 | +1.04 | +3.67 | 0.95 |
| Max drawdown | 2.7% | 1.1% | 4.6% | 0.01 |
| Win rate | 59.6% | 44.4% | 73.7% | 0.10 |



> **Lees dit zo:** Een hoge Std op Sharpe betekent dat de edge niet stabiel is over tijd. Streef naar Std < 0.5 bij Sharpe > 1.0.


---

## Cross-coin signaalcorrelatie

Fractie van candles waarop twee coins *tegelijk* een open trade hadden t.o.v. alle candles waarop minstens één coin open was. Bij hoge overlap is het effectieve risico hoger dan de per-coin limieten suggereren.


| Paar | Overlap (beide open) | Interpretatie |
|------|---------------------:|---------------|
| BTCUSDT / ETHUSDT | 32.2% | Matig — gedeeltelijk gecorreleerd |
