"""
trading/paper_trader.py — Live paper trading loop.

Wacht op candle-close van de 15m candle, haalt dan:
1. De zojuist gesloten candle op via BinanceFeed
2. SMC-signalen uit de lokale buffer
3. Regime van het HMM-model
4. Stuurt alles door de SweepDetector
5. Geeft eventuele signalen aan de OrderManager

Candle-close timing:
  De 15m candles sluiten op :00, :15, :30, :45 van elk uur.
  We wachten tot 10 seconden ná de close (buffer voor API-latency),
  dan pollen we de feed.

Gebruik:
    python scripts/run_paper_trader.py --filter baseline
    python scripts/run_paper_trader.py --filter regime_long
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class PaperTrader:
    """
    Live paper trading loop.

    Parameters
    ----------
    feed : BinanceFeed
        Live data feed.
    detector : SweepDetector
        Signaaldetector (zuiver, kent geen orders).
    order_manager : OrderManager
        Koppelt signalen aan orders en verzorgt logging.
    regime_provider : RegimeProvider | None
        Levert het actuele HMM regime. None = geen regime filter.
    candle_minutes : int
        Timeframe in minuten (15 voor 15m).
    close_offset_seconds : int
        Wacht N seconden na candle-close voor API-latency.
    """

    def __init__(
        self,
        feed,
        detector,
        order_manager,
        regime_provider=None,
        candle_minutes:        int      = 15,
        close_offset_seconds:  int      = 10,
        heartbeat_hours:       int | None = None,
    ) -> None:
        self._feed             = feed
        self._detector         = detector
        self._order_manager    = order_manager
        self._regime_provider  = regime_provider
        self._candle_minutes   = candle_minutes
        self._close_offset     = close_offset_seconds
        self._running          = False
        self._heartbeat_interval: timedelta | None = (
            timedelta(hours=heartbeat_hours) if heartbeat_hours else None
        )
        self._last_heartbeat: datetime = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start de trading loop.
        Blokkeert tot KeyboardInterrupt of stop() aangeroepen wordt.
        """
        self._running = True
        self._setup_signal_handlers()

        print(f"\n{'='*50}")
        print(f"  PAPER TRADER GESTART")
        print(f"  Timeframe: {self._candle_minutes}m")
        print(f"  Filter: {self._detector._filters}")
        print(f"  Log: {self._order_manager.log_path}")
        print(f"{'='*50}\n")

        logger.info("Feed warmup starten…")
        self._feed.warmup()

        next_poll = self._next_candle_close()
        print(f"Wachten op eerste candle-close: "
              f"{next_poll.strftime('%H:%M:%S')} UTC\n")

        try:
            while self._running:
                now = pd.Timestamp.utcnow()

                if now >= next_poll:
                    self._on_candle_close()
                    next_poll = self._next_candle_close()
                    print(
                        f"Volgende candle-close: "
                        f"{next_poll.strftime('%H:%M:%S')} UTC"
                    )

                time.sleep(1)

        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        """Stop de loop netjes."""
        self._running = False
        print("\n[STOP] Trading loop gestopt.")
        self._order_manager.print_stats()

    # ------------------------------------------------------------------
    # Candle-close handler
    # ------------------------------------------------------------------

    def _on_candle_close(self) -> None:
        """Verwerk één gesloten candle."""
        now = pd.Timestamp.utcnow()

        # Haal candle + SMC op
        result = self._feed.poll()
        if result is None:
            logger.debug("Geen nieuwe candle op %s", now)
            return

        ohlc_row, smc_row = result
        ts = ohlc_row.name

        print(f"[{ts.strftime('%H:%M')} UTC]  "
              f"O={float(ohlc_row['open']):.0f}  "
              f"H={float(ohlc_row['high']):.0f}  "
              f"L={float(ohlc_row['low']):.0f}  "
              f"C={float(ohlc_row['close']):.0f}")

        # Haal regime op
        regime = None
        if self._regime_provider is not None:
            regime = self._regime_provider.current_regime()

        # Geef candle aan order manager (SL/TP check op open posities)
        self._order_manager.on_candle(ohlc_row, ts)

        # Detecteer signaal
        signal = self._detector.on_candle(ohlc_row, smc_row, regime)
        if signal:
            self._order_manager.on_signal(signal)

        self._maybe_heartbeat()

    def _maybe_heartbeat(self) -> None:
        if self._heartbeat_interval is None:
            return
        now = datetime.now(timezone.utc)
        if now - self._last_heartbeat >= self._heartbeat_interval:
            self._order_manager.send_heartbeat()
            self._last_heartbeat = now

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    def _next_candle_close(self) -> pd.Timestamp:
        """
        Bereken het tijdstip van de volgende candle-close
        + close_offset_seconds.
        """
        now    = datetime.now(timezone.utc)
        minute = now.minute
        # Volgende veelvoud van candle_minutes
        next_m = (minute // self._candle_minutes + 1) * self._candle_minutes
        if next_m >= 60:
            # Volgende uur
            ts = now.replace(
                hour=(now.hour + 1) % 24, minute=0,
                second=self._close_offset, microsecond=0,
            )
        else:
            ts = now.replace(
                minute=next_m,
                second=self._close_offset, microsecond=0,
            )
        return pd.Timestamp(ts)

    def _setup_signal_handlers(self) -> None:
        """Registreer SIGINT/SIGTERM voor netjes stoppen."""
        def handler(signum, frame):
            print("\n[SIGNAAL] Stop ontvangen…")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT,  handler)
        signal.signal(signal.SIGTERM, handler)


