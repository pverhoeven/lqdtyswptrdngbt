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

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


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
        self._enabled  = enabled and bool(bot_token) and bool(chat_id)
        self._url      = _TELEGRAM_API.format(token=bot_token)
        self._chat_id  = chat_id

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Notifier":
        tcfg = cfg.get("notifications", {}).get("telegram", {})
        return cls(
            enabled   = tcfg.get("enabled", False),
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", tcfg.get("bot_token", "")),
            chat_id   = os.environ.get("TELEGRAM_CHAT_ID",   tcfg.get("chat_id",   "")),
        )

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
