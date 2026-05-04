"""
tests/test_causal_shift.py — Verifieert de causal-shift invariant in build_cache.

Critical: als de shift ontbreekt of ATR mee verschuift, backtested de engine met
lookahead bias. Een backtest met lookahead bias is nutteloos — hij test kennis
van de toekomst, niet een handelsstrategie.

De test gebruikt tmp_path en patch zodat er geen data-bestanden nodig zijn.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.data.cache import build_cache, load_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SWING_LENGTH = 3
N_CANDLES    = 20
LIQ_POS      = 5     # positie van het nep-liquidity signaal
LIQ_LEVEL    = 51_000.0


def _fake_ohlc(idx: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({
        "open":  50_000.0, "high": 50_100.0,
        "low":   49_900.0, "close": 50_050.0,
    }, index=idx)


def _fake_signals(idx: pd.DatetimeIndex) -> pd.DataFrame:
    """Signals DataFrame met één liq-signaal op LIQ_POS; ATR varieert per rij."""
    liq   = np.zeros(len(idx))
    liq[LIQ_POS] = 1.0
    level = np.full(len(idx), np.nan)
    level[LIQ_POS] = LIQ_LEVEL

    return pd.DataFrame({
        "liq":       liq,
        "liq_level": level,
        "bos":       0.0,
        "atr":       [100.0 + i for i in range(len(idx))],
    }, index=idx)


def _cfg(tmp_path) -> dict:
    return {
        "data": {
            "paths": {
                "smc_cache": str(tmp_path / "cache" / "{symbol}" / "15m"),
                "processed": str(tmp_path / "processed"),
            },
            "symbol": "BTCUSDT",
            "timeframes": {"signal": "15min"},
        },
        "smc": {
            "swing_length": SWING_LENGTH,
            "lib_version":  "test_v1",
            "causal_shift": True,
        },
    }


def _build_and_load(tmp_path, causal_shift: bool) -> pd.DataFrame:
    idx  = pd.date_range("2024-01-01", periods=N_CANDLES, freq="15min", tz="UTC")
    ohlc = _fake_ohlc(idx)
    sigs = _fake_signals(idx)

    cfg = _cfg(tmp_path)
    cfg["smc"]["causal_shift"] = causal_shift

    processed = tmp_path / "processed"
    processed.mkdir(exist_ok=True)
    ohlc.to_parquet(processed / "BTCUSDT_15m.parquet")

    with patch("src.data.cache.compute_signals", return_value=sigs.copy()):
        build_cache(cfg, force=True)

    return load_cache(cfg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_smc_columns_shifted_by_swing_length(tmp_path):
    result = _build_and_load(tmp_path, causal_shift=True)

    # liq-signaal staat nu op positie LIQ_POS + SWING_LENGTH
    shifted_pos = LIQ_POS + SWING_LENGTH
    assert result["liq"].iloc[shifted_pos] == pytest.approx(1.0), (
        f"liq-signaal verwacht op positie {shifted_pos}, "
        f"maar: {result['liq'].tolist()}"
    )
    # Originele positie is 0 of NaN (niet langer het signaal)
    original_val = result["liq"].iloc[LIQ_POS]
    assert original_val == pytest.approx(0.0) or pd.isna(original_val)


def test_liq_level_shifts_with_liq(tmp_path):
    result = _build_and_load(tmp_path, causal_shift=True)

    shifted_pos = LIQ_POS + SWING_LENGTH
    assert result["liq_level"].iloc[shifted_pos] == pytest.approx(LIQ_LEVEL)


def test_atr_not_shifted(tmp_path):
    result = _build_and_load(tmp_path, causal_shift=True)

    # ATR-waarden moeten exact overeenkomen met de originele waarden
    for i in range(N_CANDLES):
        expected_atr = 100.0 + i
        assert result["atr"].iloc[i] == pytest.approx(expected_atr), (
            f"ATR op positie {i}: verwacht {expected_atr}, kreeg {result['atr'].iloc[i]}"
        )


def test_no_shift_when_causal_shift_false(tmp_path):
    result = _build_and_load(tmp_path, causal_shift=False)

    # Zonder shift staat het signaal nog op de originele positie
    assert result["liq"].iloc[LIQ_POS] == pytest.approx(1.0)
    # En níet op de verschoven positie
    shifted_pos = LIQ_POS + SWING_LENGTH
    val = result["liq"].iloc[shifted_pos]
    assert val == pytest.approx(0.0) or pd.isna(val)
