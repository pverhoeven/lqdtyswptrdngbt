# BOS20 Setup — Volledige Documentatie

> **Datum:** 2026-05-03  
> **Scope:** Hoe de `bos20` filter-configuratie werkt van ruwe data tot signaal, backtest en live trading.

---

## 1. Concept

De `bos20` setup is een combinatie van twee Smart Money Concepts:

1. **Liquidity Sweep** — prijs raakt een bekende liquiditeitszone (swing high of swing low) en keert terug. De markt sweept de stops van retail traders.
2. **Break of Structure (BOS)** — na de sweep moet de prijs een structuurbreuk bevestigen in de sweep-richting (hogere high bij long, lagere low bij short) binnen **20 candles** (= 300 minuten bij 15m timeframe).

Pas als beide condities binnen het venster optreden, wordt er een signaal gegenereerd. Dit filtert veel valse sweeps eruit.

---

## 2. Data Flow

```
Binance 1m OHLC
       │
       │  scripts/fetch_binance.py
       ▼
data/raw/binance/{symbol}/
       │
       │  src/data/aggregator.py (upsample naar 15m, 4h)
       ▼
data/processed/{symbol}_15m.parquet
data/processed/{symbol}_4h.parquet
       │
       │  scripts/build_cache.py (SMC lib op 15m)
       ▼
data/smc_cache/{symbol}/15m/*.parquet
  kolommen: liq, liq_level, bos, choch, ob, ob_top, ob_bottom, atr, ...
       │
       │  src/backtest/sweep_engine.py / scripts/run_paper_trader.py
       ▼
SweepDetector.on_candle(ohlc_row, smc_row, regime)
       │  (interne state: _PendingSweep)
       ▼
SweepSignal  →  backtest engine / live order manager
```

---

## 3. SMC Cache Kolommen (relevantie voor bos20)

| Kolom | Type | Betekenis |
|---|---|---|
| `liq` | int | `-1` = sweep van swing low (long setup), `1` = sweep van swing high (short setup), `0` = geen sweep |
| `liq_level` | float | Prijsniveau van de gesweepte liquiditeit |
| `bos` | int | `1` = bullish BOS (sluit boven vorig swing high), `-1` = bearish BOS, `0` = geen |
| `atr` | float | Average True Range (14 periodes) |

De SMC cache wordt gebouwd met `swing_length=10` (config: `smc.swing_length`).

---

## 4. SweepFilters Dataclass

**Bestand:** `src/signals/filters.py`

```python
@dataclass
class SweepFilters:
    regime:             bool = False   # HMM regime-filter
    direction:          str  = "both"  # "long" | "short" | "both" | "dynamic"
    bos_confirm:        bool = False   # BOS-bevestiging vereist
    bos_window:         int  = 10      # Max candles om BOS te zoeken
    atr_filter:         bool = False   # Alleen bij hoge volatiliteit
    atr_window:         int  = 14      # Rolling window voor ATR-MA
    sweep_rejection:    bool = False   # Candle-kwaliteitsfilter
    pre_sweep_lookback: int  = 0       # Trendrichting-validatie
    micro_bos_tf:       str | None = None  # Lagere TF bevestiging
    micro_bos_window:   int  = 20          # Max lagere-TF candles
```

**Definitie van `bos20`:**
```python
SweepFilters(bos_confirm=True, bos_window=20)
```

De `__str__` methode geeft `"bos20"` terug. Dit label wordt gebruikt als `filter_str` in het signaal en in backtest-output.

---

## 5. SweepDetector — Signaallogica

**Bestand:** `src/signals/detector.py`

De detector verwerkt één gesloten candle tegelijk en houdt intern state bij.

### Initialisatie

```python
detector = SweepDetector(
    filters       = SweepFilters(bos_confirm=True, bos_window=20),
    reward_ratio  = 1.5,   # uit config: risk.reward_ratio
    sl_buffer_pct = 0.5,   # uit config: risk.sl_buffer_pct
)
```

### Per candle: `on_candle(ohlc_row, smc_row, regime)`

De methode volgt twee stappen:

**Stap 1 — controleer bestaande pending sweep:**
- Als er een `_PendingSweep` actief is (van een eerdere candle):
  - Controleer of `bos` overeenkomt met de verwachte richting
  - Als ja → genereer `SweepSignal` en wis pending state
  - Als het venster verlopen is (`candle_idx - created_idx > bos_window`) → wis pending state zonder signaal

**Stap 2 — detecteer nieuwe sweep:**
- Als `liq != 0`: sla `_PendingSweep` op en return `None` (bij `bos_confirm=True`)
- Filters worden in volgorde gecontroleerd: direction → regime → sweep_rejection → pre_sweep_lookback → atr_filter
- Als alle filters passeren → maak `_PendingSweep` aan

