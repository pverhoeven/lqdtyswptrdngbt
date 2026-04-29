"""
tests/test_paper_broker_trailing.py — Tests voor de trailing-stop logica.

Critical: een bug hier verschuift SL's naar ongunstige prijzen of mist BE-activatie,
waardoor winst-trades alsnog op SL eindigen.

NB: PaperBroker draait trailing-update + SL/TP-check op dezelfde candle als de fill.
Daarom houden de test-candles bewust een marge zodat de fill-candle niet direct
BE/trailing triggert.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.trading.broker.base import OrderSide
from src.trading.broker.paper import PaperBroker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candle(open_p: float, high: float, low: float, close: float) -> pd.Series:
    return pd.Series({"open": open_p, "high": high, "low": low, "close": close})


def _ts(minute: int) -> pd.Timestamp:
    return pd.Timestamp("2024-01-01 00:00:00", tz="UTC") + pd.Timedelta(minutes=minute)


@pytest.fixture
def trailing_cfg() -> dict:
    return {
        "enabled":        True,
        "breakeven_at_r": 1.0,
        "trail_after_r":  2.0,
        "trail_step_r":   0.5,
    }


# ---------------------------------------------------------------------------
# LONG trailing
# ---------------------------------------------------------------------------

def test_long_breakeven_activates_at_1r(trailing_cfg):
    """LONG: prijs raakt 1R winst → SL beweegt naar entry (BE)."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg)
    # Entry 100, SL 99 (1R = 1 USDT), TP 110
    broker.place_order("BTC", OrderSide.LONG, 100.0, 99.0, 110.0, risk_amount=100.0)

    # Fill candle: high blijft <100.7 zodat r_mult max 0.7 — geen BE
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    order = broker.open_orders("BTC")[0]
    assert order.sl_price == 99.0  # nog niet bewogen

    # Trigger candle: high=101 (1R), low blijft >100 zodat SL niet hit wordt
    broker.on_candle("BTC", _candle(100.3, 101.0, 100.2, 100.8), _ts(15))
    assert order.sl_price == 100.0  # SL → entry


def test_long_trailing_starts_after_2r(trailing_cfg):
    """LONG: na 2R trailt SL op (best_price - dist * step)."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg)
    broker.place_order("BTC", OrderSide.LONG, 100.0, 99.0, 110.0, risk_amount=100.0)

    # Fill (geen BE)
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    order = broker.open_orders("BTC")[0]

    # Klim naar 102 (2R) — low moet >SL_nieuw blijven (101.5)
    broker.on_candle("BTC", _candle(100.3, 102.0, 101.6, 101.8), _ts(15))
    assert order.sl_price == 101.5  # 102 - 1.0*0.5

    # Klim verder naar 103 — low moet >SL_nieuw (102.5) blijven
    broker.on_candle("BTC", _candle(101.8, 103.0, 102.6, 102.8), _ts(30))
    assert order.sl_price == 102.5  # 103 - 1.0*0.5


def test_long_trailing_sl_never_moves_backward(trailing_cfg):
    """LONG: zonder nieuwe piek mag SL NIET zakken."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg)
    broker.place_order("BTC", OrderSide.LONG, 100.0, 99.0, 110.0, risk_amount=100.0)

    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    order = broker.open_orders("BTC")[0]

    # Piek 103 → SL=102.5
    broker.on_candle("BTC", _candle(100.3, 103.0, 102.6, 102.8), _ts(15))
    assert order.sl_price == 102.5

    # Daling: high=102.8 (geen nieuwe piek), low=102.6 — SL moet blijven
    broker.on_candle("BTC", _candle(102.8, 102.8, 102.6, 102.7), _ts(30))
    assert order.sl_price == 102.5


# ---------------------------------------------------------------------------
# SHORT trailing (gespiegeld)
# ---------------------------------------------------------------------------

def test_short_breakeven_activates_at_1r(trailing_cfg):
    """SHORT: prijs daalt 1R → SL → entry."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg)
    broker.place_order("BTC", OrderSide.SHORT, 100.0, 101.0, 90.0, risk_amount=100.0)

    # Fill (high >= entry). Low blijft >99.3 zodat r_mult <0.7
    broker.on_candle("BTC", _candle(99.7, 100.0, 99.5, 99.7), _ts(0))
    order = broker.open_orders("BTC")[0]
    assert order.sl_price == 101.0

    # Trigger: low=99 (1R). high blijft <SL=100 (na BE)
    broker.on_candle("BTC", _candle(99.7, 99.8, 99.0, 99.2), _ts(15))
    assert order.sl_price == 100.0


def test_short_trailing_after_2r(trailing_cfg):
    """SHORT: na 2R trailt SL omlaag (best_price + dist * step)."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg)
    broker.place_order("BTC", OrderSide.SHORT, 100.0, 101.0, 90.0, risk_amount=100.0)

    broker.on_candle("BTC", _candle(99.7, 100.0, 99.5, 99.7), _ts(0))
    order = broker.open_orders("BTC")[0]

    # Daling naar 98 (2R), high blijft < nieuwe SL (98.5)
    broker.on_candle("BTC", _candle(99.7, 98.4, 98.0, 98.2), _ts(15))
    assert order.sl_price == 98.5  # 98 + 1.0*0.5


