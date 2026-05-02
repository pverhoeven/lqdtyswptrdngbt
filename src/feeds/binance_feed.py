"""
feeds/binance_feed.py — Live candle feed met lokale SMC-buffer.

Verantwoordelijkheid:
- Haalt de meest recente gesloten 15m candle op via Binance REST API
- Houdt een lokale rolling buffer bij van de laatste N candles
  (N = swing_length × 10, genoeg voor SMC lookback)
- Berekent SMC-signalen op de volledige buffer na elke nieuwe candle
- Geeft de SMC-rij van de zojuist gesloten candle terug

Robuustheid:
- Automatische retry met exponential backoff (2s → 4s → 8s)
- 4xx fouten worden direct gegooien (niet geretried)
- Stale-candle detectie: candles ouder dan 2 intervals worden overgeslagen

Gebruik:
    feed = BinanceFeed(cfg)
    feed.warmup()                    # vul buffer initieel

    # In de loop:
    result = feed.poll()             # None als nog geen nieuwe candle
    if result:
        ohlc_row, smc_row = result
        signal = detector.on_candle(ohlc_row, smc_row, regime)
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_KLINES_URL      = "https://api.binance.com/api/v3/klines"
_MAX_RETRIES     = 3
_BACKOFF_SECONDS = [2, 4, 8]


class BinanceFeed:
    """
    Live 15m candle feed via Binance REST + lokale SMC-buffer.

    Parameters
    ----------
    cfg : dict
        Volledige config dict.
    """

    def __init__(self, cfg: dict, symbol: str | None = None) -> None:
        self._symbol       = symbol or cfg["data"]["symbol"]
        self._interval     = (
            cfg["data"]["timeframes"]["signal"]
            .replace("min", "m")
            .replace("h", "h")
        )
        self._swing_length = cfg["smc"]["swing_length"]
        self._buffer_size  = self._swing_length * 10

        match = re.match(r"(\d+)m", self._interval)
        self._interval_minutes = int(match.group(1)) if match else 15

        self._buffer: deque[dict] = deque(maxlen=self._buffer_size)
        self._last_candle_ts: pd.Timestamp | None = None

    # ------------------------------------------------------------------
    # Initialisatie
    # ------------------------------------------------------------------

    def warmup(self) -> None:
        """
        Vul de buffer met historische candles.
        Moet één keer aangeroepen worden voor de trading loop start.
        """
        logger.info(
            "Warmup: ophalen van %d candles voor SMC buffer…", self._buffer_size
        )
        candles = self._fetch_with_retry(limit=self._buffer_size)
        candles = candles[:-1]  # laatste candle mogelijk nog open
        for c in candles:
            self._buffer.append(c)

        if self._buffer:
            self._last_candle_ts = pd.Timestamp(
                self._buffer[-1]["open_time"], unit="ms", tz="UTC"
            )
        logger.info(
            "Buffer gevuld: %d candles, laatste: %s",
            len(self._buffer),
            self._last_candle_ts,
        )

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def history_df(self) -> pd.DataFrame:
        """Retourneert de huidige buffer als DataFrame voor detector-warmup."""
        if not self._buffer:
            return pd.DataFrame()
        return self._buffer_to_df()

    def poll(self) -> tuple[pd.Series, pd.Series] | None:
        """
        Controleer of er een nieuwe gesloten candle beschikbaar is.

        Returns
        -------
        tuple[pd.Series, pd.Series] | None
            (ohlc_row, smc_row) van de zojuist gesloten candle,
            of None als er geen nieuwe candle is.
        """
        try:
            latest = self._fetch_with_retry(limit=2)
        except Exception as exc:
            logger.warning("Feed poll mislukt: %s", exc)
            return None

        if len(latest) < 2:
            return None

        closed    = latest[-2]
        closed_ts = pd.Timestamp(closed["open_time"], unit="ms", tz="UTC")

        if self._last_candle_ts is not None and closed_ts <= self._last_candle_ts:
            return None

        # Stale-candle check: candle ouder dan 2 intervals is verdacht
        stale_cutoff = pd.Timestamp.utcnow() - pd.Timedelta(
            minutes=self._interval_minutes * 2.5
        )
        if closed_ts < stale_cutoff:
            logger.warning(
                "Stale candle gedetecteerd: %s (>2 intervals oud). Overgeslagen.",
                closed_ts,
            )
            return None

        self._buffer.append(closed)
        self._last_candle_ts = closed_ts
        logger.debug("Nieuwe candle: %s", closed_ts)

        df_buffer = self._buffer_to_df()
        smc_row   = self._compute_smc(df_buffer, closed_ts)
        ohlc_row  = df_buffer.loc[closed_ts]

        return ohlc_row, smc_row

    # ------------------------------------------------------------------
    # Binance API met retry
    # ------------------------------------------------------------------

    def _fetch_with_retry(self, limit: int = 100) -> list[dict]:
        """
        Haal klines op met automatische retry bij netwerkfouten of 5xx.

        - Max 3 pogingen, exponential backoff: 2s → 4s → 8s
        - 4xx: direct fout gooien
        - Na 3 mislukte pogingen: log critical en propageer exception
        """
        last_exc: Exception | None = None
        for attempt, wait in enumerate(
            [0] + _BACKOFF_SECONDS, start=1
        ):
            if wait:
                logger.warning(
                    "Binance API poging %d/%d, wacht %ds…",
                    attempt, _MAX_RETRIES + 1, wait,
                )
                time.sleep(wait)
            try:
                return self._fetch_klines(limit)
            except requests.HTTPError as exc:
                if exc.response is not None and 400 <= exc.response.status_code < 500:
                    raise  # 4xx direct doorgeven, niet retrien
                last_exc = exc
                logger.warning("Binance 5xx fout (poging %d): %s", attempt, exc)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                logger.warning("Binance verbindingsfout (poging %d): %s", attempt, exc)

        logger.critical(
            "Binance API onbereikbaar na %d pogingen: %s", _MAX_RETRIES, last_exc
        )
        raise RuntimeError(
            f"Binance API onbereikbaar na {_MAX_RETRIES} pogingen"
        ) from last_exc

    def _fetch_klines(self, limit: int = 100) -> list[dict]:
        """Haal de meest recente klines op via Binance REST."""
        params = {
            "symbol":   self._symbol,
            "interval": self._interval,
            "limit":    limit,
        }
        resp = requests.get(_KLINES_URL, params=params, timeout=10)
        resp.raise_for_status()
        return [
            {
                "open_time": int(r[0]),
                "open":      float(r[1]),
                "high":      float(r[2]),
                "low":       float(r[3]),
                "close":     float(r[4]),
                "volume":    float(r[5]),
            }
            for r in resp.json()
        ]

    # ------------------------------------------------------------------
    # Buffer → DataFrame → SMC
    # ------------------------------------------------------------------

    def _buffer_to_df(self) -> pd.DataFrame:
        df = pd.DataFrame(list(self._buffer))
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("open_time").sort_index()
        return df.astype(float)

    def _compute_smc(
        self,
        df: pd.DataFrame,
        target_ts: pd.Timestamp,
    ) -> pd.Series:
        try:
            from src.smc.signals import compute_signals
            signals_df = compute_signals(df, swing_length=self._swing_length)
            if target_ts in signals_df.index:
                return signals_df.loc[target_ts]
        except Exception as exc:
            logger.warning("SMC berekening mislukt: %s", exc)

        return _empty_smc_row()


def _empty_smc_row() -> pd.Series:
    cols = [
        "ob", "ob_top", "ob_bottom", "ob_pct", "ob_mitigated_idx",
        "liq", "liq_level", "liq_end_idx", "liq_swept_idx",
        "bos", "choch", "structure_level", "structure_broken_idx", "atr",
    ]
    return pd.Series(
        {c: 0.0 if c in ("ob", "liq", "bos", "choch") else float("nan") for c in cols}
    )
