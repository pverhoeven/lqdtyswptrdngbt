"""
DEPRECATED: engine.py — Gebruik run_sweep_backtest (sweep_engine.py) en run_walk_forward (walk_forward.py).
Deze module wordt niet meer actief onderhouden.

Verwerkt de 15m data candle-for-candle. Integreert:
- LifecycleEngine (signalen)
- HMM regime (filter)
- Risicobeheer (positiebepaling, SL/TP)
- Kostensimulatie (fee per trade)

OOS-afgrendeling: data na oos_start wordt geblokkeerd tenzij allow_oos=True.

Vereenvoudigingen voor eerste backtest:
- Limit order op OB-midpoint: gevuld als low (long) of high (short) de entry raakt
- Max 1 open trade tegelijk
- Geen slippage model buiten de fee
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from src.backtest.metrics import BacktestMetrics, Trade, compute_metrics, equity_curve
from src.config_loader import load_config
from src.data.cache import load_cache
from src.regime.hmm import align_regimes_to_15m, load_model, predict_regimes
from src.smc.lifecycle import LifecycleEngine, SetupSignal

logger = logging.getLogger(__name__)

OOS_START = "2023-01-01"


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def run_backtest(
    cfg: dict | None = None,
    dataset: str = "in_sample",   # "in_sample" of "oos"
    allow_oos: bool = False,
    symbol: str | None = None,
) -> tuple[BacktestMetrics, list[Trade]]:
    """
    Voer een volledige backtest uit.

    Parameters
    ----------
    cfg : dict, optional
    dataset : str
        "in_sample" of "oos".
    allow_oos : bool
        Moet expliciet True zijn voor OOS-run (veiligheidsslot).
    symbol : str, optional
        Symbool override (bijv. "ETHUSDT"). Standaard: cfg["data"]["symbol"].

    Returns
    -------
    tuple[BacktestMetrics, list[Trade]]
    """
    if cfg is None:
        cfg = load_config()

    symbol = symbol or cfg["data"]["symbol"]
    start, end = _resolve_period(cfg, dataset, allow_oos)
    logger.info("Backtest: %s  %s  (%s → %s)", symbol, dataset.upper(), start, end)

    # --- Data laden ---
    df_15m, df_4h = _load_data(cfg, start, end, symbol)

    if df_15m.empty:
        raise RuntimeError("Geen 15m data gevonden voor de opgegeven periode.")

    # --- Regime detectie ---
    processed_dir = Path(cfg["data"]["paths"]["processed"])
    model_path    = str(processed_dir / f"hmm_regime_model_{symbol}.pkl")

    if not Path(model_path).exists():
        # Train altijd op in-sample data, ook als de huidige run OOS is.
        logger.info("Geen regime model gevonden voor %s — training starten op in-sample 4h data.", symbol)
        from src.regime.hmm import train
        is_start, is_end = _resolve_period(cfg, "in_sample", allow_oos=False)
        _, df_4h_train = _load_data(cfg, is_start, is_end, symbol)
        regime_model = train(df_4h_train, cfg=cfg, save_path=model_path)
    else:
        regime_model = load_model(cfg, symbol=symbol)
    regimes_4h   = predict_regimes(df_4h, regime_model)
    regimes_15m  = align_regimes_to_15m(regimes_4h, df_15m)

    # --- SMC cache laden ---
    smc_cache = load_cache(cfg, start=start, end=end, symbol=symbol)

    # Zorg dat cache en 15m data dezelfde index hebben
    common_idx = df_15m.index.intersection(smc_cache.index)
    df_15m    = df_15m.loc[common_idx]
    smc_cache = smc_cache.loc[common_idx]
    regimes_15m = regimes_15m.reindex(common_idx)

    # --- Backtest loop ---
    trades = _run_loop(cfg, df_15m, smc_cache, regimes_15m)

    logger.info("Backtest klaar: %d trades", len(trades))

    metrics = compute_metrics(
        trades,
        initial_capital=cfg["risk"]["capital_initial"],
    )
    return metrics, trades


# ---------------------------------------------------------------------------
# Periode resolver
# ---------------------------------------------------------------------------

def _resolve_period(cfg: dict, dataset: str, allow_oos: bool) -> tuple[str, str]:
    split = cfg["split"]

    if dataset == "in_sample":
        return split["in_sample_start"], split["in_sample_end"]

    if dataset == "oos":
        if not allow_oos:
            raise ValueError(
                "OOS-data is afgegrendeld. "
                "Gebruik allow_oos=True alleen voor de finale evaluatie."
            )
        return split["oos_start"], split["oos_end"]

    raise ValueError(f"Onbekend dataset: '{dataset}'. Gebruik 'in_sample' of 'oos'.")


# ---------------------------------------------------------------------------
# Data laden
# ---------------------------------------------------------------------------

def _load_data(
    cfg: dict,
    start: str,
    end: str,
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Laad 15m en 4h data voor de opgegeven periode."""
    processed_dir = Path(cfg["data"]["paths"]["processed"])

    path_15m = processed_dir / f"{symbol}_15m.parquet"
    path_4h  = processed_dir / f"{symbol}_4h.parquet"

    for p in (path_15m, path_4h):
        if not p.exists():
            raise FileNotFoundError(
                f"{p} niet gevonden. "
                "Voer eerst scripts/build_cache.py uit."
            )

    df_15m = pd.read_parquet(path_15m)
    df_4h  = pd.read_parquet(path_4h)

    # Filter op periode
    ts_start = pd.Timestamp(start, tz="UTC")
    ts_end   = pd.Timestamp(end,   tz="UTC")

    df_15m = df_15m[(df_15m.index >= ts_start) & (df_15m.index <= ts_end)]

    # 4h: laad iets ruimer voor HMM warmup
    warmup_4h = pd.Timedelta(days=90)
    df_4h = df_4h[
        (df_4h.index >= ts_start - warmup_4h) &
        (df_4h.index <= ts_end)
    ]

    return df_15m, df_4h


