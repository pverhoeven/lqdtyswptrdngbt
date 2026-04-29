"""
DEPRECATED: scripts/run_backtest.py — Gebruik run_sweep_backtest.py.

    python scripts/run_sweep_backtest.py --set in_sample --filter baseline
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.engine import run_backtest
from src.backtest.metrics import equity_curve
from src.config_loader import load_config

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC backtest uitvoeren.")
    parser.add_argument(
        "--set",
        choices=["in_sample", "oos"],
        required=True,
        help="Dataset: 'in_sample' (2019–2022) of 'oos' (2023–2024)",
    )
    parser.add_argument("--symbol", default=None, help="Symbool override (bijv. ETHUSDT)")
    parser.add_argument("--config", default=None, help="Pad naar config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    is_oos = args.set == "oos"

    if is_oos:
        logger.warning("=" * 60)
        logger.warning("OOS EVALUATIE — GEBRUIK ALLEEN NA BEVROREN PARAMETERS")
        logger.warning("=" * 60)

    try:
        metrics, trades = run_backtest(
            cfg       = cfg,
            dataset   = args.set,
            allow_oos = is_oos,
            symbol    = args.symbol,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    # --- Output ---
    symbol_label = args.symbol or cfg["data"]["symbol"]
    print(f"\n{'='*50}")
    print(f"  RESULTATEN — {symbol_label}  {args.set.upper().replace('_', '-')}")
    print(f"{'='*50}")
    print(metrics)
    print(f"{'='*50}")
    print(metrics.interpret())
    print()

    # Equity curve samenvatting
    if trades:
        curve = equity_curve(trades, cfg["risk"]["capital_initial"])
        print(f"Equity: {curve.iloc[0]:.0f} → {curve.iloc[-1]:.0f} USDT")
        print(f"Periode: {trades[0].entry_time.date()} → {trades[-1].exit_time.date()}")

        wins   = [t for t in trades if t.outcome == "win"]
        losses = [t for t in trades if t.outcome == "loss"]
        print(f"Wins: {len(wins)}  |  Losses: {len(losses)}")


if __name__ == "__main__":
    main()
