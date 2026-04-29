"""
scripts/run_param_sweep.py — Grid sweep over risico-parameters voor long_only.

Sweept reward_ratio × sl_buffer_pct op in-sample data.
Run OOS validatie daarna handmatig met de beste combo.

Gebruik:
    python scripts/run_param_sweep.py
    python scripts/run_param_sweep.py --set oos
"""

import argparse
import copy
import logging
import sys
from itertools import product
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest.sweep_engine import SweepFilters, run_sweep_backtest
from src.config_loader import load_config

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

REWARD_RATIOS  = [1.5, 2.0, 2.5, 3.0]
SL_BUFFERS_PCT = [0.5, 1.0, 1.5, 2.0]
FILTER = SweepFilters(direction="long")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=["in_sample", "oos"], default="in_sample")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    is_oos = args.set == "oos"
    if is_oos:
        print("=" * 60)
        print("OOS EVALUATIE — GEBRUIK ALLEEN NA BEVROREN PARAMETERS")
        print("=" * 60)

    cfg_base = load_config(args.config)

    combos = list(product(REWARD_RATIOS, SL_BUFFERS_PCT))
    print(f"\nParam sweep: {len(combos)} combinaties (long_only, {args.set})\n")

    rows = []
    for rr, sl_buf in combos:
        cfg = copy.deepcopy(cfg_base)
        cfg["risk"]["reward_ratio"]   = rr
        cfg["risk"]["sl_buffer_pct"]  = sl_buf

        try:
            m, _ = run_sweep_backtest(cfg, dataset=args.set, filters=FILTER, allow_oos=is_oos)
            rows.append({
                "rr":            rr,
                "sl_buf_%":      sl_buf,
                "trades":        m.trade_count,
                "win_rate":      round(m.win_rate * 100, 1),
                "sharpe":        round(m.sharpe_ratio, 2),
                "max_dd_%":      round(m.max_drawdown * 100, 1),
                "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else 999,
                "total_ret_%":   round(m.total_return * 100, 1),
            })
        except Exception as exc:
            rows.append({
                "rr": rr, "sl_buf_%": sl_buf,
                "trades": 0, "win_rate": None, "sharpe": None,
                "max_dd_%": None, "profit_factor": None, "total_ret_%": None,
                "_error": str(exc),
            })

    df = pd.DataFrame(rows)
    if "_error" in df.columns:
        df = df.drop(columns=["_error"])

    df_sorted = df.sort_values("sharpe", ascending=False)

    pd.set_option("display.width", 120)
    pd.set_option("display.max_rows", 100)
    print(df_sorted.to_string(index=False))

    best = df_sorted[df_sorted["trades"] >= 30].head(1)
    if not best.empty:
        r = best.iloc[0]
        print(f"\n  Beste (≥30 trades, hoogste Sharpe):")
        print(f"  reward_ratio={r['rr']}  sl_buffer_pct={r['sl_buf_%']}%")
        print(f"  Sharpe={r['sharpe']}  Win rate={r['win_rate']}%  "
              f"Max DD={r['max_dd_%']}%  Profit factor={r['profit_factor']}")
        if not is_oos:
            print(f"\n  Valideer daarna op OOS:")
            print(f"  python scripts/run_param_sweep.py --set oos")


if __name__ == "__main__":
    main()
