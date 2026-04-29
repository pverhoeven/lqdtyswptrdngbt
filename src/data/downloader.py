"""
downloader.py — Binance 1m klines ophalen met checkpoint en rate limiting.

Gedrag:
- Haalt BTCUSDT 1m candles op via Binance REST API
- Slaat op als parquet per jaar: BTCUSDT_1m_<jaar>.parquet
- Checkpoint (.checkpoint) onthoudt laatste succesvolle timestamp
- Bij herstart: hervat automatisch vanaf checkpoint
- Rate limiting: bewaakt X-MBX-USED-WEIGHT-1M header, wacht bij 429/418
"""

import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    # Fallback zonder progress bar als tqdm niet geïnstalleerd is
    def tqdm(iterable=None, **kwargs):  # type: ignore[misc]
        return iterable if iterable is not None else range(kwargs.get("total", 0))

from src.config_loader import load_config

logger = logging.getLogger(__name__)

# Binance endpoint
_KLINES_URL = "https://api.binance.com/api/v3/klines"
_KLINES_WEIGHT = 2          # weight per klines request
_KLINES_LIMIT = 1000        # max candles per request

# Kolommen die Binance teruggeeft (positie-gebaseerd)
_KLINES_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "num_trades",
    "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
]

# Kolommen die we bewaren
_KEEP_COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def download(cfg: dict | None = None, symbol: str | None = None) -> None:
    """
    Download alle ontbrekende 1m candles en sla op als parquet per jaar.

    Parameters
    ----------
    cfg : dict, optional
        Geladen config dict. Als None: laad automatisch.
    symbol : str, optional
        Symbool override (bijv. "ETHUSDT"). Standaard: cfg["data"]["symbol"].
    """
    if cfg is None:
        cfg = load_config()

    symbol   = symbol or cfg["data"]["symbol"]
    raw_dir  = Path(cfg["data"]["paths"]["raw"].format(symbol=symbol))
    raw_dir.mkdir(parents=True, exist_ok=True)
    interval = cfg["data"]["base_interval"]
    start    = pd.Timestamp(cfg["data"]["start_date"], tz="UTC")
    end_cfg  = cfg["data"]["end_date"]
    end      = pd.Timestamp(end_cfg, tz="UTC") if end_cfg else pd.Timestamp.utcnow()

    rpm_budget  = cfg["data"]["binance"]["requests_per_minute"]
    retry_extra = cfg["data"]["binance"]["retry_wait_seconds"]

    checkpoint_path = raw_dir / ".checkpoint"
    resume_from = _read_checkpoint(checkpoint_path)

    fetch_from = max(start, resume_from) if resume_from else start

    if fetch_from >= end:
        logger.info("Data is al up-to-date (checkpoint: %s).", fetch_from)
        return

    logger.info(
        "Download %s %s van %s t/m %s",
        symbol, interval,
        fetch_from.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )

    rate_limiter = _RateLimiter(rpm_budget, _KLINES_WEIGHT, retry_extra)
    buffer: list[list] = []
    current_ms = _ts_to_ms(fetch_from)
    end_ms     = _ts_to_ms(end)

    # Schat totaal aantal requests voor progress bar
    total_ms   = end_ms - current_ms
    ms_per_req = _KLINES_LIMIT * 60_000  # 1000 candles × 1 min
    total_reqs = max(1, int(total_ms / ms_per_req) + 1)

    with tqdm(total=total_reqs, unit="req", desc="Downloading") as pbar:
        while current_ms < end_ms:
            params = {
                "symbol":    symbol,
                "interval":  interval,
                "startTime": current_ms,
                "limit":     _KLINES_LIMIT,
            }

            rows, next_ms, used_weight = _fetch_klines(
                params, rate_limiter, retry_extra
            )

            if not rows:
                break

            buffer.extend(rows)
            current_ms = next_ms

            # Checkpoint na elke batch
            last_ts = pd.Timestamp(rows[-1][0], unit="ms", tz="UTC")
            _write_checkpoint(checkpoint_path, last_ts)

            # Tussentijds wegschrijven per jaar
            _flush_buffer_to_parquet(buffer, raw_dir, symbol, partial=True)
            buffer.clear()

            pbar.update(1)
            pbar.set_postfix({"weight/min": used_weight, "up_to": str(last_ts.date())})

    # Eventuele resterende buffer wegschrijven
    if buffer:
        _flush_buffer_to_parquet(buffer, raw_dir, symbol, partial=True)

    logger.info("Download klaar. Checkpoint: %s", _read_checkpoint(checkpoint_path))


