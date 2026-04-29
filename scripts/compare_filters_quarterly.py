"""
Vergelijk alle filters per kwartaal en per jaar (2023–2024) in een tabel en grafieken.
Inclusief: max drawdown, avg win/loss, long/short verdeling, en grafische weergave.
Gebruik:
    python scripts/compare_filters_quarterly.py --min-trades 50
"""
import argparse
import logging
import sys
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.sweep_engine import SweepFilters, run_sweep_backtest
from src.backtest.metrics import compute_metrics
from src.config_loader import load_config

# Configureer logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Beschikbare filters
_FILTER_PRESETS: Dict[str, SweepFilters] = {
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
    "long_atr":      SweepFilters(direction="long",  atr_filter=True),
    "short_atr":     SweepFilters(direction="short", atr_filter=True),
    "atr":           SweepFilters(direction="both",  atr_filter=True),
    "dynamic_200ma": SweepFilters(direction="dynamic"),
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
    "2024-Q4": {"start": "2024-10-01", "end": "2024-12-31"},
    "2025-Q1": {"start": "2025-01-01", "end": "2025-03-31"},
    "2025-Q2": {"start": "2025-04-01", "end": "2025-06-30"},
    "2025-Q3": {"start": "2025-07-01", "end": "2025-09-30"},
    "2025-Q4": {"start": "2025-10-01", "end": "2025-12-31"},
}

def filter_trades_by_period(trades: List, start_date: str, end_date: str) -> List:
    """Filter trades op basis van entry_time (timezone-aware)."""
    start = pd.Timestamp(start_date, tz='UTC')
    end = pd.Timestamp(end_date, tz='UTC')
    return [t for t in trades if start <= t.entry_time <= end]

def compute_period_metrics(trades: List, capital_initial: float) -> Dict:
    """Bereken alle metrics voor een periode."""
    if not trades:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "total_return": 0.0,
            "avg_pnl": 0.0,
            "total_fees": 0.0,
            "avg_fee": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "win_loss_ratio": 0.0,
            "long_trades": 0,
            "short_trades": 0,
        }

    metrics = compute_metrics(trades, capital_initial)
    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]
    long_trades = [t for t in trades if t.direction == "long"]
    short_trades = [t for t in trades if t.direction == "short"]
    total_fees = sum(t.fee_cost for t in trades)
    avg_fee = total_fees / len(trades) if trades else 0.0

    avg_win = sum(t.pnl_capital for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_capital for t in losses) / len(losses) if losses else 0.0
    win_loss_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else float("inf")

    return {
        "trades": len(trades),
        "win_rate": metrics.win_rate,
        "sharpe_ratio": metrics.sharpe_ratio,
        "max_drawdown": metrics.max_drawdown,
        "profit_factor": metrics.profit_factor,
        "total_return": metrics.total_return,
        "avg_pnl": metrics.avg_trade_pnl,
        "total_fees": total_fees,
        "avg_fee": avg_fee,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
    }

def run_quarterly_comparison(cfg: Dict, filters: Dict[str, SweepFilters], min_trades: int = 50) -> pd.DataFrame:
    capital_initial = cfg["risk"]["capital_initial"]
    results = []

    for filter_name, filter_obj in filters.items():
        logger.info(f"Backtesten voor filter: {filter_name}...")
        try:
            _, all_trades = run_sweep_backtest(
                cfg, dataset="oos", filters=filter_obj, allow_oos=True
            )
        except Exception as e:
            logger.error(f"Fout bij backtest voor {filter_name}: {e}")
            continue

        for period, period_data in QUARTERLY_PERIODS.items():
            period_trades = filter_trades_by_period(
                all_trades, period_data["start"], period_data["end"]
            )
            period_metrics = compute_period_metrics(period_trades, capital_initial)
            results.append({
                "Filter": filter_name,
                "Periode": period,
                **period_metrics,
            })

    return pd.DataFrame(results)

def group_by_year(df: pd.DataFrame) -> pd.DataFrame:
    """Groepeer resultaten per jaar."""
    df_yearly = df.copy()
    df_yearly["Year"] = df_yearly["Periode"].str[:4]  # Extract jaar uit "2023-Q1"
    yearly_results = []

    for filter_name in df_yearly["Filter"].unique():
        filter_df = df_yearly[df_yearly["Filter"] == filter_name]
        for year in filter_df["Year"].unique():
            year_df = filter_df[filter_df["Year"] == year]
            if len(year_df) == 0:
                continue

            aggregated = {
                "Filter": filter_name,
                "Year": year,
                "trades": int(year_df["trades"].sum()),
                "win_rate": year_df["win_rate"].mean(),
                "sharpe_ratio": year_df["sharpe_ratio"].mean(),
                "max_drawdown": year_df["max_drawdown"].max(),
                "profit_factor": year_df["profit_factor"].mean(),
                "total_return": year_df["total_return"].sum(),
                "avg_win": year_df["avg_win"].mean(),
                "avg_loss": year_df["avg_loss"].mean(),
                "win_loss_ratio": year_df["win_loss_ratio"].mean(),
                "long_trades": int(year_df["long_trades"].sum()),
                "short_trades": int(year_df["short_trades"].sum()),
                "total_fees": year_df["total_fees"].sum(),
                "avg_fee": year_df["avg_fee"].mean(),
            }
            yearly_results.append(aggregated)

    return pd.DataFrame(yearly_results)

