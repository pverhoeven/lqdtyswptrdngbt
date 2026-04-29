"""
trading/order_manager.py — Beheert de volledige trading-levenscyclus.

Verantwoordelijkheid:
- Ontvangt SweepSignal van de detector
- Berekent positiegrootte (risk_pct van kapitaal)
- Stuurt order naar broker
- Bewaakt circuit breaker (N verliezen, dagelijks verlies, max drawdown)
- Schrijft elk signaal en elke trade naar:
    * Terminal (stdout)
    * JSON logboek (persistent)
- Houdt sessie-statistieken bij
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum, auto
from pathlib import Path

import pandas as pd

from src.signals.detector import SweepSignal
from src.trading.broker.base import AbstractBroker, Order, OrderSide, OrderStatus
from src.trading.funding_rate import FundingRateFilter

logger = logging.getLogger(__name__)

_LOG_DIR = Path("logs")


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class _CBState(Enum):
    CLOSED    = auto()   # normaal handelen
    DAY_PAUSE = auto()   # pauze tot einde UTC dag
    HARD_STOP = auto()   # harde stop — herstart vereist


@dataclass
class CircuitBreakerState:
    """
    Bijhoudt circuit breaker toestand.

    Triggers:
    - max_consecutive opeenvolgende verliezen → DAY_PAUSE tot UTC 00:00
    - dagelijks verlies > max_daily_loss_pct% van startkapitaal → DAY_PAUSE
    - drawdown > max_drawdown_pct% van startkapitaal → HARD_STOP
    """
    max_consecutive:    int
    max_daily_loss_pct: float   # % van startkapitaal
    max_drawdown_pct:   float   # % van startkapitaal
    start_capital:      float

    _state:              _CBState  = field(init=False)
    _reason:             str       = field(init=False, default="")
    _consecutive_losses: int       = field(init=False, default=0)
    _daily_pnl:          float     = field(init=False, default=0.0)
    _daily_date:         date      = field(init=False)
    _peak_equity:        float     = field(init=False)

    def __post_init__(self) -> None:
        self._state       = _CBState.CLOSED
        self._peak_equity = self.start_capital
        self._daily_date  = datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------

    def is_open(self) -> bool:
        """True als nieuwe orders geblokkeerd zijn."""
        self._check_daily_reset()
        return self._state in (_CBState.DAY_PAUSE, _CBState.HARD_STOP)

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def is_hard_stop(self) -> bool:
        return self._state == _CBState.HARD_STOP

    def record_trade(self, pnl: float, equity: float) -> str | None:
        """
        Verwerk een gesloten trade en update circuit breaker state.

        Returns
        -------
        str | None
            Reden als de breaker net geopend is, anders None.
        """
        self._check_daily_reset()
        self._daily_pnl += pnl

        if pnl > 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

        self._peak_equity = max(self._peak_equity, equity)

        drawdown_pct   = (self._peak_equity - equity) / self.start_capital * 100.0
        daily_loss_pct = max(0.0, -self._daily_pnl / self.start_capital * 100.0)

        # Harde stop: drawdown overschrijdt limiet
        if drawdown_pct >= self.max_drawdown_pct and self._state != _CBState.HARD_STOP:
            self._state  = _CBState.HARD_STOP
            self._reason = (
                f"Drawdown {drawdown_pct:.1f}% ≥ {self.max_drawdown_pct}% — "
                "HARDE STOP (herstart vereist)"
            )
            logger.critical("CIRCUIT BREAKER HARD STOP: %s", self._reason)
            return self._reason

        if self._state == _CBState.CLOSED:
            if daily_loss_pct >= self.max_daily_loss_pct:
                self._state  = _CBState.DAY_PAUSE
                self._reason = (
                    f"Dagelijks verlies {daily_loss_pct:.1f}% ≥ "
                    f"{self.max_daily_loss_pct}% — pauze tot UTC 00:00"
                )
                logger.warning("CIRCUIT BREAKER: %s", self._reason)
                return self._reason

            if self._consecutive_losses >= self.max_consecutive:
                self._state  = _CBState.DAY_PAUSE
                self._reason = (
                    f"{self._consecutive_losses} opeenvolgende verliezen — "
                    "pauze tot einde UTC dag"
                )
                logger.warning("CIRCUIT BREAKER: %s", self._reason)
                return self._reason

        return None

    def _check_daily_reset(self) -> None:
        """Reset dagelijkse tellers bij nieuwe UTC dag."""
        today = datetime.now(timezone.utc).date()
        if self._daily_date != today:
            self._daily_date        = today
            self._daily_pnl         = 0.0
            self._consecutive_losses = 0
            if self._state == _CBState.DAY_PAUSE:
                self._state  = _CBState.CLOSED
                self._reason = ""
                logger.info("Circuit breaker gereset (nieuwe UTC dag).")


# ---------------------------------------------------------------------------
# Account-niveau circuit breaker (gedeeld over alle coins)
# ---------------------------------------------------------------------------

@dataclass
class AccountCircuitBreaker:
    """
    Gedeelde circuit breaker voor multi-coin trading.

    Aggregeert P&L van alle coins en beschermt het totale account.
    Triggers:
    - Totaal dagelijks verlies > max_daily_loss_pct% → DAY_PAUSE alle coins
    - Totale drawdown > max_drawdown_pct% → HARD_STOP alle coins

    Consecutive-losses wordt hier niet bijgehouden; dat is per-coin verantwoordelijk-
    heid van de individuele CircuitBreakerState.
    """
    max_daily_loss_pct: float
    max_drawdown_pct:   float
    start_capital:      float

    _state:       _CBState = field(init=False)
    _reason:      str      = field(init=False, default="")
    _daily_pnl:   float    = field(init=False, default=0.0)
    _daily_date:  date     = field(init=False)
    _peak_equity: float    = field(init=False)

    def __post_init__(self) -> None:
        self._state       = _CBState.CLOSED
        self._peak_equity = self.start_capital
        self._daily_date  = datetime.now(timezone.utc).date()

    def is_open(self) -> bool:
        """True als alle coins geblokkeerd zijn."""
        self._check_daily_reset()
        return self._state in (_CBState.DAY_PAUSE, _CBState.HARD_STOP)

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def is_hard_stop(self) -> bool:
        return self._state == _CBState.HARD_STOP

    def record_trade(self, pnl: float, equity: float) -> str | None:
        """
        Verwerk een gesloten trade (van welke coin dan ook).

        Returns trigger-bericht als de breaker net geopend is, anders None.
        """
        self._check_daily_reset()
        self._daily_pnl  += pnl
        self._peak_equity = max(self._peak_equity, equity)

        drawdown_pct   = (self._peak_equity - equity) / self.start_capital * 100.0
        daily_loss_pct = max(0.0, -self._daily_pnl / self.start_capital * 100.0)

        if drawdown_pct >= self.max_drawdown_pct and self._state != _CBState.HARD_STOP:
            self._state  = _CBState.HARD_STOP
            self._reason = (
                f"[ACCOUNT] Drawdown {drawdown_pct:.1f}% ≥ "
                f"{self.max_drawdown_pct}% — HARDE STOP alle coins"
            )
            logger.critical("ACCOUNT CIRCUIT BREAKER HARD STOP: %s", self._reason)
            return self._reason

        if self._state == _CBState.CLOSED and daily_loss_pct >= self.max_daily_loss_pct:
            self._state  = _CBState.DAY_PAUSE
            self._reason = (
                f"[ACCOUNT] Dagelijks verlies {daily_loss_pct:.1f}% ≥ "
                f"{self.max_daily_loss_pct}% — alle coins gepauzeerd tot UTC 00:00"
            )
            logger.warning("ACCOUNT CIRCUIT BREAKER: %s", self._reason)
            return self._reason

        return None

    def _check_daily_reset(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._daily_date != today:
            self._daily_date = today
            self._daily_pnl  = 0.0
            if self._state == _CBState.DAY_PAUSE:
                self._state  = _CBState.CLOSED
                self._reason = ""
                logger.info("Account circuit breaker gereset (nieuwe UTC dag).")


# ---------------------------------------------------------------------------
# Sessie statistieken
# ---------------------------------------------------------------------------

@dataclass
class SessionStats:
    signals_detected: int   = 0
    orders_placed:    int   = 0
    orders_filled:    int   = 0
    wins:             int   = 0
    losses:           int   = 0
    total_pnl:        float = 0.0
    start_capital:    float = 0.0
    current_capital:  float = 0.0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    @property
    def total_return(self) -> float:
        if self.start_capital == 0:
            return 0.0
        return (self.current_capital - self.start_capital) / self.start_capital


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------

class OrderManager:
    """
    Koppelt detector-signalen aan broker-orders en verzorgt logging.

    Parameters
    ----------
    broker : AbstractBroker
    symbol : str
    risk_pct : float
    max_open : int
    log_dir : Path
    cb_cfg : dict | None
        Circuit breaker config (uit risk.circuit_breaker in config.yaml).
        None = circuit breaker uitgeschakeld.
    notifier : Notifier | None
        Optionele Telegram notifier.
    """

    def __init__(
        self,
        broker:          AbstractBroker,
        symbol:          str,
        risk_pct:        float = 1.0,
        max_open:        int   = 1,
        log_dir:         Path  = _LOG_DIR,
        cb_cfg:          dict | None = None,
        notifier                     = None,
        funding_filter:  FundingRateFilter | None = None,
        account_cb:      "AccountCircuitBreaker | None" = None,
    ) -> None:
        self._broker          = broker
        self._symbol          = symbol
        self._risk_pct        = risk_pct / 100.0
        self._max_open        = max_open
        self._notifier        = notifier
        self._funding_filter  = funding_filter
        self._account_cb      = account_cb

        self._log_dir  = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / f"trades_{_today()}.jsonl"

        initial_equity = broker.equity()
        self._stats = SessionStats(
            start_capital   = initial_equity,
            current_capital = initial_equity,
        )

        if cb_cfg:
            self._cb: CircuitBreakerState | None = CircuitBreakerState(
                max_consecutive    = cb_cfg.get("max_consecutive_losses", 3),
                max_daily_loss_pct = cb_cfg.get("max_daily_loss_pct", 3.0),
                max_drawdown_pct   = cb_cfg.get("max_drawdown_pct", 10.0),
                start_capital      = initial_equity,
            )
        else:
            self._cb = None

        logger.info(
            "OrderManager gestart: %s  kapitaal=%.0f  risk=%.1f%%  "
            "circuit_breaker=%s",
            symbol, initial_equity, risk_pct,
            "aan" if self._cb else "uit",
        )

    # ------------------------------------------------------------------
    # Hoofdinterface
    # ------------------------------------------------------------------

    def on_signal(self, signal: SweepSignal) -> Order | None:
        """
        Verwerk een SweepSignal.

        Plaatst een order als circuit breaker gesloten is en er ruimte is.
        """
        self._stats.signals_detected += 1
        self._log_event("signal", _signal_to_dict(signal))
        self._print_signal(signal)

        # Circuit breaker check (per-coin)
        if self._cb is not None and self._cb.is_open():
            logger.info(
                "Circuit breaker open — signaal overgeslagen: %s",
                self._cb.reason,
            )
            return None

        # Account-niveau circuit breaker check (gedeeld over alle coins)
        if self._account_cb is not None and self._account_cb.is_open():
            logger.info(
                "Account circuit breaker open — signaal overgeslagen: %s",
                self._account_cb.reason,
            )
            return None

        # Funding rate filter
        if self._funding_filter is not None:
            if not self._funding_filter.allows(signal.direction):
                logger.info(
                    "Funding rate filter actief — signaal overgeslagen "
                    "(rate=%.4f%%, richting=%s)",
                    (self._funding_filter.current_rate or 0) * 100,
                    signal.direction,
                )
                return None

        n_open = len(self._broker.open_orders(self._symbol))
        if n_open >= self._max_open:
            logger.debug(
                "Signaal overgeslagen: max open posities (%d) bereikt.", self._max_open
            )
            return None

        capital     = self._broker.equity()
        risk_amount = capital * self._risk_pct
        side        = OrderSide.LONG if signal.direction == "long" else OrderSide.SHORT

        try:
            order = self._broker.place_order(
                symbol      = self._symbol,
                side        = side,
                entry_price = signal.entry_price,
                sl_price    = signal.sl_price,
                tp_price    = signal.tp_price,
                risk_amount = risk_amount,
            )
            self._stats.orders_placed += 1
            self._log_event("order_placed", _order_to_dict(order))

            if self._notifier:
                self._notifier.notify_trade_opened(order, self._broker.equity())

            return order

        except ValueError as exc:
            logger.warning("Order geweigerd: %s", exc)
            return None

    def on_candle(
        self,
        ohlc_row:  pd.Series,
        timestamp: pd.Timestamp,
    ) -> list[Order]:
        """
        Geef broker de nieuwe candle-data.
        Logt gesloten trades en werkt circuit breaker bij.
        """
        closed = self._broker.on_candle(self._symbol, ohlc_row, timestamp)

        for order in closed:
            self._stats.current_capital = self._broker.equity()
            self._stats.total_pnl      += order.pnl

            if order.pnl > 0:
                self._stats.wins += 1
            else:
                self._stats.losses += 1

            self._log_event("trade_closed", _order_to_dict(order))
            self._print_trade_closed(order)

            if self._notifier:
                self._notifier.notify_trade_closed(order, self._broker.equity())

            if self._cb is not None:
                trigger_msg = self._cb.record_trade(order.pnl, self._broker.equity())
                if trigger_msg:
                    print(f"\n[CIRCUIT BREAKER] {trigger_msg}")
                    if self._notifier:
                        self._notifier.notify_circuit_breaker(trigger_msg)

            if self._account_cb is not None:
                acb_msg = self._account_cb.record_trade(order.pnl, self._broker.equity())
                if acb_msg:
                    print(f"\n[ACCOUNT CIRCUIT BREAKER] {acb_msg}")
                    if self._notifier:
                        self._notifier.notify_circuit_breaker(acb_msg)

        return closed

    def send_heartbeat(self) -> None:
        """Stuur een heartbeat Telegram-bericht met de huidige sessie-status."""
        if not self._notifier:
            return
        s = self._stats
        self._notifier.notify_heartbeat(
            equity         = self._broker.equity(),
            open_positions = len(self._broker.open_orders(self._symbol)),
            wins           = s.wins,
            losses         = s.losses,
        )

    def open_count(self) -> int:
        """Aantal open posities voor dit symbool."""
        return len(self._broker.open_orders(self._symbol))

    def print_stats(self) -> None:
        s = self._stats
        sep = "─" * 45
        print(f"\n{sep}")
        print(f"  SESSIE STATISTIEKEN")
        print(sep)
        print(f"  Kapitaal:        {s.start_capital:.0f} → {s.current_capital:.0f} USDT")
        print(f"  Totaal return:   {s.total_return:+.1%}")
        print(f"  Signalen:        {s.signals_detected}")
        print(f"  Orders:          {s.orders_placed}")
        print(f"  Trades:          {s.wins + s.losses}")
        print(f"  Win rate:        {s.win_rate:.1%}")
        print(f"  Totaal P&L:      {s.total_pnl:+.2f} USDT")
        print(sep)

    @property
    def stats(self) -> SessionStats:
        return self._stats

    @property
    def log_path(self) -> Path:
        return self._log_path

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_event(self, event_type: str, data: dict) -> None:
        entry = {
            "event":     event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _print_signal(self, signal: SweepSignal) -> None:
        direction_arrow = "▲ LONG" if signal.direction == "long" else "▼ SHORT"
        regime_str = (
            "bullish" if signal.regime is True
            else "bearish" if signal.regime is False
            else "onbekend"
        )
        print(
            f"\n[SIGNAAL] {direction_arrow}  {signal.timestamp.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"  Entry:   {signal.entry_price:>10.2f}  |  "
            f"SL: {signal.sl_price:>10.2f}  |  "
            f"TP: {signal.tp_price:>10.2f}\n"
            f"  Liq lvl: {signal.liq_level:>10.2f}  |  "
            f"R:R: 1:{signal.risk_reward:.1f}  |  "
            f"Regime: {regime_str}  |  "
            f"Filter: {signal.filter_str}"
        )

    def _print_trade_closed(self, order: Order) -> None:
        outcome = "WIN ✓" if order.pnl > 0 else "LOSS ✗"
        print(
            f"\n[TRADE]   {outcome}  [{order.order_id}]  "
            f"{order.side.name}\n"
            f"  Entry:   {order.entry_price:>10.2f}  →  "
            f"Exit: {order.close_price:>10.2f}\n"
            f"  P&L:     {order.pnl:>+10.2f} USDT  |  "
            f"Kapitaal: {self._broker.equity():.2f} USDT"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal_to_dict(s: SweepSignal) -> dict:
    return {
        "signal_ts":   s.timestamp.isoformat(),
        "direction":   s.direction,
        "entry_price": s.entry_price,
        "sl_price":    s.sl_price,
        "tp_price":    s.tp_price,
        "liq_level":   s.liq_level,
        "regime":      s.regime,
        "filter":      s.filter_str,
    }


def _order_to_dict(o: Order) -> dict:
    return {
        "order_id":    o.order_id,
        "symbol":      o.symbol,
        "side":        o.side.name,
        "entry_price": o.entry_price,
        "sl_price":    o.sl_price,
        "tp_price":    o.tp_price,
        "size":        o.size,
        "status":      o.status.name,
        "filled_at":   o.filled_at.isoformat() if o.filled_at else None,
        "closed_at":   o.closed_at.isoformat() if o.closed_at else None,
        "close_price": o.close_price,
        "pnl":         o.pnl,
    }


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")
