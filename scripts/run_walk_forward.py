"""
scripts/run_walk_forward.py — Walk-forward validatie uitvoeren.

Rolt een train/test venster over de in-sample data (OOS wordt niet aangeraakt).
Geeft per venster de backtest-metrics en een geaggregeerde samenvatting.

Gebruik:
    python scripts/run_walk_forward.py
    python scripts/run_walk_forward.py --train 12 --test 3
    python scripts/run_walk_forward.py --filter bos10
    python scripts/run_walk_forward.py --filter micro_bos_3m
    python scripts/run_walk_forward.py --compare
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

_FILTER_PRESETS: dict[str, SweepFilters] = {
    "baseline":           SweepFilters(),
    "regime":             SweepFilters(regime=True),
    "long_only":          SweepFilters(direction="long"),
    "short_only":         SweepFilters(direction="short"),
    "bos10":              SweepFilters(bos_confirm=True, bos_window=10),
    "bos20":              SweepFilters(bos_confirm=True, bos_window=20),
    "regime_long":        SweepFilters(regime=True, direction="long"),
    "regime_short":       SweepFilters(regime=True, direction="short"),
    "regime_bos10":       SweepFilters(regime=True, bos_confirm=True, bos_window=10),
    "regime_bos20":       SweepFilters(regime=True, bos_confirm=True, bos_window=20),
    "long_bos10":         SweepFilters(direction="long",  bos_confirm=True, bos_window=10),
    "short_bos10":        SweepFilters(direction="short", bos_confirm=True, bos_window=10),
    "long_atr14":         SweepFilters(direction="long", atr_filter=True),
    "dynamic_200ma":      SweepFilters(direction="dynamic"),
    # Micro-BoS op lagere timeframe
    "micro_bos_3m":       SweepFilters(micro_bos_tf="3min", micro_bos_window=20),
    "micro_bos_5m":       SweepFilters(micro_bos_tf="5min", micro_bos_window=20),
    "long_micro_bos_3m":  SweepFilters(direction="long",  micro_bos_tf="3min", micro_bos_window=20),
    "short_micro_bos_3m": SweepFilters(direction="short", micro_bos_tf="3min", micro_bos_window=20),
    "long_micro_bos_5m":  SweepFilters(direction="long",  micro_bos_tf="5min", micro_bos_window=20),
    "short_micro_bos_5m": SweepFilters(direction="short", micro_bos_tf="5min", micro_bos_window=20),
}

# Filters die naast elkaar worden gezet bij --compare
_COMPARE_SET = [
    "baseline",
    "bos10",
    "bos20",
    "regime_bos10",
    "regime_bos20",
    "short_bos10",
    "micro_bos_3m",
    "micro_bos_5m",
    "short_micro_bos_3m",
    "short_micro_bos_5m",
]


def main() -> None:
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
    parser.add_argument(
        "--compare", action="store_true",
        help="Vergelijk een set filters naast elkaar (negeert --filter)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.symbol:
        cfg["data"]["symbol"] = args.symbol

    if args.compare:
        _run_compare(cfg, args)
    else:
        _run_single(cfg, args)


def _run_single(cfg: dict, args) -> None:
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
            filters      = filters,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if not windows:
        print("Geen vensters gevonden. Controleer de datum-range en venster-grootte.")
        sys.exit(1)

    _print_windows(windows)
    _print_summary(windows)


def _run_compare(cfg: dict, args) -> None:
    """Voer walk-forward uit voor meerdere filters en toon een vergelijkingstabel."""
    print(f"\n{'='*72}")
    print(f"  WALK-FORWARD VERGELIJKING — {len(_COMPARE_SET)} filters")
    print(f"{'='*72}")

    rows = []
    for naam in _COMPARE_SET:
        filters = _FILTER_PRESETS[naam]
        try:
            windows = run_walk_forward(
                cfg          = cfg,
                train_months = args.train,
                test_months  = args.test,
                start        = args.start,
                end          = args.end,
                filters      = filters,
            )
            s = summarize(windows)
            rows.append({
                "filter":          naam,
                "vensters":        s["n_windows"],
                "trades":          s["total_trades"],
                "sharpe_gem":      f"{s['sharpe_mean']:+.2f}",
                "sharpe_pct_pos":  f"{s['sharpe_positive_pct']:.0%}",
                "win_rate_gem":    f"{s['win_rate_mean']:.1%}",
                "mdd_gem":         f"{s['max_drawdown_mean']:.1%}",
                "pf_gem":          f"{s['profit_factor_mean']:.2f}",
            })
        except FileNotFoundError as exc:
            logger.warning("Overgeslagen — %s: %s", naam, exc)
            rows.append({"filter": naam, "vensters": 0, "trades": 0,
                         "sharpe_gem": "–", "sharpe_pct_pos": "–",
                         "win_rate_gem": "–", "mdd_gem": "–", "pf_gem": "–"})
        except Exception as exc:
            logger.warning("Fout bij '%s': %s", naam, exc)
            rows.append({"filter": naam, "vensters": 0, "trades": 0,
                         "sharpe_gem": "–", "sharpe_pct_pos": "–",
                         "win_rate_gem": "–", "mdd_gem": "–", "pf_gem": "–"})

    # Sorteer op Sharpe gem. (hoog → laag)
    def _sort_key(r):
        try:
            return float(r["sharpe_gem"])
        except (ValueError, TypeError):
            return -999.0

    rows.sort(key=_sort_key, reverse=True)

    # Tabel afdrukken
    hdr = f"  {'filter':<22} {'vnst':>4} {'trades':>6} {'sharpe':>7} {'>0%':>5} {'winrate':>8} {'mdd':>6} {'pf':>5}"
    print(f"\n{'─'*72}")
    print(hdr)
    print(f"{'─'*72}")
    for r in rows:
        print(
            f"  {r['filter']:<22} {r['vensters']:>4} {r['trades']:>6} "
            f"{r['sharpe_gem']:>7} {r['sharpe_pct_pos']:>5} "
            f"{r['win_rate_gem']:>8} {r['mdd_gem']:>6} {r['pf_gem']:>5}"
        )
    print(f"{'─'*72}\n")


def _print_windows(windows) -> None:
    print(f"\n{'─'*60}")
    print(f"  {'VENSTER':<20} {'TRADES':>7} {'WIN%':>6} {'SHARPE':>7} {'MDD':>6} {'PF':>5}")
    print(f"{'─'*60}")

    for w in windows:
        m            = w.metrics
        window_label = f"{w.test_start[:7]} → {w.test_end[:7]}"
        sharpe_str   = f"{m.sharpe_ratio:>+.2f}"
        mdd_str      = f"{m.max_drawdown:.1%}"
        pf_str       = f"{m.profit_factor:.2f}" if m.profit_factor < 99 else "∞"
        flag = " ✓" if m.sharpe_ratio > 1.0 else (" ✗" if m.sharpe_ratio < 0 else "")
        print(
            f"  {window_label:<20} {m.trade_count:>7} "
            f"{m.win_rate:>5.1%} {sharpe_str:>7} {mdd_str:>6} {pf_str:>5}{flag}"
        )


def _print_summary(windows) -> None:
    summary    = summarize(windows)
    sharpe_pct = summary["sharpe_positive_pct"]
    sharpe_avg = summary["sharpe_mean"]

    print(f"\n{'─'*60}")
    print(f"  SAMENVATTING  ({summary['n_windows']} vensters, {summary['total_trades']} trades)")
    print(f"{'─'*60}")
    print(f"  Gem. trades/venster:  {summary['avg_trades_per_wnd']:.1f}")
    print(f"  Sharpe gem.:          {sharpe_avg:+.2f}")
    print(f"  Sharpe min/max:       {summary['sharpe_min']:+.2f} / {summary['sharpe_max']:+.2f}")
    print(f"  Sharpe > 0:           {sharpe_pct:.0%} van vensters")
    print(f"  Win rate gem.:        {summary['win_rate_mean']:.1%}")
    print(f"  Max drawdown gem.:    {summary['max_drawdown_mean']:.1%}")
    print(f"  Profit factor gem.:   {summary['profit_factor_mean']:.2f}")

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