def plot_results(df: pd.DataFrame, min_trades: int = 50):
    """Maak grafieken van de resultaten."""
    df_filtered = df[df["trades"] >= min_trades]

    if df_filtered.empty:
        logger.warning(f"Geen data om te plotten (geen periodes met ≥{min_trades} trades).")
        return

    fig, axes = plt.subplots(3, 2, figsize=(18, 18))
    fig.suptitle("Filter Vergelijking per Kwartaal (2023–2024)", fontsize=16, y=1.02)

    # 1. Win Rate
    ax1 = axes[0, 0]
    for filter_name in df_filtered["Filter"].unique():
        filter_df = df_filtered[df_filtered["Filter"] == filter_name]
        ax1.plot(filter_df["Periode"], filter_df["win_rate"], marker='o', label=filter_name)
    ax1.set_title("Win Rate per Kwartaal")
    ax1.set_ylabel("Win Rate")
    ax1.set_xticklabels(ax1.get_xticklabels(), rotation=45)
    ax1.legend()
    ax1.grid(True)

    # 2. Sharpe Ratio
    ax2 = axes[0, 1]
    for filter_name in df_filtered["Filter"].unique():
        filter_df = df_filtered[df_filtered["Filter"] == filter_name]
        ax2.plot(filter_df["Periode"], filter_df["sharpe_ratio"], marker='o', label=filter_name)
    ax2.set_title("Sharpe Ratio per Kwartaal")
    ax2.set_ylabel("Sharpe Ratio")
    ax2.set_xticklabels(ax2.get_xticklabels(), rotation=45)
    ax2.legend()
    ax2.grid(True)

    # 3. Max Drawdown
    ax3 = axes[1, 0]
    for filter_name in df_filtered["Filter"].unique():
        filter_df = df_filtered[df_filtered["Filter"] == filter_name]
        ax3.plot(filter_df["Periode"], filter_df["max_drawdown"] * 100, marker='o', label=filter_name)
    ax3.set_title("Max Drawdown per Kwartaal (%)")
    ax3.set_ylabel("Max Drawdown (%)")
    ax3.set_xticklabels(ax3.get_xticklabels(), rotation=45)
    ax3.legend()
    ax3.grid(True)

    # 4. Profit Factor
    ax4 = axes[1, 1]
    for filter_name in df_filtered["Filter"].unique():
        filter_df = df_filtered[df_filtered["Filter"] == filter_name]
        ax4.plot(filter_df["Periode"], filter_df["profit_factor"], marker='o', label=filter_name)
    ax4.set_title("Profit Factor per Kwartaal")
    ax4.set_ylabel("Profit Factor")
    ax4.set_xticklabels(ax4.get_xticklabels(), rotation=45)
    ax4.legend()
    ax4.grid(True)

    # 5. Avg Win vs Avg Loss
    ax5 = axes[2, 0]
    for filter_name in df_filtered["Filter"].unique():
        filter_df = df_filtered[df_filtered["Filter"] == filter_name]
        ax5.bar(
            [p + f"_{filter_name}" for p in filter_df["Periode"]],
            filter_df["avg_win"],
            label=f"{filter_name} Win"
        )
        ax5.bar(
            [p + f"_{filter_name}" for p in filter_df["Periode"]],
            filter_df["avg_loss"],
            label=f"{filter_name} Loss",
            alpha=0.7
        )
    ax5.set_title("Gemiddelde Win vs Loss per Kwartaal")
    ax5.set_ylabel("P&L (USDT)")
    ax5.set_xticklabels(filter_df["Periode"].unique(), rotation=45)
    ax5.legend()
    ax5.grid(True)

    # 6. Long/Short Verdeling
    ax6 = axes[2, 1]
    for filter_name in df_filtered["Filter"].unique():
        filter_df = df_filtered[df_filtered["Filter"] == filter_name]
        ax6.plot(
            filter_df["Periode"],
            filter_df["long_trades"],
            marker='o',
            label=f"{filter_name} Long"
        )
        ax6.plot(
            filter_df["Periode"],
            filter_df["short_trades"],
            marker='o',
            label=f"{filter_name} Short",
            linestyle='--'
        )
    ax6.set_title("Long/Short Verdeling per Kwartaal")
    ax6.set_ylabel("Aantal Trades")
    ax6.set_xticklabels(ax6.get_xticklabels(), rotation=45)
    ax6.legend()
    ax6.grid(True)

    plt.tight_layout()
    plt.savefig("filter_comparison_quarterly.png", dpi=300, bbox_inches='tight')
    plt.close()

