"""
notifications/notifier.py — Telegram notificaties voor trade events.

Gebruik:
    notifier = Notifier.from_cfg(cfg)
    notifier.send("Bot gestart")

Als enabled=False of token leeg: alle methoden zijn no-ops.
Credentials via environment variables (OKX_API_KEY, TELEGRAM_BOT_TOKEN, etc.),
nooit in config.yaml committen.
"""

from __future__ import annotations

import logging
import os

import requests

from src.trading.broker.base import Order, OrderSide

logger = logging.getLogger(__name__)

_TELEGRAM_API       = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"


def _fmt_price(v: float) -> str:
    if v >= 10_000:
        return f"${v:,.0f}"
    elif v >= 100:
        return f"${v:,.1f}"
    else:
        return f"${v:,.2f}"


class Notifier:
    """
    Stuurt berichten via Telegram Bot API.

    Parameters
    ----------
    enabled : bool
    bot_token : str
    chat_id : str
    """

    def __init__(
        self,
        enabled:   bool = False,
        bot_token: str  = "",
        chat_id:   str  = "",
    ) -> None:
        self._enabled   = enabled and bool(bot_token) and bool(chat_id)
        self._url       = _TELEGRAM_API.format(token=bot_token)
        self._photo_url = _TELEGRAM_PHOTO_API.format(token=bot_token)
        self._chat_id   = chat_id

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Notifier":
        tcfg = cfg.get("notifications", {}).get("telegram", {})
        return cls(
            enabled   = tcfg.get("enabled", False),
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", tcfg.get("bot_token", "")),
            chat_id   = os.environ.get("TELEGRAM_CHAT_ID",   tcfg.get("chat_id",   "")),
        )

    def send_photo(self, image_bytes: bytes, caption: str) -> None:
        """Stuur een PNG-afbeelding met bijschrift. Falen wordt gelogd, nooit gegooien."""
        if not self._enabled:
            return
        try:
            requests.post(
                self._photo_url,
                data={"chat_id": self._chat_id, "caption": caption},
                files={"photo": ("chart.png", image_bytes, "image/png")},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Telegram foto notificatie mislukt: %s", exc)

    # ------------------------------------------------------------------
    # Algemeen
    # ------------------------------------------------------------------

    def send(self, text: str) -> None:
        """Stuur een vrij-tekst bericht. Falen wordt gelogd, nooit gegooien."""
        if not self._enabled:
            return
        try:
            requests.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text},
                timeout=5,
            )
        except Exception as exc:
            logger.warning("Telegram notificatie mislukt: %s", exc)

    # ------------------------------------------------------------------
    # Trade events
    # ------------------------------------------------------------------

    def notify_trade_opened(self, order: Order, equity: float) -> None:
        direction = "▲ LONG" if order.side == OrderSide.LONG else "▼ SHORT"
        self.send(
            f"[TRADE GEOPEND] {direction}\n"
            f"Entry: {order.entry_price:.2f}  SL: {order.sl_price:.2f}  TP: {order.tp_price:.2f}\n"
            f"Size: {order.size:.6f}\n"
            f"Equity: {equity:.2f} USDT"
        )

    def notify_trade_closed(self, order: Order, equity: float) -> None:
        outcome = "WIN ✓" if order.pnl > 0 else "LOSS ✗"
        self.send(
            f"[TRADE GESLOTEN] {outcome}\n"
            f"P&L: {order.pnl:+.2f} USDT\n"
            f"Exit: {order.close_price:.2f}\n"
            f"Equity: {equity:.2f} USDT"
        )

    def notify_circuit_breaker(self, reason: str) -> None:
        self.send(f"[CIRCUIT BREAKER] {reason}")

    def notify_started(self, symbol: str, filter_str: str, equity: float) -> None:
        self.send(
            f"[BOT GESTART]\n"
            f"Symbool: {symbol}  Filter: {filter_str}\n"
            f"Kapitaal: {equity:.2f} USDT"
        )

    def notify_stopped(self, equity: float) -> None:
        self.send(f"[BOT GESTOPT]\nEindkapitaal: {equity:.2f} USDT")

    def notify_heartbeat(
        self,
        equity:         float,
        open_positions: int,
        wins:           int,
        losses:         int,
    ) -> None:
        total = wins + losses
        wr = f"{wins/total:.1%}" if total > 0 else "—"
        self.send(
            f"[HEARTBEAT]\n"
            f"Equity: {equity:.2f} USDT\n"
            f"Open posities: {open_positions}\n"
            f"Sessie: {wins}W / {losses}L  (WR: {wr})"
        )

    def notify_error(self, description: str) -> None:
        self.send(f"[FOUT] {description}")

    # ------------------------------------------------------------------
    # Dagelijks rapport
    # ------------------------------------------------------------------

    def notify_daily_report(
        self,
        setups: list,
        date_str: str,
    ) -> None:
        """
        Stuur het dagelijkse SMC aankomende-setups rapport.

        Parameters
        ----------
        setups : list[DailySetup]
            Gerangschikte setups uit run_daily_scan().
        date_str : str
            Datum-string voor de header, bijv. "ma 5 mei 2026".
        """
        lines = [f"📡 SMC AANKOMENDE SETUPS — {date_str}", ""]

        sep = "─" * 32

        if not setups:
            lines.append("Geen setups gevonden binnen bereik vandaag.")
            lines.append(sep)
            lines.append("Analyse: Binance 1H  |  Executie: OKX XPERP")
            self.send("\n".join(lines))
            return

        # Groepeer per symbool
        symbols_seen: list[str] = []
        by_symbol: dict[str, list] = {}
        for s in setups:
            if s.symbol not in by_symbol:
                by_symbol[s.symbol] = []
                symbols_seen.append(s.symbol)
            by_symbol[s.symbol].append(s)

        for symbol in symbols_seen:
            coin_setups = by_symbol[symbol]
            current_price = coin_setups[0].current_price
            xperp = coin_setups[0].xperp
            lines.append(sep)
            lines.append(f"{symbol}  ({_fmt_price(current_price)})")
            lines.append(f"OKX: {xperp}")
            lines.append("")

            for s in coin_setups:
                arrow  = "▲ LONG" if s.direction == "long" else "▼ SHORT"
                stars  = "⭐" * s.stars
                sign   = "-" if s.direction == "long" else "+"
                dist   = f"{sign}{s.distance_pct:.1%}"
                rr_val = abs(s.tp - s.entry_zone) / max(abs(s.entry_zone - s.sl), 1e-8)
                setup_type = getattr(s, "setup_type", "EQL/EQH")
                tag    = s.fase if setup_type == "EQL/EQH" else setup_type

                lines.append(f"{arrow}  {stars}  |  {tag}  |  {s.fase_label}")

                if setup_type == "EQL/EQH":
                    # EQL/EQH-specifiek: zone, sweep, BoS
                    zone_lbl = "EQL" if s.direction == "long" else "EQH"
                    lines.append(
                        f"Zone: {_fmt_price(s.zone_level)} ({s.n_equal}× equal  |  {dist})"
                    )
                    if s.fase in ("FASE 2", "FASE 3"):
                        sweep_lbl = "Sweep low" if s.direction == "long" else "Sweep high"
                        lines.append(f"{sweep_lbl}: {_fmt_price(s.sweep_low)}")
                    if s.fase == "FASE 3" and s.bos_level > 0:
                        lines.append(f"BoS niveau: {_fmt_price(s.bos_level)}")
                else:
                    # FVG / OB / BOS / FIB: toon confluences
                    for conf in s.confluences:
                        lines.append(f"  {conf}")

                # Entry / SL / TP
                lines.append(
                    f"Entry: ~{_fmt_price(s.entry_zone)}  "
                    f"SL: {_fmt_price(s.sl)}  "
                    f"TP: {_fmt_price(s.tp)}  "
                    f"RR: 1:{rr_val:.1f}"
                )
                lines.append("")

        lines.append(sep)
        lines.append("Analyse: Binance 1H  |  Executie: OKX XPERP")
        lines.append("Setups: EQL/EQH · FVG · OB · BoS · FIB  |  Entry na bevestiging")

        self.send("\n".join(lines))

    # ------------------------------------------------------------------
    # Sweep scanner
    # ------------------------------------------------------------------

    def notify_sweep_detected(
        self,
        symbol:      str,
        direction:   str,
        entry_price: float,
        liq_level:   float,
        sl_price:    float,
        tp_price:    float,
        timestamp:   "pd.Timestamp",
    ) -> None:
        arrow    = "▲ LONG"  if direction == "long"  else "▼ SHORT"
        liq_type = "SSL gesweept" if direction == "long" else "BSL gesweept"
        self.send(
            f"[SWEEP SCANNER] {arrow}\n"
            f"Pair: {symbol}\n"
            f"Liq niveau: {liq_level:.4f}  ({liq_type})\n"
            f"Entry: {entry_price:.4f}  SL: {sl_price:.4f}  TP: {tp_price:.4f}\n"
            f"Candle: {timestamp.strftime('%Y-%m-%d %H:%M')} UTC"
        )
