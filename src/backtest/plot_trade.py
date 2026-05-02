"""
plot_trade.py — Statische trade charts exporteren als PNG.

Elke chart toont: sweep candle, BOS candle, entry, SL, TP en uitkomst.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")  # headless, geen venster nodig

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def plot_trade(
    trade,
    df_15m:    pd.DataFrame,
    cache:     pd.DataFrame | None = None,
    out_path:  Path | str | None = None,
    lookback:  int = 50,
    lookforward: int = 15,
) -> None:
    """
    Render één trade als statische PNG.

    Parameters
    ----------
    trade       : Trade dataclass
    df_15m      : volledige 15m OHLCV DataFrame
    cache       : SMC cache DataFrame (voor sweep/BOS markers)
    out_path    : pad om PNG op te slaan (None = niet opslaan)
    lookback    : candles vóór entry_time
    lookforward : candles ná exit_time
    """
    entry_idx = _safe_loc(df_15m.index, trade.entry_time)
    exit_idx  = _safe_loc(df_15m.index, trade.exit_time)

    start_idx = max(0, entry_idx - lookback)
    end_idx   = min(len(df_15m), exit_idx + lookforward)
    df = df_15m.iloc[start_idx:end_idx].copy()
    if df.empty:
        return

    sweep_ts, bos_ts = _find_sweep_and_bos(trade, cache, lookback + 5)

    fig, ax = plt.subplots(figsize=(18, 7))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    _draw_candles(ax, df)
    _draw_smc_markers(ax, df, sweep_ts, bos_ts)
    _draw_levels(ax, df, trade)
    _draw_entry_exit_markers(ax, df, trade)
    _style_axes(ax, df, trade)

    plt.tight_layout()
    if out_path is not None:
        fig.savefig(out_path, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


# ---------------------------------------------------------------------------
# SMC markers zoeken in cache
# ---------------------------------------------------------------------------

def _find_sweep_and_bos(
    trade,
    cache: pd.DataFrame | None,
    lookback: int,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Zoek de sweep- en BOS-candle vóór de entry in de SMC cache."""
    if cache is None:
        return None, None

    entry_idx = _safe_loc(cache.index, trade.entry_time)
    start_idx = max(0, entry_idx - lookback)
    window    = cache.iloc[start_idx : entry_idx + 1]

    # liq == -1 → long sweep (sweep van lows), liq == 1 → short sweep
    liq_val = -1 if trade.direction == "long" else 1
    sweeps  = window[window.get("liq", pd.Series(dtype=float)) == liq_val]
    sweep_ts = sweeps.index[-1] if not sweeps.empty else None

    # BOS == 1 → bullish (bevestigt long), BOS == -1 → bearish (bevestigt short)
    bos_val = 1 if trade.direction == "long" else -1
    bos_start = _safe_loc(cache.index, sweep_ts) if sweep_ts is not None else start_idx
    bos_window = cache.iloc[bos_start : entry_idx + 1]
    boses   = bos_window[bos_window.get("bos", pd.Series(dtype=float)) == bos_val]
    bos_ts  = boses.index[-1] if not boses.empty else None

    return sweep_ts, bos_ts


# ---------------------------------------------------------------------------
# Tekenfuncties
# ---------------------------------------------------------------------------

def _draw_candles(ax, df: pd.DataFrame) -> None:
    for i, (_, row) in enumerate(df.iterrows()):
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        color = "#26A69A" if c >= o else "#EF5350"
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=1)
        body_h = abs(c - o) or (h - l) * 0.005
        ax.add_patch(Rectangle((i - 0.35, min(o, c)), 0.7, body_h,
                                color=color, zorder=2))
    ax.set_xlim(-1, len(df))
    price_range = df["high"].max() - df["low"].min()
    ax.set_ylim(df["low"].min()  - price_range * 0.03,
                df["high"].max() + price_range * 0.05)


def _draw_smc_markers(ax, df: pd.DataFrame, sweep_ts, bos_ts) -> None:
    y_top = ax.get_ylim()[1]

    if sweep_ts is not None:
        x = _ts_to_x(df, sweep_ts)
        if x is not None:
            ax.axvline(x=x, color="#CE93D8", alpha=0.5, linewidth=1.8, zorder=3)
            ax.text(x, y_top * 0.998, "Sweep", color="#CE93D8",
                    fontsize=8, ha="center", va="top", fontweight="bold")

    if bos_ts is not None:
        x = _ts_to_x(df, bos_ts)
        if x is not None:
            ax.axvline(x=x, color="#64B5F6", alpha=0.5, linewidth=1.8, zorder=3)
            ax.text(x, y_top * 0.990, "BOS", color="#64B5F6",
                    fontsize=8, ha="center", va="top", fontweight="bold")


