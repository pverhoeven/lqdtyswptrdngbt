"""
tests/test_order_manager_signal.py — Integration tests voor OrderManager.on_signal()
met circuit breaker in verschillende toestanden.

Critical: de CB-check in on_signal is de enige barrière die voorkomt dat de bot
doorhandelt als de breaker open staat. Als deze check wegvalt, handelt de bot
door verlieslimiet heen.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.signals.detector import SweepSignal
from src.trading.broker.paper import PaperBroker
from src.trading.order_manager import AccountCircuitBreaker, OrderManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal() -> SweepSignal:
    return SweepSignal(
        timestamp   = pd.Timestamp("2024-01-01 12:00", tz="UTC"),
        direction   = "long",
        entry_price = 100.0,
        sl_price    = 99.0,
        tp_price    = 102.0,
        liq_level   = 98.5,
        regime      = None,
        filter_str  = "",
    )


def _make_om(tmp_path, cb_cfg=None, account_cb=None) -> OrderManager:
    broker = PaperBroker(initial_capital=10_000, fee_pct=0.0)
    return OrderManager(
        broker     = broker,
        symbol     = "BTC",
        risk_pct   = 1.0,
        log_dir    = tmp_path / "logs",
        cb_cfg     = cb_cfg,
        account_cb = account_cb,
    )


# ---------------------------------------------------------------------------
# CB gesloten — normaal pad
# ---------------------------------------------------------------------------

def test_signal_places_order_when_cb_closed(tmp_path):
    """CB dicht → on_signal retourneert een order."""
    om = _make_om(tmp_path, cb_cfg={"max_consecutive_losses": 3,
                                     "max_daily_loss_pct": 3.0,
                                     "max_drawdown_pct": 10.0})
    order = om.on_signal(_signal())
    assert order is not None


# ---------------------------------------------------------------------------
# CB open (DAY_PAUSE)
# ---------------------------------------------------------------------------

def test_signal_blocked_when_cb_day_pause(tmp_path):
    """Na N consecutive losses (DAY_PAUSE) retourneert on_signal None."""
    om = _make_om(tmp_path, cb_cfg={"max_consecutive_losses": 1,
                                     "max_daily_loss_pct": 99.0,
                                     "max_drawdown_pct": 99.0})
    om._cb.record_trade(pnl=-100, equity=9_900)   # één verlies → DAY_PAUSE
    assert om._cb.is_open() is True
    assert om._cb.is_hard_stop is False

    result = om.on_signal(_signal())
    assert result is None


# ---------------------------------------------------------------------------
# CB open (HARD_STOP)
# ---------------------------------------------------------------------------

def test_signal_blocked_when_cb_hard_stop(tmp_path):
    """Na HARD_STOP (max drawdown) retourneert on_signal None."""
    om = _make_om(tmp_path, cb_cfg={"max_consecutive_losses": 99,
                                     "max_daily_loss_pct": 99.0,
                                     "max_drawdown_pct": 10.0})
    om._cb.record_trade(pnl=+1_000, equity=11_000)   # bouw piek
    om._cb.record_trade(pnl=-2_000, equity=9_000)    # 18% DD > 10% → HARD_STOP
    assert om._cb.is_hard_stop is True

    result = om.on_signal(_signal())
    assert result is None


# ---------------------------------------------------------------------------
# Account CB open
# ---------------------------------------------------------------------------

def test_signal_blocked_when_account_cb_open(tmp_path):
    """Account-niveau CB open → on_signal geblokkeerd, ook als coin-CB dicht is."""
    acb = AccountCircuitBreaker(
        max_daily_loss_pct = 1.0,
        max_drawdown_pct   = 10.0,
        start_capital      = 10_000.0,
    )
    acb.record_trade(pnl=-200, equity=9_800)   # 2% daily loss > 1% → open

    om = _make_om(tmp_path, account_cb=acb)
    assert om._cb is None                       # coin-CB uitgeschakeld

    result = om.on_signal(_signal())
    assert result is None
