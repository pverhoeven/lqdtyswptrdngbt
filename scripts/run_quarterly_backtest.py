"""
Sweep strategie backtest per kwartaal (2023–2024).
Gebruik:
    python scripts/run_quarterly_backtest.py --filter regime_long --year 2023
    python scripts/run_quarterly_backtest.py --filter regime_long --year 2024
    python scripts/run_quarterly_backtest.py --filter regime_long --year all
"""
import argparse
import logging
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.sweep_engine import SweepFilters, run_sweep_backtest
from src.backtest.metrics import compute_metrics, BacktestMetrics
from src.config_loader import load_config

# Beschikbare filters
_FILTER_PRESETS = {
    "baseline":      SweepFilters(direction="both"),
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
    "long_atr14":   SweepFilters(direction="long", atr_filter=True),
}

# Kwartalen definieren
QUARTERLY_PERIODS = {
    "2023-Q1": {"start": "2023-01-01", "end": "2023-03-31"},
    "2023-Q2": {"start": "2023-04-01", "end": "2023-06-30"},
    "2023-Q3": {"start": "2023-07-01", "end": "2023-09-30"},
    "2023-Q4": {"start": "2023-10-01", "end": "2023-12-31"},
    "2024-Q1": {"start": "2024-01-01", "end": "2024-03-31"},
    "2024-Q2": {"start": "2024-04-01", "end": "2024-06-30"},
    "2024-Q3": {"start": "2024-07-01", "end": "2024-09-30"},
    "2024-Q4": {"start": "2024-10-01", "end": "2024-12-31"}
}

# Configureer logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

def filter_trades_by_period(trades, start_date: str, end_date: str):
    """Filter trades op basis van entry_time (timezone-aware)."""
    start = pd.Timestamp(start_date, tz='UTC')  # ← FIX: Voeg tz='UTC' toe
    end = pd.Timestamp(end_date, tz='UTC')      # ← FIX: Voeg tz='UTC' toe
    return [t for t in trades if start <= t.entry_time <= end]

def print_period_results(period: str, trades: list, capital_initial: float):
    """Print resultaten voor een specifieke periode."""
    if not trades:
        print(f"{period}: Geen trades")
        return

    metrics = compute_metrics(trades, capital_initial)
    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]
    total_fees = sum(t.fee_cost for t in trades)
    avg_fee = total_fees / len(trades) if trades else 0

    print(f"\n{period}:")
    print(f"  Trades:         {len(trades)}")
    print(f"  Win rate:       {metrics.win_rate:.1%}")
    print(f"  Sharpe ratio:   {metrics.sharpe_ratio:.2f}")
    print(f"  Max drawdown:   {metrics.max_drawdown:.1%}")
    print(f"  Profit factor:  {metrics.profit_factor:.2f}")
    print(f"  Total return:   {metrics.total_return:.1%}")
    print(f"  Avg trade P&L: {metrics.avg_trade_pnl:.2f} USDT")
    print(f"  Wins:           {len(wins)}  |  Losses: {len(losses)}")
    print(f"  Totale kosten:  {total_fees:.2f} USDT")
    print(f"  Gem. kosten/trade: {avg_fee:.2f} USDT")

def main():
    parser = argparse.ArgumentParser(
        description="Sweep backtest per kwartaal (2023–2024-2025)."
    )
    parser.add_argument(
        "--filter",
        default="regime_long",
        choices=list(_FILTER_PRESETS.keys()),
        help="Filter om te gebruiken (default: regime_long)."
    )
    parser.add_argument(
        "--year",
        choices=["2023", "2024", "2025", "all"],
        default="all",
        help="Jaar om te backtesten (default: all)."
    )
    args = parser.parse_args()

    # Laad config
    cfg = load_config()
    capital_initial = cfg["risk"]["capital_initial"]
    filters = _FILTER_PRESETS[args.filter]

    # Voer backtest uit voor het hele OOS bereik
    logger.info(f"Backtesten voor {args.filter} over 2023–2024-2025...")
    metrics, all_trades = run_sweep_backtest(
        cfg, dataset="oos", filters=filters, allow_oos=True
    )

    # Bepaal welke periodes
    if args.year == "2023":
        periods = [p for p in QUARTERLY_PERIODS if p.startswith("2023")]
    elif args.year == "2024":
        periods = [p for p in QUARTERLY_PERIODS if p.startswith("2024")]
    elif args.year == "2025":
        periods = [p for p in QUARTERLY_PERIODS if p.startswith("2025")]
    else:  # all
        periods = list(QUARTERLY_PERIODS.keys())

    # Print header
    print(f"\n{'='*70}")
    print(f"  SWEEP BACKTEST — {args.filter.upper()} (PER KWARTAAL)")
    print(f"{'='*70}")

    # Toon resultaten per kwartaal
    for period in periods:
        period_data = QUARTERLY_PERIODS[period]
        period_trades = filter_trades_by_period(
            all_trades,
            period_data["start"],
            period_data["end"]
        )
        print_period_results(period, period_trades, capital_initial)

    # Toon samenvatting
    print(f"\n{'='*70}")
    print(f"  SAMENVATTING ({args.filter.upper()})")
    print(f"{'='*70}")
    print_period_results("2023–2024 (Totaal)", all_trades, capital_initial)

if __name__ == "__main__":
    main()