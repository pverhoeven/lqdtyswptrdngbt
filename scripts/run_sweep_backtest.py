"""
scripts/run_sweep_backtest.py — Sweep-strategie backtest met filter-vergelijking.

Gebruik:
    # Vergelijk alle filters op in-sample data
    python scripts/run_sweep_backtest.py --set in_sample

    # Draai één specifieke configuratie
    python scripts/run_sweep_backtest.py --set in_sample --filter baseline
    python scripts/run_sweep_backtest.py --set in_sample --filter regime
    python scripts/run_sweep_backtest.py --set in_sample --filter long_only
    python scripts/run_sweep_backtest.py --set in_sample --filter bos10
    python scripts/run_sweep_backtest.py --set in_sample --filter regime_long

    # OOS evaluatie (alleen na bevroren parameters)
    python scripts/run_sweep_backtest.py --set oos --filter regime_long

    # Stress-periode analyse: één filter op alle marktcondities
    python scripts/run_sweep_backtest.py --period all --filter regime_long

    # Één specifieke stress-periode
    python scripts/run_sweep_backtest.py --period ftx_crash --filter regime_long
"""

import argparse
import logging
import sys
from pathlib import Path



sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.sweep_engine import SweepFilters, compare_filters, run_sweep_backtest
from src.backtest.monte_carlo import monte_carlo
from src.backtest.metrics import equity_curve
from src.config_loader import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Voorgedefinieerde stress-periodes (BTC marktcondities)
_STRESS_PERIODS: dict[str, tuple[str, str, str]] = {
    #                    start          end           label
    "bear_2018":     ("2018-01-01", "2018-12-31", "Bear 2018"),
    "sideways_2019": ("2019-01-01", "2019-10-31", "Sideways 2019"),
    "covid_crash":   ("2020-02-15", "2020-05-01", "COVID crash"),
    "bull_2021_q1":  ("2020-10-01", "2021-05-15", "Bull Q1/2021"),
    "bull_2021_q4":  ("2021-08-01", "2021-11-30", "Bull Q4/2021"),
    "bear_2022":     ("2022-01-01", "2022-06-30", "Bear H1/2022"),
    "ftx_crash":     ("2022-11-01", "2022-12-31", "FTX crash"),
    "recovery_2023": ("2023-01-01", "2023-12-31", "Recovery 2023"),
    "halving_2024":  ("2024-01-01", "2024-12-31", "Halving 2024"),
}

# Minimale trades voor betrouwbare Sharpe in de matrix
_MIN_TRADES = 20

