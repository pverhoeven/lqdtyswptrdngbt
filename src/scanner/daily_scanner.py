"""
scanner/daily_scanner.py — Dagelijkse SMC setup scanner voor Telegram rapport.

Detecteert twee spiegelbeeldige patronen op 1H Binance data:

  LONG:  Equal Lows (EQL)  → sweep  → bullish BoS  → entry bij re-test BoS-niveau
  SHORT: Equal Highs (EQH) → sweep  → bearish BoS  → entry bij re-test BoS-niveau

De BoS is de bevestiging — de sweep zonder BoS is ruis.
Entry altijd op re-test van het gebroken niveau, nooit op de sweep zelf.

Drie fasen per setup:
  FASE 1  EQL/EQH aanwezig, geen sweep gezien       → "let op dit niveau"
  FASE 2  Sweep gezien (wick + rejection), geen BoS → "wacht op BoS"
  FASE 3  Sweep + BoS bevestigd, re-test verwacht   → "entry op komst"  ⭐⭐⭐

Data: Binance 1H (granulariteit voor EQL/EQH-detectie).
Uitvoering: OKX XPERP (identieke prijsniveaus).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

from src.smc.signals import compute_signals

logger = logging.getLogger(__name__)

_BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
_FETCH_LIMIT    = 200    # 1H candles ≈ 8 dagen
_SWING_LENGTH   = 10
_EQL_TOL        = 0.007  # 0.7% tolerantie voor "gelijke" highs/lows
_SWEEP_LOOKBACK = 50     # candles terug om sweep te zoeken
_BOS_WINDOW     = 20     # candles na sweep voor BoS-check
_PROXIMITY      = 0.06   # max 6% afstand tot EQL/EQH zone
_SL_BUF         = 0.005  # 0.5% voorbij sweep wick
_RR             = 2.0


@dataclass
class DailySetup:
    symbol:          str
    xperp:           str
    direction:       str    # "long" | "short"
    fase:            str    # "FASE 1" | "FASE 2" | "FASE 3"
    fase_label:      str    # leesbare omschrijving
    current_price:   float
    zone_level:      float  # EQL (long) of EQH (short) zone
    sweep_low:       float  # diepste wick (long) of hoogste wick (short)
    bos_level:       float  # gebroken swing high/low na de sweep (0 als geen BoS)
    entry_zone:      float  # geschatte entry (BoS-niveau of zone zelf)
    sl:              float
    tp:              float
    stars:           int    # 1–3
    n_equal:         int    # hoeveel gelijke highs/lows
    confluences:     list[str] = field(default_factory=list)
    distance_pct:    float = 0.0


def run_daily_scan(cfg: dict) -> list[DailySetup]:
    """Scan alle geconfigureerde coins en geef gerangschikte setups terug."""
    coins = cfg.get("coins", [])

    all_setups: list[DailySetup] = []
    for coin in coins:
        symbol = coin.get("symbol", "")
        xperp  = coin.get("swap_symbol", "")
        if not symbol or not xperp:
            continue
        try:
            setups = _scan_symbol(symbol, xperp)
            all_setups.extend(setups)
            logger.info("%s: %d setup(s)", symbol, len(setups))
        except Exception as exc:
            logger.warning("Scan mislukt voor %s: %s", symbol, exc)

    # Sorteer: fase 3 eerst, dan fase 2, dan fase 1; daarna op afstand
    fase_order = {"FASE 3": 0, "FASE 2": 1, "FASE 1": 2}
    all_setups.sort(key=lambda s: (fase_order.get(s.fase, 9), -s.stars, s.distance_pct))
    return all_setups


# ---------------------------------------------------------------------------
# Per-symbool scan
# ---------------------------------------------------------------------------

def _scan_symbol(symbol: str, xperp: str) -> list[DailySetup]:
    try:
        from smartmoneyconcepts import smc as smc_lib
    except ImportError as exc:
        raise ImportError("smartmoneyconcepts niet geïnstalleerd") from exc

    df      = _fetch_ohlcv(symbol, "1h", limit=_FETCH_LIMIT)
    if len(df) < _SWING_LENGTH * 4:
        return []

    current_price = float(df["close"].iloc[-1])
    signals       = compute_signals(df, swing_length=_SWING_LENGTH)
    swing         = smc_lib.swing_highs_lows(df, swing_length=_SWING_LENGTH)

    setups: list[DailySetup] = []

    # --- LONG: Equal Lows onder prijs ---
    lows = swing[swing["HighLow"] == -1.0]["Level"].values
    for zone_level, n_eq in _find_equal_levels(lows):
        if zone_level >= current_price:
            continue
        dist = (current_price - zone_level) / current_price
        if dist > _PROXIMITY:
            continue
        s = _evaluate_long(symbol, xperp, df, signals, zone_level, n_eq, current_price, dist)
        if s:
            setups.append(s)

    # --- SHORT: Equal Highs boven prijs ---
    highs = swing[swing["HighLow"] == 1.0]["Level"].values
    for zone_level, n_eq in _find_equal_levels(highs):
        if zone_level <= current_price:
            continue
        dist = (zone_level - current_price) / current_price
        if dist > _PROXIMITY:
            continue
        s = _evaluate_short(symbol, xperp, df, signals, zone_level, n_eq, current_price, dist)
        if s:
            setups.append(s)

    # Max 4 per symbool: prioriteer hogere fasen
    fase_order = {"FASE 3": 0, "FASE 2": 1, "FASE 1": 2}
    setups.sort(key=lambda s: (fase_order.get(s.fase, 9), -s.stars, s.distance_pct))
    return setups[:4]


# ---------------------------------------------------------------------------
# Long-setup evaluatie  (EQL → sweep → bullish BoS → re-test)
# ---------------------------------------------------------------------------

def _evaluate_long(
    symbol: str, xperp: str, df: pd.DataFrame, signals: pd.DataFrame,
    zone_level: float, n_eq: int, current_price: float, dist: float,
) -> DailySetup | None:

    recent_df  = df.iloc[-_SWEEP_LOOKBACK:]
    recent_sig = signals.iloc[-_SWEEP_LOOKBACK:]

    # Stap 1: sweep detectie (candle low < zone_level)
    sweep_mask = recent_df["low"] < zone_level
    swept = sweep_mask.any()

    if not swept:
        # FASE 1: EQL aanwezig, geen sweep
        stars = min(3, 1 + (1 if n_eq >= 3 else 0) + (1 if dist < 0.02 else 0))
        entry = zone_level
        sl    = zone_level * (1 - _SL_BUF * 2)
        tp    = entry + abs(entry - sl) * _RR
        return DailySetup(
            symbol=symbol, xperp=xperp, direction="long",
            fase="FASE 1", fase_label="EQL zone nadert — watch for sweep",
            current_price=current_price, zone_level=zone_level,
            sweep_low=zone_level, bos_level=0.0, entry_zone=entry,
            sl=sl, tp=tp, stars=stars, n_equal=n_eq,
            confluences=[
                f"EQL zone @ {_fmt(zone_level)} ({n_eq}× equal low)",
                f"Afstand tot zone: {dist:.1%}",
            ],
            distance_pct=dist,
        )

    # Sweep gevonden — neem de EERSTE sweep in het window
    sweep_pos  = int(sweep_mask.values.argmax())
    sweep_ts   = recent_df.index[sweep_pos]
    sweep_low  = float(recent_df.loc[sweep_ts, "low"])
    sweep_close = float(recent_df.loc[sweep_ts, "close"])
    wick_rej   = sweep_close > zone_level  # sluit boven EQL = wick rejection

    # Stap 2: BoS na sweep (bos == 1)
    sig_pos    = recent_sig.index.get_loc(sweep_ts)
    sigs_after = recent_sig.iloc[sig_pos + 1 : sig_pos + 1 + _BOS_WINDOW]
    bos_mask   = sigs_after["bos"] == 1.0
    bos_ok     = bos_mask.any()

    if not bos_ok:
        if not wick_rej:
            return None  # sweep zonder wick rejection = ruis
        # FASE 2: sweep + wick rejection, wacht op BoS
        entry = zone_level
        sl    = sweep_low * (1 - _SL_BUF)
        sl_dist = abs(entry - sl)
        if sl_dist < entry * 0.001:
            return None
        tp = entry + sl_dist * _RR
        return DailySetup(
            symbol=symbol, xperp=xperp, direction="long",
            fase="FASE 2", fase_label="Sweep gezien — wacht op bullish BoS",
            current_price=current_price, zone_level=zone_level,
            sweep_low=sweep_low, bos_level=0.0, entry_zone=entry,
            sl=sl, tp=tp, stars=2, n_equal=n_eq,
            confluences=[
                f"EQL zone @ {_fmt(zone_level)} ({n_eq}× equal low)",
                f"Sweep wick @ {_fmt(sweep_low)} met rejection ✓",
                "Wacht op sluiting boven vorige swing high (BoS)",
            ],
            distance_pct=dist,
        )

    # BoS bevestigd
    bos_level = float(sigs_after.loc[bos_mask, "structure_level"].iloc[0])

    # Is re-test al uitgespeeld? (prijs ging significant onder BoS na bevestiging)
    idx_after_bos = sigs_after.index[bos_mask][0]
    bos_pos       = df.index.get_loc(idx_after_bos)
    post_bos_lows = df["low"].iloc[bos_pos + 1:]
    if len(post_bos_lows) > 0 and (post_bos_lows < bos_level * 0.99).any():
        return None  # re-test al geweest of BoS gefaald — setup uitgespeeld

    # FASE 3: BoS bevestigd, entry op komst
    in_retest = current_price <= bos_level * 1.015  # prijs al bij BoS-niveau
    fase_label = "BoS bevestigd — ENTRY NU" if in_retest else "BoS bevestigd — entry op komst"
    entry = bos_level
    sl    = sweep_low * (1 - _SL_BUF)
    sl_dist = abs(entry - sl)
    if sl_dist < entry * 0.001:
        return None
    tp    = entry + sl_dist * _RR
    stars = 3

    confluences = [
        f"EQL zone @ {_fmt(zone_level)} ({n_eq}× equal low)",
        f"Sweep wick @ {_fmt(sweep_low)} ✓",
        f"Bullish BoS @ {_fmt(bos_level)} ✓",
        "Entry bij re-test van BoS-niveau" + (" — PRIJS HIER NU" if in_retest else ""),
    ]
    return DailySetup(
        symbol=symbol, xperp=xperp, direction="long",
        fase="FASE 3", fase_label=fase_label,
        current_price=current_price, zone_level=zone_level,
        sweep_low=sweep_low, bos_level=bos_level, entry_zone=entry,
        sl=sl, tp=tp, stars=stars, n_equal=n_eq,
        confluences=confluences, distance_pct=dist,
    )


# ---------------------------------------------------------------------------
# Short-setup evaluatie  (EQH → sweep → bearish BoS → re-test)
# ---------------------------------------------------------------------------

def _evaluate_short(
    symbol: str, xperp: str, df: pd.DataFrame, signals: pd.DataFrame,
    zone_level: float, n_eq: int, current_price: float, dist: float,
) -> DailySetup | None:

    recent_df  = df.iloc[-_SWEEP_LOOKBACK:]
    recent_sig = signals.iloc[-_SWEEP_LOOKBACK:]

    # Stap 1: sweep detectie (candle high > zone_level)
    sweep_mask = recent_df["high"] > zone_level
    swept = sweep_mask.any()

    if not swept:
        # FASE 1: EQH aanwezig, geen sweep
        stars = min(3, 1 + (1 if n_eq >= 3 else 0) + (1 if dist < 0.02 else 0))
        entry = zone_level
        sl    = zone_level * (1 + _SL_BUF * 2)
        tp    = entry - abs(sl - entry) * _RR
        return DailySetup(
            symbol=symbol, xperp=xperp, direction="short",
            fase="FASE 1", fase_label="EQH zone nadert — watch for sweep",
            current_price=current_price, zone_level=zone_level,
            sweep_low=zone_level, bos_level=0.0, entry_zone=entry,
            sl=sl, tp=tp, stars=stars, n_equal=n_eq,
            confluences=[
                f"EQH zone @ {_fmt(zone_level)} ({n_eq}× equal high)",
                f"Afstand tot zone: {dist:.1%}",
            ],
            distance_pct=dist,
        )

    # Sweep gevonden
    sweep_pos   = int(sweep_mask.values.argmax())
    sweep_ts    = recent_df.index[sweep_pos]
    sweep_high  = float(recent_df.loc[sweep_ts, "high"])
    sweep_close = float(recent_df.loc[sweep_ts, "close"])
    wick_rej    = sweep_close < zone_level  # sluit onder EQH = wick rejection

    # Stap 2: BoS na sweep (bos == -1)
    sig_pos    = recent_sig.index.get_loc(sweep_ts)
    sigs_after = recent_sig.iloc[sig_pos + 1 : sig_pos + 1 + _BOS_WINDOW]
    bos_mask   = sigs_after["bos"] == -1.0
    bos_ok     = bos_mask.any()

    if not bos_ok:
        if not wick_rej:
            return None  # ruis
        # FASE 2
        entry = zone_level
        sl    = sweep_high * (1 + _SL_BUF)
        sl_dist = abs(sl - entry)
        if sl_dist < entry * 0.001:
            return None
        tp = entry - sl_dist * _RR
        return DailySetup(
            symbol=symbol, xperp=xperp, direction="short",
            fase="FASE 2", fase_label="Sweep gezien — wacht op bearish BoS",
            current_price=current_price, zone_level=zone_level,
            sweep_low=sweep_high, bos_level=0.0, entry_zone=entry,
            sl=sl, tp=tp, stars=2, n_equal=n_eq,
            confluences=[
                f"EQH zone @ {_fmt(zone_level)} ({n_eq}× equal high)",
                f"Sweep wick @ {_fmt(sweep_high)} met rejection ✓",
                "Wacht op sluiting onder vorige swing low (BoS)",
            ],
            distance_pct=dist,
        )

    # BoS bevestigd
    bos_level = float(sigs_after.loc[bos_mask, "structure_level"].iloc[0])

    # Is re-test al uitgespeeld?
    idx_after_bos  = sigs_after.index[bos_mask][0]
    bos_pos        = df.index.get_loc(idx_after_bos)
    post_bos_highs = df["high"].iloc[bos_pos + 1:]
    if len(post_bos_highs) > 0 and (post_bos_highs > bos_level * 1.01).any():
        return None  # re-test al geweest of BoS gefaald

    # FASE 3
    in_retest  = current_price >= bos_level * 0.985
    fase_label = "BoS bevestigd — ENTRY NU" if in_retest else "BoS bevestigd — entry op komst"
    entry = bos_level
    sl    = sweep_high * (1 + _SL_BUF)
    sl_dist = abs(sl - entry)
    if sl_dist < entry * 0.001:
        return None
    tp    = entry - sl_dist * _RR
    stars = 3

    confluences = [
        f"EQH zone @ {_fmt(zone_level)} ({n_eq}× equal high)",
        f"Sweep wick @ {_fmt(sweep_high)} ✓",
        f"Bearish BoS @ {_fmt(bos_level)} ✓",
        "Entry bij re-test van BoS-niveau" + (" — PRIJS HIER NU" if in_retest else ""),
    ]
    return DailySetup(
        symbol=symbol, xperp=xperp, direction="short",
        fase="FASE 3", fase_label=fase_label,
        current_price=current_price, zone_level=zone_level,
        sweep_low=sweep_high, bos_level=bos_level, entry_zone=entry,
        sl=sl, tp=tp, stars=stars, n_equal=n_eq,
        confluences=confluences, distance_pct=dist,
    )


# ---------------------------------------------------------------------------
# Equal levels detectie
# ---------------------------------------------------------------------------

def _find_equal_levels(levels: np.ndarray, tol: float = _EQL_TOL) -> list[tuple[float, int]]:
    """
    Vind clusters van 2+ levels die binnen tol% van elkaar liggen.
    Geeft [(gemiddeld_niveau, n_levels), ...] terug.
    """
    if len(levels) < 2:
        return []

    sorted_lvl = np.sort(levels)
    zones: list[tuple[float, int]] = []
    i = 0
    while i < len(sorted_lvl):
        group = [sorted_lvl[i]]
        j = i + 1
        while j < len(sorted_lvl) and (sorted_lvl[j] - sorted_lvl[i]) / sorted_lvl[i] <= tol:
            group.append(sorted_lvl[j])
            j += 1
        if len(group) >= 2:
            zones.append((float(np.mean(group)), len(group)))
        i = j if j > i else i + 1

    # Merge overlappende zones
    merged: list[tuple[float, int]] = []
    for z in zones:
        if merged and abs(z[0] - merged[-1][0]) / z[0] < tol * 2:
            pl, pn = merged[-1]
            merged[-1] = ((pl * pn + z[0] * z[1]) / (pn + z[1]), pn + z[1])
        else:
            merged.append(z)

    return merged


# ---------------------------------------------------------------------------
# Binance REST helper
# ---------------------------------------------------------------------------

def _fetch_ohlcv(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    resp = requests.get(
        _BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    df = pd.DataFrame(
        [
            [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
            for r in resp.json()
        ],
        columns=["open_time", "open", "high", "low", "close", "volume"],
    )
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("open_time")
    return df.iloc[:-1]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _fmt(v: float) -> str:
    if v >= 10_000:
        return f"${v:,.0f}"
    elif v >= 100:
        return f"${v:,.1f}"
    else:
        return f"${v:,.2f}"
