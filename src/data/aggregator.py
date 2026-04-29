"""
aggregator.py — Upsampling van 1m naar hogere timeframes (15m, 4h).

Gedrag:
- Leest alle jaarlijkse 1m parquet-bestanden uit data/raw/
- Herberekent 15m en 4h lokaal via pandas resample
- Schrijft naar data/processed/BTCUSDT_15m.parquet en BTCUSDT_4h.parquet
- Voert een verplichte validatietest uit voor wegschrijven:
    high van willekeurige hogere-timeframe candle == max(high van onderliggende 1m candles)
- Incremental: als processed-bestand bestaat, alleen nieuwe data toevoegen
"""

import logging
import random
from pathlib import Path

import pandas as pd

from src.config_loader import load_config

logger = logging.getLogger(__name__)

_RESAMPLE_RULES = {
    "open":   "first",
    "high":   "max",
    "low":    "min",
    "close":  "last",
    "volume": "sum",
}

# Minimaal aantal candles dat aanwezig moet zijn voor validatie
_VALIDATION_SAMPLES = 20


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def aggregate(cfg: dict | None = None, symbol: str | None = None) -> None:
    """
    Lees 1m parquet-bestanden en schrijf 15m en 4h parquet weg.

    Parameters
    ----------
    cfg : dict, optional
        Geladen config dict. Als None: laad automatisch.
    symbol : str, optional
        Symbool override (bijv. "ETHUSDT"). Standaard: cfg["data"]["symbol"].
    """
    if cfg is None:
        cfg = load_config()

    symbol        = symbol or cfg["data"]["symbol"]
    raw_dir       = Path(cfg["data"]["paths"]["raw"].format(symbol=symbol))
    processed_dir = Path(cfg["data"]["paths"]["processed"])
    processed_dir.mkdir(parents=True, exist_ok=True)
    tf_signal = cfg["data"]["timeframes"]["signal"]   # "15min"
    tf_regime = cfg["data"]["timeframes"]["regime"]   # "4h"

    logger.info("Laden van 1m data uit %s …", raw_dir)
    df_1m = _load_1m(raw_dir, symbol)

    if df_1m.empty:
        raise RuntimeError(
            f"Geen 1m data gevonden in {raw_dir}. "
            "Voer eerst scripts/download_data.py uit."
        )

    logger.info("Geladen: %d candles (1m), %s → %s",
                len(df_1m), df_1m.index[0].date(), df_1m.index[-1].date())

    for label, rule in [(tf_signal, tf_signal), (tf_regime, tf_regime)]:
        friendly = label.replace("min", "m").replace("h", "h")
        out_path = processed_dir / f"{symbol}_{friendly}.parquet"

        # Incrementeel: bepaal vanaf wanneer we moeten herberekenen
        df_1m_slice = _get_new_slice(df_1m, out_path, rule)

        if df_1m_slice.empty:
            logger.info("%s is al up-to-date.", out_path.name)
            continue

        logger.info("Upsamplen naar %s (%d 1m candles) …", label, len(df_1m_slice))
        df_resampled = _resample(df_1m_slice, rule)

        # Verplichte validatietest
        logger.info("Validatietest voor %s …", label)
        _validate(df_1m_slice, df_resampled, rule, samples=_VALIDATION_SAMPLES)
        logger.info("Validatietest geslaagd ✓")

        # Merge met bestaand bestand (incrementeel)
        df_resampled = _merge_with_existing(df_resampled, out_path)

        # Atomisch wegschrijven
        tmp = out_path.with_suffix(".tmp.parquet")
        df_resampled.to_parquet(tmp)
        tmp.replace(out_path)
        logger.info("Geschreven: %s (%d candles)", out_path.name, len(df_resampled))


# ---------------------------------------------------------------------------
# Laden
# ---------------------------------------------------------------------------

def _load_1m(raw_dir: Path, symbol: str) -> pd.DataFrame:
    """Laad alle jaarlijkse parquet-bestanden en concateneer."""
    files = sorted(raw_dir.glob(f"{symbol}_1m_*.parquet"))
    if not files:
        return pd.DataFrame()

    parts = [pd.read_parquet(f) for f in files]
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()
    return df


