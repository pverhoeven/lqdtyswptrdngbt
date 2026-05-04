# Plan: Look-ahead Bias Fix — Incrementele SMC Cache

> **Status: AFGEROND** — de causal shift (`signals.shift(swing_length)`) staat al in `src/data/cache.py:122`.
> Metingen tonen aan dat ook funding rate en next-open-entry een verwaarloosbare impact hebben (<1%).
> Dit document is historisch en hoeft niet geïmplementeerd te worden.

---

## Probleem

`build_cache.py` roept de SMC-library aan op de **volledige dataset**. Een swing low op candle T is alleen een swing low als T+1 t/m T+swing_length allemaal hogere lows hebben. De backtest "ziet" deze niveaus direct op candle T, terwijl ze in live trading pas op T+10 bevestigd zijn. Gevolg: alle signalen zijn gebaseerd op niveau-informatie die op dat moment nog niet bestond.

## Oplossing: causal shift op cache-niveau

Na het bouwen van de cache: verschuif elke `liq`- en `bos`-gebeurtenis **forward** met `swing_length` rijen.

**Semantiek:** "De sweep die *plaatsvond* op candle T is pas *detecteerbaar* op candle T+swing_length."

```
Huidige cache:   row T   → liq=-1, liq_level=49500
Causal cache:    row T+10 → liq=-1, liq_level=49500  (event van T, detectie op T+10)
```

De detector ontvangt het signaal 10 candles later. Het BOS-venster van 20 candles start dan ook 10 candles later. Entry wordt conservatiever maar realistischer.

## Implementatie

### Stap 1 — `scripts/build_cache.py`

Voeg flag `--causal` toe. Na het bouwen van het cache-parquet:

```python
def apply_causal_shift(df: pd.DataFrame, swing_length: int) -> pd.DataFrame:
    """
    Verschuif liq- en bos-kolommen forward met swing_length rijen.
    Prijs-kolommen (liq_level, structure_level, etc.) meeschuiven.
    """
    signal_cols  = ["liq", "bos", "choch"]
    payload_cols = ["liq_level", "liq_end_idx", "liq_swept_idx",
                    "structure_level", "structure_broken_idx"]

    df_out = df.copy()
    for col in signal_cols + payload_cols:
        if col in df.columns:
            df_out[col] = df[col].shift(swing_length)

    # Vul NaN's die door shift ontstaan
    for col in signal_cols:
        df_out[col] = df_out[col].fillna(0)

    return df_out
```

Gebruik: `python scripts/build_cache.py --causal`
Sla op als aparte submap: `data/smc_cache/{symbol}/15m_causal/`

### Stap 2 — `config.yaml`

```yaml
smc:
  swing_length: 10
  causal: false        # true = gebruik look-ahead-vrije cache (conservatief)
```

### Stap 3 — `src/data/cache.py` — `load_cache()`

```python
def load_cache(cfg, start=None, end=None):
    causal    = cfg.get("smc", {}).get("causal", False)
    subfolder = "15m_causal" if causal else "15m"
    cache_dir = Path(cfg["data"]["paths"]["smc_cache"].replace("/15m", f"/{subfolder}"))
    ...
```

### Stap 4 — Validatie

Run beide varianten naast elkaar:

```bash
# Standaard (met look-ahead)
python scripts/run_sweep_backtest.py --set in_sample --filter bos20

# Causal (zonder look-ahead)
# Zet smc.causal: true in config.yaml, rebuild cache, dan:
python scripts/run_sweep_backtest.py --set in_sample --filter bos20
```

Verwachte impact:
- Sharpe daalt van ~5 naar waarschijnlijk 0.8–1.5
- Win rate daalt van ~57% naar ~45–52%
- Trade count daalt (sommige sweeps zijn te oud om nog relevante BOS te triggeren)

## Wat dit NIET oplost

- Entry op close ipv open: al opgelost via `backtest.next_open_entry: true`
- Funding rate: apart te implementeren (zie hieronder)

## Funding Rate (bonus)

In `_close()` of als aparte cost per open candle:

```python
# In _run_loop, bij elke candle dat positie open is:
if open_pos is not None:
    funding_cost = (
        open_pos.entry_price * open_pos.size
        * funding_rate_per_candle   # bijv. 0.0001/32 per 15m candle (0.01% per 8u / 32 candles)
    )
    capital -= funding_cost
```

Config:
```yaml
risk:
  funding_rate_per_8h: 0.0001   # 0.01% per 8u (conservatieve schatting)
```

## Verwacht resultaat na alle fixes

| Metriek | Nu | Na fixes |
|---|---|---|
| Sharpe | 4.94 | 0.8–1.5 |
| Win rate | 56.9% | 45–52% |
| Max DD | 8.4% | 12–20% |
| Trades | 626 | 400–550 (minder door latere detectie) |
