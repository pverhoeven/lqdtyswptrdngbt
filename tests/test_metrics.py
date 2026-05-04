"""
tests/test_metrics.py — Unit tests voor compute_metrics.

Critical: verkeerde win_rate, profit_factor of Sharpe leiden direct tot
verkeerde strategiebeslissingen.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.backtest.metrics import BacktestMetrics, Trade, compute_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(day: int) -> pd.Timestamp:
    return pd.Timestamp(f"2024-01-{day:02d}", tz="UTC")


def _trade(pnl: float, direction: str = "long", day_in: int = 1, day_out: int = 2) -> Trade:
    outcome = "win" if pnl > 0 else "loss"
    return Trade(
        entry_time  = _ts(day_in),
        exit_time   = _ts(day_out),
        direction   = direction,
        entry_price = 50_000.0,
        exit_price  = 50_000.0 + (pnl / 0.1),
        sl_price    = 49_000.0,
        tp_price    = 51_500.0,
        outcome     = outcome,
        pnl_pct     = pnl / 10_000.0,
        pnl_capital = pnl,
        fee_cost    = 0.0,
        regime      = None,
    )


# ---------------------------------------------------------------------------
# Lege trade lijst
# ---------------------------------------------------------------------------

def test_empty_trades_returns_zero_metrics():
    m = compute_metrics([], 10_000.0)
    assert m.trade_count    == 0
    assert m.win_rate       == 0.0
    assert m.sharpe_ratio   == 0.0
    assert m.total_return   == 0.0


# ---------------------------------------------------------------------------
# Win rate
# ---------------------------------------------------------------------------

def test_win_rate_two_wins_one_loss():
    trades = [_trade(100, day_in=1, day_out=2),
              _trade(100, day_in=3, day_out=4),
              _trade(-50, day_in=5, day_out=6)]
    m = compute_metrics(trades, 10_000.0)
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.trade_count == 3


def test_all_wins_gives_max_win_rate():
    trades = [_trade(100, day_in=i, day_out=i+1) for i in range(1, 6, 2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.win_rate == pytest.approx(1.0)


def test_all_losses_gives_zero_win_rate():
    trades = [_trade(-50, day_in=i, day_out=i+1) for i in range(1, 6, 2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.win_rate == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Profit factor
# ---------------------------------------------------------------------------

def test_profit_factor_two_wins_one_loss():
    # wins = 200, losses = 50 → pf = 4.0
    trades = [_trade(100, day_in=1, day_out=2),
              _trade(100, day_in=3, day_out=4),
              _trade(-50, day_in=5, day_out=6)]
    m = compute_metrics(trades, 10_000.0)
    assert m.profit_factor == pytest.approx(4.0)


def test_profit_factor_all_wins_is_inf():
    trades = [_trade(100, day_in=i, day_out=i+1) for i in range(1, 6, 2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.profit_factor == float("inf")


# ---------------------------------------------------------------------------
# Total return
# ---------------------------------------------------------------------------

def test_total_return_correct():
    # 3 × +100 USDT op 10_000 startkapitaal = +3%
    trades = [_trade(100, day_in=i, day_out=i+1) for i in range(1, 6, 2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.total_return == pytest.approx(0.03)


def test_total_return_negative_when_net_loss():
    trades = [_trade(-200, day_in=1, day_out=2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.total_return == pytest.approx(-0.02)


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------

def test_max_drawdown_is_positive():
    trades = [_trade(100, day_in=1, day_out=2),
              _trade(-300, day_in=3, day_out=4),
              _trade(50,  day_in=5, day_out=6)]
    m = compute_metrics(trades, 10_000.0)
    assert m.max_drawdown > 0.0


def test_no_drawdown_when_all_wins():
    trades = [_trade(100, day_in=i, day_out=i+1) for i in range(1, 6, 2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.max_drawdown == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Sharpe — teken en richting
# ---------------------------------------------------------------------------

def test_sharpe_positive_for_profitable_strategy():
    trades = [_trade(100, day_in=i, day_out=i+1) for i in range(1, 20, 2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.sharpe_ratio > 0.0


def test_sharpe_negative_for_losing_strategy():
    trades = [_trade(-50, day_in=i, day_out=i+1) for i in range(1, 20, 2)]
    m = compute_metrics(trades, 10_000.0)
    assert m.sharpe_ratio < 0.0
