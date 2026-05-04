"""
tests/test_sweep_detector.py — Unit tests voor SweepDetector.

Critical: de detector is de gedeelde kern van backtest én live trading.
Een bug hier produceert verkeerde signaalrichting, verkeerde SL/TP-prijzen,
of mist BOS-bevestiging waardoor te vroeg ingestapt wordt.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.signals.detector import SweepDetector
from src.signals.filters import SweepFilters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ohlc(close=50_000.0, open_p=49_900.0, high=50_100.0, low=49_800.0,
          ts="2024-01-01 00:00") -> pd.Series:
    row = pd.Series({"open": open_p, "high": high, "low": low, "close": close})
    row.name = pd.Timestamp(ts, tz="UTC")
    return row


def _smc(liq=0.0, liq_level=float("nan"), bos=0.0, atr=500.0) -> pd.Series:
    return pd.Series({"liq": liq, "liq_level": liq_level, "bos": bos, "atr": atr,
                      "ob": 0.0, "choch": 0.0})


def _detector(**filter_kw) -> SweepDetector:
    return SweepDetector(
        filters=SweepFilters(**filter_kw),
        reward_ratio=1.5,
        sl_buffer_pct=0.5,
    )


# ---------------------------------------------------------------------------
# Basisdetectie
# ---------------------------------------------------------------------------

def test_long_sweep_returns_long_signal():
    # liq=-1: low gesweept → verwacht long setup
    signal = _detector(bos_confirm=False).on_candle(
        _ohlc(close=50_000), _smc(liq=-1, liq_level=49_000)
    )
    assert signal is not None
    assert signal.direction == "long"


def test_short_sweep_returns_short_signal():
    # liq=1: high gesweept → verwacht short setup
    signal = _detector(bos_confirm=False).on_candle(
        _ohlc(close=50_000), _smc(liq=1, liq_level=51_000)
    )
    assert signal is not None
    assert signal.direction == "short"


def test_no_sweep_returns_none():
    signal = _detector().on_candle(_ohlc(), _smc(liq=0))
    assert signal is None


# ---------------------------------------------------------------------------
# SL / TP richting
# ---------------------------------------------------------------------------

def test_long_sl_below_entry_tp_above_entry():
    signal = _detector(bos_confirm=False).on_candle(
        _ohlc(close=50_000), _smc(liq=-1, liq_level=49_000)
    )
    assert signal.sl_price < signal.entry_price
    assert signal.tp_price > signal.entry_price


def test_short_sl_above_entry_tp_below_entry():
    signal = _detector(bos_confirm=False).on_candle(
        _ohlc(close=50_000), _smc(liq=1, liq_level=51_000)
    )
    assert signal.sl_price > signal.entry_price
    assert signal.tp_price < signal.entry_price


def test_risk_reward_matches_configured_ratio():
    rr = 1.5
    det = SweepDetector(SweepFilters(bos_confirm=False), reward_ratio=rr, sl_buffer_pct=0.5)
    signal = det.on_candle(_ohlc(close=50_000), _smc(liq=-1, liq_level=49_000))
    assert signal.risk_reward == pytest.approx(rr, rel=1e-3)


# ---------------------------------------------------------------------------
# BOS-bevestiging
# ---------------------------------------------------------------------------

def test_bos_confirm_no_immediate_signal_on_sweep():
    det = _detector(bos_confirm=True, bos_window=5)
    signal = det.on_candle(
        _ohlc(ts="2024-01-01 00:00"), _smc(liq=-1, liq_level=49_000)
    )
    assert signal is None


def test_bos_confirm_fires_on_correct_bos():
    det = _detector(bos_confirm=True, bos_window=5)
    det.on_candle(_ohlc(ts="2024-01-01 00:00"), _smc(liq=-1, liq_level=49_000))
    det.on_candle(_ohlc(ts="2024-01-01 00:15"), _smc(bos=0))
    # bos=1 = bullish BOS → bevestigt long setup
    signal = det.on_candle(_ohlc(ts="2024-01-01 00:30"), _smc(bos=1))
    assert signal is not None
    assert signal.direction == "long"


def test_bos_confirm_wrong_direction_gives_no_signal():
    det = _detector(bos_confirm=True, bos_window=5)
    det.on_candle(_ohlc(ts="2024-01-01 00:00"), _smc(liq=-1, liq_level=49_000))
    # bos=-1 = bearish BOS na long sweep → geen signaal
    signal = det.on_candle(_ohlc(ts="2024-01-01 00:15"), _smc(bos=-1))
    assert signal is None


def test_bos_confirm_window_expires_no_late_signal():
    det = _detector(bos_confirm=True, bos_window=2)
    det.on_candle(_ohlc(ts="2024-01-01 00:00"), _smc(liq=-1, liq_level=49_000))
    det.on_candle(_ohlc(ts="2024-01-01 00:15"), _smc(bos=0))
    det.on_candle(_ohlc(ts="2024-01-01 00:30"), _smc(bos=0))
    # Venster verlopen — een BOS nu mag geen signaal meer geven
    signal = det.on_candle(_ohlc(ts="2024-01-01 00:45"), _smc(bos=1))
    assert signal is None


# ---------------------------------------------------------------------------
# Richtingsfilters
# ---------------------------------------------------------------------------

def test_long_only_filter_blocks_short_sweep():
    det = _detector(direction="long", bos_confirm=False)
    signal = det.on_candle(_ohlc(), _smc(liq=1, liq_level=51_000))
    assert signal is None


def test_short_only_filter_blocks_long_sweep():
    det = _detector(direction="short", bos_confirm=False)
    signal = det.on_candle(_ohlc(), _smc(liq=-1, liq_level=49_000))
    assert signal is None


def test_both_direction_passes_long_and_short():
    det = _detector(direction="both", bos_confirm=False)
    s_long  = det.on_candle(_ohlc(ts="2024-01-01 00:00"), _smc(liq=-1, liq_level=49_000))
    det.reset()
    s_short = det.on_candle(_ohlc(ts="2024-01-01 00:15"), _smc(liq=1,  liq_level=51_000))
    assert s_long  is not None
    assert s_short is not None
