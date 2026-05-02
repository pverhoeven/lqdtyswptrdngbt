"""
metrics.py — Backtest metrics berekening.

Alle metrics worden berekend uit een lijst van afgesloten trades.
Geen dependencies op de backtest engine zelf.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """Eén afgesloten trade."""
    entry_time:   pd.Timestamp
    exit_time:    pd.Timestamp
    direction:    str            # "long" of "short"
    entry_price:  float
    exit_price:   float
    sl_price:     float
    tp_price:     float
    outcome:      str            # "win", "loss", "timeout"
    pnl_pct:      float          # procentueel rendement op trade-niveau
    pnl_capital:  float          # rendement in USDT (na kosten)
    fee_cost:     float          # kosten
    regime:       bool | None


@dataclass
class BacktestMetrics:
    """Alle berekende metrics voor één backtest-run."""
    sharpe_ratio:   float
    max_drawdown:   float        # als positief getal (bijv. 0.25 = 25% drawdown)
    win_rate:       float        # 0.0 – 1.0
    trade_count:    int
    profit_factor:  float
    total_return:   float        # cumulatief rendement
    avg_trade_pnl:  float        # gemiddeld P&L per trade in USDT
    fill_rate:      float | None = None   # None = fill-simulatie niet gedraaid
    signals_count:  int          = 0     # totaal aantal gedetecteerde signalen

    def __str__(self) -> str:
        lines = [
            f"Trades:         {self.trade_count}",
            f"Win rate:       {self.win_rate:.1%}",
            f"Sharpe ratio:   {self.sharpe_ratio:.2f}",
            f"Max drawdown:   {self.max_drawdown:.1%}",
            f"Profit factor:  {self.profit_factor:.2f}",
            f"Total return:   {self.total_return:.1%}",
            f"Avg trade P&L:  {self.avg_trade_pnl:.2f} USDT",
        ]
        if self.fill_rate is not None:
            lines.append(
                f"Fill rate:      {self.fill_rate:.1%}"
                f"  ({self.trade_count}/{self.signals_count} signalen gevuld)"
            )
        return "\n".join(lines)

    def interpret(self) -> str:
        """Eenvoudige tekstinterpretatie van de resultaten (uit spec)."""
        if self.trade_count < 50:
            return "⚠️  Minder dan 50 trades — te weinig data om conclusies te trekken."
        if self.sharpe_ratio > 1.0:
            return "✅ Edge aanwezig (Sharpe > 1.0) — verfijnen heeft zin."
        if self.sharpe_ratio >= 0.5:
            return "⚠️  Zwak signaal (Sharpe 0.5–1.0) — parameters onderzoeken."
        return "❌ Geen edge (Sharpe < 0.5) — kernhypothese herzien."


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def compute_metrics(
    trades: list[Trade],
    initial_capital: float,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 365 * 24 * 4,   # 15m candles per jaar
) -> BacktestMetrics:
    """
    Bereken alle backtest metrics uit een lijst van Trade objecten.

    Parameters
    ----------
    trades : list[Trade]
    initial_capital : float
        Startkapitaal in USDT.
    risk_free_rate : float
        Jaarlijkse risicovrije rente (standaard 0.0).
    periods_per_year : int
        Aantal 15m candles per jaar voor annualisatie van Sharpe.

    Returns
    -------
    BacktestMetrics
    """
    if not trades:
        return BacktestMetrics(
            sharpe_ratio=0.0, max_drawdown=0.0, win_rate=0.0,
            trade_count=0, profit_factor=0.0, total_return=0.0,
            avg_trade_pnl=0.0,
        )

    # Bereken equity curve per candle

    # --- Equity curve ---
    equity_curve = _build_equity_curve(trades, initial_capital, freq="15min")

    # --- Metrics ---
    pnl_series = pd.Series([t.pnl_capital for t in trades])

    # Sharpe ratio (op basis van equity curve)
    sharpe = _sharpe_ratio_from_equity(equity_curve, risk_free_rate)

    equity = initial_capital + pnl_series.cumsum()

    wins   = pnl_series[pnl_series > 0]
    losses = pnl_series[pnl_series <= 0]

    win_rate      = len(wins) / len(trades)
    logger.info("Win rate: %s. Wins %s, Losses %s ", win_rate, len(wins), len(losses))

    profit_factor = (wins.sum() / abs(losses.sum())) if len(losses) > 0 and losses.sum() != 0 else float("inf")
    total_return  = (equity.iloc[-1] - initial_capital) / initial_capital
    avg_trade_pnl = pnl_series.mean()

    max_dd  = _max_drawdown(equity)



    return BacktestMetrics(
        sharpe_ratio  = sharpe,
        max_drawdown  = max_dd,
        win_rate      = win_rate,
        trade_count   = len(trades),
        profit_factor = profit_factor,
        total_return  = total_return,
        avg_trade_pnl = avg_trade_pnl,

    )


def compute_metrics_from_equity(equity_series, initial_capital, risk_free_rate=0.0):
    if len(equity_series) < 2:
        return BacktestMetrics(
            sharpe_ratio=0.0,
            max_drawdown=0.0,  # ✅ Positief
            win_rate=0.0,
            trade_count=0,
            profit_factor=0.0,
            total_return=0.0,
            avg_trade_pnl=0.0,
        )

    # Sharpe ratio (op basis van equity curve returns)
    returns = equity_series.pct_change().dropna()
    mean_r = returns.mean()
    std_r = returns.std(ddof=1)
    if std_r == 0:
        sharpe = 0.0
    else:
        time_delta = (equity_series.index[-1] - equity_series.index[0]).total_seconds()
        n_periods = len(equity_series) - 1
        if n_periods > 0 and time_delta > 0:
            avg_period_seconds = time_delta / n_periods
            periods_per_year = (365.25 * 24 * 3600) / avg_period_seconds
        else:
            periods_per_year = 252
        rf_per_period = risk_free_rate / periods_per_year
        sharpe = (mean_r - rf_per_period) / std_r * np.sqrt(periods_per_year)

    # Max drawdown (✅ FIX: neem absolute waarde of negatieve waarde)
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max
    max_drawdown = abs(drawdown.min())  # ✅ FIX: Neem absolute waarde!

    # Total return
    total_return = (equity_series.iloc[-1] - initial_capital) / initial_capital

    return BacktestMetrics(
        sharpe_ratio=sharpe,
        max_drawdown=max_drawdown,  # ✅ Nu positief (bijv. 0.05 voor 5%)
        win_rate=0.0,  # Wordt later ingevuld
        trade_count=len(equity_series),
        profit_factor=0.0,  # Wordt later ingevuld
        total_return=total_return,
        avg_trade_pnl=0.0,
    )

def _sharpe_ratio_from_equity(
    equity_series: pd.Series,
    risk_free_rate: float = 0.0,
) -> float:
    """
    Berekent de geannualiseerde Sharpe ratio op basis van een equity curve (tijdsgebaseerd).

    Parameters
    ----------
    equity_series : pd.Series
        Equity curve met tijdstempels als index (bijv. per 15M candle).
    risk_free_rate : float
        Risicovrije rente (jaarlijks).

    Returns
    -------
    float
        Geannualiseerde Sharpe ratio.
    """
    if len(equity_series) < 2:
        return 0.0

    # Bereken returns als % verandering
    returns = equity_series.pct_change().dropna()

    if returns.empty:
        return 0.0

    mean_r = returns.mean()
    std_r = returns.std(ddof=1)

    if std_r == 0:
        return 0.0

    # Bepaal het aantal periodes per jaar op basis van de tijdsfrequentie
    time_delta = (equity_series.index[-1] - equity_series.index[0]).total_seconds()
    n_periods = len(equity_series) - 1

    if n_periods > 0 and time_delta > 0:
        avg_period_seconds = time_delta / n_periods
        periods_per_year = (365.25 * 24 * 3600) / avg_period_seconds
    else:
        periods_per_year = 252  # Default voor dagelijkse data

    # Voorkom deling door 0
    if periods_per_year <= 0:
        periods_per_year = 252

    rf_per_period = risk_free_rate / periods_per_year
    return float((mean_r - rf_per_period) / std_r * np.sqrt(periods_per_year))

def _build_equity_curve(
    trades: list[Trade],
    initial_capital: float,
    freq: str = "15min",
) -> pd.Series:
    """
    Bouwt een equity curve (cumulatieve PnL over tijd) als pd.Series.

    Parameters
    ----------
    trades : list[Trade]
        Lijst met Trade objecten.
    initial_capital : float
        Startkapitaal.
    freq : str
        Tijdsfrequentie (bijv. "15T" voor 15 minuten).

    Returns
    -------
    pd.Series
        Equity curve met tijdstempels als index.
    """
    if not trades:
        return pd.Series(dtype=float)

    # Haal alle unieke tijdstempels op (inclusief start en eind)
    all_times = sorted({t.entry_time for t in trades} | {t.exit_time for t in trades})
    if not all_times:
        return pd.Series(dtype=float)

    # Maak een DatetimeIndex met de juiste frequentie
    start = min(all_times)
    end = max(all_times)
    time_index = pd.date_range(start=start, end=end, freq=freq, tz="UTC")

    # Vul de equity curve in
    equity = pd.Series(0.0, index=time_index, dtype=float)
    equity.iloc[0] = initial_capital
    for trade in trades:
        # Voeg PnL toe op de dichtstbijzijnde 15m-mark als exit_time niet exact klopt
        loc = equity.index.get_indexer([trade.exit_time], method="nearest")[0]
        if loc >= 0:
            equity.iloc[loc] += trade.pnl_capital

    # Cumulatieve som
    equity = equity.cumsum()
    return equity

def equity_curve(trades: list[Trade], initial_capital: float) -> pd.Series:
    """
    Bouw de equity curve op als pd.Series met entry_time als index.

    Returns
    -------
    pd.Series
        Index: entry_time van elke trade. Values: cumulatief kapitaal.
    """
    if not trades:
        return pd.Series(dtype=float)

    times = [t.entry_time for t in trades]
    pnls  = [t.pnl_capital for t in trades]
    curve = pd.Series(pnls, index=times).cumsum() + initial_capital
    return curve


# ---------------------------------------------------------------------------
# Berekeningen
# ---------------------------------------------------------------------------

def _sharpe_ratio(
    pnl_series: pd.Series,
    initial_capital: float,
    risk_free_rate: float,
    periods_per_year: int,
    trade_times: pd.Series | None = None,
) -> float:
    """
    Annualized Sharpe ratio op basis van per-trade P&L.

    Formule: (mean(returns) - rf_per_period) / std(returns) * sqrt(periods_per_year)
    waarbij returns = pnl / initial_capital per trade.
    """
    if len(pnl_series) < 2:
        return 0.0

    returns = pnl_series / initial_capital
    mean_r  = returns.mean()
    std_r   = returns.std(ddof=1)

    if std_r == 0:
        return 0.0

        # Gebruik trade_times als beschikbaar
    if trade_times is not None and len(trade_times) > 1:
        time_deltas = (trade_times[1:] - trade_times[:-1]).dt.total_seconds() / (365.25 * 24 * 3600)  # in jaren
        avg_time_between_trades = time_deltas.mean()
        periods_per_year = 1 / avg_time_between_trades if avg_time_between_trades > 0 else 0
    elif periods_per_year is None:
        periods_per_year = 252  # Default voor dagelijkse data

        # Voorkom deling door 0
    if periods_per_year == 0:
        periods_per_year = 252  # Veilige default

    rf_per_period = risk_free_rate / periods_per_year
    return float((mean_r - rf_per_period) / std_r * np.sqrt(periods_per_year))


def _max_drawdown(equity: pd.Series) -> float:
    """
    Maximum drawdown als fractie (positief getal).
    Bijv. 0.25 = 25% drawdown.
    """
    if len(equity) < 2:
        return 0.0

    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max
    return float(abs(drawdown.min()))