# ---------------------------------------------------------------------------
# Backtest loop
# ---------------------------------------------------------------------------

def _run_loop(
    cfg: dict,
    df_15m: pd.DataFrame,
    smc_cache: pd.DataFrame,
    regimes_15m: pd.Series,
) -> list[Trade]:

    rcfg     = cfg["risk"]
    fee_pct  = cfg["backtest"]["fee_pct"] / 100.0
    capital  = rcfg["capital_initial"]
    rr       = rcfg["reward_ratio"]
    risk_pct = rcfg["risk_per_trade_pct"] / 100.0
    max_open = rcfg["max_open_trades"]

    ts_cfg = rcfg.get("trailing_stop", {})
    trailing_cfg = ts_cfg if ts_cfg.get("enabled", False) else {}

    pe_cfg = rcfg.get("partial_exit", {})
    partial_cfg = pe_cfg if pe_cfg.get("enabled", False) else {}

    lifecycle = LifecycleEngine(cfg)
    trades: list[Trade] = []
    open_trade: _OpenTrade | None = None

    for i, (ts, ohlc_row) in enumerate(df_15m.iterrows()):
        smc_row = smc_cache.loc[ts] if ts in smc_cache.index else _empty_smc_row()
        regime  = regimes_15m.get(ts)  # type: ignore[call-overload]

        # --- Check open trade ---
        if open_trade is not None:
            result = open_trade.check(ohlc_row, i)
            if result is not None:
                trade = _close_trade(open_trade, result, ohlc_row, ts, fee_pct, capital)
                trades.append(trade)
                capital += trade.pnl_capital
                open_trade = None

        # --- Lifecycle update ---
        n_open = 1 if open_trade is not None else 0
        signals = lifecycle.update(i, ohlc_row, smc_row, bool(regime) if pd.notna(regime) else None)

        # --- Verwerk signalen ---
        for signal in signals:
            if n_open >= max_open:
                break

            tp_price = _calc_tp(signal, rr)
            risk_amount = capital * risk_pct
            sl_distance = abs(signal.entry_price - signal.sl_price)

            if sl_distance == 0:
                continue

            position_size = risk_amount / sl_distance  # in base asset

            open_trade = _OpenTrade(
                signal        = signal,
                tp_price      = tp_price,
                position_size = position_size,
                open_candle   = i,
                trailing_cfg  = trailing_cfg,
                partial_cfg   = partial_cfg,
            )
            n_open = 1

    return trades


