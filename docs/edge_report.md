# Edge-validatierapport

Gegenereerd: 2026-05-02 13:48 UTC  
Periode: 2017-08-01 → 2022-12-31  
Vensters: train=12m / test=3m  
Filters: bos20

---

## BTCUSDT

**Vensters:** 17  **Totaal trades:** 475  **Gem. trades/venster:** 27.9

**Verdict:** ✅ Robuuste edge


### Per-venster resultaten

| Venster | Trades | Win% | Sharpe | MDD | PF |
|---------|-------:|-----:|-------:|----:|----|
| 2018-08 → 2018-10 | 25 | 56.0% | +2.01 ✓ | 7.6% | 1.42 |
| 2018-11 → 2019-01 | 21 | 66.7% | +3.69 ✓ | 4.7% | 2.26 |
| 2019-02 → 2019-04 | 26 | 50.0% | +0.11 | 5.4% | 1.01 |
| 2019-05 → 2019-07 | 23 | 78.3% | +6.42 ✓ | 2.1% | 4.72 |
| 2019-08 → 2019-10 | 33 | 66.7% | +4.48 ✓ | 4.7% | 2.19 |
| 2019-11 → 2020-01 | 22 | 63.6% | +3.04 ✓ | 4.6% | 1.91 |
| 2020-02 → 2020-04 | 28 | 71.4% | +5.62 ✓ | 2.3% | 2.90 |
| 2020-05 → 2020-07 | 26 | 65.4% | +3.78 ✓ | 3.8% | 2.09 |
| 2020-08 → 2020-10 | 21 | 42.9% | -0.93 ✗ | 6.3% | 0.81 |
| 2020-11 → 2021-01 | 26 | 61.5% | +3.40 ✓ | 2.3% | 1.96 |
| 2021-02 → 2021-04 | 28 | 82.1% | +7.68 ✓ | 2.4% | 5.19 |
| 2021-05 → 2021-07 | 34 | 82.4% | +8.51 ✓ | 2.3% | 5.52 |
| 2021-08 → 2021-10 | 31 | 64.5% | +4.38 ✓ | 2.3% | 2.25 |
| 2021-11 → 2022-01 | 35 | 77.1% | +7.23 ✓ | 3.3% | 4.15 |
| 2022-02 → 2022-04 | 22 | 68.2% | +4.12 ✓ | 3.4% | 2.48 |
| 2022-05 → 2022-07 | 35 | 88.6% | +9.63 ✓ | 1.2% | 9.07 |
| 2022-08 → 2022-10 | 39 | 76.9% | +7.19 ✓ | 4.5% | 3.49 |



### Inter-venster variantie

| Metriek | Gemiddeld | Min | Max | Std |
|---------|----------:|----:|----:|----:|
| Sharpe | +4.73 | -0.93 | +9.63 | 2.86 |
| Profit factor | +3.14 | +0.81 | +9.07 | 2.07 |
| Max drawdown | 3.7% | 1.2% | 7.6% | 0.02 |
| Win rate | 68.4% | 42.9% | 88.6% | 0.12 |



> **Lees dit zo:** Een hoge Std op Sharpe betekent dat de edge niet stabiel is over tijd. Streef naar Std < 0.5 bij Sharpe > 1.0.


---

## Cross-coin signaalcorrelatie

Fractie van candles waarop twee coins *tegelijk* een open trade hadden t.o.v. alle candles waarop minstens één coin open was. Bij hoge overlap is het effectieve risico hoger dan de per-coin limieten suggereren.


_Slechts één symbool beschikbaar — correlatieanalyse niet van toepassing._