# ---------------------------------------------------------------------------
# Regime provider
# ---------------------------------------------------------------------------

class RegimeProvider:
    """
    Levert het actuele HMM regime op basis van 4h candles.

    Werkt met een incrementele causale forward filter:
    - Eenmalige warmup op ~300 historische candles initialiseert de filterstate.
    - Bij elke nieuwe 4h candle wordt slechts één forward stap gedaan —
      geen Viterbi, geen window-restart, geen look-ahead.
    """

    def __init__(self, cfg: dict, recalc_every: int = 16, symbol: str | None = None) -> None:
        """
        Parameters
        ----------
        cfg : dict
        recalc_every : int
            Stap het regime elke N 15m candles (16 = elke 4h candle).
        symbol : str, optional
            Symbool voor de 4h Binance feed en modelkeuze (bijv. "ETHUSDT").
            Standaard: cfg["data"]["symbol"].
        """
        from src.regime.hmm import load_model
        self._symbol       = symbol or cfg["data"]["symbol"]
        self._model        = load_model(cfg, symbol=self._symbol)
        self._cfg          = cfg
        self._recalc_every = recalc_every
        self._call_count   = 0
        self._regime:      bool | None = None

        rcfg = cfg["regime"]
        self._atr_period    = rcfg["atr_period"]
        self._atr_ma_period = rcfg["atr_ma_period"]
        # Bewaar de laatste hist_size gesloten 4h candles voor rolling features
        hist_size = self._atr_period + self._atr_ma_period + 5
        self._ohlcv_hist: deque = deque(maxlen=hist_size)
        self._log_alpha:  np.ndarray | None = None  # forward-filter state
        self._last_4h_ts: pd.Timestamp | None = None

        self._warmup()

    def current_regime(self) -> bool | None:
        """Geef huidig regime terug. Stapt forward bij nieuwe 4h candle."""
        self._call_count += 1
        if self._call_count % self._recalc_every == 0:
            self._update_regime()
        return self._regime

    # ------------------------------------------------------------------
    # Initialisatie
    # ------------------------------------------------------------------

    def _warmup(self) -> None:
        """
        Initialiseer de forward-filter state op ~300 historische 4h candles.
        Met 300 candles en een warmup van ~62 blijven ~238 geldige observaties
        over — genoeg om de startprob_ prior te vergeten.
        """
        try:
            rows = self._fetch_4h_rows(limit=300)
            rows = rows[:-1]  # laatste candle is mogelijk nog open
            if not rows:
                return

            df_4h = self._rows_to_df(rows)
            for r in rows[-self._ohlcv_hist.maxlen:]:
                self._ohlcv_hist.append(r)
            self._last_4h_ts = df_4h.index[-1]

            self._log_alpha = self._model.init_forward_state(df_4h)
            state = np.argmax(self._log_alpha)
            self._regime = bool(state == self._model.bullish_state)
            logger.info(
                "Regime initieel: %s (%.0f geldige 4h observaties)",
                "bullish" if self._regime else "bearish",
                len(df_4h),
            )
        except Exception as exc:
            logger.warning("Regime warmup mislukt: %s", exc)

    # ------------------------------------------------------------------
    # Incrementele update
    # ------------------------------------------------------------------

    def _update_regime(self) -> None:
        """
        Haal de meest recente gesloten 4h candle op en doe één causale
        forward stap. Geen window-restart, geen look-ahead.
        """
        try:
            rows = self._fetch_4h_rows(limit=2)
            if len(rows) < 2:
                return

            closed_row = rows[-2]  # rows[-1] is de huidige open candle
            closed_ts  = pd.Timestamp(int(closed_row[0]), unit="ms", tz="UTC")

            if self._last_4h_ts is not None and closed_ts <= self._last_4h_ts:
                return  # geen nieuwe 4h candle

            self._last_4h_ts = closed_ts
            self._ohlcv_hist.append(closed_row)

            if self._log_alpha is None:
                self._warmup()
                return

            # Bereken features over de rolling buffer
            df_hist = self._rows_to_df(list(self._ohlcv_hist))
            from src.regime.hmm import _compute_atr
            log_return = float(np.log(
                df_hist["close"].iloc[-1] / df_hist["close"].iloc[-2]
            ))
            atr    = _compute_atr(df_hist, self._atr_period)
            atr_ma = atr.rolling(self._atr_ma_period).mean()
            lr_val = log_return
            ar_val = float(atr.iloc[-1] / atr_ma.iloc[-1]) if atr_ma.iloc[-1] else np.nan

            if np.isnan(lr_val) or np.isnan(ar_val):
                return

            x_scaled = self._model._scaler.transform([[lr_val, ar_val]])[0]
            self._log_alpha = self._model.forward_step(x_scaled, self._log_alpha)

            state = np.argmax(self._log_alpha)
            self._regime = bool(state == self._model.bullish_state)
            logger.debug("Regime stap: %s", "bullish" if self._regime else "bearish")

        except Exception as exc:
            logger.warning("Regime update mislukt: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_4h_rows(self, limit: int) -> list:
        import requests
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol":   self._symbol,
                "interval": "4h",
                "limit":    limit,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _rows_to_df(self, rows: list) -> pd.DataFrame:
        times = [pd.Timestamp(int(r[0]), unit="ms", tz="UTC") for r in rows]
        return pd.DataFrame({
            "open":   [float(r[1]) for r in rows],
            "high":   [float(r[2]) for r in rows],
            "low":    [float(r[3]) for r in rows],
            "close":  [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        }, index=times)


# ---------------------------------------------------------------------------
# Multi-coin trading
# ---------------------------------------------------------------------------

@dataclass
class CoinRunner:
    """Bevat alle componenten voor één coin in de multi-coin trader."""
    symbol:          str
    feed:            Any
    detector:        Any
    order_manager:   Any
    regime_provider: Any | None = None


class MultiCoinTrader:
    """
    Live trading loop voor meerdere coins tegelijk.

    Elke coin heeft zijn eigen feed, detector en order_manager.
    De timing-loop is gedeeld: alle coins worden per 15m candle-close verwerkt.

    Parameters
    ----------
    runners : list[CoinRunner]
        Één runner per coin.
    candle_minutes : int
        Timeframe in minuten (15 voor 15m).
    close_offset_seconds : int
        Wacht N seconden na candle-close voor API-latency.
    """

    def __init__(
        self,
        runners:               list[CoinRunner],
        candle_minutes:        int      = 15,
        close_offset_seconds:  int      = 10,
        notifier               = None,
        heartbeat_hours:       int | None = None,
    ) -> None:
        self._runners        = runners
        self._candle_minutes = candle_minutes
        self._close_offset   = close_offset_seconds
        self._running        = False
        self._notifier       = notifier
        self._heartbeat_interval: timedelta | None = (
            timedelta(hours=heartbeat_hours) if heartbeat_hours else None
        )
        self._last_heartbeat: datetime = datetime.now(timezone.utc)

    def start(self) -> None:
        """Start de multi-coin trading loop. Blokkeert tot Ctrl+C."""
        self._running = True
        self._setup_signal_handlers()

        symbols = [r.symbol for r in self._runners]
        print(f"\n{'='*55}")
        print(f"  MULTI-COIN TRADER GESTART")
        print(f"  Coins: {', '.join(symbols)}")
        print(f"  Timeframe: {self._candle_minutes}m")
        print(f"{'='*55}\n")

        logger.info("Warmup starten voor %d coins…", len(self._runners))
        for runner in self._runners:
            logger.info("Warmup: %s", runner.symbol)
            runner.feed.warmup()

        next_poll = self._next_candle_close()
        print(f"Wachten op eerste candle-close: "
              f"{next_poll.strftime('%H:%M:%S')} UTC\n")

        try:
            while self._running:
                now = pd.Timestamp.utcnow()
                if now >= next_poll:
                    self._on_candle_close()
                    next_poll = self._next_candle_close()
                    print(f"Volgende candle-close: {next_poll.strftime('%H:%M:%S')} UTC")
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._running = False
        print("\n[STOP] Multi-coin trading loop gestopt.")
        for runner in self._runners:
            print(f"\n--- {runner.symbol} ---")
            runner.order_manager.print_stats()

    def _on_candle_close(self) -> None:
        for runner in self._runners:
            result = runner.feed.poll()
            if result is None:
                logger.debug("Geen nieuwe candle voor %s", runner.symbol)
                continue

            ohlc_row, smc_row = result
            ts = ohlc_row.name

            print(f"[{runner.symbol}] [{ts.strftime('%H:%M')} UTC]  "
                  f"O={float(ohlc_row['open']):.2f}  "
                  f"H={float(ohlc_row['high']):.2f}  "
                  f"L={float(ohlc_row['low']):.2f}  "
                  f"C={float(ohlc_row['close']):.2f}")

            regime = None
            if runner.regime_provider is not None:
                regime = runner.regime_provider.current_regime()

            runner.order_manager.on_candle(ohlc_row, ts)

            signal = runner.detector.on_candle(ohlc_row, smc_row, regime)
            if signal:
                runner.order_manager.on_signal(signal)

        self._maybe_heartbeat()

    def _maybe_heartbeat(self) -> None:
        if not self._notifier or self._heartbeat_interval is None:
            return
        now = datetime.now(timezone.utc)
        if now - self._last_heartbeat < self._heartbeat_interval:
            return
        total_wins   = sum(r.order_manager.stats.wins   for r in self._runners)
        total_losses = sum(r.order_manager.stats.losses for r in self._runners)
        n_open       = sum(r.order_manager.open_count() for r in self._runners)
        # stats.current_capital wordt bijgewerkt bij elke trade-close
        equity = sum(r.order_manager.stats.current_capital for r in self._runners)
        self._notifier.notify_heartbeat(equity, n_open, total_wins, total_losses)
        self._last_heartbeat = now

    def _next_candle_close(self) -> pd.Timestamp:
        now    = datetime.now(timezone.utc)
        minute = now.minute
        next_m = (minute // self._candle_minutes + 1) * self._candle_minutes
        if next_m >= 60:
            ts = now.replace(
                hour=(now.hour + 1) % 24, minute=0,
                second=self._close_offset, microsecond=0,
            )
        else:
            ts = now.replace(
                minute=next_m,
                second=self._close_offset, microsecond=0,
            )
        return pd.Timestamp(ts)

    def _setup_signal_handlers(self) -> None:
        def handler(signum, frame):
            print("\n[SIGNAAL] Stop ontvangen…")
            self.stop()
            sys.exit(0)
        signal.signal(signal.SIGINT,  handler)
        signal.signal(signal.SIGTERM, handler)