# ---------------------------------------------------------------------------
# Open trade management
# ---------------------------------------------------------------------------

class _OpenTrade:
    """Beheert één open positie, inclusief optionele trailing stop / breakeven."""

    def __init__(
        self,
        signal: SetupSignal,
        tp_price: float,
        position_size: float,
        open_candle: int,
        trailing_cfg: dict | None = None,
        partial_cfg:  dict | None = None,
    ) -> None:
        self.signal        = signal
        self.tp_price      = tp_price
        self.position_size = position_size
        self._original_size = position_size   # ongewijzigd voor fee-berekening
        self.open_candle   = open_candle
        self._filled       = False

        self._trailing_cfg  = trailing_cfg or {}
        self._sl_distance   = abs(signal.entry_price - signal.sl_price)
        self._current_sl    = signal.sl_price  # beweegt bij trailing/BE
        self._best_price: float | None = None
        self._be_activated  = False

        self._partial_cfg             = partial_cfg or {}
        self._partial_taken           = False
        self._partial_pnl_raw         = 0.0   # P&L van partial exits, vóór fees
        self._partial_exit_notional   = 0.0   # som van (prijs × grootte) voor fee-calc

    def check(
        self,
        ohlc_row: pd.Series,
        candle_idx: int,
    ) -> str | None:
        """
        Controleer of de limit entry gevuld wordt en of SL/TP geraakt is.

        Returns
        -------
        str | None
            "win", "loss", of None als de trade nog open is.
        """
        sig  = self.signal
        low  = float(ohlc_row["low"])
        high = float(ohlc_row["high"])

        # --- Wacht op fill als entry nog niet gevuld is ---
        if not self._filled:
            if sig.direction == "long" and low <= sig.entry_price:
                self._filled = True
            elif sig.direction == "short" and high >= sig.entry_price:
                self._filled = True
            else:
                return None  # nog niet gevuld
            return None  # gevuld op deze candle; SL/TP check pas volgende candle

        # --- Update trailing/breakeven vóór SL/TP check ---
        if self._trailing_cfg:
            self._update_sl(low, high)

        # --- Partial exit check ---
        if self._partial_cfg and not self._partial_taken:
            self._check_partial(low, high)

        # --- Eenmaal gevuld: check SL/TP in volgorde van worst-case ---
        if sig.direction == "long":
            if low <= self._current_sl:
                return "loss"
            if high >= self.tp_price:
                return "win"
        else:
            if high >= self._current_sl:
                return "loss"
            if low <= self.tp_price:
                return "win"

        return None  # nog open

    def _update_sl(self, low: float, high: float) -> None:
        """Beweeg SL naar breakeven en/of trail achter de beste prijs."""
        sig   = self.signal
        entry = sig.entry_price
        dist  = self._sl_distance
        cfg   = self._trailing_cfg

        be_r     = cfg.get("breakeven_at_r", 0.0)
        trail_r  = cfg.get("trail_after_r")
        trail_s  = cfg.get("trail_step_r", 0.5)

        if dist == 0:
            return

        if sig.direction == "long":
            favorable = high
            if self._best_price is None or favorable > self._best_price:
                self._best_price = favorable
            r_mult = (self._best_price - entry) / dist
        else:
            favorable = low
            if self._best_price is None or favorable < self._best_price:
                self._best_price = favorable
            r_mult = (entry - self._best_price) / dist

        # Breakeven: verschuif SL naar entry na be_r × risico winst
        if be_r > 0 and r_mult >= be_r and not self._be_activated:
            self._current_sl   = entry
            self._be_activated = True

        # Trailing: trail SL achter beste prijs na trail_r × risico winst
        if trail_r and r_mult >= trail_r:
            if sig.direction == "long":
                new_sl = self._best_price - dist * trail_s
                self._current_sl = max(self._current_sl, new_sl)
            else:
                new_sl = self._best_price + dist * trail_s
                self._current_sl = min(self._current_sl, new_sl)


    def _check_partial(self, low: float, high: float) -> None:
        """Sluit een deel van de positie zodra exit_r winst bereikt is."""
        sig      = self.signal
        dist     = self._sl_distance
        exit_r   = self._partial_cfg.get("exit_r", 1.0)
        fraction = self._partial_cfg.get("exit_fraction", 0.5)

        if sig.direction == "long":
            exit_level = sig.entry_price + dist * exit_r
            if high < exit_level:
                return
            partial_exit_price = exit_level
            self._partial_pnl_raw += (
                (partial_exit_price - sig.entry_price) * self.position_size * fraction
            )
        else:
            exit_level = sig.entry_price - dist * exit_r
            if low > exit_level:
                return
            partial_exit_price = exit_level
            self._partial_pnl_raw += (
                (sig.entry_price - partial_exit_price) * self.position_size * fraction
            )

        partial_size = self.position_size * fraction
        self._partial_exit_notional += partial_exit_price * partial_size
        self.position_size          -= partial_size
        self._partial_taken          = True

        if self._partial_cfg.get("move_sl_to_be", True):
            self._current_sl   = sig.entry_price
            self._be_activated = True


