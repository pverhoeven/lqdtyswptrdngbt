"""
run_sweep_scanner.py — Telegram signalen bij liquidity sweeps.

Ondersteunt OKX EEA (XPERP futures) en Binance (USDT perpetual futures).
Bij een sweep wordt direct een Telegram-bericht gestuurd zodat je handmatig
kunt beoordelen of het interessant is.
De bestaande trading bot wordt op geen enkele manier beïnvloed.

Gebruik:
    python scripts/run_sweep_scanner.py
    python scripts/run_sweep_scanner.py --exchange binance
    python scripts/run_sweep_scanner.py --exchange okx --symbols BTC-USD_UM_XPERP-310404
    python scripts/run_sweep_scanner.py --exchange binance --symbols BTCUSDT ETHUSDT
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config_loader import load_config
from src.notifications.notifier import Notifier
from src.signals.detector import SweepSignal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_OKX_REST_BASE     = "https://eea.okx.com"
_BINANCE_REST_BASE = "https://fapi.binance.com"


def fetch_okx_instruments() -> list[str]:
    """Haal alle actieve XPERP FUTURES instrumenten op van OKX EEA."""
    try:
        resp = requests.get(
            f"{_OKX_REST_BASE}/api/v5/public/instruments",
            params={"instType": "FUTURES"},
            timeout=15,
        )
        resp.raise_for_status()
        instruments = resp.json().get("data", [])
    except Exception as exc:
        logger.error("Kan OKX instrumenten niet ophalen: %s", exc)
        sys.exit(1)

    xperp = sorted(
        inst["instId"]
        for inst in instruments
        if "XPERP" in inst.get("instId", "") and inst.get("state") == "live"
    )

    if not xperp:
        logger.error("Geen actieve XPERP instrumenten gevonden op OKX EEA.")
        sys.exit(1)

    logger.info("Gevonden %d OKX XPERP instrumenten: %s", len(xperp), xperp)
    return xperp


def fetch_binance_instruments() -> list[str]:
    """Haal alle actieve USDT-perpetual futures op van Binance."""
    try:
        resp = requests.get(
            f"{_BINANCE_REST_BASE}/fapi/v1/exchangeInfo",
            timeout=15,
        )
        resp.raise_for_status()
        symbols_data = resp.json().get("symbols", [])
    except Exception as exc:
        logger.error("Kan Binance instrumenten niet ophalen: %s", exc)
        sys.exit(1)

    symbols = sorted(
        s["symbol"]
        for s in symbols_data
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    )

    if not symbols:
        logger.error("Geen actieve USDT-perpetual futures gevonden op Binance.")
        sys.exit(1)

    logger.info("Gevonden %d Binance USDT-perpetuals: %s...", len(symbols), symbols[:5])
    return symbols


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep scanner — Telegram signalen, geen trading"
    )
    parser.add_argument(
        "--exchange",
        choices=["okx", "binance"],
        default="okx",
        help="Exchange om te bewaken (standaard: okx)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        metavar="SYMBOL",
        help=(
            "Optioneel: specifieke symbolen om te bewaken. "
            "OKX: bijv. BTC-USD_UM_XPERP-310404. "
            "Binance: bijv. BTCUSDT ETHUSDT. "
            "(standaard: alle actieve paren van de exchange)"
        ),
    )
    args = parser.parse_args()

    cfg      = load_config()
    notifier = Notifier.from_cfg(cfg)

    if args.symbols:
        instruments = args.symbols
        logger.info("Handmatig opgegeven symbolen: %s", instruments)
    elif args.exchange == "okx":
        instruments = fetch_okx_instruments()
    else:
        instruments = fetch_binance_instruments()

    if args.exchange == "okx":
        from src.feeds.okx_multi_feed import SweepScanner
        exchange_label = "OKX EEA (XPERP)"
    else:
        from src.feeds.binance_multi_feed import SweepScanner
        exchange_label = "Binance (USDT-perps)"

    def on_sweep(symbol: str, sig: SweepSignal) -> None:
        notifier.notify_sweep_detected(
            symbol      = symbol,
            direction   = sig.direction,
            entry_price = sig.entry_price,
            liq_level   = sig.liq_level,
            sl_price    = sig.sl_price,
            tp_price    = sig.tp_price,
            timestamp   = sig.timestamp,
        )

    scanner = SweepScanner(inst_ids=instruments, cfg=cfg, on_sweep=on_sweep)

    def _handle_stop(signum, frame) -> None:
        logger.info("Stoppen (signaal %s)...", signum)
        notifier.send(f"[SWEEP SCANNER GESTOPT] ({exchange_label})")
        scanner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    preview = instruments[:8]
    suffix  = f" (+{len(instruments) - 8} meer)" if len(instruments) > 8 else ""
    notifier.send(
        f"[SWEEP SCANNER GESTART]\n"
        f"Exchange: {exchange_label}\n"
        f"Bewaakt {len(instruments)} paren.\n"
        f"Paren: {', '.join(preview)}{suffix}"
    )

    logger.info(
        "Scanner actief voor %d paren op %s. Ctrl+C om te stoppen.",
        len(instruments), exchange_label,
    )
    scanner.start()


if __name__ == "__main__":
    main()
