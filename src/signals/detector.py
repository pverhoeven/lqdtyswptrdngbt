"""
signals/detector.py — Zuivere sweep-signaaldetectie.

Verantwoordelijkheid:
  Krijgt één gesloten 15m candle + bijbehorende SMC-rij binnen.
  Geeft een SweepSignal terug als er een valide setup is, anders None.

Weet NIETS van orders, kapitaal, broker of backtest.
Wordt gebruikt door zowel de backtest als de live trading loop.

Gebruik:
    detector = SweepDetector(filters, cfg)

    # Per gesloten candle:
    signal = detector.on_candle(ohlc_row, smc_row, regime)
    if signal:
        # stuur naar order manager
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import pandas as pd

from src.signals.filters import SweepFilters

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signaal datatype
# ---------------------------------------------------------------------------

@dataclass
class SweepSignal:
    """
    Een gedetecteerde sweep-setup op candle-close.

    Alle prijzen zijn op het moment van signaal — de broker bepaalt
    of en hoe er gehandeld wordt.
    """
    timestamp:   pd.Timestamp
    direction:   str            # "long" of "short"
    entry_price: float          # close van sweep-candle
    sl_price:    float          # liq_level ± buffer (of fallback)
    tp_price:    float          # entry ± sl_afstand × reward_ratio
    liq_level:   float          # het gesweepte liquiditeitsniveau
    regime:      bool | None    # HMM regime op moment van signaal
    filter_str:  str            # welke filters actief waren

    @property
    def sl_distance(self) -> float:
        return abs(self.entry_price - self.sl_price)

    @property
    def risk_reward(self) -> float:
        if self.sl_distance == 0:
            return 0.0
        tp_dist = abs(self.tp_price - self.entry_price)
        return tp_dist / self.sl_distance

    def __str__(self) -> str:
        return (
            f"[{self.direction.upper()}] {self.timestamp.strftime('%Y-%m-%d %H:%M')} UTC  "
            f"entry={self.entry_price:.2f}  sl={self.sl_price:.2f}  "
            f"tp={self.tp_price:.2f}  rr=1:{self.risk_reward:.1f}  "
            f"filter={self.filter_str}"
        )


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class SweepDetector:
    """
    Verwerkt één candle tegelijk en detecteert sweep-signalen.

    Onthoudt BOS-bevestiging als bos_confirm actief is:
    bij een sweep wordt gewacht op BOS binnen bos_window candles.

    Parameters
    ----------
    filters : SweepFilters
        Welke filters actief zijn.
    reward_ratio : float
        Risk:reward voor TP-berekening (uit config: risk.reward_ratio).
    sl_buffer_pct : float
        Buffer op SL als percentage (uit config: risk.sl_buffer_pct).
    """

    def __init__(
        self,
        filters:       SweepFilters,
        reward_ratio:  float = 2.0,
        sl_buffer_pct: float = 0.1,
    ) -> None:
        self._filters      = filters
        self._rr           = reward_ratio
        self._sl_buf       = sl_buffer_pct / 100.0
        self._pending:     _PendingSweep | None = None
        self._candle_count = 0
        self._atr_buf:     deque[float] = deque(maxlen=filters.atr_window)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def on_candle(
        self,
        ohlc_row: pd.Series,
        smc_row:  pd.Series,
        regime:   bool | None = None,
    ) -> SweepSignal | None:
        """
        Verwerk één gesloten candle.

        Parameters
        ----------
        ohlc_row : pd.Series
            open, high, low, close, volume. Index-naam = candle timestamp.
        smc_row : pd.Series
            SMC-signalen voor deze candle (uit library of cache).
        regime : bool | None
            Huidig HMM regime. None = warmup/onbekend.

        Returns
        -------
        SweepSignal | None
        """
        self._candle_count += 1

        liq       = _safe(smc_row.get("liq",       0))
        bos       = _safe(smc_row.get("bos",        0))
        liq_level = _safe_float(smc_row.get("liq_level", float("nan")))
        ts        = ohlc_row.name  # DatetimeIndex timestamp
        atr_val   = _safe_float(smc_row.get("atr", float("nan")))

        signal: SweepSignal | None = None

        # --- Stap 1: controleer pending BOS-sweep ---
        if self._pending is not None:
            signal = self._pending.check_bos(bos, self._candle_count, ohlc_row)
            if signal is not None or self._pending.is_expired(self._candle_count):
                self._pending = None

        # --- Stap 2: detecteer nieuwe sweep op deze candle ---
        if liq != 0 and not _is_nan(liq):
            new_signal = self._process_sweep(
                liq, liq_level, ohlc_row, regime, ts, atr_val
            )
            # Nieuw signaal overschrijft alleen als er nog geen pending signaal is
            if new_signal is not None and signal is None:
                signal = new_signal

        # ATR buffer na verwerking bijwerken (rolling window van vorige candles)
        if not _is_nan(atr_val) and atr_val > 0:
            self._atr_buf.append(atr_val)

        return signal

    def reset(self) -> None:
        """Wis pending state. Gebruik bij herstart of nieuwe run."""
        self._pending      = None
        self._candle_count = 0
        self._atr_buf.clear()

    @property
    def has_pending(self) -> bool:
        """True als er een sweep wacht op BOS-bevestiging."""
        return self._pending is not None

    # ------------------------------------------------------------------
    # Interne logica
    # ------------------------------------------------------------------

    def _process_sweep(
        self,
        liq:         float,
        liq_level:   float,
        ohlc_row:    pd.Series,
        regime:      bool | None,
        ts:          pd.Timestamp,
        current_atr: float = float("nan"),
    ) -> SweepSignal | None:

        direction = "long" if liq == -1 else "short"

        # --- Filter: richting ---
        if not self._filters.allows(direction):
            logger.debug("Sweep gefilterd (direction): %s op %s", direction, ts)
            return None

        # --- Filter: regime ---
        if self._filters.regime:
            if regime is None:
                logger.debug("Sweep gefilterd (regime=None) op %s", ts)
                return None
            if direction == "long"  and regime is False:
                logger.debug("Sweep gefilterd (bearish regime, long sweep) op %s", ts)
                return None
            if direction == "short" and regime is True:
                logger.debug("Sweep gefilterd (bullish regime, short sweep) op %s", ts)
                return None

        # --- Filter: ATR (hoge volatiliteit = trending markt) ---
        if self._filters.atr_filter:
            if len(self._atr_buf) < self._filters.atr_window or _is_nan(current_atr):
                logger.debug("ATR filter: warmup (%d/%d) op %s", len(self._atr_buf), self._filters.atr_window, ts)
                return None
            atr_ma = sum(self._atr_buf) / len(self._atr_buf)
            if current_atr <= atr_ma:
                logger.debug("ATR filter: lage volatiliteit (%.2f ≤ %.2f) op %s", current_atr, atr_ma, ts)
                return None

        entry = float(ohlc_row["close"])
        sl, tp = _calc_sl_tp(direction, entry, liq_level, self._sl_buf, self._rr)

        if sl is None:
            logger.debug("Sweep overgeslagen: ongeldige SL op %s", ts)
            return None

        # --- Filter: BOS bevestiging ---
        if self._filters.bos_confirm:
            # Sla op als pending — wacht op BOS
            self._pending = _PendingSweep(
                direction    = direction,
                entry        = entry,
                liq_level    = liq_level,
                sl_buf       = self._sl_buf,
                rr           = self._rr,
                regime       = regime,
                filter_str   = str(self._filters),
                created_idx  = self._candle_count,
                bos_window   = self._filters.bos_window,
            )
            logger.debug("BOS-pending aangemaakt (%s) op %s", direction, ts)
            return None

        return SweepSignal(
            timestamp   = ts,
            direction   = direction,
            entry_price = entry,
            sl_price    = sl,
            tp_price    = tp,
            liq_level   = liq_level if not _is_nan(liq_level) else 0.0,
            regime      = regime,
            filter_str  = str(self._filters),
        )


# ---------------------------------------------------------------------------
# BOS-pending state
# ---------------------------------------------------------------------------

@dataclass
class _PendingSweep:
    """Wacht op BOS-bevestiging na een sweep."""
    direction:   str
    entry:       float
    liq_level:   float
    sl_buf:      float
    rr:          float
    regime:      bool | None
    filter_str:  str
    created_idx: int
    bos_window:  int

    def check_bos(
        self,
        bos:        float,
        candle_idx: int,
        ohlc_row:   pd.Series,
    ) -> SweepSignal | None:
        """Geef signaal als BOS in de juiste richting verschijnt."""
        bos_matches = (
            (self.direction == "long"  and bos == 1) or
            (self.direction == "short" and bos == -1)
        )
        if not bos_matches:
            return None

        entry = float(ohlc_row["close"])
        sl, tp = _calc_sl_tp(
            self.direction, entry, self.liq_level, self.sl_buf, self.rr
        )
        if sl is None:
            return None

        return SweepSignal(
            timestamp   = ohlc_row.name,
            direction   = self.direction,
            entry_price = entry,
            sl_price    = sl,
            tp_price    = tp,
            liq_level   = self.liq_level if not _is_nan(self.liq_level) else 0.0,
            regime      = self.regime,
            filter_str  = self.filter_str,
        )

    def is_expired(self, candle_idx: int) -> bool:
        return (candle_idx - self.created_idx) > self.bos_window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calc_sl_tp(
    direction:  str,
    entry:      float,
    liq_level:  float,
    sl_buf:     float,
    rr:         float,
) -> tuple[float | None, float | None]:
    if _is_nan(liq_level) or liq_level <= 0:
        sl = entry * (1 - sl_buf) if direction == "long" else entry * (1 + sl_buf)
    else:
        sl = liq_level * (1 - sl_buf) if direction == "long" else liq_level * (1 + sl_buf)

    sl_dist = abs(entry - sl)
    if sl_dist < entry * 0.0001:
        return None, None

    tp = (entry + sl_dist * rr) if direction == "long" else (entry - sl_dist * rr)
    return sl, tp


def _safe(v) -> float:
    try:
        f = float(v)
        return 0.0 if _is_nan(f) else f
    except (TypeError, ValueError):
        return 0.0


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _is_nan(v) -> bool:
    try:
        return v != v  # snelste NaN check
    except TypeError:
        return False