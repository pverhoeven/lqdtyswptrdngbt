"""
scripts/download_data.py — eenmalig uitvoeren om historische 1m data op te halen.

Gebruik:
    python scripts/download_data.py
    python scripts/download_data.py --config config/config.yaml
"""

import argparse
import logging
import sys
from pathlib import Path

# Projectroot op sys.path zodat `src` vindbaar is
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_config
from src.data.downloader import download

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Binance 1m klines.")
    parser.add_argument("--config", default=None, help="Pad naar config.yaml")
    parser.add_argument(
        "--symbol",
        default=None,
        help="Enkel symbool downloaden (bijv. ETHUSDT). Standaard: alle coins uit config.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.symbol:
        coins = [{"symbol": args.symbol}]
    else:
        coins = cfg.get("coins", [{"symbol": cfg["data"]["symbol"]}])

    for coin in coins:
        sym = coin["symbol"]
        logging.info("=== Download %s ===", sym)
        download(cfg, symbol=sym)


if __name__ == "__main__":
    main()