def _get_new_slice(
    df_1m: pd.DataFrame,
    out_path: Path,
    resample_rule: str,
) -> pd.DataFrame:
    """
    Geef het deel van df_1m terug dat nog niet in out_path verwerkt is.
    Voeg overlap toe van één hogere-timeframe candle zodat de laatste
    incomplete candle correct herberekend wordt.
    """
    if not out_path.exists():
        return df_1m

    existing = pd.read_parquet(out_path)
    if existing.empty:
        return df_1m

    last_processed = existing.index[-1]

    # Overlap: één candle-duur terug om grensgevallen correct te verwerken
    overlap_offset = pd.tseries.frequencies.to_offset(resample_rule)
    start = last_processed - overlap_offset

    return df_1m[df_1m.index >= start]


# ---------------------------------------------------------------------------
# Resample
# ---------------------------------------------------------------------------

def _resample(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Resample 1m DataFrame naar hogere timeframe.
    Sluit onvolledige laatste candle uit (label='left', closed='left').
    """
    df = (
        df_1m
        .resample(rule, label="left", closed="left")
        .agg(_RESAMPLE_RULES)
        .dropna()
    )
    # Laatste candle is mogelijk incompleet — verwijder als deze in de toekomst ligt
    now = pd.Timestamp.utcnow().tz_localize(None)
    if df.index[-1].tz is not None:
        now = pd.Timestamp.utcnow()
    df = df[df.index <= now]
    return df


# ---------------------------------------------------------------------------
# Validatie
# ---------------------------------------------------------------------------

def _validate(
    df_1m: pd.DataFrame,
    df_higher: pd.DataFrame,
    rule: str,
    samples: int = 20,
) -> None:
    """
    Verplichte validatietest:
    high van willekeurige hogere-timeframe candle == max(high van onderliggende 1m candles).

    Gooit ValueError als de validatie mislukt.
    """
    if len(df_higher) < samples:
        samples = len(df_higher)

    if samples == 0:
        raise ValueError("Geen hogere-timeframe candles om te valideren.")

    offset = pd.tseries.frequencies.to_offset(rule)
    indices = random.sample(range(len(df_higher)), samples)

    for idx in indices:
        candle_start = df_higher.index[idx]
        candle_end   = candle_start + offset

        # Selecteer onderliggende 1m candles
        mask = (df_1m.index >= candle_start) & (df_1m.index < candle_end)
        underlying = df_1m.loc[mask]

        if underlying.empty:
            continue

        expected_high = float(underlying["high"].max())
        actual_high   = float(df_higher.iloc[idx]["high"])

        if not _approx_equal(expected_high, actual_high):
            raise ValueError(
                f"Validatie mislukt voor {rule} candle op {candle_start}: "
                f"verwacht high={expected_high:.6f}, "
                f"gevonden high={actual_high:.6f}"
            )

        expected_low = float(underlying["low"].min())
        actual_low   = float(df_higher.iloc[idx]["low"])

        if not _approx_equal(expected_low, actual_low):
            raise ValueError(
                f"Validatie mislukt voor {rule} candle op {candle_start}: "
                f"verwacht low={expected_low:.6f}, "
                f"gevonden low={actual_low:.6f}"
            )


def _approx_equal(a: float, b: float, rel_tol: float = 1e-4) -> bool:
    """Relatieve tolerantie voor float32-afronding."""
    if a == b:
        return True
    return abs(a - b) / max(abs(a), abs(b), 1e-10) < rel_tol


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _merge_with_existing(df_new: pd.DataFrame, out_path: Path) -> pd.DataFrame:
    """Merge nieuwe candles met bestaand parquet-bestand (dedupliceer op index)."""
    if not out_path.exists():
        return df_new

    existing = pd.read_parquet(out_path)
    combined = pd.concat([existing, df_new])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined = combined.sort_index()
    return combined
