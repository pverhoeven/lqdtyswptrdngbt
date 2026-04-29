"""
signals.py — Wrapper rond de smartmoneyconcepts library.

Verantwoordelijkheid:
- Roept smc.ob(), smc.liquidity(), smc.bos_choch(), smc.atr() aan
- Geeft één DataFrame terug met alle SMC-kolommen per candle (15m index)
- Bevat geen caching-logica (dat zit in cache.py)

SMC library API (versie 0.0.27):
  De functies ob(), liquidity() en bos_choch() vereisen een vooraf berekende
  swing_highs_lows DataFrame als tweede argument:

  swing_highs_lows = smc.swing_highs_lows(ohlc, swing_length=50)
    → DataFrame met kolommen: HighLow (1=high, -1=low), Level

  smc.ob(ohlc, swing_highs_lows, close_mitigation=False)
    → DataFrame met kolommen: OB, Top, Bottom, OBVolume, Percentage, MitigatedIndex

  smc.liquidity(ohlc, swing_highs_lows, range_percent=0.01)
    → DataFrame met kolommen: Liquidity, Level, End, Swept

  smc.bos_choch(ohlc, swing_highs_lows, close_break=True)
    → DataFrame met kolommen: BOS, CHOCH, Level, BrokenIndex


Als de library een andere kolomnaam gebruikt, pas _COLUMN_MAP aan in plaats
van de rest van de code te wijzigen.
"""

import logging
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kolomnamen-mapping: SMC library output → interne namen
# Pas hier aan als de library-versie andere namen gebruikt.
# ---------------------------------------------------------------------------
_OB_MAP = {
    "OB":            "ob",
    "Top":           "ob_top",
    "Bottom":        "ob_bottom",
    "Percentage":    "ob_pct",
    "MitigatedIndex":"ob_mitigated_idx",
}

_LIQ_MAP = {
    "Liquidity": "liq",
    "Level":     "liq_level",
    "End":       "liq_end_idx",
    "Swept":     "liq_swept_idx",
}

_BOSCHOCH_MAP = {
    "BOS":         "bos",
    "CHOCH":       "choch",
    "Level":       "structure_level",
    "BrokenIndex": "structure_broken_idx",
}


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def compute_signals(ohlc: pd.DataFrame, swing_length: int = 50) -> pd.DataFrame:
    """
    Bereken alle SMC-signalen voor een OHLCV DataFrame.

    Parameters
    ----------
    ohlc : pd.DataFrame
        DataFrame met kolommen open, high, low, close, volume en een DatetimeIndex.
    swing_length : int
        Lookback voor swing highs/lows (uit config: smc.swing_length).

    Returns
    -------
    pd.DataFrame
        Zelfde index als ohlc, met alle SMC-signaalkolommen + atr.
        Zie _EXPECTED_OUTPUT_COLUMNS voor de volledige lijst.
    """
    try:
        from smartmoneyconcepts import smc  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "smartmoneyconcepts niet geïnstalleerd. "
            "Voer uit: pip install smartmoneyconcepts==0.0.27"
        ) from exc

    result = pd.DataFrame(index=ohlc.index)

    # --- Swing highs/lows (verplicht als input voor ob, liquidity, bos_choch in 0.0.27) ---
    swing_hl = smc.swing_highs_lows(ohlc, swing_length=swing_length)

    # --- Order Blocks ---
    ob_raw = smc.ob(ohlc, swing_hl, close_mitigation=False)
    result = _merge_mapped(result, ob_raw, _OB_MAP)

    # --- Liquidity ---
    liq_raw = smc.liquidity(ohlc, swing_hl, range_percent=0.01)
    result = _merge_mapped(result, liq_raw, _LIQ_MAP)

    # --- BOS / CHoCH ---
    boschoch_raw = smc.bos_choch(ohlc, swing_hl, close_break=True)
    result = _merge_mapped(result, boschoch_raw, _BOSCHOCH_MAP)

    # --- ATR (Wilder's smoothing, smc library has no atr method) ---
    high = ohlc["high"]
    low = ohlc["low"]
    close_prev = ohlc["close"].shift(1)
    tr = pd.concat([high - low, (high - close_prev).abs(), (low - close_prev).abs()], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    result["atr"] = atr_series.values

    _validate_output(result)
    return result


def get_expected_columns() -> list[str]:
    """Geef de lijst van verwachte output-kolommen terug."""
    return list(_EXPECTED_OUTPUT_COLUMNS)


# ---------------------------------------------------------------------------
# Interne helpers
# ---------------------------------------------------------------------------

def _merge_mapped(
    result: pd.DataFrame,
    raw: pd.DataFrame,
    column_map: dict[str, str],
) -> pd.DataFrame:
    """
    Hernoem kolommen van raw via column_map en voeg toe aan result.
    Ontbrekende kolommen worden als NaN toegevoegd met een waarschuwing.
    """
    if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
        for internal_name in column_map.values():
            result[internal_name] = np.nan
        return result

    for lib_name, internal_name in column_map.items():
        if lib_name in raw.columns:
            result[internal_name] = raw[lib_name].values
        else:
            logger.warning(
                "SMC library kolom '%s' niet gevonden — wordt NaN. "
                "Controleer library-versie of pas _COLUMN_MAP aan.",
                lib_name,
            )
            result[internal_name] = np.nan

    return result


# Verwachte output-kolommen (voor validatie en cache-schema)
_EXPECTED_OUTPUT_COLUMNS = (
    # Order blocks
    "ob", "ob_top", "ob_bottom", "ob_pct", "ob_mitigated_idx",
    # Liquidity
    "liq", "liq_level", "liq_end_idx", "liq_swept_idx",
    # BOS / CHoCH
    "bos", "choch", "structure_level", "structure_broken_idx",
    # ATR
    "atr",
)


def _validate_output(df: pd.DataFrame) -> None:
    """Controleer dat alle verwachte kolommen aanwezig zijn."""
    missing = [c for c in _EXPECTED_OUTPUT_COLUMNS if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"SMC output mist kolommen: {missing}. "
            "Controleer de library-versie en _COLUMN_MAP."
        )
