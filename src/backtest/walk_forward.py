"""
backtest/walk_forward.py — Walk-forward validatie.

Rolt een train/test venster over de beschikbare data:
  - Elke iteratie: train HMM op [train_start, train_end], test op [test_start, test_end]
  - Geen OOS-data wordt aangeraakt
  - Retourneert metriek per venster + geaggregeerde samenvatting

Gebruik:
    python scripts/run_walk_forward.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.backtest.metrics import BacktestMetrics, Trade, compute_metrics
from src.backtest.sweep_engine import _run_loop as sweep_run_loop
from src.config_loader import load_config
from src.data.cache import load_cache
from src.regime.hmm import align_regimes_to_15m, predict_regimes, train
from src.signals.filters import SweepFilters

logger = logging.getLogger(__name__)


@dataclass
class WalkForwardWindow:
    train_start: str
    train_end:   str
    test_start:  str
    test_end:    str
    metrics:     BacktestMetrics
    trades:      list[Trade]


def run_walk_forward(
    cfg: dict | None = None,
    train_months: int | None = None,
    test_months:  int | None = None,
    start: str | None = None,
    end:   str | None = None,
    symbol: str | None = None,
    filters: SweepFilters | None = None,
) -> list[WalkForwardWindow]:
    """
    Voer een walk-forward validatie uit.

    Parameters
    ----------
    cfg : dict, optional
    train_months : int
        Grootte van het trainingsvenster in maanden.
        Standaard: cfg["backtest"]["walk_forward"]["train_months"].
    test_months : int
        Grootte van het testvenster in maanden.
        Standaard: cfg["backtest"]["walk_forward"]["test_months"].
    start : str
        Startdatum van de gehele walk-forward periode.
        Standaard: cfg["split"]["in_sample_start"].
    end : str
        Einddatum van de gehele walk-forward periode.
        Standaard: cfg["split"]["in_sample_end"].
    symbol : str, optional
        Symbool override.
    filters : SweepFilters, optional
        Sweep-filters (richting, regime, BOS). Standaard: geen filters.

    Returns
    -------
    list[WalkForwardWindow]
        Resultaten per venster.
    """
    if cfg is None:
        cfg = load_config()
    if filters is None:
        filters = SweepFilters()

    wf_cfg       = cfg.get("backtest", {}).get("walk_forward", {})
    train_months = train_months or wf_cfg.get("train_months", 12)
    test_months  = test_months  or wf_cfg.get("test_months",  3)
    start        = start        or wf_cfg.get("start") or cfg["split"]["in_sample_start"]
    end          = end          or wf_cfg.get("end")   or cfg["split"]["in_sample_end"]
    symbol       = symbol       or cfg["data"]["symbol"]

    windows = _generate_windows(start, end, train_months, test_months)
    if not windows:
        raise ValueError(
            f"Geen walk-forward vensters mogelijk voor {start} → {end} "
            f"met train={train_months}m / test={test_months}m."
        )

    logger.info(
        "Walk-forward: %d vensters  train=%dm  test=%dm  %s → %s",
        len(windows), train_months, test_months, start, end,
    )

    # Laad alle data één keer
    processed_dir = Path(cfg["data"]["paths"]["processed"])
    path_15m = processed_dir / f"{symbol}_15m.parquet"
    path_4h  = processed_dir / f"{symbol}_4h.parquet"
    df_15m_all = pd.read_parquet(path_15m)
    df_4h_all  = pd.read_parquet(path_4h)

    results: list[WalkForwardWindow] = []

    for i, (tr_start, tr_end, te_start, te_end) in enumerate(windows, 1):
        logger.info(
            "Venster %d/%d: train=%s→%s  test=%s→%s",
            i, len(windows), tr_start, tr_end, te_start, te_end,
        )

        metrics, trades = _run_window(
            cfg, df_15m_all, df_4h_all, symbol,
            tr_start, tr_end, te_start, te_end,
            filters=filters,
        )
        results.append(WalkForwardWindow(
            train_start = tr_start,
            train_end   = tr_end,
            test_start  = te_start,
            test_end    = te_end,
            metrics     = metrics,
            trades      = trades,
        ))

    return results


def summarize(windows: list[WalkForwardWindow]) -> dict:
    """Geaggregeerde statistieken over alle walk-forward vensters."""
    if not windows:
        return {}

    sharpes       = [w.metrics.sharpe_ratio  for w in windows]
    win_rates     = [w.metrics.win_rate      for w in windows]
    max_dds       = [w.metrics.max_drawdown  for w in windows]
    pfs           = [w.metrics.profit_factor for w in windows]
    trade_counts  = [w.metrics.trade_count   for w in windows]

    return {
        "n_windows":          len(windows),
        "total_trades":       sum(trade_counts),
        "avg_trades_per_wnd": sum(trade_counts) / len(windows),
        "sharpe_mean":        _mean(sharpes),
        "sharpe_min":         min(sharpes),
        "sharpe_max":         max(sharpes),
        "sharpe_positive_pct": sum(1 for s in sharpes if s > 0) / len(sharpes),
        "win_rate_mean":      _mean(win_rates),
        "max_drawdown_mean":  _mean(max_dds),
        "profit_factor_mean": _mean(pfs),
    }


# ---------------------------------------------------------------------------
# Venster generatie
# ---------------------------------------------------------------------------

def _generate_windows(
    start: str,
    end: str,
    train_months: int,
    test_months: int,
) -> list[tuple[str, str, str, str]]:
    """Genereer (train_start, train_end, test_start, test_end) tuples."""
    ts_start = pd.Timestamp(start, tz="UTC")
    ts_end   = pd.Timestamp(end,   tz="UTC")
    windows  = []

    cursor = ts_start
    while True:
        tr_end = cursor + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        te_end = tr_end  + pd.DateOffset(months=test_months)

        if te_end > ts_end:
            break

        windows.append((
            cursor.strftime("%Y-%m-%d"),
            tr_end.strftime("%Y-%m-%d"),
            (tr_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            te_end.strftime("%Y-%m-%d"),
        ))
        cursor += pd.DateOffset(months=test_months)  # rol venster 1 testperiode op

    return windows


# ---------------------------------------------------------------------------
# Één venster uitvoeren
# ---------------------------------------------------------------------------

def _run_window(
    cfg: dict,
    df_15m_all: pd.DataFrame,
    df_4h_all:  pd.DataFrame,
    symbol: str,
    tr_start: str,
    tr_end:   str,
    te_start: str,
    te_end:   str,
    filters: SweepFilters | None = None,
) -> tuple[BacktestMetrics, list[Trade]]:
    """Train HMM op train-venster, test backtest op test-venster."""

    def _slice(df: pd.DataFrame, s: str, e: str) -> pd.DataFrame:
        ts_s = pd.Timestamp(s, tz="UTC")
        ts_e = pd.Timestamp(e, tz="UTC")
        return df[(df.index >= ts_s) & (df.index <= ts_e)]

    # Train HMM op training data (met 90 dagen warmup voor ATR)
    warmup_4h  = pd.Timedelta(days=90)
    ts_tr_start = pd.Timestamp(tr_start, tz="UTC")
    df_4h_train = df_4h_all[
        (df_4h_all.index >= ts_tr_start - warmup_4h) &
        (df_4h_all.index <= pd.Timestamp(tr_end, tz="UTC"))
    ]
    regime_model = train(df_4h_train, cfg=cfg, save_path=None)

    # Regime voorspelling op test-venster (met 90 dagen warmup)
    ts_te_start = pd.Timestamp(te_start, tz="UTC")
    df_4h_test = df_4h_all[
        (df_4h_all.index >= ts_te_start - warmup_4h) &
        (df_4h_all.index <= pd.Timestamp(te_end, tz="UTC"))
    ]
    regimes_4h  = predict_regimes(df_4h_test, regime_model)

    # 15m data voor test-venster
    df_15m_test = _slice(df_15m_all, te_start, te_end)
    if df_15m_test.empty:
        logger.warning("Geen 15m data voor test-venster %s → %s", te_start, te_end)
        return _empty_metrics(cfg), []

    regimes_15m = align_regimes_to_15m(regimes_4h, df_15m_test)

    # SMC cache voor test-venster
    smc_cache = load_cache(cfg, start=te_start, end=te_end, symbol=symbol)
    common_idx  = df_15m_test.index.intersection(smc_cache.index)
    df_15m_test = df_15m_test.loc[common_idx]
    smc_cache   = smc_cache.loc[common_idx]
    regimes_15m = regimes_15m.reindex(common_idx)

    trades = sweep_run_loop(cfg, df_15m_test, smc_cache, regimes_15m, filters or SweepFilters())

    metrics = compute_metrics(
        trades,
        initial_capital=cfg["risk"]["capital_initial"],
    )
    return metrics, trades


def _empty_metrics(cfg: dict) -> BacktestMetrics:
    from src.backtest.metrics import BacktestMetrics
    return BacktestMetrics(
        sharpe_ratio=0.0, max_drawdown=0.0, win_rate=0.0,
        trade_count=0, profit_factor=0.0, total_return=0.0,
        avg_trade_pnl=0.0,
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
