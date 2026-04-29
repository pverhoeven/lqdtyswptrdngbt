"""
tests/test_state_persistence.py — Roundtrip tests voor PaperBroker.save_state / load_state.

Critical: bij een herstart van de bot moet de state exact hersteld worden.
Een bug hier geeft dubbele posities, verkeerd kapitaal, of verloren orders.

NB: trailing state wordt bewust NIET opgeslagen — na laden ontbreekt _trailing
voor open posities. Dit is een bekende beperking van de huidige implementatie.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.trading.broker.base import OrderSide, OrderStatus
from src.trading.broker.paper import PaperBroker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candle(open_p, high, low, close):
    return pd.Series({"open": open_p, "high": high, "low": low, "close": close})


def _ts(minute: int) -> pd.Timestamp:
    return pd.Timestamp("2024-01-01 00:00:00", tz="UTC") + pd.Timedelta(minutes=minute)


# ---------------------------------------------------------------------------
# Pending order roundtrip
# ---------------------------------------------------------------------------

def test_pending_order_survives_roundtrip(tmp_path):
    """Een pending order (nog niet gevuld) moet na save/load intact zijn."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    orig = broker.place_order("BTC", OrderSide.LONG,
                              entry_price=100.0, sl_price=99.0, tp_price=105.0,
                              risk_amount=100.0)

    path = tmp_path / "state.json"
    broker.save_state(path)

    broker2 = PaperBroker(initial_capital=0, fee_pct=0.0)
    broker2.load_state(path)

    loaded = broker2.open_orders("BTC")
    assert len(loaded) == 1
    o = loaded[0]
    assert o.order_id    == orig.order_id
    assert o.side        == OrderSide.LONG
    assert o.entry_price == pytest.approx(100.0)
    assert o.sl_price    == pytest.approx(99.0)
    assert o.status      == OrderStatus.PENDING


# ---------------------------------------------------------------------------
# Open (gevulde) order roundtrip
# ---------------------------------------------------------------------------

def test_open_order_survives_roundtrip(tmp_path):
    """Een gevulde open positie (na fill candle) moet na save/load intact zijn."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    orig = broker.place_order("BTC", OrderSide.LONG,
                              entry_price=100.0, sl_price=99.0, tp_price=110.0,
                              risk_amount=100.0)
    # Fill
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))

    capital_before = broker._capital
    path = tmp_path / "state.json"
    broker.save_state(path)

    broker2 = PaperBroker(initial_capital=0, fee_pct=0.0)
    broker2.load_state(path)

    assert broker2._capital == pytest.approx(capital_before)
    open_orders = [o for o in broker2.open_orders("BTC")
                   if o.status == OrderStatus.OPEN]
    assert len(open_orders) == 1
    o = open_orders[0]
    assert o.order_id    == orig.order_id
    assert o.size        == pytest.approx(orig.size)
    assert o.status      == OrderStatus.OPEN


# ---------------------------------------------------------------------------
# Kapitaal roundtrip
# ---------------------------------------------------------------------------

def test_capital_survives_roundtrip(tmp_path):
    """Gewijzigd kapitaal (na fee-aftrek of PnL) wordt correct opgeslagen."""
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.1)
    broker.place_order("BTC", OrderSide.LONG,
                       entry_price=100.0, sl_price=99.0, tp_price=110.0,
                       risk_amount=100.0)
    # Fill → fee wordt afgetrokken van kapitaal
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    expected_capital = broker._capital

    path = tmp_path / "state.json"
    broker.save_state(path)

    broker2 = PaperBroker(initial_capital=0, fee_pct=0.0)
    broker2.load_state(path)

    assert broker2._capital == pytest.approx(expected_capital)


# ---------------------------------------------------------------------------
# partial_taken roundtrip
# ---------------------------------------------------------------------------

def test_partial_taken_survives_roundtrip(tmp_path):
    """Order-IDs in partial_taken moeten na save/load hersteld worden."""
    trailing_cfg = {"enabled": True, "breakeven_at_r": 1.0,
                    "trail_after_r": 2.0, "trail_step_r": 0.5}
    partial_cfg  = {"enabled": True, "exit_r": 1.0,
                    "exit_fraction": 0.5, "move_sl_to_be": True}
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0,
                         trailing_cfg=trailing_cfg, partial_exit_cfg=partial_cfg)

    orig = broker.place_order("BTC", OrderSide.LONG,
                              entry_price=100.0, sl_price=99.0, tp_price=110.0,
                              risk_amount=100.0)
    # Fill
    broker.on_candle("BTC", _candle(100.0, 100.5, 99.8, 100.3), _ts(0))
    # Trigger partial exit @ 1R
    broker.on_candle("BTC", _candle(100.3, 101.5, 100.5, 101.0), _ts(15))
    assert orig.order_id in broker._partial_taken

    path = tmp_path / "state.json"
    broker.save_state(path)

    broker2 = PaperBroker(initial_capital=0, fee_pct=0.0,
                          trailing_cfg=trailing_cfg, partial_exit_cfg=partial_cfg)
    broker2.load_state(path)

    assert orig.order_id in broker2._partial_taken


# ---------------------------------------------------------------------------
# Lege state-file wordt stilzwijgend genegeerd
# ---------------------------------------------------------------------------

def test_load_nonexistent_state_is_noop(tmp_path):
    """load_state op een niet-bestaand pad mag de broker-state niet wijzigen."""
    broker = PaperBroker(initial_capital=5_000, fee_pct=0.0)
    broker.load_state(tmp_path / "does_not_exist.json")
    assert broker._capital == pytest.approx(5_000)
    assert broker._pending == []
    assert broker._open    == []