# ---------------------------------------------------------------------------
# Sanity: zonder trailing config gebeurt er niets
# ---------------------------------------------------------------------------

def test_no_trailing_config_keeps_sl_static():
    """Zonder trailing_cfg moet SL nooit bewegen."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)  # geen trailing_cfg
    broker.place_order("BTC", OrderSide.LONG, 100.0, 99.0, 110.0, risk_amount=100.0)

    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    order = broker.open_orders("BTC")[0]

    # Pomp naar 103 (3R) — SL moet NIET bewegen
    broker.on_candle("BTC", _candle(100.3, 103.0, 100.5, 102.8), _ts(15))
    assert order.sl_price == 99.0


# ---------------------------------------------------------------------------
# Partial exit + trailing combinatie
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Gap-down edge cases
# ---------------------------------------------------------------------------

def test_long_gap_down_through_be_sl_closes_at_sl_price(trailing_cfg):
    """
    LONG met BE-SL op entry (100): candle opent beneden SL (gap-down).
    Paper broker sluit op SL-prijs (100), niet op gap-open — paper-trading simplificatie.
    """
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg)
    broker.place_order("BTC", OrderSide.LONG, 100.0, 99.0, 110.0, risk_amount=100.0)

    # Fill (geen BE nog, high=100.5, r_mult=0.5, low=99.8 > SL=99)
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    # BE activeren (high=101 = 1R, low=100.2 > BE=100)
    broker.on_candle("BTC", _candle(100.3, 101.0, 100.2, 100.8), _ts(15))
    order = broker.open_orders("BTC")[0]
    assert order.sl_price == 100.0  # BE actief

    # Gap-down: open=99.5 (onder SL=100), high=99.8, low=99.2
    closed = broker.on_candle("BTC", _candle(99.5, 99.8, 99.2, 99.5), _ts(30))
    assert len(closed) == 1
    assert closed[0].close_price == pytest.approx(100.0)   # SL-prijs, niet gap-open


def test_long_gap_down_through_trailing_sl(trailing_cfg):
    """
    LONG: trailing SL staat op 102.5 na 3R-piek (103).
    Candle opent op 102.0 (onder SL=102.5) — trade sluit op trailing SL-prijs.
    """
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg)
    broker.place_order("BTC", OrderSide.LONG, 100.0, 99.0, 110.0, risk_amount=100.0)

    # Fill (geen BE, high=100.5)
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    # Klim naar piek 103 → trailing SL = 103 - 0.5 = 102.5, low=102.6 > SL
    broker.on_candle("BTC", _candle(100.3, 103.0, 102.6, 102.8), _ts(15))
    order = broker.open_orders("BTC")[0]
    assert order.sl_price == pytest.approx(102.5)

    # Gap-down: open=102.0 (onder trailing SL=102.5), low=101.8 → sluit op SL
    closed = broker.on_candle("BTC", _candle(102.0, 102.2, 101.8, 102.0), _ts(30))
    assert len(closed) == 1
    assert closed[0].close_price == pytest.approx(102.5)


def test_partial_exit_moves_sl_to_breakeven():
    """
    Bij partial exit met move_sl_to_be=True moet SL → entry, en de trailing
    state moet meegezet worden zodat een latere lage candle exit op BE geeft.
    """
    trailing_cfg = {"enabled": True, "breakeven_at_r": 1.0,
                    "trail_after_r": 2.0, "trail_step_r": 0.5}
    partial_cfg = {"enabled": True, "exit_r": 1.0,
                   "exit_fraction": 0.5, "move_sl_to_be": True}
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg, partial_exit_cfg=partial_cfg)

    broker.place_order("BTC", OrderSide.LONG, 100.0, 99.0, 110.0, risk_amount=100.0)
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    order = broker.open_orders("BTC")[0]
    initial_size = order.size

    # Pomp naar 1R: partial @ 101, low moet >SL=100 zodat trade open blijft
    broker.on_candle("BTC", _candle(100.3, 101.5, 100.5, 101.0), _ts(15))
    assert order.size == pytest.approx(initial_size * 0.5)
    assert order.sl_price == 100.0

    # Daling onder 100 → trade sluit op BE (=100), niet op oorspronkelijke 99
    closed = broker.on_candle("BTC", _candle(101.0, 101.0, 99.5, 99.8), _ts(30))
    assert len(closed) == 1
    assert closed[0].close_price == 100.0
