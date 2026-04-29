"""
scripts/run_paper_trader.py — Start de live paper trading loop.

Gebruik:
    python scripts/run_paper_trader.py --filter baseline
    python scripts/run_paper_trader.py --filter long_only
    python scripts/run_paper_trader.py --filter regime_long
    python scripts/run_paper_trader.py --filter bos10

Stop met: Ctrl+C (slaat statistieken op voor de loop stopt)

Filter opties:
    baseline      geen filters (alle sweeps)
    regime        alleen sweeps in lijn met HMM regime
    long_only     alleen bearish sweeps → long
    short_only    alleen bullish sweeps → short
    bos10         wacht op BOS bevestiging binnen 10 candles
    bos20         wacht op BOS bevestiging binnen 20 candles
    regime_long   regime + alleen long
    regime_short  regime + alleen short
    regime_bos10  regime + BOS bevestiging
    long_bos10    alleen long + BOS bevestiging
    short_bos10   alleen short + BOS bevestiging
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.secrets_loader import load_secrets

load_secrets()

from src.config_loader import load_config
from src.feeds.binance_feed import BinanceFeed
from src.notifications.notifier import Notifier
from src.signals.detector import SweepDetector
from src.signals.filters import SweepFilters
from src.trading.broker.paper import PaperBroker
from src.trading.order_manager import OrderManager
from src.trading.paper_trader import PaperTrader, RegimeProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

_FILTER_PRESETS: dict[str, SweepFilters] = {
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
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper trading loop starten.")
    parser.add_argument(
        "--filter",
        default="baseline",
        choices=list(_FILTER_PRESETS.keys()),
        help="Welke sweep-filter gebruiken (standaard: baseline)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Startkapitaal in USDT (standaard: uit config)",
    )
    parser.add_argument(
        "--no-regime",
        action="store_true",
        help="HMM regime provider uitschakelen (ook als filter dit vereist)",
    )
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Overschrijf kapitaal als opgegeven
    if args.capital is not None:
        cfg["risk"]["capital_initial"] = args.capital

    filters = _FILTER_PRESETS[args.filter]
    capital = cfg["risk"]["capital_initial"]
    fee_pct = cfg["backtest"]["fee_pct"]

    # --- Componenten bouwen ---
    feed = BinanceFeed(cfg)

    detector = SweepDetector(
        filters       = filters,
        reward_ratio  = cfg["risk"]["reward_ratio"],
        sl_buffer_pct = cfg["risk"]["sl_buffer_pct"],
    )

    broker = PaperBroker(
        initial_capital = capital,
        fee_pct         = fee_pct,
        max_open        = cfg["risk"]["max_open_trades"],
    )

    notifier = Notifier.from_cfg(cfg)
    cb_cfg   = cfg.get("risk", {}).get("circuit_breaker")

    order_manager = OrderManager(
        broker   = broker,
        symbol   = cfg["data"]["symbol"],
        risk_pct = cfg["risk"]["risk_per_trade_pct"],
        max_open = cfg["risk"]["max_open_trades"],
        cb_cfg   = cb_cfg,
        notifier = notifier,
    )

    # Regime provider (optioneel)
    regime_provider = None
    if filters.regime and not args.no_regime:
        model_path = (
            Path(cfg["data"]["paths"]["processed"]) / "hmm_regime_model.pkl"
        )
        if model_path.exists():
            regime_provider = RegimeProvider(cfg)
        else:
            print(
                f"⚠️  Geen regime model op {model_path}.\n"
                f"   Train eerst: python scripts/run_backtest.py --set in_sample\n"
                f"   Of gebruik --no-regime om zonder regime filter te draaien."
            )
            sys.exit(1)

    # --- Start ---
    trader = PaperTrader(
        feed           = feed,
        detector       = detector,
        order_manager  = order_manager,
        regime_provider= regime_provider,
    )
    trader.start()


if __name__ == "__main__":
    main()