def _draw_levels(ax, df: pd.DataFrame, trade) -> None:
    price_min = df["low"].min()
    price_max = df["high"].max()
    price_range = price_max - price_min

    def _label_x(price):
        # Zet label aan de kant die het minst botst
        return 0.01 if price > (price_min + price_max) / 2 else 0.99

    for price, color, style, label in [
        (trade.entry_price, "#42A5F5", "--", f"Entry  {trade.entry_price:,.0f}"),
        (trade.sl_price,    "#EF5350", ":",  f"SL      {trade.sl_price:,.0f}"),
        (trade.tp_price,    "#66BB6A", ":",  f"TP      {trade.tp_price:,.0f}"),
    ]:
        ax.axhline(y=price, color=color, linestyle=style, linewidth=1.2,
                   alpha=0.85, zorder=4)
        ax.text(len(df) - 0.5, price + price_range * 0.004, label,
                color=color, fontsize=8, ha="right", va="bottom")


def _draw_entry_exit_markers(ax, df: pd.DataFrame, trade) -> None:
    entry_x = _ts_to_x(df, trade.entry_time)
    exit_x  = _ts_to_x(df, trade.exit_time)

    if entry_x is not None:
        marker = "^" if trade.direction == "long" else "v"
        ax.plot(entry_x, trade.entry_price, marker=marker, color="#42A5F5",
                markersize=11, zorder=6, markeredgecolor="white", markeredgewidth=0.5)

    if exit_x is not None:
        color = "#66BB6A" if trade.outcome == "win" else "#EF5350"
        ax.plot(exit_x, trade.exit_price, marker="o", color=color,
                markersize=11, zorder=6, markeredgecolor="white", markeredgewidth=0.5)


def _style_axes(ax, df: pd.DataFrame, trade) -> None:
    # X-as labels: maximaal 10 ticks
    n = max(1, len(df) // 10)
    ticks = list(range(0, len(df), n))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [df.index[i].strftime("%m-%d %H:%M") for i in ticks],
        rotation=30, ha="right", fontsize=8, color="#cccccc",
    )
    ax.tick_params(axis="y", colors="#cccccc", labelsize=8)
    ax.set_ylabel("Prijs (USDT)", color="#cccccc", fontsize=9)
    ax.grid(axis="y", alpha=0.15, color="#cccccc")
    for spine in ax.spines.values():
        spine.set_edgecolor("#333355")

    outcome_color = "#66BB6A" if trade.outcome == "win" else "#EF5350"
    pnl_str = f"{trade.pnl_capital:+.0f} USDT"
    ax.set_title(
        f"{trade.direction.upper()}  ·  "
        f"{trade.entry_time.strftime('%Y-%m-%d %H:%M')} UTC  ·  "
        f"{trade.outcome.upper()}  {pnl_str}",
        color=outcome_color, fontsize=11, fontweight="bold", pad=10,
    )

    # Legenda
    legend_items = [
        mpatches.Patch(color="#42A5F5", label="Entry"),
        mpatches.Patch(color="#EF5350", label="SL"),
        mpatches.Patch(color="#66BB6A", label="TP"),
        mpatches.Patch(color="#CE93D8", label="Sweep"),
        mpatches.Patch(color="#64B5F6", label="BOS"),
    ]
    ax.legend(handles=legend_items, loc="upper left", fontsize=8,
              facecolor="#2a2a4a", edgecolor="#555577", labelcolor="white")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_loc(index: pd.Index, ts) -> int:
    """Geef de integer-positie van ts in index, of de dichtstbijzijnde."""
    if ts is None:
        return 0
    try:
        loc = index.get_loc(ts)
        return int(loc) if isinstance(loc, (int, float)) else int(loc.start)
    except KeyError:
        pos = index.searchsorted(ts)
        return int(min(pos, len(index) - 1))


def _ts_to_x(df: pd.DataFrame, ts) -> int | None:
    """Zet een timestamp om naar een x-positie in df. None als buiten bereik."""
    if ts is None or ts not in df.index:
        return None
    return df.index.get_loc(ts)
