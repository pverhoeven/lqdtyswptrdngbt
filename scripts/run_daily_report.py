"""
run_daily_report.py — Dagelijks SMC setup rapport via Telegram.

Scant BTC, ETH en SOL op Binance 4H data, identificeert aankomende
liquidity sweeps en frisse Order Blocks, en stuurt een samenvatting
via Telegram. Prijsniveaus zijn direct toepasbaar op OKX XPERP.

Gebruik (handmatig):
    python scripts/run_daily_report.py

Daemon-modus (Docker / continue uitvoering):
    python scripts/run_daily_report.py --daemon
    python scripts/run_daily_report.py --daemon --hour 8   # 08:00 UTC

Via cron (elke dag 07:00 UTC):
    0 7 * * * cd /app && python scripts/run_daily_report.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.secrets_loader import load_secrets
load_secrets()

from src.config_loader import load_config
from src.notifications.notifier import Notifier
from src.scanner.daily_scanner import run_daily_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_DAYS_NL = ["ma", "di", "wo", "do", "vr", "za", "zo"]
_MONTHS_NL = [
    "", "jan", "feb", "mrt", "apr", "mei", "jun",
    "jul", "aug", "sep", "okt", "nov", "dec",
]


def _date_str_nl(dt: datetime) -> str:
    day   = _DAYS_NL[dt.weekday()]
    month = _MONTHS_NL[dt.month]
    return f"{day} {dt.day} {month} {dt.year}"


def _seconds_until(hour_utc: int) -> float:
    """Seconden tot het eerstvolgende tijdstip op hour_utc:00 UTC."""
    now    = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    diff   = (target - now).total_seconds()
    if diff <= 60:          # al voorbij of binnen 1 minuut → volgende dag
        diff += 86_400
    return diff


def run_once(cfg: dict, notifier: Notifier) -> None:
    now      = datetime.now(timezone.utc)
    date_str = _date_str_nl(now)

    logger.info("Dagelijks rapport starten voor %s", date_str)

    try:
        setups = run_daily_scan(cfg)
    except Exception as exc:
        logger.error("Scan mislukt: %s", exc)
        notifier.notify_error(f"Dagrapport mislukt: {exc}")
        return

    logger.info("%d setup(s) gevonden, rapport versturen…", len(setups))
    notifier.notify_daily_report(setups, date_str)
    logger.info("Rapport verstuurd.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dagelijks SMC setup rapport")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Blijf draaien en stuur elke dag een rapport op --hour UTC",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=7,
        metavar="H",
        help="Uur (UTC) waarop het rapport wordt verstuurd in daemon-modus (standaard: 7)",
    )
    args = parser.parse_args()

    cfg      = load_config()
    notifier = Notifier.from_cfg(cfg)

    if not args.daemon:
        run_once(cfg, notifier)
        return

    # --- Daemon: run direct bij start, daarna elke dag op args.hour:00 UTC ---
    logger.info("Daemon gestart. Dagelijks rapport om %02d:00 UTC.", args.hour)
    run_once(cfg, notifier)

    while True:
        wait = _seconds_until(args.hour)
        logger.info("Volgende rapport over %.0f uur.", wait / 3600)
        time.sleep(wait)
        run_once(cfg, notifier)


if __name__ == "__main__":
    main()
