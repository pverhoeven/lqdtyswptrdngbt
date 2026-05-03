"""
scripts/build_cache.py — upsampling + SMC cache bouwen (eenmalig na download).

Gebruik:
    python scripts/build_cache.py
    python scripts/build_cache.py --config config/config.yaml
    python scripts/build_cache.py --skip-aggregate   # alleen cache herbouwen
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_config
from src.data.aggregator import aggregate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Upsampling en SMC cache bouwen.")
    parser.add_argument("--config", default=None, help="Pad naar config.yaml")
    parser.add_argument(
        "--skip-aggregate",
        action="store_true",
        help="Sla upsampling over (alleen SMC cache herbouwen)",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="Enkel symbool verwerken (bijv. ETHUSDT). Standaard: alle coins uit config.",
    )
    parser.add_argument(
        "--lower-tf",
        action="append",
        dest="lower_tfs",
        metavar="TF",
        help="Extra lagere timeframe bouwen (bijv. '3min' of '5min'). Herhaalbaar.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.symbol:
        coins = [{"symbol": args.symbol}]
    else:
        coins = cfg.get("coins", [{"symbol": cfg["data"]["symbol"]}])

    try:
        from src.data.cache import build_cache
    except ImportError as exc:
        logger.error("SMC cache kon niet worden gebouwd: %s", exc)
        logger.error("Controleer of smartmoneyconcepts geïnstalleerd is.")
        sys.exit(1)

    for coin in coins:
        sym = coin["symbol"]

        if not args.skip_aggregate:
            extra = args.lower_tfs or []
            label = "/".join(["15m", "4h"] + [tf.replace("min", "m") for tf in extra])
            logger.info("=== %s — Stap 1/2: Upsampling 1m → %s ===", sym, label)
            aggregate(cfg, symbol=sym, extra_tfs=extra if extra else None)
        else:
            logger.info("%s — Upsampling overgeslagen (--skip-aggregate).", sym)

        logger.info("=== %s — Stap 2/2: SMC cache bouwen ===", sym)
        build_cache(cfg, symbol=sym)

    logger.info("Klaar. Je kunt nu run_backtest.py uitvoeren.")


if __name__ == "__main__":
    main()
