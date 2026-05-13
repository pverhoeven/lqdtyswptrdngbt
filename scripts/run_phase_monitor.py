"""
run_phase_monitor.py — Achtergrond fase-monitor voor SMC setups.

Draait elke 30 minuten run_daily_scan() en stuurt een Telegram-bericht
alleen als een setup een fase-upgrade heeft:

  Nieuw op FASE 2/3   → setup is snel door FASE 1 heen gegaan
  FASE 1 → FASE 2     → sweep gedetecteerd, wacht op BoS
  FASE 2 → FASE 3     → BoS bevestigd, entry op komst  ⭐⭐⭐
  FASE 3 → in retest  → prijs bij BoS-niveau — ENTRY NU

Het dagelijkse rapport (run_daily_report.py) blijft voor het overzicht.

Gebruik:
    python scripts/run_phase_monitor.py
    python scripts/run_phase_monitor.py --interval 60   # 60 minuten
"""

from __future__ import annotations

import argparse
import json
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
from src.scanner.chart_generator import generate_setup_chart
from src.scanner.daily_scanner import DailySetup, run_daily_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent.parent / "data" / "phase_monitor_state.json"
_FASE_RANK  = {"FASE 1": 1, "FASE 2": 2, "FASE 3": 3}


def _zone_key(s: DailySetup) -> str:
    """Stabiele sleutel per (symbool, richting, zone). Afgerond op magnitude."""
    z = s.zone_level
    if z >= 10_000:
        rounded = round(z / 10) * 10
    elif z >= 1_000:
        rounded = round(z)
    elif z >= 100:
        rounded = round(z, 1)
    else:
        rounded = round(z, 2)
    return f"{s.symbol}_{s.direction}_{rounded}"


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _fmt(v: float) -> str:
    if v >= 10_000:
        return f"${v:,.0f}"
    elif v >= 100:
        return f"${v:,.1f}"
    else:
        return f"${v:,.2f}"


def _build_alert(s: DailySetup, reason: str) -> str:
    arrow  = "▲ LONG" if s.direction == "long" else "▼ SHORT"
    stars  = "⭐" * s.stars
    rr_val = abs(s.tp - s.entry_zone) / max(abs(s.entry_zone - s.sl), 1e-8)
    zone_lbl = "EQL" if s.direction == "long" else "EQH"

    lines = [
        f"[SMC FASE-ALERT] {arrow} {stars}",
        f"{s.symbol}  ({_fmt(s.current_price)})",
        f"{reason}",
        "",
        f"{s.fase}  |  {s.fase_label}",
        f"Zone ({zone_lbl}): {_fmt(s.zone_level)} ({s.n_equal}× equal)",
        f"Entry: ~{_fmt(s.entry_zone)}  SL: {_fmt(s.sl)}  TP: {_fmt(s.tp)}  RR: 1:{rr_val:.1f}",
    ]
    if s.fase in ("FASE 2", "FASE 3"):
        sweep_lbl = "Sweep low" if s.direction == "long" else "Sweep high"
        lines.append(f"{sweep_lbl}: {_fmt(s.sweep_low)}")
    if s.fase == "FASE 3" and s.bos_level > 0:
        lines.append(f"BoS niveau: {_fmt(s.bos_level)}")
    lines.append(f"OKX: {s.xperp}")
    return "\n".join(lines)


def _send_alert(notifier: Notifier, setup: DailySetup, reason: str) -> None:
    """Genereer chart en stuur als foto met caption. Valt terug op tekst bij fout."""
    caption = _build_alert(setup, reason)
    try:
        chart = generate_setup_chart(setup)
        notifier.send_photo(chart, caption)
    except Exception as exc:
        logger.warning("Chart generatie mislukt (%s), stuur tekst: %s", setup.symbol, exc)
        notifier.send(caption)


def check_upgrades(setups: list[DailySetup], state: dict, notifier: Notifier) -> dict:
    """Vergelijk nieuwe scan met opgeslagen state, stuur alerts bij upgrades."""
    new_state = {}

    for s in setups:
        key       = _zone_key(s)
        prev      = state.get(key, {})
        prev_fase = prev.get("fase", "")
        prev_retest = prev.get("in_retest", False)
        in_retest = "ENTRY NU" in s.fase_label

        new_state[key] = {
            "fase":      s.fase,
            "in_retest": in_retest,
            "symbol":    s.symbol,
            "direction": s.direction,
        }

        curr_rank = _FASE_RANK.get(s.fase, 0)
        prev_rank = _FASE_RANK.get(prev_fase, 0)

        # Nieuwe setup direct op FASE 2 of 3 (nooit eerder gezien als FASE 1)
        if not prev_fase and s.fase in ("FASE 2", "FASE 3"):
            reason = (
                "Sweep gedetecteerd — nieuw in ons bereik"
                if s.fase == "FASE 2"
                else "BoS bevestigd — nieuw in ons bereik"
            )
            logger.info("NIEUW op %s: %s %s", s.fase, s.symbol, s.direction)
            _send_alert(notifier, s, reason)

        # Fase-upgrade
        elif curr_rank > prev_rank:
            if s.fase == "FASE 2":
                reason = "Sweep gedetecteerd — wacht op BoS"
            else:
                reason = "BoS bevestigd — entry op komst"
            logger.info("UPGRADE %s → %s: %s %s", prev_fase, s.fase, s.symbol, s.direction)
            _send_alert(notifier, s, reason)

        # FASE 3: prijs raakt re-test zone (nog niet eerder gemeld)
        elif s.fase == "FASE 3" and in_retest and not prev_retest:
            reason = "ENTRY NU — prijs bij BoS-niveau"
            logger.info("RETEST: %s %s", s.symbol, s.direction)
            _send_alert(notifier, s, reason)

    return new_state


def run_once(cfg: dict, notifier: Notifier) -> None:
    logger.info("Scan starten…")
    try:
        setups = run_daily_scan(cfg)
    except Exception as exc:
        logger.error("Scan mislukt: %s", exc)
        return

    logger.info("%d setup(s) gevonden.", len(setups))
    state     = _load_state()
    new_state = check_upgrades(setups, state, notifier)
    _save_state(new_state)


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC fase-monitor")
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        metavar="MIN",
        help="Poll-interval in minuten (standaard: 30)",
    )
    args = parser.parse_args()

    cfg      = load_config()
    notifier = Notifier.from_cfg(cfg)
    interval = args.interval * 60

    logger.info(
        "Fase-monitor gestart. Poll elke %d minuten. State: %s",
        args.interval, _STATE_FILE,
    )
    notifier.send(
        f"[FASE MONITOR GESTART]\n"
        f"Poll-interval: {args.interval} minuten\n"
        f"Alleen alerts bij fase-upgrades."
    )

    while True:
        run_once(cfg, notifier)
        next_run = datetime.now(timezone.utc).strftime("%H:%M UTC")
        logger.info("Volgende scan over %d minuten.", args.interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
