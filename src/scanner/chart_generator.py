"""
scanner/chart_generator.py — Genereer een setup-chart als PNG bytes.

Toont de laatste N 1H-candles met entry, SL, TP en SMC-niveaus
(zone, sweep, BoS) als horizontale lijnen en kleurvlakken.
"""
from __future__ import annotations

import io
from typing import TYPE_CHECKING

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib.patches import Rectangle

if TYPE_CHECKING:
    from src.scanner.daily_scanner import DailySetup

_BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
_CANDLES        = 80   # 1H candles in de chart (~3.3 dagen)
_SWING_LEN      = 10   # voor FVG/BoS berekening in chart

# Kleurpalet (donker thema)
_BG         = "#1a1a2e"
_BULL       = "#26A69A"
_BEAR       = "#EF5350"
_ENTRY      = "#42A5F5"
_SL         = "#EF5350"
_TP         = "#66BB6A"
_ZONE       = "#FFA726"
_SWEEP      = "#CE93D8"
_BOS        = "#64B5F6"
_CHOCH      = "#FFB74D"
_DAILY_SUP  = "#4CAF50"
_DAILY_RES  = "#EF5350"


def generate_setup_chart(setup: "DailySetup", timeframe: str = "1h") -> bytes:
    """
    Genereer een PNG-chart voor een DailySetup.

    Returns
    -------
    bytes
        PNG-afbeelding als bytes, klaar om naar Telegram te sturen.
    """
    df = _fetch_ohlcv(setup.symbol, limit=_CANDLES, interval=timeframe)
    if df.empty:
        raise RuntimeError(f"Geen OHLCV data voor {setup.symbol}")

    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    _draw_candles(ax, df)

    # SMC overlays (achtergrond → voorgrond)
    signals = _compute_chart_signals(df)
    if signals is not None:
        _draw_fvg(ax, df, signals)
        _draw_bos_choch(ax, df, signals)
    _draw_daily_sr(ax, df, setup.symbol)

    _draw_levels(ax, df, setup)
    _draw_zones(ax, df, setup)
    _style_axes(ax, df, setup)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Data ophalen
# ---------------------------------------------------------------------------

def _fetch_ohlcv(symbol: str, limit: int = 80, interval: str = "1h") -> pd.DataFrame:
    resp = requests.get(
        _BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "limit": limit + 1},
        timeout=15,
    )
    resp.raise_for_status()
    df = pd.DataFrame(
        [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
         for r in resp.json()],
        columns=["open_time", "open", "high", "low", "close", "volume"],
    )
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("open_time")
    return df.iloc[:-1]  # drop onafgesloten candle


# ---------------------------------------------------------------------------
# Dagelijkse S/R data
# ---------------------------------------------------------------------------

def _fetch_daily_ohlcv(symbol: str, limit: int = 30) -> pd.DataFrame | None:
    try:
        resp = requests.get(
            _BINANCE_KLINES,
            params={"symbol": symbol, "interval": "1d", "limit": limit + 1},
            timeout=15,
        )
        resp.raise_for_status()
        df = pd.DataFrame(
            [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])]
             for r in resp.json()],
            columns=["open_time", "open", "high", "low", "close"],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("open_time")
        return df.iloc[:-1]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SMC overlay berekening
# ---------------------------------------------------------------------------

def _compute_chart_signals(df: pd.DataFrame) -> pd.DataFrame | None:
    """Bereken FVG en BoS/CHoCH signalen op de chart-candles. Geeft None bij fouten."""
    try:
        from src.smc.signals import compute_signals
        if len(df) < _SWING_LEN * 4:
            return None
        return compute_signals(df, swing_length=_SWING_LEN)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fair Value Gaps
# ---------------------------------------------------------------------------

def _draw_fvg(ax, df: pd.DataFrame, signals: pd.DataFrame) -> None:
    """Teken ongemittigeerde FVGs als gekleurde semi-transparante vlakken."""
    fvg_rows = signals[
        signals["fvg"].isin([1.0, -1.0]) &
        (signals["fvg_mitigated_idx"] == 0.0) &
        signals["fvg_top"].notna() &
        signals["fvg_bottom"].notna()
    ]
    if fvg_rows.empty:
        return

    ts_to_x  = {ts: i for i, ts in enumerate(df.index)}
    n        = len(df)
    price_lo = df["low"].min()
    price_hi = df["high"].max()

    for ts, row in fvg_rows.iterrows():
        x = ts_to_x.get(ts)
        if x is None:
            continue
        top    = float(row["fvg_top"])
        bottom = float(row["fvg_bottom"])
        if top <= bottom:
            continue
        if top < price_lo * 0.95 or bottom > price_hi * 1.05:
            continue
        color = _BULL if int(row["fvg"]) == 1 else _BEAR
        # Kleurvlak van de FVG-candle tot rechterrand
        ax.fill_between(range(x, n), bottom, top, color=color, alpha=0.10, zorder=0)
        # Dunne randlijnen boven en onder de gap
        ax.plot([x, n], [top,    top],    color=color, linewidth=0.4, alpha=0.35, zorder=1)
        ax.plot([x, n], [bottom, bottom], color=color, linewidth=0.4, alpha=0.35, zorder=1)
        # Label rechts in het midden van de gap
        ax.text(n - 0.4, (top + bottom) / 2, "FVG",
                color=color, fontsize=6, ha="right", va="center",
                alpha=0.80, zorder=3)