def _close_trade(
    open_trade: _OpenTrade,
    outcome: str,
    ohlc_row: pd.Series,
    exit_time: pd.Timestamp,
    fee_pct: float,
    capital: float,
) -> Trade:
    sig            = open_trade.signal
    remaining_size = open_trade.position_size        # gereduceerd als partial genomen
    original_size  = open_trade._original_size       # voor entry-fee berekening

    exit_price = open_trade.tp_price if outcome == "win" else open_trade._current_sl

    if sig.direction == "long":
        final_raw = (exit_price - sig.entry_price) * remaining_size
    else:
        final_raw = (sig.entry_price - exit_price) * remaining_size

    total_raw_pnl = open_trade._partial_pnl_raw + final_raw

    # Fees: entry (op volle grootte) + partial exits + finale exit
    cost = (
        sig.entry_price * original_size            # entry
        + open_trade._partial_exit_notional        # partial exits (0 als geen partial)
        + exit_price * remaining_size              # finale exit
    ) * fee_pct

    net_pnl = total_raw_pnl - cost
    pnl_pct = net_pnl / capital

    return Trade(
        entry_time   = sig.candle_time,
        exit_time    = exit_time,
        direction    = sig.direction,
        entry_price  = sig.entry_price,
        exit_price   = exit_price,
        sl_price     = sig.sl_price,
        tp_price     = open_trade.tp_price,
        outcome      = "win" if net_pnl > 0 else outcome,
        pnl_pct      = pnl_pct,
        pnl_capital  = net_pnl,
        regime       = sig.regime,
        fee_cost     = cost,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calc_tp(signal: SetupSignal, reward_ratio: float) -> float:
    sl_distance = abs(signal.entry_price - signal.sl_price)
    if signal.direction == "long":
        return signal.entry_price + sl_distance * reward_ratio
    else:
        return signal.entry_price - sl_distance * reward_ratio


def _empty_smc_row() -> pd.Series:
    """Lege SMC rij voor candles die niet in de cache zitten."""
    import numpy as np
    cols = ["ob","ob_top","ob_bottom","ob_pct","ob_mitigated_idx",
            "liq","liq_level","liq_end_idx","liq_swept_idx",
            "bos","choch","structure_level","structure_broken_idx","atr"]
    return pd.Series({c: 0.0 if c in ("ob","liq","bos","choch") else float("nan")
                      for c in cols})
