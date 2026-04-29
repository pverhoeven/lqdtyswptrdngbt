"""
trading/funding_rate.py — Funding rate filter voor OKX perpetual swaps.

Logica:
- Hoge positieve funding (longs betalen shorts) → markt overbought → skip longs
- Hoge negatieve funding (shorts betalen longs) → markt oversold → skip shorts

Funding rate wordt gecached om overmatige API-calls te vermijden.
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

_OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"


class FundingRateFilter:
    """
    Checkt of de huidige funding rate een trade-richting toestaat.

    Parameters
    ----------
    inst_id : str
        OKX instrument ID (bijv. "BTC-USDT-SWAP").
    max_long_rate : float
        Skip longs als funding_rate > max_long_rate (bijv. 0.0003 = 0.03%).
    min_short_rate : float
        Skip shorts als funding_rate < min_short_rate (bijv. -0.0003 = -0.03%).
    cache_seconds : int
        Hoe lang de gecachede rate geldig blijft (standaard 300 = 5 min).
    """

    def __init__(
        self,
        inst_id:        str,
        max_long_rate:  float = 0.0003,
        min_short_rate: float = -0.0003,
        cache_seconds:  int   = 300,
    ) -> None:
        self._inst_id       = inst_id
        self._max_long      = max_long_rate
        self._min_short     = min_short_rate
        self._cache_seconds = cache_seconds
        self._cached_rate:  float | None = None
        self._cache_ts:     float        = 0.0

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def allows(self, direction: str) -> bool:
        """
        Geeft True terug als de richting is toegestaan bij de huidige funding rate.

        Parameters
        ----------
        direction : str
            "long" of "short".
        """
        rate = self._get_rate()
        if rate is None:
            return True  # Bij API-fout: niet blokkeren

        if direction == "long" and rate > self._max_long:
            logger.info(
                "Funding rate filter: skip LONG  rate=%.4f%% > drempel=%.4f%%",
                rate * 100, self._max_long * 100,
            )
            return False

        if direction == "short" and rate < self._min_short:
            logger.info(
                "Funding rate filter: skip SHORT rate=%.4f%% < drempel=%.4f%%",
                rate * 100, self._min_short * 100,
            )
            return False

        return True

    @property
    def current_rate(self) -> float | None:
        """Huidig gecachede funding rate (of None als nog niet opgehaald)."""
        return self._cached_rate

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------

    def _get_rate(self) -> float | None:
        """Haal funding rate op uit cache of API."""
        now = time.monotonic()
        if self._cached_rate is not None and (now - self._cache_ts) < self._cache_seconds:
            return self._cached_rate

        return self._fetch_rate()

    def _fetch_rate(self) -> float | None:
        """Haal funding rate op van OKX public API (geen authenticatie nodig)."""
        try:
            resp = requests.get(
                _OKX_FUNDING_URL,
                params={"instId": self._inst_id},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data:
                logger.warning("Funding rate: lege response voor %s", self._inst_id)
                return self._cached_rate

            rate = float(data[0]["fundingRate"])
            self._cached_rate = rate
            self._cache_ts    = time.monotonic()
            logger.debug(
                "Funding rate %s: %.5f%% (volgende funding: %s)",
                self._inst_id, rate * 100, data[0].get("nextFundingTime", "?"),
            )
            return rate

        except Exception as exc:
            logger.warning("Funding rate ophalen mislukt voor %s: %s", self._inst_id, exc)
            return self._cached_rate  # gebruik stale cache bij fout


def build_funding_filter(cfg: dict) -> FundingRateFilter | None:
    """
    Bouw een FundingRateFilter op basis van config.
    Retourneert None als de filter uitgeschakeld is.
    """
    drv_cfg = cfg.get("derivatives", {})
    ff_cfg  = drv_cfg.get("funding_rate_filter", {})

    if not ff_cfg.get("enabled", False):
        return None

    inst_id = drv_cfg.get("symbol", "BTC-USDT-SWAP")
    return FundingRateFilter(
        inst_id        = inst_id,
        max_long_rate  = ff_cfg.get("max_long_rate",  0.0003),
        min_short_rate = ff_cfg.get("min_short_rate", -0.0003),
        cache_seconds  = ff_cfg.get("cache_seconds",  300),
    )
