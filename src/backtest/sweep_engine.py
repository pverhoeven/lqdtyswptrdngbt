"""
backtest/sweep_engine.py — Sweep-strategie backtest engine.

Gebruikt SweepDetector intern — dezelfde detector als de live trading loop.
Backtest en live trading gebruiken nu identieke signaallogica.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.backtest.metrics import BacktestMetrics, Trade, compute_metrics
from src.config_loader import load_config
from src.signals.detector import SweepDetector
from src.signals.filters import SweepFilters

logger = logging.getLogger(__name__)

__all__ = ["SweepFilters", "run_sweep_backtest", "compare_filters"]


def run_sweep_backtest(
    cfg:       dict | None = None,
    dataset:   str  = "in_sample",
    filters:   SweepFilters | None = None,
    allow_oos: bool = False,
    start:     str | None = None,
    end:       str | None = None,
) -> tuple[BacktestMetrics, list[Trade]]:
    if cfg is None:
        cfg = load_config()
    if filters is None:
        filters = SweepFilters()

    if start and end:
        label = f"CUSTOM ({start} → {end})"
    else:
        start, end = _resolve_period(cfg, dataset, allow_oos)
        label = f"{dataset.upper()} ({start} → {end})"
    logger.info("Sweep backtest: %s filter=%s", label, filters)

    df_15m, regimes_15m, cache, ma200 = _load_data(cfg, start, end)
    if df_15m.empty:
        raise RuntimeError("Geen 15m data voor de opgegeven periode.")

    trades = _run_loop(cfg, df_15m, cache, regimes_15m, filters, ma200)
    logger.info("Klaar: %d trades", len(trades))
    return compute_metrics(trades, cfg["risk"]["capital_initial"]), trades


def compare_filters(
    cfg:     dict | None = None,
    dataset: str  = "in_sample",
) -> pd.DataFrame:
    if cfg is None:
        cfg = load_config()

    configs = {
        "baseline":    SweepFilters(direction="both"),
        "regime":      SweepFilters(regime=True),
        "long_only":   SweepFilters(direction="long"),
        "short_only":  SweepFilters(direction="short"),
        "bos10":       SweepFilters(bos_confirm=True, bos_window=10),
        "bos20":       SweepFilters(bos_confirm=True, bos_window=20),
        "regime_long": SweepFilters(regime=True, direction="long"),
        "regime_short":SweepFilters(regime=True, direction="short"),
        "regime_bos10":SweepFilters(regime=True, bos_confirm=True, bos_window=10),
        "long_bos10":  SweepFilters(direction="long",  bos_confirm=True, bos_window=10),
        "short_bos10": SweepFilters(direction="short", bos_confirm=True, bos_window=10),
        "long_atr14":    SweepFilters(direction="long", atr_filter=True),
        "dynamic_200ma": SweepFilters(direction="dynamic"),
    }

    rows = []
    for naam, f in configs.items():
        try:
            m, _ = run_sweep_backtest(cfg, dataset, f)
            rows.append({
                "configuratie":  naam,
                "trades":        m.trade_count,
                "win_rate":      f"{m.win_rate:.1%}",
                "sharpe":        round(m.sharpe_ratio, 2),
                "max_drawdown":  f"{m.max_drawdown:.1%}",
                "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else "∞",
                "total_return":  f"{m.total_return:.1%}",
            })
        except Exception as exc:
            logger.warning("Filter '%s' mislukt: %s", naam, exc)
            rows.append({"configuratie": naam, "trades": 0, "win_rate": "–",
                         "sharpe": None, "max_drawdown": "–",
                         "profit_factor": None, "total_return": "–"})

    return pd.DataFrame(rows).set_index("configuratie")


# ---------------------------------------------------------------------------
# Data laden
# ---------------------------------------------------------------------------

def _load_data(cfg, start, end):
    from src.data.cache import load_cache
    from src.regime.hmm import align_regimes_to_15m, load_model, predict_regimes

    processed_dir = Path(cfg["data"]["paths"]["processed"])
    symbol = cfg["data"]["symbol"]
    tf     = cfg["data"]["timeframes"]["signal"].replace("min", "m")

    path_15m = processed_dir / f"{symbol}_{tf}.parquet"
    path_4h  = processed_dir / f"{symbol}_4h.parquet"
    for p in (path_15m, path_4h):
        if not p.exists():
            raise FileNotFoundError(f"{p} niet gevonden.")

    ts_start = pd.Timestamp(start, tz="UTC")
    ts_end   = pd.Timestamp(end,   tz="UTC")

    df_15m_full = pd.read_parquet(path_15m)
    # 200-daagse MA: 200 dagen × 96 15m-candles per dag = 19200 periodes.
    # Berekend op volledige dataset zodat ook vroege backtestperiodes correcte waarden hebben.
    ma200_full = df_15m_full["close"].rolling(19200, min_periods=19200).mean()

    df_15m = df_15m_full[(df_15m_full.index >= ts_start) & (df_15m_full.index <= ts_end)]
    ma200  = ma200_full.reindex(df_15m.index)

    df_4h  = pd.read_parquet(path_4h)
    df_4h  = df_4h[
        (df_4h.index >= ts_start - pd.Timedelta(days=90)) &
        (df_4h.index <= ts_end)
    ]

    model_path = processed_dir / "hmm_regime_model.pkl"
    if not model_path.exists():
        logger.info("Geen regime model gevonden — training starten op in-sample 4h data.")
        from src.regime.hmm import train
        is_start, is_end = _resolve_period(cfg, "in_sample", allow_oos=False)
        ts_is_start = pd.Timestamp(is_start, tz="UTC")
        ts_is_end   = pd.Timestamp(is_end,   tz="UTC")
        df_4h_train = pd.read_parquet(path_4h)
        df_4h_train = df_4h_train[
            (df_4h_train.index >= ts_is_start - pd.Timedelta(days=90)) &
            (df_4h_train.index <= ts_is_end)
        ]
        regime_model = train(df_4h_train, cfg=cfg, save_path=str(model_path))
    else:
        regime_model = load_model(cfg)
    regimes_15m = align_regimes_to_15m(predict_regimes(df_4h, regime_model), df_15m)

    cache = load_cache(cfg, start=start, end=end)
    common      = df_15m.index.intersection(cache.index)
    df_15m      = df_15m.loc[common]
    cache       = cache.loc[common]
    regimes_15m = regimes_15m.reindex(common)
    return df_15m, regimes_15m, cache, ma200


# ---------------------------------------------------------------------------
# Backtest loop
# ---------------------------------------------------------------------------

def _run_loop(cfg, df_15m, cache, regimes, filters, ma200=None):
    rcfg     = cfg["risk"]
    fee_pct  = cfg["backtest"]["fee_pct"] / 100.0
    slippage_pct  = cfg["backtest"]["slippage_pct"] / 100.0
    capital  = rcfg["capital_initial"]
    risk_pct = rcfg["risk_per_trade_pct"] / 100.0

    is_dynamic = filters.direction == "dynamic"

    detector = SweepDetector(
        filters       = filters,
        reward_ratio  = rcfg["reward_ratio"],
        sl_buffer_pct = rcfg["sl_buffer_pct"],
    )

    trades:   list[Trade] = []
    open_pos: _Position | None = None

    for i, ts in enumerate(df_15m.index):
        ohlc_row      = df_15m.iloc[i].copy()
        ohlc_row.name = ts
        regime        = _get_regime(regimes, ts)
        smc_row       = cache.loc[ts] if ts in cache.index else _empty_smc_row()

        if is_dynamic and ma200 is not None:
            ma200_val = ma200.get(ts, float("nan"))
            filters.direction = (
                "long" if (pd.isna(ma200_val) or ohlc_row["close"] > ma200_val) else "short"
            )

        if open_pos is not None:
            result = open_pos.check(ohlc_row)
            if result:
                trade      = _close(open_pos, result, ts, fee_pct,slippage_pct, capital)
                trades.append(trade)
                capital   += trade.pnl_capital
                open_pos   = None

        if open_pos is None:
            signal = detector.on_candle(ohlc_row, smc_row, regime)
            if signal is not None and signal.sl_distance > 0:
                size     = (capital * risk_pct) / signal.sl_distance
                open_pos = _Position(
                    direction   = signal.direction,
                    entry_price = signal.entry_price,
                    sl_price    = signal.sl_price,
                    tp_price    = signal.tp_price,
                    size        = size,
                    entry_ts    = ts,
                    regime      = signal.regime,
                )

    if is_dynamic:
        filters.direction = "dynamic"

    return trades


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _Position:
    direction:   str
    entry_price: float
    sl_price:    float
    tp_price:    float
    size:        float
    entry_ts:    pd.Timestamp
    regime:      bool | None

    def check(self, row: pd.Series) -> str | None:
        low, high = float(row["low"]), float(row["high"])
        if self.direction == "long":
            if low  <= self.sl_price: return "loss"
            if high >= self.tp_price: return "win"
        else:
            if high >= self.sl_price: return "loss"
            if low  <= self.tp_price: return "win"
        return None


def _close(pos, outcome, exit_ts, fee_pct, slippage_pct, capital):
    exit_price    = pos.tp_price if outcome == "win" else pos.sl_price
    raw_pnl       = ((exit_price - pos.entry_price) if pos.direction == "long"
                     else (pos.entry_price - exit_price)) * pos.size
    fee_deducted  = (pos.entry_price + exit_price) * pos.size * fee_pct
    slippage_cost = abs(exit_price - pos.entry_price) * pos.size * slippage_pct
    net_pnl       = raw_pnl - fee_deducted - slippage_cost

    return Trade(
        entry_time=pos.entry_ts, exit_time=exit_ts,
        direction=pos.direction, entry_price=pos.entry_price,
        exit_price=exit_price, sl_price=pos.sl_price, tp_price=pos.tp_price,
        outcome=outcome, pnl_pct=net_pnl/capital,
        pnl_capital=net_pnl, regime=pos.regime, fee_cost=fee_deducted + slippage_cost
    )


def _resolve_period(cfg, dataset, allow_oos):
    split = cfg["split"]
    if dataset == "in_sample":
        return split["in_sample_start"], split["in_sample_end"]
    if dataset == "oos":
        if not allow_oos:
            raise ValueError("OOS afgegrendeld. Gebruik allow_oos=True.")
        return split["oos_start"], split["oos_end"]
    raise ValueError(f"Onbekend dataset: '{dataset}'")


def _get_regime(regimes, ts):
    try:
        v = regimes.get(ts)
        return None if pd.isna(v) else bool(v)
    except (KeyError, TypeError):
        return None


def _empty_smc_row():
    cols = ["ob","ob_top","ob_bottom","ob_pct","ob_mitigated_idx",
            "liq","liq_level","liq_end_idx","liq_swept_idx",
            "bos","choch","structure_level","structure_broken_idx","atr"]
    return pd.Series(
        {c: 0.0 if c in ("ob","liq","bos","choch") else float("nan") for c in cols}
    )