def main():
    parser = argparse.ArgumentParser(
        description="Vergelijk filters per kwartaal en jaar (inclusief grafieken)."
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=50,
        help="Minimaal aantal trades om een periode mee te nemen (default: 50)."
    )
    args = parser.parse_args()

    cfg = load_config()
    df_results = run_quarterly_comparison(cfg, _FILTER_PRESETS, min_trades=args.min_trades)

    # Sla ALLE resultaten op in CSV
    csv_path = "filter_comparison_quarterly.csv"
    df_results.to_csv(csv_path, index=False)
    logger.info(f"Alle resultaten opgeslagen in {csv_path}")

    # --- KWARTAAL RESULTATEN ---
    df_filtered = df_results[df_results["trades"] >= args.min_trades].copy()

    print("\n" + "=" * 120)
    print(f"  VERGELIJKING VAN ALLE FILTERS PER KWARTAAL (2023–2024) - MINIMAAL {args.min_trades} TRADES")
    print("=" * 120 + "\n")

    if not df_filtered.empty:
        # Kolommen voor kwartaal resultaten (bevat "Periode")
        display_cols_quarterly = [
            "Periode", "trades", "win_rate", "sharpe_ratio", "max_drawdown",
            "profit_factor", "total_return", "avg_win", "avg_loss", "win_loss_ratio",
            "long_trades", "short_trades"
        ]
        for filter_name in df_filtered["Filter"].unique():
            filter_df = df_filtered[df_filtered["Filter"] == filter_name]
            print(f"\n{'─' * 120}")
            print(f"  Filter: {filter_name}")
            print(f"{'─' * 120}")
            print(filter_df[display_cols_quarterly].to_string(index=False))

    # --- JAARLIJKSE RESULTATEN ---
    df_yearly = group_by_year(df_results)
    df_yearly_filtered = df_yearly[df_yearly["trades"] >= args.min_trades]

    print("\n" + "=" * 120)
    print(f"  VERGELIJKING VAN ALLE FILTERS PER JAAR (2023–2024) - MINIMAAL {args.min_trades} TRADES")
    print("=" * 120 + "\n")

    if not df_yearly_filtered.empty:
        # Kolommen voor jaarlijkse resultaten (bevat "Year" in plaats van "Periode")
        display_cols_yearly = [
            "Year", "trades", "win_rate", "sharpe_ratio", "max_drawdown",
            "profit_factor", "total_return", "avg_win", "avg_loss", "win_loss_ratio",
            "long_trades", "short_trades"
        ]
        for filter_name in df_yearly_filtered["Filter"].unique():
            filter_df = df_yearly_filtered[df_yearly_filtered["Filter"] == filter_name]
            print(f"\n{'─' * 120}")
            print(f"  Filter: {filter_name}")
            print(f"{'─' * 120}")
            print(filter_df[display_cols_yearly].to_string(index=False))

    # --- SAMENVATTING ---
    print("\n" + "=" * 120)
    print(f"  SAMENVATTING: BESTE FILTER PER METRIC (PERIODES MET ≥{args.min_trades} TRADES)")
    print("=" * 120 + "\n")

    metrics_to_compare = ["win_rate", "sharpe_ratio", "max_drawdown", "profit_factor", "total_return", "win_loss_ratio"]
    for metric in metrics_to_compare:
        filtered_df = df_filtered[df_filtered[metric].notna()]
        if not filtered_df.empty:
            if metric == "max_drawdown":
                best_row = filtered_df.loc[filtered_df[metric].idxmin()]
                print(
                    f"{metric.replace('_', ' ').title():<18}: {best_row['Filter']:<15} "
                    f"(Periode: {best_row['Periode']}, Waarde: {best_row[metric]:.1%})"
                )
            else:
                best_row = filtered_df.loc[filtered_df[metric].idxmax()]
                print(
                    f"{metric.replace('_', ' ').title():<18}: {best_row['Filter']:<15} "
                    f"(Periode: {best_row['Periode']}, Waarde: {best_row[metric]:.2f})"
                )
        else:
            print(f"{metric.replace('_', ' ').title():<18}: Geen data")

    # --- GRAFIEKEN ---
    #print("\n" + "=" * 120)
    #print("  GRAFIEKEN WORDEN GEGENEREERD...")
    #print("=" * 120 + "\n")
    #plot_results(df_results, min_trades=args.min_trades)

if __name__ == "__main__":
    main()