### _PendingSweep state

```python
@dataclass
class _PendingSweep:
    direction:   str          # "long" of "short"
    entry:       float        # Close van de sweep-candle
    liq_level:   float        # Gesweept liquiditeitsniveau
    sl_buf:      float        # SL-buffer als decimaal (0.005 = 0.5%)
    rr:          float        # Risk:reward ratio (1.5)
    regime:      bool | None  # HMM regime op moment van sweep
    filter_str:  str          # "bos20"
    created_idx: int          # Candle-index bij aanmaken
    bos_window:  int          # 20 voor bos20
```

**Verloopregel:** `candle_idx - created_idx > 20` → pending verwijderd.  
Maximale wachttijd: 20 × 15m = **300 minuten (5 uur)**.

---

## 6. SL/TP Berekening

**Bestand:** `src/signals/detector.py` — `_calc_sl_tp()`

Bij BOS-bevestiging wordt entry, SL en TP **opnieuw berekend** op de BOS-candle (niet de sweep-candle):

```
entry  = close van BOS-candle

SL (long):  liq_level × (1 - 0.005)   →  0.5% onder gesweept low
SL (short): liq_level × (1 + 0.005)   →  0.5% boven gesweept high

Als liq_level ongeldig of 0:
SL (long):  entry × (1 - 0.005)
SL (short): entry × (1 + 0.005)

sl_distance = |entry - sl|

TP (long):  entry + sl_distance × 1.5
TP (short): entry - sl_distance × 1.5
```

Parameters uit `config.yaml`:
- `risk.sl_buffer_pct`: `0.5`
- `risk.reward_ratio`: `1.5`

Minimale SL-afstand: `entry × 0.0001` — als de afstand kleiner is wordt het signaal afgewezen.

---

## 7. SweepSignal Output

**Bestand:** `src/signals/detector.py`

```python
@dataclass
class SweepSignal:
    timestamp:   pd.Timestamp  # Sluitingstijd van de BOS-candle
    direction:   str           # "long" of "short"
    entry_price: float         # Close van de BOS-candle
    sl_price:    float         # Berekend via liq_level ± buffer
    tp_price:    float         # entry ± sl_distance × 1.5
    liq_level:   float         # Gesweept prijsniveau
    regime:      bool | None   # True = bullish, False = bearish, None = onbekend
    filter_str:  str           # "bos20"
```

---

## 8. Backtest Integratie

**Bestand:** `src/backtest/sweep_engine.py`

### Data laden (`_load_data`)

1. **15m OHLC** uit `data/processed/{symbol}_15m.parquet`
2. **4h OHLC** uit `data/processed/{symbol}_4h.parquet`
3. **HMM regime model** uit `data/processed/hmm_regime_model.pkl` (wordt getraind als het niet bestaat)
4. **SMC cache** via `src/data/cache.load_cache()` — laadt de kwartaalbestanden voor de gevraagde periode
5. **200-daagse MA** — berekend over de volledige 15m dataset (`rolling(19200)`) en geherindexeerd op de backtest-periode

### Backtest loop (`_run_loop`)

Per 15m candle, in volgorde:

1. **Open positie bewaken** — check SL/TP hit op de huidige candle
2. **Pending limit order** — probeer te vullen als prijs de entry raakt
3. **Pending micro-BoS** — controleer lagere-TF candles binnen dit 15m venster (alleen bij `micro_bos_tf`)
4. **Detector aanroepen** — altijd, ook als er een open positie is (bewaart interne BOS-state)
5. **Signaal verwerken** — alleen als volledig vrij (geen open positie, geen pending)

### Positiebeheer

- **Maximaal 1 open positie** tegelijk (`risk.max_open_trades: 1`)
- **Positiegrootte:** `(capital × risk_pct) / sl_distance`
- **Kosten:** fee 0.1% + slippage 0.05% per kant

### `compare_filters()` — alle presets

```python
{
    "baseline":       SweepFilters(direction="both"),
    "regime":         SweepFilters(regime=True),
    "long_only":      SweepFilters(direction="long"),
    "short_only":     SweepFilters(direction="short"),
    "bos10":          SweepFilters(bos_confirm=True, bos_window=10),
    "bos20":          SweepFilters(bos_confirm=True, bos_window=20),
    "regime_long":    SweepFilters(regime=True, direction="long"),
    "regime_short":   SweepFilters(regime=True, direction="short"),
    "regime_bos10":   SweepFilters(regime=True, bos_confirm=True, bos_window=10),
    "long_bos10":     SweepFilters(direction="long",  bos_confirm=True, bos_window=10),
    "short_bos10":    SweepFilters(direction="short", bos_confirm=True, bos_window=10),
    "long_atr14":     SweepFilters(direction="long", atr_filter=True),
    "dynamic_200ma":  SweepFilters(direction="dynamic"),
    "micro_bos_3m":   SweepFilters(micro_bos_tf="3min", micro_bos_window=20),
    "micro_bos_5m":   SweepFilters(micro_bos_tf="5min", micro_bos_window=20),
    "long_micro_3m":  SweepFilters(direction="long",  micro_bos_tf="3min", micro_bos_window=20),
    "short_micro_3m": SweepFilters(direction="short", micro_bos_tf="3min", micro_bos_window=20),
}
```

