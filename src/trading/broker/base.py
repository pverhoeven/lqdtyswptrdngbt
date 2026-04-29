"""
trading/broker/base.py — Abstracte broker interface.

Elke broker-implementatie (paper, Binance, IBKR, …) implementeert deze
interface. De trading loop weet niets van de onderliggende broker.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum, auto

import pandas as pd


class OrderSide(Enum):
    LONG  = auto()
    SHORT = auto()


class OrderStatus(Enum):
    PENDING   = auto()   # wacht op fill
    OPEN      = auto()   # gevuld, positie open
    CLOSED    = auto()   # gesloten (SL/TP geraakt)
    CANCELLED = auto()   # handmatig geannuleerd


@dataclass
class Order:
    """Representatie van een order/positie."""
    order_id:    str
    symbol:      str
    side:        OrderSide
    entry_price: float
    sl_price:    float
    tp_price:    float
    size:        float          # in base asset
    status:      OrderStatus = OrderStatus.PENDING
    filled_at:   pd.Timestamp | None = None
    closed_at:   pd.Timestamp | None = None
    close_price: float = 0.0
    pnl:         float = 0.0    # netto P&L in quote asset


class AbstractBroker(abc.ABC):
    """
    Interface die alle broker-implementaties moeten volgen.

    De trading loop werkt uitsluitend via deze interface,
    zodat paper <-> live swappen één regelwijziging is.
    """

    @abc.abstractmethod
    def place_order(
        self,
        symbol:      str,
        side:        OrderSide,
        entry_price: float,
        sl_price:    float,
        tp_price:    float,
        risk_amount: float,     # in quote asset (USDT)
    ) -> Order:
        """
        Plaats een order. Retourneert het Order object.
        Bij paper trading: directe fill op entry_price.
        Bij live trading: limit order op exchange.
        """

    @abc.abstractmethod
    def on_candle(
        self,
        symbol:   str,
        ohlc_row: pd.Series,
        timestamp: pd.Timestamp,
    ) -> list[Order]:
        """
        Informeer de broker over een nieuwe gesloten candle.
        Retourneert lijst van orders die in deze candle gesloten zijn.
        """

    @abc.abstractmethod
    def open_orders(self, symbol: str | None = None) -> list[Order]:
        """Geef alle open orders/posities terug."""

    @abc.abstractmethod
    def closed_orders(self, symbol: str | None = None) -> list[Order]:
        """Geef alle gesloten orders terug."""

    @abc.abstractmethod
    def equity(self) -> float:
        """Huidig totaal kapitaal (cash + open posities)."""

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Annuleer een pending order. True als gelukt."""