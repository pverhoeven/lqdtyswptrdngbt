"""
tests/test_backtest_position.py — Unit tests voor _Position.check() in sweep_engine.

Critical: _Position.check() bepaalt of een backtest-trade wint of verliest en of de
trailing SL correct verschuift. Een bug hier produceert een onbetrouwbare equity curve.

NB: PaperBroker heeft een aparte trailing-implementatie (test_paper_broker_trailing.py).
Deze tests dekken de backtest-engine-specifieke implementatie in sweep_engine._Position.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.backtest.sweep_engine import _Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(low: float, high: float) -> pd.Series:
    mid = (low + high) / 2
    return pd.Series({"open": mid, "high": high, "low": low, "close": mid})


def _long(entry=50_000.0, sl=49_000.0, tp=51_500.0, **kw) -> _Position:
    return _Position(
        direction="long", entry_price=entry, sl_price=sl, tp_price=tp,
        size=0.1, entry_ts=pd.Timestamp("2024-01-01", tz="UTC"), regime=None, **kw,
    )


def _short(entry=50_000.0, sl=51_000.0, tp=48_500.0, **kw) -> _Position:
    return _Position(
        direction="short", entry_price=entry, sl_price=sl, tp_price=tp,
        size=0.1, entry_ts=pd.Timestamp("2024-01-01", tz="UTC"), regime=None, **kw,
    )


TRAIL = {"enabled": True, "breakeven_at_r": 1.0, "trail_after_r": 2.0, "trail_step_r": 0.5}


# ---------------------------------------------------------------------------
# SL / TP basisgedrag
# ---------------------------------------------------------------------------

def test_long_tp_hit_returns_win():
    assert _long(entry=50_000, sl=49_000, tp=51_500).check(_row(50_100, 51_600)) == "win"


def test_long_sl_hit_returns_loss():
    assert _long(entry=50_000, sl=49_000, tp=51_500).check(_row(48_900, 50_100)) == "loss"


def test_long_no_hit_returns_none():
    assert _long(entry=50_000, sl=49_000, tp=51_500).check(_row(49_500, 51_000)) is None


def test_short_tp_hit_returns_win():
    assert _short(entry=50_000, sl=51_000, tp=48_500).check(_row(48_400, 49_900)) == "win"


def test_short_sl_hit_returns_loss():
    assert _short(entry=50_000, sl=51_000, tp=48_500).check(_row(49_800, 51_100)) == "loss"


def test_short_no_hit_returns_none():
    assert _short(entry=50_000, sl=51_000, tp=48_500).check(_row(49_000, 50_500)) is None


def test_long_sl_checked_before_tp_same_candle():
    # Low raakt SL én high raakt TP op dezelfde candle → engine checkt SL eerst
    pos = _long(entry=50_000, sl=49_000, tp=51_500)
    assert pos.check(_row(low=48_000, high=52_000)) == "loss"


# ---------------------------------------------------------------------------
# Trailing stop — breakeven
# ---------------------------------------------------------------------------

def test_long_breakeven_moves_sl_to_entry():
    pos = _long(entry=50_000, sl=49_000, tp=52_000, trailing_cfg=TRAIL)
    # sl_dist = 1000; breakeven_at_r=1.0 → trigger bij 51_000
    pos.check(_row(low=50_500, high=51_100))
    assert pos.sl_price == pytest.approx(50_000.0)


def test_long_below_breakeven_sl_stays():
    pos = _long(entry=50_000, sl=49_000, tp=52_000, trailing_cfg=TRAIL)
    pos.check(_row(low=50_100, high=50_800))  # high < 51_000 → geen BE
    assert pos.sl_price == pytest.approx(49_000.0)


def test_short_breakeven_moves_sl_to_entry():
    pos = _short(entry=50_000, sl=51_000, tp=47_000, trailing_cfg=TRAIL)
    # sl_dist = 1000; BE-trigger bij 49_000
    pos.check(_row(low=48_900, high=49_500))
    assert pos.sl_price == pytest.approx(50_000.0)


# ---------------------------------------------------------------------------
# Trailing stop — trailing fase
# ---------------------------------------------------------------------------

def test_long_trailing_raises_sl_past_2r():
    pos = _long(entry=50_000, sl=49_000, tp=53_000, trailing_cfg=TRAIL)
    # sl_dist=1000; trail_after_r=2.0, step=500 → eerste step vereist ideal ≥ sl+500
    # ideal = peak - 2*sl_dist → peak moet ≥ 50_000 + 2*1000 + 500 = 52_500
    pos.check(_row(low=51_000, high=52_600))
    assert pos.sl_price > 50_000.0  # SL voorbij breakeven


def test_long_trailing_sl_never_moves_backward():
    pos = _long(entry=50_000, sl=49_000, tp=53_000, trailing_cfg=TRAIL)
    pos.check(_row(low=51_000, high=52_600))  # SL beweegt omhoog
    sl_na_stijging = pos.sl_price
    assert sl_na_stijging > 50_000.0
    pos.check(_row(low=51_000, high=51_200))  # geen nieuw high → SL mag niet zakken
    assert pos.sl_price == pytest.approx(sl_na_stijging)


def test_no_trailing_cfg_keeps_sl_static():
    pos = _long(entry=50_000, sl=49_000, tp=53_000, trailing_cfg=None)
    pos.check(_row(low=51_000, high=52_500))
    assert pos.sl_price == pytest.approx(49_000.0)