---

## 9. Standaard Configuratie (config.yaml)

```yaml
filters:
  direction:   "both"   # long + short sweeps
  regime:      false    # geen HMM-filter
  bos_confirm: true     # BOS-bevestiging actief
  bos_window:  20       # max 20 candles = 5 uur
  atr_filter:  false
  atr_window:  14

risk:
  capital_initial:    10000   # USDT
  risk_per_trade_pct: 1.0     # 1% per trade
  reward_ratio:       1.5     # 1:1.5 R:R
  sl_buffer_pct:      0.5     # 0.5% voorbij liq-niveau
  max_open_trades:    1

smc:
  swing_length: 10            # lookback voor swing highs/lows
  lib_version:  "0.0.27"

data:
  timeframes:
    signal: "15min"
    regime: "4h"

split:
  in_sample_start:  "2017-08-01"
  in_sample_end:    "2022-12-31"
  oos_start:        "2023-01-01"
  oos_end:          "2024-12-31"
```

---

## 10. Live Trading (Paper Trader)

**Bestand:** `scripts/run_paper_trader.py`

Dezelfde `SweepDetector` als de backtest. Per afgesloten 15m candle:

```python
filters  = SweepFilters(bos_confirm=True, bos_window=20)
detector = SweepDetector(
    filters       = filters,
    reward_ratio  = cfg["risk"]["reward_ratio"],   # 1.5
    sl_buffer_pct = cfg["risk"]["sl_buffer_pct"],  # 0.5
)

signal = detector.on_candle(ohlc_row, smc_row, regime)
if signal:
    # → order manager → OKX API
```

De exchange is OKX met `BTC-USD_UM_XPERP-310404` (EU XPERP perpetual futures), leverage 5×, isolated margin.

---

## 11. Overige Filters (beschikbare opties)

| Filter | Parameter | Gedrag |
|---|---|---|
| `direction` | `"long"` / `"short"` / `"both"` / `"dynamic"` | `dynamic` = volgt 200-daagse MA: boven MA → long, onder MA → short |
| `regime` | `True` | Bullish HMM-regime → alleen longs; bearish → alleen shorts |
| `sweep_rejection` | `True` | Long: sweep-candle moet groen sluiten (wick rejecteert low). Short: rood sluiten. |
| `pre_sweep_lookback` | `N > 0` | Long: prijs moet N candles geleden hoger hebben gestaan (van boven gekomen). Short: vice versa. |
| `atr_filter` | `True` | Alleen traden als huidige ATR > ATR moving average (trending markt) |
| `micro_bos_tf` | `"3min"` / `"5min"` | Na sweep op 15m: wacht op BOS-bevestiging op lagere timeframe in plaats van 15m BOS |

Filters zijn cumulatief: `SweepFilters(regime=True, bos_confirm=True, bos_window=20)` geeft het label `"regime+bos20"`.

---

## 12. Scripts

## 14. Backtest Resultaten

> ⚠️ **Zie sectie 15 voor bekende beperkingen. De cijfers hieronder zijn waarschijnlijk opgeblazen door structurele problemen in de testopzet.**

### In-sample (2017-08-01 → 2022-12-31) — alle filters

| Configuratie | Trades | Win rate | Sharpe | Max DD | Profit factor | Total return |
|---|---|---|---|---|---|---|
| **bos20** | **626** | **56.9%** | **4.94** | **8.4%** | **3.50** | **2330.9%** |
| bos10 | 383 | 58.7% | 4.00 | 6.1% | 3.34 | 673.3% |
| short_bos10 | 190 | 63.7% | 3.59 | 4.0% | 4.65 | 262.5% |
| regime_bos10 | 193 | 56.0% | 2.51 | 5.8% | 2.74 | 147.9% |
| short_only | 1073 | 43.2% | 2.28 | 15.7% | 1.52 | 526.9% |
| baseline | 1347 | 43.1% | 2.22 | 19.2% | 1.44 | 649.8% |
| long_bos10 | 199 | 54.3% | 2.17 | 7.0% | 2.30 | 122.1% |
| dynamic_200ma | 1006 | 42.7% | 1.95 | 14.3% | 1.40 | 353.0% |
| regime | 1052 | 41.0% | 1.51 | 30.4% | 1.28 | 225.1% |
| long_only | 979 | 41.6% | 1.47 | 23.2% | 1.27 | 206.8% |
| long_atr14 | 333 | 39.3% | 0.58 | 18.1% | 1.16 | 28.6% |

