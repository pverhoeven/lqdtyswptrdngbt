"""
trading/broker/paper.py — Paper trading broker.

Simuleert orders volledig in geheugen:
- Fill op de volgende candle als entry_price geraakt wordt (limit logica)
- SL/TP bewaking op elke candle
- Fee-simulatie

Swap PaperBroker → BinanceBroker (of andere) om live te gaan.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.trading.broker.base import AbstractBroker, Order, OrderSide, OrderStatus

logger = logging.getLogger(__name__)


@dataclass
class _TrailingState:
    sl_distance:  float
    current_sl:   float
    original_sl:  float
    best_price:   float | None = None
    be_activated: bool         = False


class PaperBroker(AbstractBroker):
    """
    Paper trading broker.

    Parameters
    ----------
    initial_capital : float
        Startkapitaal in USDT.
    fee_pct : float
        Fee als percentage per trade (bijv. 0.1 voor 0.1%).
    max_open : int
        Maximum gelijktijdig open posities.
    """

    def __init__(
        self,
        initial_capital:  float = 10_000.0,
        fee_pct:          float = 0.1,
        max_open:         int   = 1,
        trailing_cfg:     dict | None = None,
        partial_exit_cfg: dict | None = None,
    ) -> None:
        self._capital    = initial_capital
        self._fee        = fee_pct / 100.0
        self._max_open   = max_open
        self._pending:   list[Order] = []
        self._open:      list[Order] = []
        self._closed:    list[Order] = []
        self._last_price: dict[str, float] = {}  # meest recente close per symbool
        self._trailing_cfg: dict = trailing_cfg or {}
        self._trailing: dict[str, _TrailingState] = {}  # order_id → state
        self._partial_cfg: dict = partial_exit_cfg or {}
        self._partial_taken: dict[str, bool] = {}  # order_id → bool

    # ------------------------------------------------------------------
    # AbstractBroker interface
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol:      str,
        side:        OrderSide,
        entry_price: float,
        sl_price:    float,
        tp_price:    float,
        risk_amount: float,
    ) -> Order:
        """
        Registreer een limiet-order.
        Wordt gevuld zodra de prijs entry_price raakt in on_candle().
        """
        if len(self._open) >= self._max_open:
            raise ValueError(
                f"Max open posities ({self._max_open}) bereikt. "
                "Sluit een positie voor je een nieuwe opent."
            )

        sl_dist = abs(entry_price - sl_price)
        if sl_dist == 0:
            raise ValueError("SL-afstand is nul — ongeldige order.")

        size = risk_amount / sl_dist

        order = Order(
            order_id    = str(uuid.uuid4())[:8],
            symbol      = symbol,
            side        = side,
            entry_price = entry_price,
            sl_price    = sl_price,
            tp_price    = tp_price,
            size        = size,
            status      = OrderStatus.PENDING,
        )
        self._pending.append(order)
        logger.info(
            "Order geplaatst [%s] %s %s  entry=%.2f  sl=%.2f  tp=%.2f  size=%.6f",
            order.order_id, symbol, side.name,
            entry_price, sl_price, tp_price, size,
        )
        return order

    def on_candle(
        self,
        symbol:    str,
        ohlc_row:  pd.Series,
        timestamp: pd.Timestamp,
    ) -> list[Order]:
        """
        Verwerk één gesloten candle:
        1. Probeer pending orders te vullen
        2. Controleer SL/TP van open posities
        Retourneert lijst van orders die gesloten zijn.
        """
        self._last_price[symbol] = float(ohlc_row["close"])
        low   = float(ohlc_row["low"])
        high  = float(ohlc_row["high"])
        closed_this_candle: list[Order] = []

        # --- Fill pending orders ---
        still_pending = []
        for order in self._pending:
            if order.symbol != symbol:
                still_pending.append(order)
                continue

            filled = (
                (order.side == OrderSide.LONG  and low  <= order.entry_price) or
                (order.side == OrderSide.SHORT and high >= order.entry_price)
            )
            if filled:
                order.status    = OrderStatus.OPEN
                order.filled_at = timestamp
                entry_cost      = order.entry_price * order.size * self._fee
                self._capital  -= entry_cost
                self._open.append(order)
                # Initialiseer trailing state bij fill
                if self._trailing_cfg:
                    sl_dist = abs(order.entry_price - order.sl_price)
                    self._trailing[order.order_id] = _TrailingState(
                        sl_distance = sl_dist,
                        current_sl  = order.sl_price,
                        original_sl = order.sl_price,
                    )
                logger.info(
                    "Order gevuld  [%s] %s @ %.2f",
                    order.order_id, order.symbol, order.entry_price,
                )
            else:
                still_pending.append(order)

        self._pending = still_pending

        # --- Controleer SL/TP van open posities ---
        still_open = []
        for order in self._open:
            if order.symbol != symbol:
                still_open.append(order)
                continue

            # Update trailing/breakeven SL
            if self._trailing_cfg and order.order_id in self._trailing:
                order.sl_price = self._update_trailing(order, low, high)

            # Partial exit (50% bij 1R, rest trailing)
            if self._partial_cfg and order.order_id not in self._partial_taken:
                self._check_partial_exit(order, low, high, timestamp)

            outcome = _check_sl_tp(order, low, high)
            if outcome:
                exit_price  = order.tp_price if outcome == "win" else order.sl_price
                exit_cost   = exit_price * order.size * self._fee
                raw_pnl     = _calc_pnl(order, exit_price)
                net_pnl     = raw_pnl - exit_cost

                order.status      = OrderStatus.CLOSED
                order.closed_at   = timestamp
                order.close_price = exit_price
                order.pnl         = net_pnl
                self._capital    += net_pnl
                self._closed.append(order)
                closed_this_candle.append(order)

                # Trailing SL exit detectie — vóór pop zodat state nog beschikbaar is
                if outcome == "loss":
                    ts = self._trailing.get(order.order_id)
                    if ts and order.sl_price != ts.original_sl:
                        slippage = exit_price - order.sl_price  # paper: altijd 0.00
                        logger.info(
                            "TRAILING_SL_EXIT [%s] planned_sl=%.2f  fill=%.2f  slippage=%.4f",
                            order.order_id, order.sl_price, exit_price, slippage,
                        )

                self._trailing.pop(order.order_id, None)
                self._partial_taken.pop(order.order_id, None)

                logger.info(
                    "Positie gesloten [%s] %s %s @ %.2f  P&L: %.2f USDT",
                    order.order_id, order.symbol, outcome,
                    exit_price, net_pnl,
                )
            else:
                still_open.append(order)

        self._open = still_open
        return closed_this_candle

    def _update_trailing(self, order: Order, low: float, high: float) -> float:
        """
        Beweeg SL naar breakeven en/of trail achter beste prijs.
        Retourneert de nieuwe (mogelijk ongewijzigde) SL prijs.
        """
        state = self._trailing[order.order_id]
        cfg   = self._trailing_cfg
        entry = order.entry_price
        dist  = state.sl_distance
        be_r  = cfg.get("breakeven_at_r", 0.0)
        trail_r = cfg.get("trail_after_r")
        trail_s = cfg.get("trail_step_r", 0.5)

        if dist == 0:
            return state.current_sl

        if order.side == OrderSide.LONG:
            favorable = high
            if state.best_price is None or favorable > state.best_price:
                state.best_price = favorable
            r_mult = (state.best_price - entry) / dist
        else:
            favorable = low
            if state.best_price is None or favorable < state.best_price:
                state.best_price = favorable
            r_mult = (entry - state.best_price) / dist

        if be_r > 0 and r_mult >= be_r and not state.be_activated:
            state.current_sl   = entry
            state.be_activated = True
            logger.info(
                "Breakeven geactiveerd [%s] SL → %.2f (was %.2f)",
                order.order_id, entry, order.sl_price,
            )

        if trail_r and r_mult >= trail_r:
            if order.side == OrderSide.LONG:
                new_sl = state.best_price - dist * trail_s
                state.current_sl = max(state.current_sl, new_sl)
            else:
                new_sl = state.best_price + dist * trail_s
                state.current_sl = min(state.current_sl, new_sl)

        return state.current_sl

    def _check_partial_exit(
        self,
        order:     Order,
        low:       float,
        high:      float,
        timestamp: pd.Timestamp,
    ) -> None:
        """
        Sluit exit_fraction van de positie zodra exit_r winst bereikt is.
        Optioneel: verschuif SL naar breakeven na de partial exit.
        """
        exit_r     = self._partial_cfg.get("exit_r", 1.0)
        fraction   = self._partial_cfg.get("exit_fraction", 0.5)
        move_to_be = self._partial_cfg.get("move_sl_to_be", True)

        # Gebruik originele sl-afstand (uit trailing state of huidige order)
        if order.order_id in self._trailing:
            sl_dist = self._trailing[order.order_id].sl_distance
        else:
            sl_dist = abs(order.entry_price - order.sl_price)

        if sl_dist == 0:
            return

        if order.side == OrderSide.LONG:
            exit_level = order.entry_price + exit_r * sl_dist
            if high < exit_level:
                return
            exit_price = exit_level
            raw_pnl    = (exit_price - order.entry_price) * order.size * fraction
        else:
            exit_level = order.entry_price - exit_r * sl_dist
            if low > exit_level:
                return
            exit_price = exit_level
            raw_pnl    = (order.entry_price - exit_price) * order.size * fraction

        partial_size = order.size * fraction
        fee          = exit_price * partial_size * self._fee
        net_pnl      = raw_pnl - fee

        order.size             -= partial_size
        self._capital          += net_pnl
        self._partial_taken[order.order_id] = True

        if move_to_be:
            order.sl_price = order.entry_price
            if order.order_id in self._trailing:
                self._trailing[order.order_id].current_sl   = order.entry_price
                self._trailing[order.order_id].be_activated = True

        logger.info(
            "Partial exit [%s] %.0f%% @ %.2f  P&L: %.2f USDT  SL → %.2f",
            order.order_id, fraction * 100, exit_price, net_pnl, order.sl_price,
        )

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        orders = self._open + self._pending
        return [o for o in orders if symbol is None or o.symbol == symbol]

    def closed_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._closed if symbol is None or o.symbol == symbol]

    def equity(self) -> float:
        """Cash + mark-to-market van open posities (mark = meest recente close)."""
        unrealized = sum(
            _calc_pnl(o, self._last_price.get(o.symbol, o.entry_price))
            for o in self._open
        )
        return self._capital + unrealized

    def cancel_order(self, order_id: str) -> bool:
        before = len(self._pending)
        self._pending = [o for o in self._pending if o.order_id != order_id]
        return len(self._pending) < before

    # ------------------------------------------------------------------
    # State persistentie
    # ------------------------------------------------------------------

    def save_state(self, path: Path) -> None:
        """Sla de huidige state op als JSON (open/pending orders + kapitaal)."""
        state = {
            "capital":       self._capital,
            "pending":       [_order_to_dict(o) for o in self._pending],
            "open":          [_order_to_dict(o) for o in self._open],
            "partial_taken": list(self._partial_taken.keys()),
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info("State opgeslagen: %s", path)

    def load_state(self, path: Path) -> None:
        """
        Laad eerder opgeslagen state bij herstart.
        Overschrijft huidige pending/open orders en kapitaal.
        """
        path = Path(path)
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self._capital        = data["capital"]
        self._pending        = [_order_from_dict(o) for o in data.get("pending", [])]
        self._open           = [_order_from_dict(o) for o in data.get("open", [])]
        self._partial_taken  = {oid: True for oid in data.get("partial_taken", [])}
        logger.info(
            "State geladen: kapitaal=%.2f  open=%d  pending=%d",
            self._capital, len(self._open), len(self._pending),
        )

    # ------------------------------------------------------------------
    # Extra info
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Beknopte samenvatting van de paper trading sessie."""
        closed = self._closed
        wins   = [o for o in closed if o.pnl > 0]
        losses = [o for o in closed if o.pnl <= 0]
        total_pnl = sum(o.pnl for o in closed)

        return {
            "equity":        self._capital,
            "total_trades":  len(closed),
            "open_trades":   len(self._open),
            "pending_orders":len(self._pending),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      len(wins) / len(closed) if closed else 0.0,
            "total_pnl":     total_pnl,
            "profit_factor": (
                sum(o.pnl for o in wins) / abs(sum(o.pnl for o in losses))
                if losses and sum(o.pnl for o in losses) != 0
                else float("inf")
            ),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_sl_tp(order: Order, low: float, high: float) -> str | None:
    """Retourneert 'win', 'loss', of None."""
    if order.side == OrderSide.LONG:
        if low  <= order.sl_price: return "loss"
        if high >= order.tp_price: return "win"
    else:
        if high >= order.sl_price: return "loss"
        if low  <= order.tp_price: return "win"
    return None


def _calc_pnl(order: Order, exit_price: float) -> float:
    if order.side == OrderSide.LONG:
        return (exit_price - order.entry_price) * order.size
    return (order.entry_price - exit_price) * order.size


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
        "close_price": o.close_price,
        "pnl":         o.pnl,
    }


def _order_from_dict(d: dict) -> Order:
    return Order(
        order_id    = d["order_id"],
        symbol      = d["symbol"],
        side        = OrderSide[d["side"]],
        entry_price = d["entry_price"],
        sl_price    = d["sl_price"],
        tp_price    = d["tp_price"],
        size        = d["size"],
        status      = OrderStatus[d["status"]],
        filled_at   = pd.Timestamp(d["filled_at"]) if d.get("filled_at") else None,
        close_price = d.get("close_price", 0.0),
        pnl         = d.get("pnl", 0.0),
    )