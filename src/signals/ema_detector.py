"""
signals/ema_detector.py — Eenvoudige EMA-crossover detector voor broker-testing.

Genereert een signaal bij elke EMA fast/slow crossover:
  - fast EMA kruist slow EMA omhoog → LONG
  - fast EMA kruist slow EMA omlaag → SHORT

Dezelfde interface als SweepDetector: on_candle(ohlc_row, smc_row, regime).
Retourneert een SweepSignal zodat OrderManager ongewijzigd blijft.
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from src.signals.detector import SweepSignal


class _FilterLabel:
    """Dummy filters-object voor PaperTrader print."""
    def __init__(self, label: str) -> None:
        self._label = label

    def __str__(self) -> str:
        return self._label


class EMADetector:
    """
    EMA-crossover detector.

    Parameters
    ----------
    fast : int
        Periode van de snelle EMA (standaard 5).
    slow : int
        Periode van de trage EMA (standaard 13).
    reward_ratio : float
        Risk:reward voor TP (standaard 2.0).
    sl_buffer_pct : float
        SL afstand als % van entry (standaard 0.5%).
    """

    def __init__(
        self,
        fast:          int   = 5,
        slow:          int   = 13,
        reward_ratio:  float = 2.0,
        sl_buffer_pct: float = 0.5,
    ) -> None:
        self._fast    = fast
        self._slow    = slow
        self._rr      = reward_ratio
        self._sl_pct  = sl_buffer_pct / 100.0
        self._closes: deque[float] = deque(maxlen=slow)
        self._fast_ema: float | None = None
        self._slow_ema: float | None = None
        self._filters = _FilterLabel(f"ema{fast}/{slow}")

    # ------------------------------------------------------------------
    # Publieke interface (zelfde als SweepDetector)
    # ------------------------------------------------------------------

    def on_candle(
        self,
        ohlc_row: pd.Series,
        smc_row:  pd.Series,
        regime:   bool | None = None,
    ) -> SweepSignal | None:
        """
        Verwerk één gesloten candle.
        smc_row en regime worden genegeerd — enkel ohlc_row wordt gebruikt.
        """
        close = float(ohlc_row["close"])
        ts    = ohlc_row.name

        self._closes.append(close)

        k_fast = 2.0 / (self._fast + 1)
        k_slow = 2.0 / (self._slow + 1)

        if len(self._closes) < self._slow:
            return None  # warmup

        if self._fast_ema is None:
            closes = list(self._closes)
            self._fast_ema = sum(closes[-self._fast:]) / self._fast
            self._slow_ema = sum(closes) / self._slow
            return None

        prev_fast = self._fast_ema
        prev_slow = self._slow_ema

        self._fast_ema = close * k_fast + self._fast_ema * (1 - k_fast)
        self._slow_ema = close * k_slow + self._slow_ema * (1 - k_slow)

        bullish = prev_fast <= prev_slow and self._fast_ema > self._slow_ema
        bearish = prev_fast >= prev_slow and self._fast_ema < self._slow_ema

        if not (bullish or bearish):
            return None

        direction = "long" if bullish else "short"
        entry     = close

        if direction == "long":
            sl = entry * (1 - self._sl_pct)
            tp = entry + (entry - sl) * self._rr
        else:
            sl = entry * (1 + self._sl_pct)
            tp = entry - (sl - entry) * self._rr

        return SweepSignal(
            timestamp   = ts,
            direction   = direction,
            entry_price = entry,
            sl_price    = sl,
            tp_price    = tp,
            liq_level   = round(self._slow_ema, 2),
            regime      = regime,
            filter_str  = str(self._filters),
        )

    def reset(self) -> None:
        self._closes.clear()
        self._fast_ema = None
        self._slow_ema = None