# ---------------------------------------------------------------------------
# BoS / CHoCH
# ---------------------------------------------------------------------------

def _draw_bos_choch(ax, df: pd.DataFrame, signals: pd.DataFrame) -> None:
    """Teken BoS en CHoCH structuurlijnen met labels."""
    ts_to_x  = {ts: i for i, ts in enumerate(df.index)}
    n        = len(df)
    price_lo = df["low"].min()
    price_hi = df["high"].max()
    pr       = price_hi - price_lo

    markers = [
        ("bos",   _BOS,   "BOS"),
        ("choch", _CHOCH, "CHoCH"),
    ]

    for col, color, label in markers:
        for ts, row in signals[signals[col].isin([1.0, -1.0])].iterrows():
            level = float(row["structure_level"]) if not pd.isna(row.get("structure_level")) else 0.0
            if level <= 0 or level < price_lo * 0.95 or level > price_hi * 1.05:
                continue

            x_sig = ts_to_x.get(ts)
            if x_sig is None:
                continue

            broken = row.get("structure_broken_idx", np.nan)
            if pd.isna(broken) or broken <= 0:
                x_brk = n - 1
            else:
                x_brk = min(int(broken), n - 1)

            if x_brk < x_sig:
                x_brk = n - 1

            # Stippellijn van swing-punt tot breekpunt
            ax.plot([x_sig, x_brk], [level, level],
                    color=color, linestyle="--", linewidth=0.9,
                    alpha=0.75, zorder=3)
            # Stippen op de uiteinden
            ax.plot([x_sig, x_brk], [level, level], "o",
                    color=color, markersize=2.5, alpha=0.85, zorder=4)
            # Badge bij het breekpunt
            ax.text(x_brk + 0.3, level + pr * 0.004, f" {label} ",
                    color="white", fontsize=6, ha="left", va="bottom", zorder=5,
                    bbox=dict(boxstyle="round,pad=0.15", facecolor=color,
                              edgecolor="none", alpha=0.85))


# ---------------------------------------------------------------------------
# Dagelijkse support & resistance
# ---------------------------------------------------------------------------

def _draw_daily_sr(ax, df: pd.DataFrame, symbol: str) -> None:
    """Teken de dichtstbijzijnde dagelijkse support en resistance niveaus."""
    try:
        from smartmoneyconcepts import smc as smc_lib
        daily_df = _fetch_daily_ohlcv(symbol)
        if daily_df is None or len(daily_df) < 10:
            return
        swing = smc_lib.swing_highs_lows(daily_df, swing_length=5)
    except Exception:
        return

    current  = float(df["close"].iloc[-1])
    price_lo = df["low"].min()
    price_hi = df["high"].max()
    pr       = price_hi - price_lo
    y_lo     = price_lo - pr * 0.04   # zelfde als ylim in _draw_candles
    y_hi     = price_hi + pr * 0.08

    swing_lows  = swing[swing["HighLow"] == -1.0]["Level"].values
    swing_highs = swing[swing["HighLow"] ==  1.0]["Level"].values

    sup_candidates = [l for l in swing_lows  if l < current and y_lo <= l <= y_hi]
    res_candidates = [l for l in swing_highs if l > current and y_lo <= l <= y_hi]

    if sup_candidates:
        support = max(sup_candidates)
        ax.axhline(y=support, color=_DAILY_SUP, linewidth=0.8, alpha=0.50, zorder=3)
        ax.text(0.5, support + pr * 0.003, " Daily Support",
                color="white", fontsize=6.5, ha="left", va="bottom", zorder=5,
                bbox=dict(boxstyle="round,pad=0.25", facecolor=_DAILY_SUP,
                          edgecolor="none", alpha=0.85))

    if res_candidates:
        resistance = min(res_candidates)
        ax.axhline(y=resistance, color=_DAILY_RES, linewidth=0.8, alpha=0.50, zorder=3)
        ax.text(0.5, resistance + pr * 0.003, " Daily Resistance",
                color="white", fontsize=6.5, ha="left", va="bottom", zorder=5,
                bbox=dict(boxstyle="round,pad=0.25", facecolor=_DAILY_RES,
                          edgecolor="none", alpha=0.85))


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------