### Out-of-sample (2023-01-01 → 2024-12-31) — bos20

| Metriek | Waarde |
|---|---|
| Trades | 215 |
| Win rate | 57.7% |
| Sharpe | 4.36 |
| Max drawdown | 4.5% |
| Profit factor | 2.51 |
| Total return | 169.5% |

---

## 15. Bekende Beperkingen (Look-ahead bias en overige)

De bovenstaande resultaten zijn **waarschijnlijk significant opgeblazen**. Drie structurele problemen:

### 15.1 Look-ahead bias in de SMC cache — ✅ Al gecorrigeerd

De SMC cache verschuift alle swing-afgeleide signalen al forward met `swing_length` rijen bij het bouwen (`src/data/cache.py:122`):

```python
signals[_smc_cols] = signals[_smc_cols].shift(swing_length)
```

Semantiek: een sweep die plaatsvond op candle T is pas detecteerbaar op candle T+10, omdat de swing-bevestiging 10 candles vergt. De backtest ziet het signaal pas op T+10. **Look-ahead bias is dus geen actief probleem.**

### 15.2 Entry op close van BOS-candle — ✅ Fix beschikbaar, minimale impact

Entry op close van BOS-candle vs. open van volgende candle: verschil is in de praktijk <0.3% op de resultaten. BTC 15m-candles openen dicht bij de vorige close.

**Instelling:** `backtest.next_open_entry: true` in `config.yaml` (default: `false`).

### 15.3 Funding rate — ✅ Geïmplementeerd, minimale impact

Funding wordt geaccumuleerd per overleefde candle: `entry_price × size × funding_rate_per_8h / 32`.

**Instelling:** `risk.funding_rate_per_8h: 0.0001` (0.01% per 8u, conservatief). Impact op backtest-resultaten: <1% verschil bij gemiddelde positieduur.

### 15.4 Gemeten impact van correcties (in-sample bos20)

| Configuratie | Trades | Sharpe | Profit factor | Total return |
|---|---|---|---|---|
| Baseline (entry op close, geen funding) | 626 | 4.94 | 3.50 | 2330.9% |
| + Funding 0.01%/8u + entry op next open | 627 | 4.97 | 3.41 | 2382.2% |

**Conclusie:** de gevreesde factoren (entry timing, funding) verklaren de hoge Sharpe niet. De prestatie is voor deze periode genuine. De werkelijke onzekerheid zit in de representativiteit van de periode (2017–2022 is uitzonderlijk voor BTC) en het gedrag in neutrale of langdurig zijwaartse markten.

### 15.5 Resterende onzekerheid

De Sharpe van ~5 is hoog maar niet per se vals: 2017–2022 bevat zowel de grootste bull run als twee zware bear markets, waardoor de BOS-strategie beide kanten kon bespelen. **OOS-Sharpe van 4.36** (2023–2024, ook een bull jaar) bevestigt dat het patroon generaliseert — maar beide periodes zijn bullish. Een neutrale/laterale markt is nog niet getest.

---

```bash
# Bouw/herbouw SMC cache (vereist na data-update of lib-versiewijziging)
python scripts/build_cache.py

# Backtest: één filter
python scripts/run_sweep_backtest.py --set in_sample --filter bos20

# Backtest: vergelijk alle filters
python scripts/run_sweep_backtest.py --set in_sample

# Out-of-sample backtest
python scripts/run_sweep_backtest.py --set oos --filter bos20 --allow-oos

# Walk-forward analyse (12m train / 3m test)
python scripts/run_walk_forward.py --filter bos20

# Paper trading (live OKX, testnet)
python scripts/run_paper_trader.py
```

---

## 13. Tijdlijn van een bos20 trade

```
Candle T+0  → liq = -1 (sweep van swing low)
               SweepDetector maakt _PendingSweep aan (direction=long, bos_window=20)
               → geen signaal

Candle T+1  → bos = 0 → pending blijft bestaan
Candle T+2  → bos = 0 → pending blijft bestaan
...
Candle T+N  → bos = 1 (bullish BOS)  [N ≤ 20]
               entry = close van candle T+N
               SL    = liq_level × 0.995
               TP    = entry + (entry - SL) × 1.5
               → SweepSignal(direction="long", filter_str="bos20")

Candle T+21 → bos = 0, N > 20 → pending verlopen, geen signaal
```
