"""
backtest/sweep_engine.py — Sweep-strategie backtest engine.

Gebruikt SweepDetector intern — dezelfde detector als de live trading loop.
Backtest en live trading gebruiken nu identieke signaallogica.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.backtest.metrics import BacktestMetrics, Trade, compute_metrics
from src.config_loader import load_config
from src.signals.detector import SweepDetector
from src.signals.filters import SweepFilters

logger = logging.getLogger(__name__)

__all__ = ["SweepFilters", "run_sweep_backtest", "compare_filters"]


def run_sweep_backtest(
    cfg:          dict | None = None,
    dataset:      str  = "in_sample",
    filters:      SweepFilters | None = None,
    allow_oos:    bool = False,
    start:        str | None = None,
    end:          str | None = None,
    pending_ttl:  int = 0,
) -> tuple[BacktestMetrics, list[Trade]]:
    """
    pending_ttl : int
        Aantal candles dat een limit order open blijft. 0 = directe fill
        (oorspronkelijk gedrag). Gebruik 5 om live order-fill te simuleren.
    """
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

    df_15m, regimes_15m, cache, ma200, df_lower = _load_data(cfg, start, end, filters.micro_bos_tf)
    if df_15m.empty:
        raise RuntimeError("Geen 15m data voor de opgegeven periode.")

    trades, signals_total, signals_filled = _run_loop(
        cfg, df_15m, cache, regimes_15m, filters, ma200, pending_ttl, df_lower
    )
    logger.info("Klaar: %d trades", len(trades))

    metrics = compute_metrics(trades, cfg["risk"]["capital_initial"])
    if pending_ttl > 0 and signals_total > 0:
        metrics.fill_rate     = signals_filled / signals_total
        metrics.signals_count = signals_total
    return metrics, trades


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
        "regime_bos10":   SweepFilters(regime=True, bos_confirm=True, bos_window=10),
        "long_bos10":     SweepFilters(direction="long",  bos_confirm=True, bos_window=10),
        "short_bos10":    SweepFilters(direction="short", bos_confirm=True, bos_window=10),
        "long_atr14":     SweepFilters(direction="long", atr_filter=True),
        "dynamic_200ma":  SweepFilters(direction="dynamic"),
        "micro_bos_3m":   SweepFilters(micro_bos_tf="3min", micro_bos_window=20),
        "micro_bos_5m":   SweepFilters(micro_bos_tf="5min", micro_bos_window=20),
        "long_micro_3m":  SweepFilters(direction="long",  micro_bos_tf="3min", micro_bos_window=20),
        "short_micro_3m": SweepFilters(direction="short", micro_bos_tf="3min", micro_bos_window=20),
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

def _load_data(cfg, start, end, micro_bos_tf=None):
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

    df_lower = None
    if micro_bos_tf:
        tf_friendly = micro_bos_tf.replace("min", "m")
        path_lower  = processed_dir / f"{symbol}_{tf_friendly}.parquet"
        if not path_lower.exists():
            raise FileNotFoundError(
                f"{path_lower} niet gevonden. "
                f"Voer uit: python scripts/build_cache.py --lower-tf {micro_bos_tf}"
            )
        df_lower = pd.read_parquet(path_lower)
        df_lower = df_lower[(df_lower.index >= ts_start) & (df_lower.index <= ts_end + pd.Timedelta(minutes=15))]
        logger.info("Lagere-TF data geladen: %s (%d candles)", tf_friendly, len(df_lower))

    return df_15m, regimes_15m, cache, ma200, df_lower


# ---------------------------------------------------------------------------
# Backtest loop
# ---------------------------------------------------------------------------

def _run_loop(cfg, df_15m, cache, regimes, filters, ma200=None, pending_ttl: int = 0, df_lower=None):
    rcfg         = cfg["risk"]
    fee_pct      = cfg["backtest"]["fee_pct"] / 100.0
    slippage_pct = cfg["backtest"]["slippage_pct"] / 100.0
    capital      = rcfg["capital_initial"]
    risk_pct     = rcfg["risk_per_trade_pct"] / 100.0
    rr           = rcfg["reward_ratio"]
    is_dynamic      = filters.direction == "dynamic"
    leverage             = cfg.get("derivatives", {}).get("leverage", 1)
    max_margin_frac      = rcfg.get("max_margin_fraction", 1.0)
    trailing_cfg         = rcfg.get("trailing_stop")
    next_open_entry      = cfg.get("backtest", {}).get("next_open_entry", False)
    # 8u = 32 candles van 15m; beide richtingen betalen funding (conservatieve aanname)
    funding_per_candle   = rcfg.get("funding_rate_per_8h", 0.0) / 32.0

    use_micro_bos = filters.micro_bos_tf is not None and df_lower is not None and len(df_lower) > 0

    # Als micro-BoS actief is: detector doet géén BOS-bevestiging zelf (dat doen wij hier)
    detector_filters = (
        dataclasses.replace(filters, bos_confirm=False)
        if use_micro_bos and filters.bos_confirm
        else filters
    )
    detector = SweepDetector(
        filters       = detector_filters,
        reward_ratio  = rr,
        sl_buffer_pct = rcfg["sl_buffer_pct"],
    )

    trades:           list[Trade]           = []
    open_pos:         _Position | None      = None
    pending:          _PendingLimit | None  = None
    pending_micro:    _PendingMicroBoS | None = None
    pending_market:   _PendingMarket | None = None
    signals_total    = 0
    signals_filled   = 0

    for i, ts in enumerate(df_15m.index):
        ohlc_row      = df_15m.iloc[i].copy()
        ohlc_row.name = ts
        regime        = _get_regime(regimes, ts)
        smc_row       = cache.loc[ts] if ts in cache.index else _empty_smc_row()
        low           = float(ohlc_row["low"])
        high          = float(ohlc_row["high"])

        if is_dynamic and ma200 is not None:
            ma200_val = ma200.get(ts, float("nan"))
            filters.direction = (
                "long" if (pd.isna(ma200_val) or ohlc_row["close"] > ma200_val) else "short"
            )

        # Stap 0: vul pending marktvulling op open van deze candle
        if open_pos is None and pending_market is not None:
            open_price     = float(ohlc_row["open"])
            pm             = pending_market
            pending_market = None
            sl_gapped = (
                (pm.direction == "long"  and open_price <= pm.sl_price) or
                (pm.direction == "short" and open_price >= pm.sl_price)
            )
            if not sl_gapped:
                sl_dist = abs(open_price - pm.sl_price)
                if sl_dist > open_price * 0.0001:
                    tp   = (open_price + sl_dist * rr) if pm.direction == "long" \
                           else (open_price - sl_dist * rr)
                    size = (capital * risk_pct) / sl_dist
                    if leverage > 1 and max_margin_frac < 1.0:
                        size = min(size, (capital * max_margin_frac * leverage) / open_price)
                    signals_filled += 1
                    open_pos = _Position(
                        direction               = pm.direction,
                        entry_price             = open_price,
                        sl_price                = pm.sl_price,
                        tp_price                = tp,
                        size                    = size,
                        entry_ts                = ts,
                        regime                  = pm.regime,
                        trailing_cfg            = trailing_cfg,
                        funding_rate_per_candle = funding_per_candle,
                    )

        # Stap 1: open positie bewaken
        if open_pos is not None:
            result = open_pos.check(ohlc_row)
            if result:
                trade    = _close(open_pos, result, ts, fee_pct, slippage_pct, capital)
                trades.append(trade)
                capital += trade.pnl_capital
                open_pos = None

        # Stap 2a: pending limit order proberen te vullen
        if open_pos is None and pending is not None:
            filled = (
                (pending.direction == "long"  and low  <= pending.entry_price) or
                (pending.direction == "short" and high >= pending.entry_price)
            )
            if filled:
                signals_filled += 1
                open_pos = _Position(
                    direction               = pending.direction,
                    entry_price             = pending.entry_price,
                    sl_price                = pending.sl_price,
                    tp_price                = pending.tp_price,
                    size                    = pending.size,
                    entry_ts                = ts,
                    regime                  = pending.regime,
                    trailing_cfg            = trailing_cfg,
                    funding_rate_per_candle = funding_per_candle,
                )
                pending = None
            else:
                pending.waited += 1
                if pending.waited >= pending_ttl:
                    pending = None

        # Stap 2b: pending micro-BoS controleren op lagere-TF candles binnen deze 15m-periode
        if use_micro_bos and open_pos is None and pending is None and pending_micro is not None:
            if ts > pending_micro.sweep_ts:
                lower_end   = ts + pd.Timedelta(minutes=15)
                lower_slice = df_lower[(df_lower.index >= ts) & (df_lower.index < lower_end)]
                for lt_ts, lt_row in lower_slice.iterrows():
                    lt_close = float(lt_row["close"])
                    bos_ok = (
                        (pending_micro.direction == "long"  and lt_close > pending_micro.liq_level) or
                        (pending_micro.direction == "short" and lt_close < pending_micro.liq_level)
                    )
                    pending_micro.candles_seen += 1
                    if bos_ok:
                        sl     = pending_micro.sl_price
                        sl_dist = abs(lt_close - sl)
                        if sl_dist > 0:
                            tp   = (lt_close + sl_dist * rr) if pending_micro.direction == "long" else (lt_close - sl_dist * rr)
                            size = (capital * risk_pct) / sl_dist
                            if leverage > 1 and max_margin_frac < 1.0:
                                size = min(size, (capital * max_margin_frac * leverage) / lt_close)
                            signals_filled += 1
                            open_pos = _Position(
                                direction               = pending_micro.direction,
                                entry_price             = lt_close,
                                sl_price                = sl,
                                tp_price                = tp,
                                size                    = size,
                                entry_ts                = lt_ts,
                                regime                  = pending_micro.regime,
                                trailing_cfg            = trailing_cfg,
                                funding_rate_per_candle = funding_per_candle,
                            )
                        pending_micro = None
                        break
                    if pending_micro is not None and pending_micro.candles_seen >= pending_micro.window:
                        pending_micro = None
                        break

        # Stap 3: detector altijd aanroepen (bewaart interne BOS-state)
        signal = detector.on_candle(ohlc_row, smc_row, regime)

        # Stap 4: nieuw signaal alleen verwerken als volledig vrij
        if (open_pos is None and pending is None and pending_micro is None
                and pending_market is None and signal is not None and signal.sl_distance > 0):
            signals_total += 1
            if use_micro_bos and signal.liq_level > 0:
                # Wacht op micro-BoS op lagere TF in plaats van directe entry
                pending_micro = _PendingMicroBoS(
                    direction    = signal.direction,
                    liq_level    = signal.liq_level,
                    sl_price     = signal.sl_price,
                    regime       = signal.regime,
                    sweep_ts     = ts,
                    window       = filters.micro_bos_window,
                )
            elif pending_ttl > 0:
                size = (capital * risk_pct) / signal.sl_distance
                if leverage > 1 and max_margin_frac < 1.0:
                    size = min(size, (capital * max_margin_frac * leverage) / signal.entry_price)
                pending = _PendingLimit(
                    direction   = signal.direction,
                    entry_price = signal.entry_price,
                    sl_price    = signal.sl_price,
                    tp_price    = signal.tp_price,
                    size        = size,
                    signal_ts   = ts,
                    regime      = signal.regime,
                    ttl         = pending_ttl,
                )
            elif next_open_entry:
                # Entry op open van volgende candle (realistischere simulatie)
                pending_market = _PendingMarket(
                    direction = signal.direction,
                    sl_price  = signal.sl_price,
                    regime    = signal.regime,
                )
            else:
                # Directe fill (oorspronkelijk gedrag)
                size = (capital * risk_pct) / signal.sl_distance
                if leverage > 1 and max_margin_frac < 1.0:
                    size = min(size, (capital * max_margin_frac * leverage) / signal.entry_price)
                signals_filled += 1
                open_pos = _Position(
                    direction               = signal.direction,
                    entry_price             = signal.entry_price,
                    sl_price                = signal.sl_price,
                    tp_price                = signal.tp_price,
                    size                    = size,
                    entry_ts                = ts,
                    regime                  = signal.regime,
                    trailing_cfg            = trailing_cfg,
                    funding_rate_per_candle = funding_per_candle,
                )

    if is_dynamic:
        filters.direction = "dynamic"

    return trades, signals_total, signals_filled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _PendingLimit:
    direction:   str
    entry_price: float
    sl_price:    float
    tp_price:    float
    size:        float
    signal_ts:   pd.Timestamp
    regime:      bool | None
    ttl:         int
    waited:      int = 0


@dataclass
class _PendingMarket:
    """Vul op open van volgende candle na BOS-signaal (realistischere entry)."""
    direction: str
    sl_price:  float
    regime:    bool | None


@dataclass
class _PendingMicroBoS:
    """Wacht op een BoS-bevestiging op lagere TF na een 15m sweep."""
    direction:    str
    liq_level:    float          # gesweept niveau; BoS = close boven/onder dit niveau
    sl_price:     float          # voorberekend vanuit de sweep-candle
    regime:       bool | None
    sweep_ts:     pd.Timestamp   # 15m timestamp van de sweep
    window:       int            # max lagere-TF candles te controleren
    candles_seen: int = 0        # teller lagere-TF candles al gezien


@dataclass
class _Position:
    direction:              str
    entry_price:            float
    sl_price:               float
    tp_price:               float
    size:                   float
    entry_ts:               pd.Timestamp
    regime:                 bool | None
    trailing_cfg:           dict | None = None
    funding_rate_per_candle: float      = 0.0

    def __post_init__(self) -> None:
        self._sl_dist_0       = abs(self.entry_price - self.sl_price)
        self._peak            = self.entry_price
        self._be_done         = False
        self._funding_accrued = 0.0

    def check(self, row: pd.Series) -> str | None:
        low, high = float(row["low"]), float(row["high"])

        # Controleer SL/TP op huidige candle vóór trailing-update
        if self.direction == "long":
            if low  <= self.sl_price: return "loss"
            if high >= self.tp_price: return "win"
        else:
            if high >= self.sl_price: return "loss"
            if low  <= self.tp_price: return "win"

        # Candle overleeft → peak bijwerken en trailing SL berekenen voor volgende candle
        if self.direction == "long":
            self._peak = max(self._peak, high)
        else:
            self._peak = min(self._peak, low)

        tcfg = self.trailing_cfg
        if tcfg and tcfg.get("enabled") and self._sl_dist_0 > 0:
            be_r    = tcfg.get("breakeven_at_r", 1.0)
            trail_r = tcfg.get("trail_after_r")
            step    = tcfg.get("trail_step_r", 0.5) * self._sl_dist_0

            if self.direction == "long":
                if not self._be_done and self._peak >= self.entry_price + be_r * self._sl_dist_0:
                    self.sl_price = max(self.sl_price, self.entry_price)
                    self._be_done = True
                if trail_r is not None and step > 0 and \
                        self._peak >= self.entry_price + trail_r * self._sl_dist_0:
                    ideal = self._peak - trail_r * self._sl_dist_0
                    if ideal > self.sl_price:
                        self.sl_price += int((ideal - self.sl_price) / step) * step
            else:
                if not self._be_done and self._peak <= self.entry_price - be_r * self._sl_dist_0:
                    self.sl_price = min(self.sl_price, self.entry_price)
                    self._be_done = True
                if trail_r is not None and step > 0 and \
                        self._peak <= self.entry_price - trail_r * self._sl_dist_0:
                    ideal = self._peak + trail_r * self._sl_dist_0
                    if ideal < self.sl_price:
                        self.sl_price -= int((self.sl_price - ideal) / step) * step

        # Funding kosten over deze overleefde candle
        if self.funding_rate_per_candle > 0:
            self._funding_accrued += self.entry_price * self.size * self.funding_rate_per_candle

        return None


def _close(pos, outcome, exit_ts, fee_pct, slippage_pct, capital):
    exit_price    = pos.tp_price if outcome == "win" else pos.sl_price
    raw_pnl       = ((exit_price - pos.entry_price) if pos.direction == "long"
                     else (pos.entry_price - exit_price)) * pos.size
    fee_deducted  = (pos.entry_price + exit_price) * pos.size * fee_pct
    slippage_cost = abs(exit_price - pos.entry_price) * pos.size * slippage_pct
    funding_cost  = getattr(pos, "_funding_accrued", 0.0)
    net_pnl       = raw_pnl - fee_deducted - slippage_cost - funding_cost

    return Trade(
        entry_time=pos.entry_ts, exit_time=exit_ts,
        direction=pos.direction, entry_price=pos.entry_price,
        exit_price=exit_price, sl_price=pos.sl_price, tp_price=pos.tp_price,
        outcome=outcome, pnl_pct=net_pnl/capital,
        pnl_capital=net_pnl, regime=pos.regime,
        fee_cost=fee_deducted + slippage_cost + funding_cost,
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