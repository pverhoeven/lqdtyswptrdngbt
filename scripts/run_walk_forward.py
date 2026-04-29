"""
scripts/run_walk_forward.py — Walk-forward validatie uitvoeren.

Rolt een train/test venster over de in-sample data (OOS wordt niet aangeraakt).
Geeft per venster de backtest-metrics en een geaggregeerde samenvatting.

Gebruik:
    python scripts/run_walk_forward.py
    python scripts/run_walk_forward.py --train 12 --test 3
    python scripts/run_walk_forward.py --symbol ETHUSDT --train 6 --test 2
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.walk_forward import run_walk_forward, summarize
from src.config_loader import load_config
from src.signals.filters import SweepFilters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    _FILTER_PRESETS: dict[str, SweepFilters] = {
        "baseline":      SweepFilters(),
        "regime":        SweepFilters(regime=True),
        "long_only":     SweepFilters(direction="long"),
        "short_only":    SweepFilters(direction="short"),
        "bos10":         SweepFilters(bos_confirm=True, bos_window=10),
        "bos20":         SweepFilters(bos_confirm=True, bos_window=20),
        "regime_long":   SweepFilters(regime=True, direction="long"),
        "regime_short":  SweepFilters(regime=True, direction="short"),
        "regime_bos10":  SweepFilters(regime=True, bos_confirm=True, bos_window=10),
        "long_bos10":    SweepFilters(direction="long",  bos_confirm=True, bos_window=10),
        "short_bos10":   SweepFilters(direction="short", bos_confirm=True, bos_window=10),
        "long_atr14":    SweepFilters(direction="long", atr_filter=True),
        "dynamic_200ma": SweepFilters(direction="dynamic"),
    }

    parser = argparse.ArgumentParser(description="Walk-forward validatie.")
    parser.add_argument("--train",  type=int,  default=None, help="Trainingsvenster in maanden")
    parser.add_argument("--test",   type=int,  default=None, help="Testvenster in maanden")
    parser.add_argument("--start",  default=None, help="Startdatum (YYYY-MM-DD)")
    parser.add_argument("--end",    default=None, help="Einddatum  (YYYY-MM-DD)")
    parser.add_argument("--symbol", default=None, help="Symbool override (bijv. ETHUSDT)")
    parser.add_argument("--config", default=None, help="Pad naar config.yaml")
    parser.add_argument(
        "--filter", default="baseline",
        choices=list(_FILTER_PRESETS.keys()),
        help="Sweep-filter preset (standaard: baseline)",
    )
    args = parser.parse_args()

    cfg     = load_config(args.config)
    filters = _FILTER_PRESETS[args.filter]

    print(f"\n{'='*60}")
    print(f"  WALK-FORWARD VALIDATIE  [{args.filter.upper()}]")
    print(f"{'='*60}")

    try:
        windows = run_walk_forward(
            cfg          = cfg,
            train_months = args.train,
            test_months  = args.test,
            start        = args.start,
            end          = args.end,
            symbol       = args.symbol,
            filters      = filters,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if not windows:
        print("Geen vensters gevonden. Controleer de datum-range en venster-grootte.")
        sys.exit(1)

    # --- Per venster ---
    print(f"\n{'─'*60}")
    print(f"  {'VENSTER':<20} {'TRADES':>7} {'WIN%':>6} {'SHARPE':>7} {'MDD':>6} {'PF':>5}")
    print(f"{'─'*60}")

    for w in windows:
        m = w.metrics
        window_label = f"{w.test_start[:7]} → {w.test_end[:7]}"
        sharpe_str   = f"{m.sharpe_ratio:>+.2f}"
        mdd_str      = f"{m.max_drawdown:.1%}"
        pf_str       = f"{m.profit_factor:.2f}" if m.profit_factor < 99 else "∞"

        flag = ""
        if m.sharpe_ratio > 1.0:
            flag = " ✓"
        elif m.sharpe_ratio < 0:
            flag = " ✗"

        print(
            f"  {window_label:<20} {m.trade_count:>7} "
            f"{m.win_rate:>5.1%} {sharpe_str:>7} {mdd_str:>6} {pf_str:>5}{flag}"
        )

    # --- Geaggregeerd ---
    summary = summarize(windows)
    print(f"\n{'─'*60}")
    print(f"  SAMENVATTING  ({summary['n_windows']} vensters, {summary['total_trades']} trades)")
    print(f"{'─'*60}")
    print(f"  Gem. trades/venster:  {summary['avg_trades_per_wnd']:.1f}")
    print(f"  Sharpe gem.:          {summary['sharpe_mean']:+.2f}")
    print(f"  Sharpe min/max:       {summary['sharpe_min']:+.2f} / {summary['sharpe_max']:+.2f}")
    print(f"  Sharpe > 0:           {summary['sharpe_positive_pct']:.0%} van vensters")
    print(f"  Win rate gem.:        {summary['win_rate_mean']:.1%}")
    print(f"  Max drawdown gem.:    {summary['max_drawdown_mean']:.1%}")
    print(f"  Profit factor gem.:   {summary['profit_factor_mean']:.2f}")

    # Interpretatie
    sharpe_pct = summary["sharpe_positive_pct"]
    sharpe_avg = summary["sharpe_mean"]
    print(f"\n{'─'*60}")
    if sharpe_avg > 1.0 and sharpe_pct >= 0.7:
        verdict = "✅  Robuuste edge: hoge Sharpe in meerderheid van vensters."
    elif sharpe_avg > 0.5 and sharpe_pct >= 0.6:
        verdict = "⚠️   Zwak signaal: verdere optimalisatie aanbevolen."
    elif sharpe_pct >= 0.5:
        verdict = "⚠️   Inconsistent: edge aanwezig maar niet stabiel over tijd."
    else:
        verdict = "❌  Geen robuuste edge: meerderheid vensters negatief Sharpe."
    print(f"  {verdict}")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
