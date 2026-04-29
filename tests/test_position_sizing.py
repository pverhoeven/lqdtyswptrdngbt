"""
tests/test_position_sizing.py — Unit tests voor positiegrootte-berekening.

Formule: size = risk_amount / sl_distance
         risk_amount = equity * risk_pct   (berekend in OrderManager.on_signal)

Critical: een fout in de sizing geeft te grote posities bij kleine SL,
of te kleine posities die het risicoprofiel verstoren.
"""
from __future__ import annotations

import pytest

from src.trading.broker.base import OrderSide
from src.trading.broker.paper import PaperBroker


def test_long_sizing_basic():
    """equity=10_000, risk 1% (=100 USDT), SL-dist=2 → size=50."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    order = broker.place_order(
        "BTC", OrderSide.LONG,
        entry_price=100.0, sl_price=98.0, tp_price=106.0,
        risk_amount=100.0,
    )
    # sl_dist = 100 - 98 = 2;  size = 100 / 2 = 50
    assert order.size == pytest.approx(50.0)


def test_short_sizing_basic():
    """SHORT: sl_dist = sl - entry, formule identiek aan LONG."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    order = broker.place_order(
        "BTC", OrderSide.SHORT,
        entry_price=100.0, sl_price=102.0, tp_price=94.0,
        risk_amount=100.0,
    )
    # sl_dist = 102 - 100 = 2;  size = 100 / 2 = 50
    assert order.size == pytest.approx(50.0)


def test_tighter_sl_gives_larger_position():
    """Nauwere SL bij hetzelfde risico → grotere positiegrootte."""
    b_tight = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    order_tight = b_tight.place_order(
        "BTC", OrderSide.LONG,
        entry_price=100.0, sl_price=99.5, tp_price=106.0,
        risk_amount=100.0,
    )
    b_wide = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    order_wide = b_wide.place_order(
        "BTC", OrderSide.LONG,
        entry_price=100.0, sl_price=98.0, tp_price=106.0,
        risk_amount=100.0,
    )
    assert order_tight.size > order_wide.size


def test_half_risk_halves_position():
    """risk_amount halveert → size halveert (lineaire relatie)."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    o_full = broker.place_order(
        "BTC", OrderSide.LONG,
        entry_price=100.0, sl_price=98.0, tp_price=110.0,
        risk_amount=100.0,
    )
    broker2 = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    o_half = broker2.place_order(
        "BTC", OrderSide.LONG,
        entry_price=100.0, sl_price=98.0, tp_price=110.0,
        risk_amount=50.0,
    )
    assert o_half.size == pytest.approx(o_full.size / 2)


def test_zero_sl_distance_raises():
    """SL-prijs gelijk aan entry is ongeldig — broker gooit ValueError."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    with pytest.raises(ValueError, match="SL-afstand is nul"):
        broker.place_order(
            "BTC", OrderSide.LONG,
            entry_price=100.0, sl_price=100.0, tp_price=106.0,
            risk_amount=100.0,
        )
