"""
scripts/export_trade_charts.py — Exporteer alle trades als PNG charts.

Gebruik:
    python scripts/export_trade_charts.py
    python scripts/export_trade_charts.py --set oos --filter bos20 --ttl 5
    python scripts/export_trade_charts.py --set in_sample --filter bos20
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.plot_trade import plot_trade
from src.backtest.sweep_engine import SweepFilters, run_sweep_backtest
from src.config_loader import load_config
from src.data.cache import load_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_FILTER_PRESETS: dict[str, SweepFilters] = {
    "baseline":   SweepFilters(),
    "bos20":      SweepFilters(bos_confirm=True, bos_window=20),
    "bos10":      SweepFilters(bos_confirm=True, bos_window=10),
    "long_only":  SweepFilters(direction="long"),
    "short_only": SweepFilters(direction="short"),
    "rejection":  SweepFilters(bos_confirm=True, bos_window=20, sweep_rejection=True),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Exporteer trade charts als PNG.")
    parser.add_argument("--filter", default="bos20", choices=list(_FILTER_PRESETS),
                        help="Filter preset (standaard: bos20)")
    parser.add_argument("--set", default="oos", choices=["in_sample", "oos"],
                        help="Dataset (standaard: oos)")
    parser.add_argument("--ttl", type=int, default=5,
                        help="Limit order TTL in candles (standaard: 5)")
    parser.add_argument("--out", default="docs/charts",
                        help="Output directory (standaard: docs/charts)")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    f   = _FILTER_PRESETS[args.filter]

    # ── Backtest draaien ─────────────────────────────────────────────
    logger.info("Backtest: %s  filter=%s  ttl=%d", args.set.upper(), args.filter, args.ttl)
    metrics, trades = run_sweep_backtest(
        cfg,
        dataset    = args.set,
        filters    = f,
        allow_oos  = (args.set == "oos"),
        pending_ttl = args.ttl,
    )
    logger.info(
        "%d trades geladen  |  fill rate: %s",
        metrics.trade_count,
        f"{metrics.fill_rate:.1%}" if metrics.fill_rate is not None else "n/a",
    )

    if not trades:
        logger.warning("Geen trades — niets te exporteren.")
        sys.exit(0)

    # ── Data laden ───────────────────────────────────────────────────
    processed_dir = Path(cfg["data"]["paths"]["processed"])
    symbol = cfg["data"]["symbol"]
    tf     = cfg["data"]["timeframes"]["signal"].replace("min", "m")
    df_15m = pd.read_parquet(processed_dir / f"{symbol}_{tf}.parquet")

    split = cfg["split"]
    start = split["in_sample_start"] if args.set == "in_sample" else split["oos_start"]
    end   = split["in_sample_end"]   if args.set == "in_sample" else split["oos_end"]
    cache = load_cache(cfg, start=start, end=end)

    # ── Output directory ─────────────────────────────────────────────
    out_dir = Path(args.out) / args.filter / args.set
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Charts opslaan in %s …", out_dir)

    # ── Exporteren ───────────────────────────────────────────────────
    for i, trade in enumerate(trades, 1):
        fname = (
            f"{trade.entry_time.strftime('%Y%m%d_%H%M')}"
            f"_{trade.direction}"
            f"_{trade.outcome}.png"
        )
        plot_trade(trade, df_15m, cache=cache, out_path=out_dir / fname)

        if i % 25 == 0 or i == len(trades):
            logger.info("  %d / %d  (%.0f%%)", i, len(trades), 100 * i / len(trades))

    logger.info("Klaar — %d charts in %s", len(trades), out_dir)


if __name__ == "__main__":
    main()
