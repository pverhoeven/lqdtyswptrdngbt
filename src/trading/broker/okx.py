"""
trading/broker/okx.py — OKX live trading broker via python-okx SDK.

Implementeert AbstractBroker voor BTC-USDT-SWAP perpetual swap.

Aandachtspunten:
- Contract-grootte: 1 BTC-USDT-SWAP = 0.01 BTC → omzetting verplicht
- Leverage en margin mode worden éénmalig bij opstart ingesteld
- SL/TP als attached algo-orders (bewaakt door exchange, ook als bot offline gaat)
- Net-mode (one-way): één positie per instrument (long of short, niet beide)
- Rate limit: 60 order-requests per 2s — voldoende voor candle-close polling

Credentials via environment variables:
    OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE

Testnet vs live:
    okx.testnet: true → demo trading account
    okx.testnet: false → live handel (echt geld)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.trading.broker.base import AbstractBroker, Order, OrderSide, OrderStatus

logger = logging.getLogger(__name__)

_CONTRACT_SIZE_FALLBACK = 0.01   # BTC-USDT-SWAP fallback
_RECONCILE_LOG = Path("logs/reconcile.jsonl")


@dataclass
class _TrailingState:
    sl_distance:  float
    current_sl:   float
    best_price:   float | None = None
    be_activated: bool         = False
    algo_id:      str | None   = None  # OKX algo order ID voor amendment


class OKXBroker(AbstractBroker):
    """
    Live OKX broker voor USDT-margined perpetual swaps.

    Parameters
    ----------
    cfg : dict
        Volledige config dict. Gebruikt okx + derivatives secties.
    symbol : str, optional
        OKX instrument-ID (bijv. "ETH-USDT-SWAP"). Standaard: cfg["derivatives"]["symbol"].
    leverage : int, optional
        Leverage voor dit instrument. Standaard: cfg["derivatives"]["leverage"].
    """

    def __init__(
        self,
        cfg:      dict,
        symbol:   str | None = None,
        leverage: int | None = None,
    ) -> None:
        try:
            from okx import Account, Trade
        except ImportError:
            raise ImportError(
                "python-okx niet geïnstalleerd. "
                "Voer uit: pip install python-okx"
            )

        okx_cfg  = cfg["okx"]
        drv_cfg  = cfg["derivatives"]

        api_key    = os.environ.get("OKX_API_KEY",    okx_cfg.get("api_key",    ""))
        api_secret = os.environ.get("OKX_API_SECRET", okx_cfg.get("api_secret", ""))
        passphrase = os.environ.get("OKX_PASSPHRASE", okx_cfg.get("passphrase", ""))
        flag       = "1" if okx_cfg.get("testnet", True) else "0"  # "1" = demo/testnet
        base_url   = okx_cfg.get("base_url", "https://www.okx.com")

        self._inst_id    = symbol   or drv_cfg["symbol"]      # bijv. "ETH-USDT-SWAP"
        self._leverage   = str(leverage or drv_cfg["leverage"])
        self._mgn_mode   = drv_cfg["margin_mode"]
        self._pending_ttl = drv_cfg.get("pending_order_ttl_candles", 5)

        self._trade   = Trade.TradeAPI(
            api_key, api_secret, passphrase,
            use_server_time=False, flag=flag, domain=base_url,
        )
        self._account = Account.AccountAPI(
            api_key, api_secret, passphrase,
            use_server_time=False, flag=flag, domain=base_url,
        )

        self._pending: list[Order] = []
        self._open:    list[Order] = []
        self._closed:  list[Order] = []
        self._pending_age: dict[str, int] = {}  # order_id → aantal candles pending
        self._trailing: dict[str, _TrailingState] = {}  # order_id → trailing state

        ts_cfg = cfg.get("risk", {}).get("trailing_stop", {})
        self._trailing_cfg: dict = ts_cfg if ts_cfg.get("enabled", False) else {}

        self._contract_size = self._fetch_contract_size()
        self._setup_leverage()

    # ------------------------------------------------------------------
    # Opstart
    # ------------------------------------------------------------------

    def _fetch_contract_size(self) -> float:
        """Haal de contract-grootte (ctVal) op via de OKX public API."""
        import requests
        try:
            resp = requests.get(
                "https://www.okx.com/api/v5/public/instruments",
                params={"instType": "SWAP", "instId": self._inst_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if data:
                ct_val = float(data[0]["ctVal"])
                logger.info(
                    "Contract-grootte %s: %.4f %s/contract",
                    self._inst_id, ct_val, data[0].get("ctValCcy", ""),
                )
                return ct_val
        except Exception as exc:
            logger.warning(
                "Contract-grootte ophalen mislukt voor %s: %s — fallback %.4f",
                self._inst_id, exc, _CONTRACT_SIZE_FALLBACK,
            )
        return _CONTRACT_SIZE_FALLBACK

    def _setup_leverage(self) -> None:
        """Stel leverage en margin mode in (éénmalig bij opstart)."""
        try:
            resp = self._account.set_leverage(
                instId  = self._inst_id,
                lever   = self._leverage,
                mgnMode = self._mgn_mode,
            )
            if resp.get("code") != "0":
                logger.warning("Leverage instellen mislukt: %s", resp)
            else:
                logger.info(
                    "Leverage ingesteld: %sx %s op %s",
                    self._leverage, self._mgn_mode, self._inst_id,
                )
        except Exception as exc:
            logger.warning("Leverage instellen fout: %s", exc)

    def reconcile(self) -> None:
        """
        Controleer bij herstart of onze lokale state overeenkomt met OKX.

        Haalt open posities op van exchange en vergelijkt met lokale lijsten.
        Discrepanties worden gelogd en weggeschreven naar reconcile.jsonl.
        """
        try:
            resp = self._account.get_positions(instId=self._inst_id)
            if resp.get("code") != "0":
                logger.warning("Reconcile: posities ophalen mislukt: %s", resp)
                return

            exchange_pos = resp.get("data", [])
            has_exchange_pos = any(
                abs(float(p.get("pos", 0))) > 0 for p in exchange_pos
            )
            has_local_open = len(self._open) > 0

            if has_exchange_pos != has_local_open:
                msg = (
                    f"Reconcile discrepantie: exchange_open={has_exchange_pos}, "
                    f"local_open={has_local_open}"
                )
                logger.warning(msg)
                _append_reconcile_log({"discrepantie": msg})
            else:
                logger.info("Reconcile OK: exchange en lokale state kloppen overeen.")
        except Exception as exc:
            logger.warning("Reconcile fout: %s", exc)

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
        Plaats een limit order met attached SL/TP algo-orders.

        Contract-grootte: size_in_btc / 0.01 = aantal contracten.
        """
        sl_dist = abs(entry_price - sl_price)
        if sl_dist == 0:
            raise ValueError("SL-afstand is nul — ongeldige order.")

        size_in_base = risk_amount / sl_dist
        contracts    = max(1, round(size_in_base / self._contract_size))
        okx_side     = "buy" if side == OrderSide.LONG else "sell"

        resp = self._trade.place_order(
            instId    = self._inst_id,
            tdMode    = self._mgn_mode,
            side      = okx_side,
            ordType   = "limit",
            px        = str(entry_price),
            sz        = str(contracts),
            attachAlgoOrds=[{
                "tpTriggerPx": str(tp_price),
                "tpOrdPx":     "-1",   # marktorder bij TP
                "slTriggerPx": str(sl_price),
                "slOrdPx":     "-1",   # marktorder bij SL
            }],
        )

        if resp.get("code") != "0":
            raise ValueError(f"OKX order geweigerd: {resp}")

        okx_order_id = resp["data"][0]["ordId"]
        order = Order(
            order_id    = okx_order_id,
            symbol      = symbol,
            side        = side,
            entry_price = entry_price,
            sl_price    = sl_price,
            tp_price    = tp_price,
            size        = contracts * self._contract_size,  # base-asset eenheden voor P&L
            status      = OrderStatus.PENDING,
        )
        self._pending.append(order)
        self._pending_age[okx_order_id] = 0
        logger.info(
            "Order geplaatst [%s] %s @ %.2f  SL=%.2f  TP=%.2f  contracts=%d  TTL=%d candles",
            okx_order_id, okx_side, entry_price, sl_price, tp_price, contracts, self._pending_ttl,
        )
        return order

    def on_candle(
        self,
        symbol:    str,
        ohlc_row:  pd.Series,
        timestamp: pd.Timestamp,
    ) -> list[Order]:
        """
        Poll OKX per candle-close voor fills en positie-sluitingen.

        1. Pending orders: check of gevuld
        2. Open posities: check of nog actief (SL/TP kunnen positie sluiten)
        """
        closed_this_candle: list[Order] = []

        # --- Stap 1: Pending → Open ---
        still_pending = []
        for order in self._pending:
            try:
                age = self._pending_age.get(order.order_id, 0) + 1
                self._pending_age[order.order_id] = age

                if age > self._pending_ttl:
                    self._trade.cancel_order(instId=self._inst_id, ordId=order.order_id)
                    order.status = OrderStatus.CANCELLED
                    self._pending_age.pop(order.order_id, None)
                    logger.info(
                        "Pending order geannuleerd na %d candles (TTL=%d) [%s]",
                        age, self._pending_ttl, order.order_id,
                    )
                    continue

                resp = self._trade.get_order(
                    instId=self._inst_id, ordId=order.order_id
                )
                if resp.get("code") != "0":
                    still_pending.append(order)
                    continue

                state = resp["data"][0].get("state", "")
                if state == "filled":
                    order.status    = OrderStatus.OPEN
                    order.filled_at = timestamp
                    self._pending_age.pop(order.order_id, None)
                    self._open.append(order)
                    # Initialiseer trailing state en haal algo ID op
                    if self._trailing_cfg:
                        sl_dist = abs(order.entry_price - order.sl_price)
                        ts = _TrailingState(
                            sl_distance = sl_dist,
                            current_sl  = order.sl_price,
                            algo_id     = self._fetch_algo_id(order.order_id),
                        )
                        self._trailing[order.order_id] = ts
                    logger.info("Order gevuld [%s] @ %s", order.order_id, timestamp)
                elif state in ("canceled", "mmp_canceled"):
                    order.status = OrderStatus.CANCELLED
                    self._pending_age.pop(order.order_id, None)
                    logger.info("Order geannuleerd [%s]", order.order_id)
                else:
                    still_pending.append(order)

            except Exception as exc:
                logger.warning("Order-status ophalen mislukt [%s]: %s", order.order_id, exc)
                still_pending.append(order)

        self._pending = still_pending

        # --- Stap 2: Open → Closed ---
        if not self._open:
            return closed_this_candle

        try:
            resp = self._account.get_positions(instId=self._inst_id)
            exchange_positions = resp.get("data", []) if resp.get("code") == "0" else []
            # Net-mode: pos > 0 = long, pos < 0 = short, pos = 0 = gesloten
            net_pos = sum(float(p.get("pos", 0)) for p in exchange_positions)
        except Exception as exc:
            logger.warning("Posities ophalen mislukt: %s", exc)
            return closed_this_candle

        still_open = []
        for order in self._open:
            is_long   = order.side == OrderSide.LONG
            still_live = (is_long and net_pos > 0) or (not is_long and net_pos < 0)

            # Update trailing/breakeven SL en amendeer exchange algo indien nodig
            if still_live and self._trailing_cfg and order.order_id in self._trailing:
                self._update_trailing_sl(
                    order,
                    float(ohlc_row["low"]),
                    float(ohlc_row["high"]),
                )

            if still_live:
                still_open.append(order)
            else:
                close_px, pnl = self._get_close_info(order)
                order.status      = OrderStatus.CLOSED
                order.closed_at   = timestamp
                order.close_price = close_px
                order.pnl         = pnl
                self._closed.append(order)
                closed_this_candle.append(order)
                self._trailing.pop(order.order_id, None)
                logger.info(
                    "Positie gesloten [%s]  close=%.2f  P&L=%.2f",
                    order.order_id, close_px, pnl,
                )

        self._open = still_open
        return closed_this_candle

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        orders = self._open + self._pending
        return [o for o in orders if symbol is None or o.symbol == symbol]

    def closed_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._closed if symbol is None or o.symbol == symbol]

    def equity(self) -> float:
        """Totaal USDT equity via OKX account balance."""
        try:
            resp = self._account.get_account_balance(ccy="USDT")
            if resp.get("code") == "0":
                return float(resp["data"][0].get("totalEq", 0))
        except Exception as exc:
            logger.warning("Equity ophalen mislukt: %s", exc)
        return 0.0

    def cancel_order(self, order_id: str) -> bool:
        try:
            resp = self._trade.cancel_order(
                instId=self._inst_id, ordId=order_id
            )
            success = resp.get("code") == "0"
            if success:
                self._pending = [o for o in self._pending if o.order_id != order_id]
            return success
        except Exception as exc:
            logger.warning("Cancel order mislukt [%s]: %s", order_id, exc)
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_algo_id(self, order_id: str) -> str | None:
        """Haal het OKX algo order ID op (SL/TP) voor een gevulde order."""
        try:
            resp = self._trade.get_algo_order_list(
                instId      = self._inst_id,
                algoOrdType = "oco",
            )
            if resp.get("code") != "0":
                return None
            for algo in resp.get("data", []):
                if algo.get("attachedOrdId") == order_id or algo.get("ordId") == order_id:
                    algo_id = algo.get("algoId")
                    logger.info("Algo order gevonden [%s] voor order [%s]", algo_id, order_id)
                    return algo_id
        except Exception as exc:
            logger.warning("Algo ID ophalen mislukt voor [%s]: %s", order_id, exc)
        return None

    def _update_trailing_sl(self, order: Order, low: float, high: float) -> None:
        """
        Beweeg trailing/breakeven SL en amendeer het OKX algo order indien nodig.
        De oorspronkelijke exchange-SL dient als vangnet bij bot-uitval.
        """
        state = self._trailing[order.order_id]
        cfg   = self._trailing_cfg
        entry = order.entry_price
        dist  = state.sl_distance
        be_r  = cfg.get("breakeven_at_r", 0.0)
        trail_r = cfg.get("trail_after_r")
        trail_s = cfg.get("trail_step_r", 0.5)

        if dist == 0:
            return

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

        new_sl = state.current_sl

        if be_r > 0 and r_mult >= be_r and not state.be_activated:
            new_sl             = entry
            state.be_activated = True
            logger.info(
                "Breakeven geactiveerd [%s] SL → %.2f (was %.2f)",
                order.order_id, entry, state.current_sl,
            )

        if trail_r and r_mult >= trail_r:
            if order.side == OrderSide.LONG:
                candidate = state.best_price - dist * trail_s
                new_sl = max(new_sl, candidate)
            else:
                candidate = state.best_price + dist * trail_s
                new_sl = min(new_sl, candidate)

        if new_sl != state.current_sl:
            state.current_sl = new_sl
            self._amend_algo_sl(order.order_id, new_sl, state.algo_id)

    def _amend_algo_sl(self, order_id: str, new_sl: float, algo_id: str | None) -> None:
        """Amendeer het exchange SL algo order naar de nieuwe trailing SL prijs."""
        if not algo_id:
            logger.debug(
                "Trailing SL beweegt naar %.2f voor [%s] — geen algo ID, exchange SL ongewijzigd.",
                new_sl, order_id,
            )
            return
        try:
            resp = self._trade.amend_algo_order(
                instId        = self._inst_id,
                algoId        = algo_id,
                newSlTriggerPx = str(new_sl),
                newSlOrdPx    = "-1",
            )
            if resp.get("code") == "0":
                logger.info(
                    "Exchange SL geamendeerd [%s] → %.2f", order_id, new_sl
                )
            else:
                logger.warning(
                    "Exchange SL amendment mislukt [%s]: %s", order_id, resp
                )
        except Exception as exc:
            logger.warning("Exchange SL amendment fout [%s]: %s", order_id, exc)

    def _get_close_info(self, order: Order) -> tuple[float, float]:
        """
        Haal sluitprijs en gerealiseerde P&L op uit positie-geschiedenis.

        Retourneert (close_price, pnl). Bij fout: (0.0, 0.0).
        """
        try:
            resp = self._account.get_positions_history(instId=self._inst_id)
            if resp.get("code") == "0" and resp.get("data"):
                # Meest recente gesloten positie
                hist = sorted(
                    resp["data"],
                    key=lambda x: int(x.get("uTime", 0)),
                    reverse=True,
                )
                h = hist[0]
                close_px = float(h.get("closeAvgPx") or 0)
                pnl      = float(h.get("realizedPnl") or 0)
                return close_px, pnl
        except Exception as exc:
            logger.warning("Close info ophalen mislukt: %s", exc)
        return 0.0, 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _append_reconcile_log(data: dict) -> None:
    _RECONCILE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_RECONCILE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")