def _draw_candles(ax, df: pd.DataFrame) -> None:
    for i, (_, row) in enumerate(df.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        color = _BULL if c >= o else _BEAR
        ax.plot([i, i], [l, h], color=color, linewidth=0.7, zorder=1)
        body_h = abs(c - o) or (h - l) * 0.005
        ax.add_patch(Rectangle((i - 0.35, min(o, c)), 0.7, body_h,
                                color=color, zorder=2))
    price_range = df["high"].max() - df["low"].min()
    ax.set_xlim(-1, len(df) + 1)
    ax.set_ylim(df["low"].min()  - price_range * 0.04,
                df["high"].max() + price_range * 0.08)


# ---------------------------------------------------------------------------
# Horizontale niveaus
# ---------------------------------------------------------------------------

def _draw_levels(ax, df: pd.DataFrame, setup: "DailySetup") -> None:
    n         = len(df)
    pr        = df["high"].max() - df["low"].min()
    price_min = df["low"].min()
    price_max = df["high"].max()

    levels = [
        (setup.entry_zone, _ENTRY, "--", "1.4",  f"Entry  {_fmt(setup.entry_zone)}"),
        (setup.sl,         _SL,    ":",  "1.2",  f"SL      {_fmt(setup.sl)}"),
        (setup.tp,         _TP,    ":",  "1.2",  f"TP      {_fmt(setup.tp)}"),
        (setup.zone_level, _ZONE,  "-.", "1.0",  f"Zone   {_fmt(setup.zone_level)}"),
    ]
    if setup.fase in ("FASE 2", "FASE 3") and setup.sweep_low != setup.zone_level:
        sweep_lbl = "Sweep lo" if setup.direction == "long" else "Sweep hi"
        levels.append((setup.sweep_low, _SWEEP, "--", "1.0",
                        f"{sweep_lbl}  {_fmt(setup.sweep_low)}"))
    if setup.fase == "FASE 3" and setup.bos_level > 0:
        levels.append((setup.bos_level, _BOS, "--", "1.0",
                        f"BoS       {_fmt(setup.bos_level)}"))

    for price, color, style, lw, label in levels:
        if not (price_min * 0.98 <= price <= price_max * 1.02):
            continue
        ax.axhline(y=price, color=color, linestyle=style,
                   linewidth=float(lw), alpha=0.85, zorder=4)
        ax.text(n - 0.3, price + pr * 0.003, label,
                color=color, fontsize=7.5, ha="right", va="bottom", zorder=5)


# ---------------------------------------------------------------------------
# Kleurvlakken (risico / reward zones)
# ---------------------------------------------------------------------------

def _draw_zones(ax, df: pd.DataFrame, setup: "DailySetup") -> None:
    n = len(df)
    entry = setup.entry_zone
    sl    = setup.sl
    tp    = setup.tp

    # SL-zone (rood)
    ax.fill_between(
        range(n),
        min(entry, sl), max(entry, sl),
        color=_SL, alpha=0.07, zorder=0,
    )
    # TP-zone (groen)
    ax.fill_between(
        range(n),
        min(entry, tp), max(entry, tp),
        color=_TP, alpha=0.07, zorder=0,
    )


# ---------------------------------------------------------------------------
# Stijl & legenda
# ---------------------------------------------------------------------------

def _style_axes(ax, df: pd.DataFrame, setup: "DailySetup") -> None:
    n = len(df)
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [df.index[i].strftime("%d/%m %H:%M") for i in ticks],
        rotation=25, ha="right", fontsize=7.5, color="#cccccc",
    )
    ax.tick_params(axis="y", colors="#cccccc", labelsize=8)
    ax.set_ylabel("Prijs (USDT)", color="#cccccc", fontsize=9)
    ax.grid(axis="y", alpha=0.12, color="#cccccc")
    for spine in ax.spines.values():
        spine.set_edgecolor("#333355")

    arrow = "▲ LONG" if setup.direction == "long" else "▼ SHORT"
    stars = "⭐" * setup.stars
    rr    = abs(setup.tp - setup.entry_zone) / max(abs(setup.entry_zone - setup.sl), 1e-8)
    ax.set_title(
        f"{setup.symbol}  {arrow}  {stars}  |  {setup.fase}  |  RR 1:{rr:.1f}",
        color="#e0e0e0", fontsize=10, fontweight="bold", pad=8,
    )

    legend_items = [
        mpatches.Patch(color=_ENTRY, label=f"Entry  {_fmt(setup.entry_zone)}"),
        mpatches.Patch(color=_SL,    label=f"SL      {_fmt(setup.sl)}"),
        mpatches.Patch(color=_TP,    label=f"TP      {_fmt(setup.tp)}"),
        mpatches.Patch(color=_ZONE,  label=f"{getattr(setup, 'setup_type', 'EQL/EQH')} zone"),
    ]
    if setup.fase in ("FASE 2", "FASE 3"):
        legend_items.append(mpatches.Patch(color=_SWEEP, label="Sweep"))
    if setup.fase == "FASE 3" and setup.bos_level > 0:
        legend_items.append(mpatches.Patch(color=_BOS, label="BoS"))

    ax.legend(handles=legend_items, loc="upper left", fontsize=7.5,
              facecolor="#2a2a4a", edgecolor="#555577", labelcolor="white",
              framealpha=0.85)


def _fmt(v: float) -> str:
    if v >= 10_000:
        return f"${v:,.0f}"
    elif v >= 100:
        return f"${v:,.1f}"
    else:
        return f"${v:,.2f}"
