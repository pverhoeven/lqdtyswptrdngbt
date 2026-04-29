"""
scripts/dashboard.py — Live terminal dashboard voor de trading bot.

Leest de JSONL-logbestanden in logs/ en toont:
- Equity & sessie-statistieken
- Circuit breaker status
- Open posities
- Recente trades
- Recente signalen

Gebruik:
    python scripts/dashboard.py
    python scripts/dashboard.py --log logs/trades_20260428.jsonl
    python scripts/dashboard.py --refresh 10   # elke 10 seconden vernieuwen

Stop met: Ctrl+C
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_LOG_DIR = Path("logs")
_RECENT_N = 10   # aantal regels in recente trades / signalen tabel


# ---------------------------------------------------------------------------
# Log reader
# ---------------------------------------------------------------------------

def _find_latest_log() -> Path | None:
    logs = sorted(_LOG_DIR.glob("trades_*.jsonl"))
    return logs[-1] if logs else None


def _read_log(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return events


# ---------------------------------------------------------------------------
# State berekenen uit events
# ---------------------------------------------------------------------------

def _build_state(events: list[dict]) -> dict:
    signals: list[dict] = []
    placed:  list[dict] = []
    closed:  list[dict] = []
    open_orders: dict[str, dict] = {}   # order_id → order_placed event

    for ev in events:
        t = ev.get("event")
        if t == "signal":
            signals.append(ev)
        elif t == "order_placed":
            open_orders[ev["order_id"]] = ev
            placed.append(ev)
        elif t == "trade_closed":
            closed.append(ev)
            open_orders.pop(ev.get("order_id", ""), None)

    # Statistieken
    wins   = sum(1 for c in closed if (c.get("pnl") or 0) > 0)
    losses = sum(1 for c in closed if (c.get("pnl") or 0) <= 0)
    total_pnl = sum(c.get("pnl") or 0 for c in closed)

    # Start- en huidig kapitaal traceren is niet direct uit log beschikbaar,
    # maar we kunnen de equity teruglezen uit het meest recente gesloten event.
    # Alternatief: we tonen alleen P&L.

    return {
        "signals":     signals,
        "placed":      placed,
        "closed":      closed,
        "open_orders": list(open_orders.values()),
        "wins":        wins,
        "losses":      losses,
        "total_pnl":   total_pnl,
        "n_signals":   len(signals),
        "n_placed":    len(placed),
    }


# ---------------------------------------------------------------------------
# Rich renderfuncties
# ---------------------------------------------------------------------------

def _pnl_text(pnl: float | None) -> Text:
    if pnl is None:
        return Text("–", style="dim")
    if pnl > 0:
        return Text(f"+{pnl:,.2f}", style="bold green")
    if pnl < 0:
        return Text(f"{pnl:,.2f}", style="bold red")
    return Text(f"{pnl:,.2f}", style="dim")


def _side_text(side: str) -> Text:
    if side.upper() in ("LONG", "BUY"):
        return Text("▲ LONG", style="green")
    return Text("▼ SHORT", style="red")


def _status_text(status: str) -> Text:
    colors = {
        "PENDING": "yellow",
        "OPEN":    "cyan",
        "CLOSED":  "dim",
        "CANCELLED": "dim",
    }
    return Text(status, style=colors.get(status.upper(), "white"))


def _make_stats_panel(state: dict, log_path: Path) -> Panel:
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses
    win_rate = wins / total if total > 0 else 0.0
    pnl      = state["total_pnl"]

    pnl_style = "bold green" if pnl >= 0 else "bold red"
    pnl_str   = f"[{pnl_style}]{pnl:+,.2f} USDT[/{pnl_style}]"

    text = (
        f"[bold]Logbestand:[/bold] {log_path.name}\n"
        f"[bold]Signalen:[/bold]   {state['n_signals']}   "
        f"[bold]Orders:[/bold] {state['n_placed']}\n"
        f"[bold]Trades:[/bold]     {total}   "
        f"([green]{wins}W[/green] / [red]{losses}L[/red])   "
        f"Win-rate: [bold]{win_rate:.1%}[/bold]\n"
        f"[bold]Totaal P&L:[/bold] {pnl_str}"
    )
    return Panel(text, title="[bold cyan]Sessie statistieken[/bold cyan]", border_style="cyan")


def _make_open_table(open_orders: list[dict]) -> Table:
    tbl = Table(
        title="Open posities",
        box=box.SIMPLE_HEAD,
        title_style="bold yellow",
        header_style="bold",
        show_lines=False,
    )
    tbl.add_column("ID",    style="dim",   no_wrap=True)
    tbl.add_column("Sym",   no_wrap=True)
    tbl.add_column("Side",  no_wrap=True)
    tbl.add_column("Status", no_wrap=True)
    tbl.add_column("Entry",  justify="right", no_wrap=True)
    tbl.add_column("SL",     justify="right", style="red")
    tbl.add_column("TP",     justify="right", style="green")
    tbl.add_column("Size",   justify="right", style="dim")

    for o in open_orders:
        tbl.add_row(
            o.get("order_id", "–"),
            o.get("symbol", "–"),
            _side_text(o.get("side", "")),
            _status_text(o.get("status", "")),
            f"{o.get('entry_price', 0):,.2f}",
            f"{o.get('sl_price', 0):,.2f}",
            f"{o.get('tp_price', 0):,.2f}",
            f"{o.get('size', 0):.4f}",
        )

    if not open_orders:
        tbl.add_row("[dim]–[/dim]", "", "", "", "", "", "", "")

    return tbl


def _make_closed_table(closed: list[dict]) -> Table:
    tbl = Table(
        title=f"Recente trades (laatste {_RECENT_N})",
        box=box.SIMPLE_HEAD,
        title_style="bold magenta",
        header_style="bold",
        show_lines=False,
    )
    tbl.add_column("Tijdstip", style="dim", no_wrap=True)
    tbl.add_column("ID",       style="dim", no_wrap=True)
    tbl.add_column("Side",     no_wrap=True)
    tbl.add_column("Entry",    justify="right", no_wrap=True)
    tbl.add_column("Exit",     justify="right", no_wrap=True)
    tbl.add_column("P&L",      justify="right", no_wrap=True)

    for trade in reversed(closed[-_RECENT_N:]):
        closed_at = trade.get("closed_at") or trade.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(closed_at).strftime("%m-%d %H:%M")
        except Exception:
            ts = closed_at[:16] if closed_at else "–"

        tbl.add_row(
            ts,
            trade.get("order_id", "–"),
            _side_text(trade.get("side", "")),
            f"{trade.get('entry_price', 0):,.2f}",
            f"{trade.get('close_price', 0):,.2f}",
            _pnl_text(trade.get("pnl")),
        )

    if not closed:
        tbl.add_row("[dim]–[/dim]", "", "", "", "", "")

    return tbl


def _make_signals_table(signals: list[dict]) -> Table:
    tbl = Table(
        title=f"Recente signalen (laatste {_RECENT_N})",
        box=box.SIMPLE_HEAD,
        title_style="bold blue",
        header_style="bold",
        show_lines=False,
    )
    tbl.add_column("Tijdstip", style="dim", no_wrap=True)
    tbl.add_column("Dir",      no_wrap=True)
    tbl.add_column("Entry",    justify="right", no_wrap=True)
    tbl.add_column("SL",       justify="right", style="red")
    tbl.add_column("TP",       justify="right", style="green")
    tbl.add_column("Filter",   style="dim")

    for sig in reversed(signals[-_RECENT_N:]):
        sig_ts = sig.get("signal_ts") or sig.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(sig_ts).strftime("%m-%d %H:%M")
        except Exception:
            ts = sig_ts[:16] if sig_ts else "–"

        tbl.add_row(
            ts,
            _side_text(sig.get("direction", "")),
            f"{sig.get('entry_price', 0):,.2f}",
            f"{sig.get('sl_price', 0):,.2f}",
            f"{sig.get('tp_price', 0):,.2f}",
            sig.get("filter", "–"),
        )

    if not signals:
        tbl.add_row("[dim]–[/dim]", "", "", "", "", "")

    return tbl


def _make_header(log_path: Path, refresh: int) -> Text:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return Text(
        f"BTC SMC Trader — Live Dashboard  |  {now}  |  refresh {refresh}s",
        style="bold white on dark_blue",
        justify="center",
    )


# ---------------------------------------------------------------------------
# Hoofdloop
# ---------------------------------------------------------------------------

def _render(log_path: Path, refresh: int) -> Layout:
    events = _read_log(log_path)
    state  = _build_state(events)

    layout = Layout()
    layout.split_column(
        Layout(name="header",  size=1),
        Layout(name="stats",   size=7),
        Layout(name="open",    size=min(len(state["open_orders"]) + 5, 12)),
        Layout(name="bottom"),
    )
    layout["header"].update(_make_header(log_path, refresh))
    layout["stats"].update(_make_stats_panel(state, log_path))
    layout["open"].update(_make_open_table(state["open_orders"]))
    layout["bottom"].split_row(
        Layout(_make_closed_table(state["closed"]),  name="closed"),
        Layout(_make_signals_table(state["signals"]), name="signals"),
    )
    return layout


def main() -> None:
    parser = argparse.ArgumentParser(description="Live trading dashboard")
    parser.add_argument("--log",     default=None,  help="Pad naar JSONL logbestand")
    parser.add_argument("--refresh", default=30, type=int, help="Refresh interval in seconden")
    args = parser.parse_args()

    if args.log:
        log_path = Path(args.log)
    else:
        log_path = _find_latest_log()
        if log_path is None:
            # Maak vandaag's lege logbestand pad aan (de bot heeft nog niet gestart)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            log_path = _LOG_DIR / f"trades_{today}.jsonl"
            print(f"Geen logbestand gevonden. Wachten op {log_path} ...")

    console = Console()

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        try:
            while True:
                # Herdetecteer logbestand bij elke refresh (bot kan net gestart zijn)
                if not log_path.exists():
                    new_path = _find_latest_log()
                    if new_path:
                        log_path = new_path

                live.update(_render(log_path, args.refresh))
                time.sleep(args.refresh)
        except KeyboardInterrupt:
            pass

    console.print("\n[dim]Dashboard gestopt.[/dim]")


if __name__ == "__main__":
    main()
