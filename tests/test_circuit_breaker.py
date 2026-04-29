"""
tests/test_circuit_breaker.py — Tests voor de risk-management circuit breaker.

Critical: een bug in de breaker betekent dat de bot doorhandelt door een
verlieslimiet heen. Dit is direct geld-impact.
"""
from __future__ import annotations

import pytest

from src.trading.order_manager import (
    AccountCircuitBreaker,
    CircuitBreakerState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cb() -> CircuitBreakerState:
    """Standaard CB met capital=10_000, 3 max losses, 3% daily, 10% drawdown."""
    return CircuitBreakerState(
        max_consecutive    = 3,
        max_daily_loss_pct = 3.0,
        max_drawdown_pct   = 10.0,
        start_capital      = 10_000.0,
    )


# ---------------------------------------------------------------------------
# Per-coin CircuitBreakerState
# ---------------------------------------------------------------------------

def test_initially_closed_allows_trades(cb):
    assert cb.is_open() is False
    assert cb.is_hard_stop is False


def test_two_consecutive_losses_does_not_trip(cb):
    """Onder de drempel — moet open blijven."""
    cb.record_trade(pnl=-50.0, equity=9_950.0)
    cb.record_trade(pnl=-50.0, equity=9_900.0)
    assert cb.is_open() is False


def test_three_consecutive_losses_triggers_day_pause(cb):
    cb.record_trade(pnl=-50.0, equity=9_950.0)
    cb.record_trade(pnl=-50.0, equity=9_900.0)
    msg = cb.record_trade(pnl=-50.0, equity=9_850.0)

    assert msg is not None
    assert "opeenvolgende verliezen" in msg
    assert cb.is_open() is True
    assert cb.is_hard_stop is False  # alleen DAY_PAUSE, geen hard stop


def test_winning_trade_resets_consecutive_counter(cb):
    """Na 2 losses + 1 win moeten er weer 3 nieuwe losses nodig zijn."""
    cb.record_trade(pnl=-50.0, equity=9_950.0)
    cb.record_trade(pnl=-50.0, equity=9_900.0)
    cb.record_trade(pnl=+100.0, equity=10_000.0)  # win → reset

    cb.record_trade(pnl=-50.0, equity=9_950.0)
    cb.record_trade(pnl=-50.0, equity=9_900.0)
    assert cb.is_open() is False  # nog maar 2 losses sinds reset

    msg = cb.record_trade(pnl=-50.0, equity=9_850.0)
    assert msg is not None
    assert cb.is_open() is True


def test_daily_loss_limit_triggers_day_pause(cb):
    """Eén grote loss > 3% van startkapitaal → DAY_PAUSE."""
    msg = cb.record_trade(pnl=-350.0, equity=9_650.0)  # 3.5% van 10k
    assert msg is not None
    assert "Dagelijks verlies" in msg
    assert cb.is_open() is True
    assert cb.is_hard_stop is False


def test_daily_loss_offset_by_wins_does_not_trip(cb):
    """Win + loss waar netto < 3% → CB blijft dicht."""
    cb.record_trade(pnl=+200.0, equity=10_200.0)
    msg = cb.record_trade(pnl=-250.0, equity=9_950.0)
    # Netto -50 = -0.5% — onder de drempel
    assert msg is None
    assert cb.is_open() is False


def test_drawdown_triggers_hard_stop(cb):
    """Drawdown ≥ 10% van startkapitaal → HARD_STOP (niet DAY_PAUSE)."""
    # Eerst piek opbouwen
    cb.record_trade(pnl=+1_000.0, equity=11_000.0)
    # Dan grote drawdown — DD = (11000 - 9000) / 10000 = 20%
    msg = cb.record_trade(pnl=-2_000.0, equity=9_000.0)

    assert msg is not None
    assert "HARDE STOP" in msg
    assert cb.is_hard_stop is True
    assert cb.is_open() is True


def test_hard_stop_takes_priority_over_daily_loss(cb):
    """Bij overschrijding van beide drempels moet HARD_STOP gemeld worden."""
    msg = cb.record_trade(pnl=-1_500.0, equity=8_500.0)  # 15% loss
    assert "HARDE STOP" in msg
    assert cb.is_hard_stop is True


def test_drawdown_calculated_against_peak_not_start(cb):
    """Drawdown is piek-naar-dal, niet start-naar-dal."""
    # Bouw equity op naar 12_000
    cb.record_trade(pnl=+2_000.0, equity=12_000.0)

    # Equity zakt naar 11_100 = 9% DD vanaf piek 12_000 (binnen 10%)
    # Maar 11% boven start — start-based zou geen DD detecteren.
    msg = cb.record_trade(pnl=-900.0, equity=11_100.0)
    # 900/10000 = 9% daily loss — onder 3%? Nee, boven. → DAY_PAUSE
    # Maar drawdown (12k → 11.1k) = 9% < 10% → geen HARD_STOP
    assert cb.is_hard_stop is False


# ---------------------------------------------------------------------------
# Account-level circuit breaker (multi-coin)
# ---------------------------------------------------------------------------

def test_account_cb_aggregates_losses_across_coins():
    """Account CB triggert op de SOM van alle coin-PnL, niet per coin."""
    acb = AccountCircuitBreaker(
        max_daily_loss_pct = 3.0,
        max_drawdown_pct   = 10.0,
        start_capital      = 10_000.0,
    )

    # Twee verliezen van verschillende coins, elk 2% — samen 4% > 3%
    msg1 = acb.record_trade(pnl=-200.0, equity=9_800.0)
    assert msg1 is None  # nog niet over drempel
    msg2 = acb.record_trade(pnl=-200.0, equity=9_600.0)
    assert msg2 is not None
    assert "[ACCOUNT]" in msg2
    assert acb.is_open() is True


def test_account_cb_drawdown_hard_stops():
    acb = AccountCircuitBreaker(
        max_daily_loss_pct = 5.0,
        max_drawdown_pct   = 10.0,
        start_capital      = 10_000.0,
    )

    acb.record_trade(pnl=+500.0,  equity=10_500.0)
    msg = acb.record_trade(pnl=-1_600.0, equity=8_900.0)
    # DD = (10500 - 8900) / 10000 = 16% > 10% → HARD_STOP
    assert msg is not None
    assert "HARDE STOP" in msg
    assert acb.is_hard_stop is True
