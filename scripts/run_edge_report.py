"""
scripts/run_edge_report.py — Edge-validatierapport genereren.

Voert walk-forward validatie uit per beschikbaar symbool en schrijft
docs/edge_report.md met:
  - Per-venster metrics (Sharpe, PF, DD, win rate, trades)
  - Inter-venster variantie (std, min, max)
  - Cross-coin signaalcorrelatie (overlappende open trades)

Gebruik:
    python scripts/run_edge_report.py
    python scripts/run_edge_report.py --train 12 --test 3
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.walk_forward import WalkForwardWindow, run_walk_forward, summarize
from src.config_loader import load_config
from src.signals.filters import SweepFilters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_data(cfg: dict, symbol: str) -> bool:
    processed = Path(cfg["data"]["paths"]["processed"])
    return (
        (processed / f"{symbol}_15m.parquet").exists() and
        (processed / f"{symbol}_4h.parquet").exists()
    )


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def _build_trade_mask(windows: list[WalkForwardWindow], freq: str = "15min") -> pd.Series:
    """
    Bouw een boolean Series: True op elke 15m-timestamp waarop een trade open was.
    Gebruikt entry_time → exit_time intervallen van alle trades in alle vensters.
    """
    all_trades = [t for w in windows for t in w.trades]
    if not all_trades:
        return pd.Series(dtype=bool)

    global_start = min(t.entry_time for t in all_trades)
    global_end   = max(t.exit_time  for t in all_trades)
    idx = pd.date_range(start=global_start, end=global_end, freq=freq, tz="UTC")
    mask = pd.Series(False, index=idx)

    for trade in all_trades:
        mask.loc[trade.entry_time:trade.exit_time] = True

    return mask


def _overlap_fraction(mask_a: pd.Series, mask_b: pd.Series) -> float:
    """
    Fractie van candles waarop zowel coin A als coin B een open trade had,
    t.o.v. het aantal candles waarop minstens één coin een open trade had.
    """
    common = mask_a.index.intersection(mask_b.index)
    if common.empty:
        return 0.0
    a = mask_a.reindex(common, fill_value=False)
    b = mask_b.reindex(common, fill_value=False)
    both = (a & b).sum()
    either = (a | b).sum()
    return float(both / either) if either > 0 else 0.0


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt(value: float, fmt: str = ".2f") -> str:
    return f"{value:{fmt}}"


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _render_window_table(windows: list[WalkForwardWindow]) -> str:
    lines = [
        "| Venster | Trades | Win% | Sharpe | MDD | PF |",
        "|---------|-------:|-----:|-------:|----:|----|",
    ]
    for w in windows:
        m = w.metrics
        label   = f"{w.test_start[:7]} → {w.test_end[:7]}"
        sharpe  = f"{m.sharpe_ratio:+.2f}"
        flag    = " ✓" if m.sharpe_ratio > 1.0 else (" ✗" if m.sharpe_ratio < 0 else "")
        pf      = f"{m.profit_factor:.2f}" if m.profit_factor < 99 else "∞"
        lines.append(
            f"| {label} | {m.trade_count} | {_pct(m.win_rate)} "
            f"| {sharpe}{flag} | {_pct(m.max_drawdown)} | {pf} |"
        )
    return "\n".join(lines)


def _render_variance_table(windows: list[WalkForwardWindow]) -> str:
    sharpes = [w.metrics.sharpe_ratio  for w in windows]
    pfs     = [w.metrics.profit_factor for w in windows if w.metrics.profit_factor < 99]
    dds     = [w.metrics.max_drawdown  for w in windows]
    wrs     = [w.metrics.win_rate      for w in windows]

    def row(name: str, values: list[float], pct: bool = False) -> str:
        fmt = _pct if pct else lambda v: f"{v:+.2f}"
        return (
            f"| {name} | {fmt(sum(values)/len(values))} "
            f"| {fmt(min(values))} | {fmt(max(values))} "
            f"| {_fmt(_std(values))} |"
        )

    lines = [
        "| Metriek | Gemiddeld | Min | Max | Std |",
        "|---------|----------:|----:|----:|----:|",
        row("Sharpe",         sharpes),
        row("Profit factor",  pfs if pfs else [0.0]),
        row("Max drawdown",   dds, pct=True),
        row("Win rate",       wrs, pct=True),
    ]
    return "\n".join(lines)


def _render_correlation(masks: dict[str, pd.Series]) -> str:
    symbols = list(masks.keys())
    if len(symbols) < 2:
        return "_Slechts één symbool beschikbaar — correlatieanalyse niet van toepassing._"

    lines = [
        "| Paar | Overlap (beide open) | Interpretatie |",
        "|------|---------------------:|---------------|",
    ]
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            frac = _overlap_fraction(masks[a], masks[b])
            if frac < 0.20:
                interp = "Laag — effectief onafhankelijk"
            elif frac < 0.40:
                interp = "Matig — gedeeltelijk gecorreleerd"
            else:
                interp = "**Hoog** — effectief risico verhoogd"
            lines.append(f"| {a} / {b} | {_pct(frac)} | {interp} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rapport schrijven
# ---------------------------------------------------------------------------

def _write_report(
    results: dict[str, list[WalkForwardWindow]],
    cfg: dict,
    train_months: int,
    test_months: int,
    out_path: Path,
    filters: "SweepFilters | None" = None,
) -> None:
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    is_s = cfg["split"]["in_sample_start"]
    is_e = cfg["split"]["in_sample_end"]
    filter_str = str(filters) if filters is not None else "baseline"

    sections = [
        f"# Edge-validatierapport\n",
        f"Gegenereerd: {now}  \n"
        f"Periode: {is_s} → {is_e}  \n"
        f"Vensters: train={train_months}m / test={test_months}m  \n"
        f"Filters: {filter_str}\n",
        "---\n",
    ]

    masks: dict[str, pd.Series] = {}

    for symbol, windows in results.items():
        summary = summarize(windows)
        n = summary["n_windows"]
        total = summary["total_trades"]

        verdict_map = {
            (True,  True):  "✅ Robuuste edge",
            (True,  False): "⚠️  Hoge gemiddelde Sharpe maar niet consistent",
            (False, True):  "⚠️  Consistent positief maar zwak signaal",
            (False, False): "❌ Geen robuuste edge",
        }
        high_avg     = summary["sharpe_mean"] > 1.0
        high_consist = summary["sharpe_positive_pct"] >= 0.70
        verdict = verdict_map[(high_avg, high_consist)]

        sections.append(f"## {symbol}\n")
        sections.append(
            f"**Vensters:** {n}  "
            f"**Totaal trades:** {total}  "
            f"**Gem. trades/venster:** {summary['avg_trades_per_wnd']:.1f}\n"
        )
        sections.append(f"**Verdict:** {verdict}\n")

        sections.append("\n### Per-venster resultaten\n")
        sections.append(_render_window_table(windows))
        sections.append("\n")

        sections.append("\n### Inter-venster variantie\n")
        sections.append(_render_variance_table(windows))
        sections.append("\n")

        sections.append(
            "\n> **Lees dit zo:** Een hoge Std op Sharpe betekent dat de edge "
            "niet stabiel is over tijd. Streef naar Std < 0.5 bij Sharpe > 1.0.\n"
        )

        masks[symbol] = _build_trade_mask(windows)
        sections.append("\n---\n")

    sections.append("## Cross-coin signaalcorrelatie\n")
    sections.append(
        "Fractie van candles waarop twee coins *tegelijk* een open trade hadden "
        "t.o.v. alle candles waarop minstens één coin open was. "
        "Bij hoge overlap is het effectieve risico hoger dan de per-coin limieten suggereren.\n"
    )
    sections.append("\n" + _render_correlation(masks) + "\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(sections), encoding="utf-8")
    logger.info("Rapport geschreven naar %s", out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Edge-validatierapport genereren.")
    parser.add_argument("--train",  type=int, default=None)
    parser.add_argument("--test",   type=int, default=None)
    parser.add_argument("--start",  default=None)
    parser.add_argument("--end",    default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--out",    default="docs/edge_report.md")
    args = parser.parse_args()

    cfg = load_config(args.config)

    wf_cfg       = cfg.get("backtest", {}).get("walk_forward", {})
    train_months = args.train or wf_cfg.get("train_months", 12)
    test_months  = args.test  or wf_cfg.get("test_months",  3)

    filters = SweepFilters.from_config(cfg)
    logger.info("Sweep-filters: %s", filters)

    coins = [c["symbol"] for c in cfg.get("coins", [{"symbol": cfg["data"]["symbol"]}])]
    available = [s for s in coins if _has_data(cfg, s)]
    missing   = [s for s in coins if s not in available]

    if missing:
        logger.warning("Geen data voor: %s — overgeslagen.", ", ".join(missing))
    if not available:
        logger.error("Geen enkel symbool heeft verwerkte data. Run eerst scripts/build_cache.py.")
        sys.exit(1)

    results: dict[str, list[WalkForwardWindow]] = {}
    for symbol in available:
        logger.info("Walk-forward voor %s ...", symbol)
        try:
            windows = run_walk_forward(
                cfg          = cfg,
                train_months = train_months,
                test_months  = test_months,
                start        = args.start,
                end          = args.end,
                symbol       = symbol,
                filters      = filters,
            )
            results[symbol] = windows
        except Exception as exc:
            logger.error("%s mislukt: %s", symbol, exc)

    if not results:
        logger.error("Geen resultaten — rapport niet gegenereerd.")
        sys.exit(1)

    out_path = Path(args.out)
    _write_report(results, cfg, train_months, test_months, out_path, filters=filters)
    print(f"\nRapport: {out_path.resolve()}\n")


if __name__ == "__main__":
    main()
