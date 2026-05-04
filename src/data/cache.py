"""
cache.py — SMC cache: bouwen, laden en valideren.

Structuur op disk:
    data/smc_cache/BTCUSDT/15m/
        2019_Q1.parquet   + 2019_Q1.meta.json
        2019_Q2.parquet   + 2019_Q2.meta.json
        ...

Cache-regels (uit spec):
    - Schrijf eerst naar .tmp, hernoem na succesvolle write
    - Laad nooit .tmp bestanden
    - Bij smc lib versiewijziging  → gehele cache ongeldig, herbouwen
    - Bij swing_length wijziging   → gehele cache ongeldig, herbouwen
    - Overlap-venster              → laad laatste swing_length × 2 candles
      van vorige partitie als context bij bouwen
"""

import json
import logging
from pathlib import Path

import pandas as pd

from src.config_loader import load_config
from src.smc.signals import compute_signals

logger = logging.getLogger(__name__)

try:
    from tqdm import tqdm as _tqdm
except ImportError:
    def _tqdm(iterable=None, **kwargs):  # type: ignore[misc]
        return iterable


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def _resolve_cache_dir(cfg: dict, symbol: str) -> Path:
    """Geeft de cache-map terug op basis van causal_shift instelling."""
    base = Path(cfg["data"]["paths"]["smc_cache"].format(symbol=symbol))
    if cfg.get("smc", {}).get("causal_shift", True):
        return base.parent / (base.name + "_causal")
    return base


def build_cache(cfg: dict | None = None, force: bool = False, symbol: str | None = None) -> None:
    """
    Bouw of update de SMC cache voor alle kwartaalpartities.

    Parameters
    ----------
    cfg : dict, optional
        Geladen config dict.
    force : bool
        Als True: herbouw alle partities, ook al zijn ze al compleet.
    symbol : str, optional
        Symbool override (bijv. "ETHUSDT"). Standaard: cfg["data"]["symbol"].
    """
    if cfg is None:
        cfg = load_config()

    symbol        = symbol or cfg["data"]["symbol"]
    cache_dir     = _resolve_cache_dir(cfg, symbol)
    processed_dir = Path(cfg["data"]["paths"]["processed"])
    tf            = cfg["data"]["timeframes"]["signal"].replace("min", "m")

    swing_length  = cfg["smc"]["swing_length"]
    lib_version   = cfg["smc"]["lib_version"]
    causal_shift  = cfg["smc"].get("causal_shift", True)

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Laad 15m data
    path_15m = processed_dir / f"{symbol}_{tf}.parquet"
    if not path_15m.exists():
        raise FileNotFoundError(
            f"{path_15m} niet gevonden. Voer eerst scripts/build_cache.py uit "
            "(aggregator stap)."
        )

    df_15m = pd.read_parquet(path_15m)
    logger.info("15m data geladen: %d candles", len(df_15m))

    # Controleer of de hele cache ongeldig is (versie of param gewijzigd)
    if not force and _cache_is_stale(cache_dir, lib_version, swing_length, causal_shift):
        logger.warning(
            "Cache ongeldig (versie of swing_length gewijzigd) — volledige herbouw."
        )
        _clear_cache(cache_dir)
        force = True

    # Bouw per kwartaal
    quarters = _get_quarters(df_15m)
    logger.info("Te verwerken kwartalen: %d", len(quarters))

    for quarter_label, (q_start, q_end) in _tqdm(
        quarters.items(), desc="Cache bouwen", unit="kwartaal"
    ):
        part_path  = cache_dir / f"{quarter_label}.parquet"
        meta_path  = cache_dir / f"{quarter_label}.meta.json"

        if not force and _partition_is_complete(meta_path, lib_version, swing_length, causal_shift):
            logger.debug("Partitie %s al compleet, overgeslagen.", quarter_label)
            continue

        logger.info("Verwerken: %s", quarter_label)

        # Laad context (overlap) van vorige partitie
        overlap = _load_overlap(df_15m, q_start, swing_length)
        chunk   = df_15m.loc[q_start:q_end]

        if chunk.empty:
            logger.debug("Leeg kwartaal %s, overgeslagen.", quarter_label)
            continue

        ohlc_with_context = pd.concat([overlap, chunk])
        ohlc_with_context = ohlc_with_context[
            ~ohlc_with_context.index.duplicated(keep="last")
        ]

        # Bereken SMC signalen
        signals = compute_signals(ohlc_with_context, swing_length=swing_length)

        if causal_shift:
            # Swing-niveaus worden pas na swing_length candles bevestigd.
            # Verschuif alle afgeleide kolommen zodat signalen pas beschikbaar zijn
            # wanneer ze ook in live trading detecteerbaar zouden zijn.
            _smc_cols = [c for c in signals.columns if c != "atr"]
            signals[_smc_cols] = signals[_smc_cols].shift(swing_length)

        # Bewaar alleen candles van dit kwartaal (context wegknippen)
        signals = signals.loc[q_start:q_end]

        # Atomisch schrijven
        tmp_path = part_path.with_suffix(".tmp.parquet")
        signals.to_parquet(tmp_path)
        tmp_path.replace(part_path)

        _write_meta(meta_path, quarter_label, lib_version, swing_length, symbol, causal_shift)
        logger.info("Partitie %s opgeslagen (%d candles).", quarter_label, len(signals))

    logger.info("Cache bouwen klaar.")