# Beschikbare voorgedefinieerde filters
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
    "atr14": SweepFilters(atr_filter=True),
    "long_atr14": SweepFilters(direction="long", atr_filter=True),
    "short_atr14": SweepFilters(direction="short", atr_filter=True),
    # Nieuwe SMC-kwaliteitsfilters
    "rejection":           SweepFilters(bos_confirm=True, bos_window=20, sweep_rejection=True),
    "trend20":             SweepFilters(bos_confirm=True, bos_window=20, pre_sweep_lookback=20),
    "rejection+trend20":   SweepFilters(bos_confirm=True, bos_window=20, sweep_rejection=True, pre_sweep_lookback=20),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep-strategie backtest.")
    parser.add_argument(
        "--set", choices=["in_sample", "oos"], default=None,
        help="Dataset te gebruiken (vereist als --period niet opgegeven is)",
    )
    parser.add_argument(
        "--period", default=None,
        choices=["all"] + list(_STRESS_PERIODS.keys()),
        help="Stress-periode (all = alle periodes naast elkaar)",
    )
    parser.add_argument(
        "--filter", default="all",
        choices=["all"] + list(_FILTER_PRESETS.keys()),
        help="Welke filter te gebruiken (standaard: all = vergelijk alle)",
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--symbol", default=None, help="Symbool override (bijv. ETHUSDT)")
    parser.add_argument(
        "--ttl", type=int, default=0,
        help="Limit order TTL in candles (0 = directe fill). Gebruik 5 voor fill-simulatie.",
    )
    args = parser.parse_args()

    if args.period is None and args.set is None:
        parser.error("Geef --set of --period op.")

    cfg = load_config(args.config)
    if args.symbol:
        cfg["data"]["symbol"] = args.symbol

    # ── Stress-periode modus ────────────────────────────────────────────────
    if args.period is not None:
        _run_stress_mode(cfg, args)
        return

    # ── Normaal in_sample / OOS modus ──────────────────────────────────────
    is_oos = args.set == "oos"
    if is_oos:
        logger.warning("=" * 60)
        logger.warning("OOS EVALUATIE — GEBRUIK ALLEEN NA BEVROREN PARAMETERS")
        logger.warning("=" * 60)

    if args.filter == "all":
        print(f"\n{'='*65}")
        print(f"  SWEEP STRATEGIE — FILTER VERGELIJKING ({args.set.upper()})")
        print(f"{'='*65}\n")

        try:
            df = compare_filters(cfg, dataset=args.set)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(1)

        df_sorted = df.copy()
        df_sorted["_sharpe_num"] = df_sorted["sharpe"].apply(
            lambda x: x if isinstance(x, (int, float)) and x is not None else -999
        )
        df_sorted = df_sorted.sort_values("_sharpe_num", ascending=False).drop(columns=["_sharpe_num"])
        print(df_sorted.to_string())
        print()

        best = df_sorted[df_sorted["trades"].apply(
            lambda x: isinstance(x, int) and x >= 30
        )].head(1)

        if not best.empty:
            print(f"\n{'─'*65}")
            print(f"  Beste configuratie (≥30 trades, hoogste Sharpe):")
            print(f"  → {best.index[0]}")
            print(f"    Sharpe: {best['sharpe'].iloc[0]}  |  "
                  f"Trades: {best['trades'].iloc[0]}  |  "
                  f"Win rate: {best['win_rate'].iloc[0]}")
            print(f"{'─'*65}")

        print(f"\n  Volgende stap:")
        print(f"  python scripts/run_sweep_backtest.py "
              f"--set {args.set} --filter <beste configuratie>")
        return

    _run_single(cfg, args.filter, dataset=args.set, allow_oos=is_oos, ttl=args.ttl)


def _run_stress_mode(cfg: dict, args) -> None:
    """Voer de backtest uit op één of alle stress-periodes."""
    filter_name = args.filter if args.filter != "all" else "baseline"
    f = _FILTER_PRESETS[filter_name]

    if args.period == "all":
        periods = list(_STRESS_PERIODS.items())
    else:
        periods = [(args.period, _STRESS_PERIODS[args.period])]

    if args.filter == "all" and args.period == "all":
        # Alle filters op alle stress-periodes → Sharpe matrix
        import pandas as pd
        print(f"\n{'='*70}")
        print(f"  STRESS PERIODES — ALLE FILTERS (Sharpe ratio matrix)")
        print(f"  Cellen met <{_MIN_TRADES} trades worden als '–' weergegeven")
        print(f"{'='*70}\n")
        matrix: dict[str, dict[str, object]] = {}
        for period_key, (start, end, label) in _STRESS_PERIODS.items():
            matrix[label] = {}
            for filter_naam, flt in _FILTER_PRESETS.items():
                try:
                    m, _ = run_sweep_backtest(cfg, filters=flt, start=start, end=end)
                    if m.trade_count < _MIN_TRADES:
                        matrix[label][filter_naam] = "–"
                    else:
                        matrix[label][filter_naam] = round(m.sharpe_ratio, 2)
                except Exception as exc:
                    logger.warning("Filter '%s' periode '%s' mislukt: %s", filter_naam, period_key, exc)
                    matrix[label][filter_naam] = None
        df = pd.DataFrame(matrix).T
        print(df.to_string())
        print(f"\n  Tip: python scripts/run_sweep_backtest.py --period <naam> --filter all")
        return

    if args.filter == "all" and args.period != "all":
        # Alle filters op één stress-periode
        start, end, label = _STRESS_PERIODS[args.period]
        print(f"\n{'='*70}")
        print(f"  STRESS PERIODE — {label.upper()} ({start} → {end})")
        print(f"{'='*70}\n")
        rows = []
        for naam, flt in _FILTER_PRESETS.items():
            try:
                m, _ = run_sweep_backtest(cfg, filters=flt, start=start, end=end)
                rows.append({
                    "configuratie":  naam,
                    "trades":        m.trade_count,
                    "win_rate":      f"{m.win_rate:.1%}",
                    "sharpe":        round(m.sharpe_ratio, 2),
                    "max_drawdown":  f"{m.max_drawdown:.1%}",
                    "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else "∞",
                    "total_return":  f"{m.total_return:.1%}",
                })
            except Exception as exc:
                logger.warning("Filter '%s' mislukt: %s", naam, exc)
                rows.append({"configuratie": naam, "trades": 0, "win_rate": "–",
                             "sharpe": None, "max_drawdown": "–",
                             "profit_factor": None, "total_return": "–"})
        import pandas as pd
        df = pd.DataFrame(rows).set_index("configuratie")
        df["_s"] = df["sharpe"].apply(lambda x: x if isinstance(x, (int, float)) and x is not None else -999)
        df = df.sort_values("_s", ascending=False).drop(columns=["_s"])
        print(df.to_string())
        return

    if args.period == "all":
        # Één filter op alle stress-periodes
        print(f"\n{'='*70}")
        print(f"  STRESS PERIODES — FILTER: {filter_name.upper()}")
        print(f"{'='*70}\n")
        rows = []
        for naam, (start, end, label) in periods:
            try:
                m, trades = run_sweep_backtest(cfg, filters=f, start=start, end=end)
                rows.append({
                    "periode":       label,
                    "window":        f"{start} → {end}",
                    "trades":        m.trade_count,
                    "win_rate":      f"{m.win_rate:.1%}",
                    "sharpe":        round(m.sharpe_ratio, 2),
                    "max_drawdown":  f"{m.max_drawdown:.1%}",
                    "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else "∞",
                    "total_return":  f"{m.total_return:.1%}",
                })
            except Exception as exc:
                logger.warning("Periode '%s' mislukt: %s", naam, exc)
                rows.append({"periode": label, "window": f"{start} → {end}",
                             "trades": 0, "win_rate": "–", "sharpe": None,
                             "max_drawdown": "–", "profit_factor": None, "total_return": "–"})
        import pandas as pd
        df = pd.DataFrame(rows).set_index("periode")
        print(df.to_string())
        print(f"\n  Tip: python scripts/run_sweep_backtest.py "
              f"--period <naam> --filter {filter_name}  (voor detail)")
        return

    # Één specifieke stress-periode + één filter: toon detail
    start, end, label = _STRESS_PERIODS[args.period]
    print(f"\n{'='*60}")
    print(f"  STRESS PERIODE — {label.upper()} ({start} → {end})")
    print(f"  Filter: {filter_name.upper()}")
    print(f"{'='*60}")
    try:
        metrics, trades = run_sweep_backtest(cfg, filters=f, start=start, end=end)
    except (ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        sys.exit(1)
    _print_detail(cfg, metrics, trades)


def _run_single(cfg, filter_name, dataset, allow_oos, ttl: int = 0):
    """Voer één filter uit op een dataset en druk gedetailleerde output af."""
    f = _FILTER_PRESETS[filter_name]
    print(f"\n{'='*55}")
    print(f"  SWEEP BACKTEST — {filter_name.upper()} ({dataset.upper()})")
    if ttl > 0:
        print(f"  Fill-simulatie: limit TTL = {ttl} candles ({ttl * 15} min)")
    print(f"{'='*55}")

    try:
        metrics, trades = run_sweep_backtest(
            cfg, dataset=dataset, filters=f, allow_oos=allow_oos, pending_ttl=ttl
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    _print_detail(cfg, metrics, trades)


def _print_detail(cfg, metrics, trades):
    """Druk gedetailleerde metrics, Monte Carlo en jaarlijkse verdeling af."""
    print(f"\n{metrics}")
    mc_results = monte_carlo(trades, n_simulations=1000, initial_capital=10000)
    print(f"Sharpe ratio percentielen: {mc_results['sharpe']}")
    print(f"Win rate percentielen: {mc_results['win_rate']}")
    print(f"Max drawdown percentielen: {mc_results['max_drawdown']}")
    print(f"Profit factor percentielen: {mc_results['profit_factor']}")
    print(f"Total return percentielen: {mc_results['total_return']}")

    print(f"\n{'─'*55}")
    print(metrics.interpret())
    print()

    if trades:
        curve = equity_curve(trades, cfg["risk"]["capital_initial"])
        wins   = [t for t in trades if t.outcome == "win"]
        losses = [t for t in trades if t.outcome == "loss"]

        print(f"Equity:  {curve.iloc[0]:.0f} → {curve.iloc[-1]:.0f} USDT")
        print(f"Periode: {trades[0].entry_time.date()} "
              f"→ {trades[-1].exit_time.date()}")
        print(f"Long:    {sum(1 for t in trades if t.direction=='long')}  |  "
              f"Short:   {sum(1 for t in trades if t.direction=='short')}")
        print(f"Wins:    {len(wins)}  |  Losses: {len(losses)}")

        total_fees = sum(t.fee_cost for t in trades)
        avg_fee = total_fees / len(trades) if trades else 0
        print(f"Totale kosten (fees): {total_fees:.2f} USDT")
        print(f"Gemiddelde kosten per trade: {avg_fee:.2f} USDT")

        print(f"\n  Per jaar:")
        for year in sorted({t.entry_time.year for t in trades}):
            yr_trades = [t for t in trades if t.entry_time.year == year]
            yr_wins   = sum(1 for t in yr_trades if t.outcome == "win")
            yr_pnl    = sum(t.pnl_capital for t in yr_trades)
            print(f"    {year}: {len(yr_trades):>4} trades  "
                  f"{yr_wins/len(yr_trades):>5.1%} win rate  "
                  f"P&L: {yr_pnl:>+9.0f} USDT")




if __name__ == "__main__":
    main()