# ---------------------------------------------------------------------------
# Binance API
# ---------------------------------------------------------------------------

def _fetch_klines(
    params: dict,
    rate_limiter: "_RateLimiter",
    retry_extra: int,
) -> tuple[list, int, int]:
    """
    Doe één klines-request. Retourneert (rows, next_start_ms, used_weight).
    Handelt 429 en 418 af met wachten.
    """
    while True:
        rate_limiter.wait_if_needed()

        try:
            resp = requests.get(_KLINES_URL, params=params, timeout=10)
        except requests.RequestException as exc:
            logger.warning("Netwerkfout: %s — opnieuw over 5s", exc)
            time.sleep(5)
            continue

        used_weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", 0))
        rate_limiter.update(used_weight)

        if resp.status_code == 200:
            rows = resp.json()
            if not rows:
                return [], params["startTime"], used_weight
            # next_start = close_time van laatste candle + 1 ms
            next_ms = rows[-1][6] + 1
            clean = [_clean_row(r) for r in rows]
            return clean, next_ms, used_weight

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60)) + retry_extra
            logger.warning("Rate limit (429) — wacht %ds", wait)
            time.sleep(wait)
            continue

        if resp.status_code == 418:
            wait = int(resp.headers.get("Retry-After", 120)) + retry_extra
            logger.error("IP ban (418) — wacht %ds", wait)
            time.sleep(wait)
            continue

        resp.raise_for_status()


def _clean_row(row: list) -> list:
    """Zet ruwe Binance rij om naar [open_time_ms, open, high, low, close, volume]."""
    return [
        int(row[0]),          # open_time als ms timestamp
        float(row[1]),        # open
        float(row[2]),        # high
        float(row[3]),        # low
        float(row[4]),        # close
        float(row[5]),        # volume
    ]


# ---------------------------------------------------------------------------
# Parquet opslag (per jaar)
# ---------------------------------------------------------------------------

def _flush_buffer_to_parquet(
    rows: list[list],
    raw_dir: Path,
    symbol: str,
    partial: bool = False,
) -> None:
    """
    Schrijf rows weg naar parquet, gesplitst per jaar.
    Bij partial=True: merge met bestaand bestand voor dat jaar.
    """
    if not rows:
        return

    df = pd.DataFrame(rows, columns=_KEEP_COLUMNS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("open_time").sort_index()
    df = df.astype({"open": "float32", "high": "float32",
                    "low": "float32", "close": "float32", "volume": "float32"})

    for year, year_df in df.groupby(df.index.year):
        path = raw_dir / f"{symbol}_1m_{year}.parquet"
        tmp_path = path.with_suffix(".tmp.parquet")

        if partial and path.exists():
            existing = pd.read_parquet(path)
            year_df = pd.concat([existing, year_df])
            year_df = year_df[~year_df.index.duplicated(keep="last")]
            year_df = year_df.sort_index()

        year_df.to_parquet(tmp_path)
        tmp_path.replace(path)


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def _read_checkpoint(path: Path) -> pd.Timestamp | None:
    if not path.exists():
        return None
    text = path.read_text().strip()
    if not text:
        return None
    return pd.Timestamp(text, tz="UTC")


def _write_checkpoint(path: Path, ts: pd.Timestamp) -> None:
    path.write_text(ts.isoformat())


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Bewaakt X-MBX-USED-WEIGHT-1M en pauzeert wanneer budget bereikt wordt.

    Binance reset de teller elke minuut. We bewaken het verbruik en wachten
    zodra we boven het ingestelde budget komen.
    """

    def __init__(self, requests_per_minute: int, weight_per_req: int, retry_extra: int):
        self._budget = requests_per_minute * weight_per_req
        self._retry_extra = retry_extra
        self._used_weight = 0
        self._window_start = time.monotonic()

    def update(self, used_weight: int) -> None:
        self._used_weight = used_weight

    def wait_if_needed(self) -> None:
        elapsed = time.monotonic() - self._window_start

        # Reset teller na 60 seconden
        if elapsed >= 60:
            self._used_weight = 0
            self._window_start = time.monotonic()
            return

        if self._used_weight >= self._budget:
            wait = 60 - elapsed + self._retry_extra
            logger.debug("Budget bereikt (%d weight) — wacht %.1fs", self._used_weight, wait)
            time.sleep(wait)
            self._used_weight = 0
            self._window_start = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_to_ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)