def load_cache(
    cfg: dict | None = None,
    start: str | None = None,
    end:   str | None = None,
    symbol: str | None = None,
) -> pd.DataFrame:
    """
    Laad gecachede SMC signalen voor een opgegeven periode.

    Parameters
    ----------
    cfg : dict, optional
    start : str, optional
        Startdatum (bijv. "2019-01-01"). Standaard: begin van cache.
    end : str, optional
        Einddatum (bijv. "2022-12-31"). Standaard: einde van cache.
    symbol : str, optional
        Symbool override (bijv. "ETHUSDT"). Standaard: cfg["data"]["symbol"].

    Returns
    -------
    pd.DataFrame
        Gecombineerde SMC signalen voor de gevraagde periode.

    Raises
    ------
    FileNotFoundError
        Als er geen cache-bestanden gevonden worden.
    """
    if cfg is None:
        cfg = load_config()

    symbol    = symbol or cfg["data"]["symbol"]
    cache_dir = _resolve_cache_dir(cfg, symbol)

    parquet_files = sorted(
        f for f in cache_dir.glob("*.parquet")
        if ".tmp." not in f.name
    )

    if not parquet_files:
        raise FileNotFoundError(
            f"Geen cache-bestanden in {cache_dir}. "
            "Voer eerst scripts/build_cache.py uit."
        )

    parts = [pd.read_parquet(f) for f in parquet_files]
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="last")]

    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="UTC")]

    return df


# ---------------------------------------------------------------------------
# Kwartaalpartities
# ---------------------------------------------------------------------------

def _get_quarters(df: pd.DataFrame) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """
    Geef een geordend dict van {label: (start, end)} voor elk kwartaal in df.
    """
    quarters: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}

    for year in df.index.year.unique():
        for q in range(1, 5):
            month_start = (q - 1) * 3 + 1
            q_start = pd.Timestamp(f"{year}-{month_start:02d}-01", tz="UTC")
            # Einde kwartaal = begin volgend kwartaal - 1 nanoseconde
            if q < 4:
                q_end = pd.Timestamp(f"{year}-{month_start + 3:02d}-01", tz="UTC") \
                        - pd.Timedelta(nanoseconds=1)
            else:
                q_end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC") \
                        - pd.Timedelta(nanoseconds=1)

            # Sla over als er geen data is in dit kwartaal
            mask = (df.index >= q_start) & (df.index <= q_end)
            if not mask.any():
                continue

            label = f"{year}_Q{q}"
            quarters[label] = (q_start, q_end)

    return quarters


def _load_overlap(
    df_15m: pd.DataFrame,
    q_start: pd.Timestamp,
    swing_length: int,
) -> pd.DataFrame:
    """
    Geef de laatste swing_length × 2 candles vóór q_start terug als context.
    """
    n_overlap = swing_length * 2
    before = df_15m[df_15m.index < q_start]
    if before.empty or len(before) < n_overlap:
        return before
    return before.iloc[-n_overlap:]


# ---------------------------------------------------------------------------
# Cache-validatie
# ---------------------------------------------------------------------------

def _partition_is_complete(
    meta_path: Path,
    lib_version: str,
    swing_length: int,
    causal_shift: bool = True,
) -> bool:
    """True als de partitie bestaat, compleet is, en de parameters overeenkomen."""
    if not meta_path.exists():
        return False
    meta = _read_meta(meta_path)
    # Bestaande caches zonder causal_shift-veld zijn gebouwd mét shift (historisch gedrag).
    return (
        meta.get("status") == "complete"
        and meta.get("smc_lib_version") == lib_version
        and meta.get("swing_length") == swing_length
        and meta.get("causal_shift", True) == causal_shift
    )


def _cache_is_stale(
    cache_dir: Path,
    lib_version: str,
    swing_length: int,
    causal_shift: bool = True,
) -> bool:
    """
    True als er minstens één meta.json bestaat met afwijkende versie, swing_length
    of causal_shift-instelling. Een lege cache is niet stale.
    """
    meta_files = list(cache_dir.glob("*.meta.json"))
    if not meta_files:
        return False

    for meta_path in meta_files:
        meta = _read_meta(meta_path)
        if (
            meta.get("smc_lib_version") != lib_version
            or meta.get("swing_length") != swing_length
            or meta.get("causal_shift", True) != causal_shift
        ):
            return True
    return False


def _clear_cache(cache_dir: Path) -> None:
    """Verwijder alle .parquet en .meta.json bestanden in cache_dir."""
    for f in cache_dir.iterdir():
        if f.suffix in (".parquet", ".json") and ".tmp." not in f.name:
            f.unlink()
            logger.debug("Verwijderd: %s", f.name)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _write_meta(
    meta_path: Path,
    quarter_label: str,
    lib_version: str,
    swing_length: int,
    symbol: str = "BTCUSDT",
    causal_shift: bool = True,
) -> None:
    meta = {
        "coin":            symbol,
        "timeframe":       "15m",
        "period":          quarter_label,
        "smc_lib_version": lib_version,
        "swing_length":    swing_length,
        "causal_shift":    causal_shift,
        "status":          "complete",
    }
    tmp = meta_path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(meta_path)


def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
