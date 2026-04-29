"""
DEPRECATED: lifecycle.py — Gebruik SweepDetector (src/signals/detector.py).
Deze module wordt niet meer actief onderhouden.

Stadia (in volgorde):
    ob_formed → sweep_occurred → choch_confirmed → entry_valid

Elke setup doorloopt deze stadia. Een setup eindigt als:
- Alle stadia doorlopen zijn en entry_valid vervalt → expired
- Een invalidatieconditie optreedt → invalidated
- Een TTL wordt overschreden → expired

Elke 15m candle wordt één keer door `update()` geleid. De machine
retourneert een lijst van `SetupSignal` objecten die in die candle
entry_valid bereikt hebben.

Invalidatiecondities (configureerbaar):
    ob_mitigated:  close door OB-zone heen
    opposite_bos:  tegengestelde BOS na choch
    regime_change: HMM regime gewisseld na ob_formed
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------

class Stage(Enum):
    OB_FORMED        = auto()
    SWEEP_OCCURRED   = auto()
    CHOCH_CONFIRMED  = auto()
    ENTRY_VALID      = auto()
    EXPIRED          = auto()
    INVALIDATED      = auto()


@dataclass
class SetupSignal:
    """Een setup die entry_valid heeft bereikt op `candle_index`."""
    candle_index:  int             # positie in de 15m DataFrame
    candle_time:   pd.Timestamp
    direction:     str             # "long" of "short"
    ob_top:        float
    ob_bottom:     float
    ob_midpoint:   float
    entry_price:   float           # ob_midpoint
    sl_price:      float           # 0.1% onder ob_bottom (long) / boven ob_top (short)
    regime:        bool | None     # True=bullish, False=bearish op moment van entry


@dataclass
class _Setup:
    """Interne representatie van één actieve setup."""
    direction:       str           # "long" of "short"
    ob_top:          float
    ob_bottom:       float
    ob_midpoint:     float
    ob_candle_idx:   int           # candle-index waarop ob_formed is bereikt

    stage:           Stage = Stage.OB_FORMED
    stage_entered:   int   = 0     # candle-index waarop huidig stadium is betreden
    regime_at_entry: bool | None = None

    # Voor invalidatie: onthoud de bullish richting van de setup
    @property
    def is_long(self) -> bool:
        return self.direction == "long"


# ---------------------------------------------------------------------------
# Lifecycle engine
# ---------------------------------------------------------------------------

class LifecycleEngine:
    """
    Verwerkt één 15m candle tegelijk en beheert alle actieve setups.

    Gebruik:
        engine = LifecycleEngine(cfg)
        for i, row in df_15m.iterrows():
            signals = engine.update(i_int, row, smc_row, regime)
            # signals: lijst van SetupSignal (kan leeg zijn)
    """

    def __init__(self, cfg: dict) -> None:
        lcfg = cfg["lifecycle"]
        rcfg = cfg["risk"]

        self._ttl = lcfg["ttl"]
        self._invalidation = lcfg["invalidation"]
        self._sl_buffer_pct = rcfg["sl_buffer_pct"] / 100.0

        self._active: list[_Setup] = []
        self._current_regime: bool | None = None

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def update(
        self,
        candle_idx: int,
        ohlc_row: pd.Series,
        smc_row: pd.Series,
        regime: bool | None,
    ) -> list[SetupSignal]:
        """
        Verwerk één 15m candle.

        Parameters
        ----------
        candle_idx : int
            Integer positie in de DataFrame (0-based).
        ohlc_row : pd.Series
            open, high, low, close, volume van de huidige candle.
        smc_row : pd.Series
            SMC-signalen van de huidige candle (cache output).
        regime : bool | None
            Huidig HMM regime (True=bullish, False=bearish, None=warmup).

        Returns
        -------
        list[SetupSignal]
            Lijst van setups die in deze candle entry_valid bereikten.
        """
        prev_regime = self._current_regime
        self._current_regime = regime

        # 1. Spoor nieuwe OB's op en voeg setups toe
        if regime is not None:  # geen trading tijdens warmup
            self._detect_new_obs(candle_idx, smc_row, regime)

        # 2. Update bestaande setups
        new_signals: list[SetupSignal] = []

        for setup in self._active:
            self._advance(setup, candle_idx, ohlc_row, smc_row, regime, prev_regime)

            if setup.stage == Stage.ENTRY_VALID:
                signal = self._to_signal(setup, candle_idx, ohlc_row.name, regime)
                new_signals.append(signal)
                setup.stage = Stage.EXPIRED  # entry geconsumeerd

        # 3. Ruim verlopen/geïnvalideerde setups op
        self._active = [
            s for s in self._active
            if s.stage not in (Stage.EXPIRED, Stage.INVALIDATED)
        ]

        return new_signals

    def n_active(self) -> int:
        """Aantal actieve setups op dit moment."""
        return len(self._active)

    def reset(self) -> None:
        """Wis alle actieve setups (voor herstart of OOS-run)."""
        self._active.clear()
        self._current_regime = None

    # ------------------------------------------------------------------
    # OB detectie
    # ------------------------------------------------------------------

    def _detect_new_obs(
        self,
        candle_idx: int,
        smc_row: pd.Series,
        regime: bool,
    ) -> None:
        """
        Voeg nieuwe OB-setup toe als smc_row een bullish of bearish OB signaleert
        en het regime passend is.

        SMC library output:
            ob       : 1 = bullish OB, -1 = bearish OB, 0 = geen
            ob_top   : bovenkant OB-zone
            ob_bottom: onderkant OB-zone
        """
        ob_signal = _safe_float(smc_row.get("ob", 0))
        ob_top    = _safe_float(smc_row.get("ob_top", float("nan")))
        ob_bottom = _safe_float(smc_row.get("ob_bottom", float("nan")))

        if pd.isna(ob_top) or pd.isna(ob_bottom):
            return

        if ob_signal == 1:          # bullish OB
            direction = "long"
        elif ob_signal == -1:       # bearish OB
            direction = "short"
        else:
            return

        ob_midpoint = (ob_top + ob_bottom) / 2.0

        setup = _Setup(
            direction      = direction,
            ob_top         = ob_top,
            ob_bottom      = ob_bottom,
            ob_midpoint    = ob_midpoint,
            ob_candle_idx  = candle_idx,
            stage          = Stage.OB_FORMED,
            stage_entered  = candle_idx,
            regime_at_entry= regime,
        )
        self._active.append(setup)
        logger.debug(
            "OB setup [%s] toegevoegd op candle %d (top=%.2f, bottom=%.2f)",
            direction, candle_idx, ob_top, ob_bottom,
        )

    # ------------------------------------------------------------------
    # Stage transitie logica
    # ------------------------------------------------------------------

    def _advance(
        self,
        setup: _Setup,
        candle_idx: int,
        ohlc_row: pd.Series,
        smc_row: pd.Series,
        regime: bool | None,
        prev_regime: bool | None,
    ) -> None:
        """Probeer setup naar het volgende stadium te brengen, of invalideer/verlope."""

        # --- Invalidatiechecks (altijd eerst) ---
        if self._is_invalidated(setup, candle_idx, ohlc_row, smc_row, regime, prev_regime):
            setup.stage = Stage.INVALIDATED
            logger.debug("Setup [%s] geïnvalideerd op candle %d", setup.direction, candle_idx)
            return

        # --- TTL check voor huidig stadium ---
        if self._ttl_expired(setup, candle_idx):
            setup.stage = Stage.EXPIRED
            logger.debug(
                "Setup [%s] TTL verlopen (stage=%s) op candle %d",
                setup.direction, setup.stage.name, candle_idx,
            )
            return

        # --- Transitie naar volgend stadium ---
        if setup.stage == Stage.OB_FORMED:
            if self._sweep_detected(setup, smc_row):
                setup.stage = Stage.SWEEP_OCCURRED
                setup.stage_entered = candle_idx
                logger.debug(
                    "Setup [%s] (stage=%s) op candle %d",
                    setup.direction, setup.stage.name, candle_idx,
                )

        elif setup.stage == Stage.SWEEP_OCCURRED:
            if self._choch_detected(setup, smc_row):
                setup.stage = Stage.CHOCH_CONFIRMED
                setup.stage_entered = candle_idx
                logger.debug(
                    "Setup [%s] (stage=%s) op candle %d",
                    setup.direction, setup.stage.name, candle_idx,
                )

        elif setup.stage == Stage.CHOCH_CONFIRMED:
            if self._retest_detected(setup, ohlc_row):
                setup.stage = Stage.ENTRY_VALID
                setup.stage_entered = candle_idx
                logger.debug(
                    "Setup [%s] (stage=%s) op candle %d",
                    setup.direction, setup.stage.name, candle_idx,
                )

        # ENTRY_VALID wordt afgehandeld door de caller (update)

    # ------------------------------------------------------------------
    # Transitie detectie
    # ------------------------------------------------------------------

    def _sweep_detected(self, setup: _Setup, smc_row: pd.Series) -> bool:
        """
        Liquidity sweep van het relevante swing punt.
        smc_row.liq = 1 (bullish liq swept) of -1 (bearish liq swept).
        """
        liq = _safe_float(smc_row.get("liq", 0))
        if setup.is_long:
            return liq == -1    # sweep van swing low (voor long setup)
        else:
            return liq == 1     # sweep van swing high (voor short setup)

    def _choch_detected(self, setup: _Setup, smc_row: pd.Series) -> bool:
        """
        CHoCH in de richting van de setup, NA de sweep.
        smc_row.choch = 1 (bullish CHoCH) of -1 (bearish CHoCH).
        """
        choch = _safe_float(smc_row.get("choch", 0))
        if setup.is_long:
            return choch == 1
        else:
            return choch == -1

    def _retest_detected(self, setup: _Setup, ohlc_row: pd.Series) -> bool:
        """
        Prijs retracet naar de OB-zone: low (long) of high (short) raakt de zone.
        """
        if setup.is_long:
            return float(ohlc_row["low"]) <= setup.ob_top
        else:
            return float(ohlc_row["high"]) >= setup.ob_bottom

    # ------------------------------------------------------------------
    # Invalidatie
    # ------------------------------------------------------------------

    def _is_invalidated(
        self,
        setup: _Setup,
        candle_idx: int,
        ohlc_row: pd.Series,
        smc_row: pd.Series,
        regime: bool | None,
        prev_regime: bool | None,
    ) -> bool:

        # OB mitigated: close doorbreekt OB-zone volledig
        if self._invalidation.get("ob_mitigated", True):
            if setup.is_long:
                if float(ohlc_row["close"]) < setup.ob_bottom:
                    return True
            else:
                if float(ohlc_row["close"]) > setup.ob_top:
                    return True

        # Tegengestelde BOS
        if self._invalidation.get("opposite_bos", True):
            bos = _safe_float(smc_row.get("bos", 0))
            if setup.is_long and bos == -1:
                return True
            if not setup.is_long and bos == 1:
                return True

        # Regime gewisseld
        if self._invalidation.get("regime_change", True):
            if (
                prev_regime is not None
                and regime is not None
                and regime != prev_regime
            ):
                return True

        return False

    # ------------------------------------------------------------------
    # TTL
    # ------------------------------------------------------------------

    def _ttl_expired(self, setup: _Setup, candle_idx: int) -> bool:
        stage_name = setup.stage.name.lower()
        ttl = self._ttl.get(stage_name)
        if ttl is None:
            return False
        return (candle_idx - setup.stage_entered) >= ttl

    # ------------------------------------------------------------------
    # Signal aanmaken
    # ------------------------------------------------------------------

    def _to_signal(
        self,
        setup: _Setup,
        candle_idx: int,
        candle_time: pd.Timestamp,
        regime: bool | None,
    ) -> SetupSignal:

        if setup.is_long:
            sl_price = setup.ob_bottom * (1.0 - self._sl_buffer_pct)
        else:
            sl_price = setup.ob_top * (1.0 + self._sl_buffer_pct)

        return SetupSignal(
            candle_index = candle_idx,
            candle_time  = candle_time,
            direction    = setup.direction,
            ob_top       = setup.ob_top,
            ob_bottom    = setup.ob_bottom,
            ob_midpoint  = setup.ob_midpoint,
            entry_price  = setup.ob_midpoint,
            sl_price     = sl_price,
            regime       = regime,
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _safe_float(val) -> float:
    """Converteer naar float, geeft 0.0 bij NaN/None."""
    try:
        f = float(val)
        return 0.0 if pd.isna(f) else f
    except (TypeError, ValueError):
        return 0.0
