"""
Core strategie-logica voor Smart Money Concepts (SMC) sweep detectie.
Gebruikt voor zowel backtesting als live trading.
"""
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

### **📌 1. Data Classes (Structuren voor Trades en Setups)**
@dataclass
class SweepFilters:
    """Filters voor de sweep strategie."""
    regime: bool = False       # Alleen traden in specifiek regime (bull/bear)
    direction: str = "both"   # "long", "short", of "both"
    bos_confirm: bool = False # Bevestiging van Break of Structure (BOS) vereist
    bos_window: int = 10      # Aantal candles om BOS te bevestigen

@dataclass
class SweepSetup:
    """Bevat de gegevens voor een potentiële liquidity sweep setup."""
    direction: str            # "long" of "short"
    liq_level: float          # Liquiditeitsniveau (prijs waar liquiditeit werd "gesweept")
    sweep_ts: pd.Timestamp    # Tijdstempel van de sweep candle
    regime: Optional[bool]    # Regime (True=bullish, False=bearish, None=onbekend)

@dataclass
class Position:
    """Open positie (voorafgaand aan entry)."""
    direction: str
    entry_price: float
    sl_price: float
    tp_price: float
    size: float
    entry_ts: pd.Timestamp
    regime: Optional[bool]

    def check(self, ohlc_row: pd.Series) -> Optional[str]:
        """
        Check of de positie gesloten moet worden (win/loss).
        Retourneert "win" of "loss" als SL/TP is geraakt, anders None.
        """
        low = float(ohlc_row["low"])
        high = float(ohlc_row["high"])

        if self.direction == "long":
            if low <= self.sl_price:
                return "loss"
            if high >= self.tp_price:
                return "win"
        else:  # short
            if high >= self.sl_price:
                return "loss"
            if low <= self.tp_price:
                return "win"
        return None


### **📌 2. Core Strategie Functies**
def detect_liquidity_sweep(
    df: pd.DataFrame,
    i: int,
    sl_buf: float,
    rr: float,
    filters: SweepFilters,
) -> Optional[SweepSetup]:
    """
    Detecteer liquidity sweeps (bearish/long) op candle i.

    Parameters:
    -----------
    df : pd.DataFrame
        OHLCV data met kolommen: open, high, low, close, regime.
    i : int
        Index van de huidige candle.
    sl_buf : float
        Stop-loss buffer (bijv. 0.01 voor 1%).
    rr : float
        Risk:Reward ratio (bijv. 2.0 voor 1:2).
    filters : SweepFilters
        Filters voor de strategie.

    Returns:
    --------
    Optional[SweepSetup]
        SweepSetup als een sweep wordt gedetecteerd, anders None.
    """
    if i < 1:
        return None

    # Haal current en vorige candle data op
    current = df.iloc[i]
    prev = df.iloc[i-1]

    open_ = float(current["open"])
    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])
    prev_high = float(prev["high"])
    prev_low = float(prev["low"])
    regime = current.get("regime")

    # --- Bearish Sweep (Long Entry) ---
    # Conditie: Candle sluit onder de vorige low (liquiditeit onder de low wordt "gesweept")
    if close < prev_low and low < prev_low:
        liq_level = prev_low
        return SweepSetup(
            direction="long",
            liq_level=liq_level,
            sweep_ts=df.index[i],
            regime=regime,
        )

    # --- Bullish Sweep (Short Entry) ---
    # Conditie: Candle sluit boven de vorige high (liquiditeit boven de high wordt "gesweept")
    if close > prev_high and high > prev_high:
        liq_level = prev_high
        return SweepSetup(
            direction="short",
            liq_level=liq_level,
            sweep_ts=df.index[i],
            regime=regime,
        )

    return None

def confirm_bos(
    df: pd.DataFrame,
    setup: SweepSetup,
    bos_window: int,
) -> bool:
    """
    Bevestig Break of Structure (BOS) na een sweep setup.

    Parameters:
    -----------
    df : pd.DataFrame
        OHLCV data.
    setup : SweepSetup
        De sweep setup om te bevestigen.
    bos_window : int
        Aantal candles na de setup om BOS te bevestigen.

    Returns:
    --------
    bool
        True als BOS is bevestigd, anders False.
    """
    setup_idx = df.index.get_loc(setup.sweep_ts)
    end_idx = min(setup_idx + bos_window, len(df) - 1)

    for i in range(setup_idx + 1, end_idx + 1):
        candle = df.iloc[i]
        if setup.direction == "long":
            # BOS bevestigd als prijs sluit boven de liquiditeitsniveau
            if float(candle["close"]) > setup.liq_level:
                return True
        else:  # short
            # BOS bevestigd als prijs sluit onder de liquiditeitsniveau
            if float(candle["close"]) < setup.liq_level:
                return True
    return False

def calc_sl_tp(
    entry_price: float,
    liq_level: float,
    sl_buf: float,
    rr: float,
    direction: str,
) -> tuple[float, float]:
    """
    Bereken stop-loss (SL) en take-profit (TP) prijs.

    Parameters:
    -----------
    entry_price : float
        Entry prijs.
    liq_level : float
        Liquiditeitsniveau (prijs waar liquiditeit werd gesweept).
    sl_buf : float
        Stop-loss buffer (bijv. 0.01 voor 1%).
    rr : float
        Risk:Reward ratio (bijv. 2.0 voor 1:2).
    direction : str
        "long" of "short".

    Returns:
    --------
    tuple[float, float]
        (sl_price, tp_price)
    """
    if direction == "long":
        sl_price = liq_level * (1 - sl_buf)
        tp_price = entry_price + (entry_price - sl_price) * rr
    else:  # short
        sl_price = liq_level * (1 + sl_buf)
        tp_price = entry_price - (sl_price - entry_price) * rr
    return sl_price, tp_price

### **📌 3. Hoofd Strategie Functie (voor Backtest en Live)**
def run_strategy(
    df: pd.DataFrame,
    filters: SweepFilters,
    sl_buf: float,
    rr: float,
    capital: float,
    fee_pct: float,
    slippage_pct: float,
) -> List[Position]:
    """
    Voer de strategie uit op OHLCV data en retourneer een lijst met Positions.

    Parameters:
    -----------
    df : pd.DataFrame
        OHLCV data met regime informatie.
    filters : SweepFilters
        Filters voor de strategie.
    sl_buf : float
        Stop-loss buffer.
    rr : float
        Risk:Reward ratio.
    capital : float
        Startkapitaal (voor positiesize berekening).
    fee_pct : float
        Fee percentage (bijv. 0.001 voor 0.1%).
    slippage_pct : float
        Slippage percentage (bijv. 0.0005 voor 0.05%).

    Returns:
    --------
    List[Position]
        Lijst met open posities (nog niet geëxecuted).
    """
    positions = []
    for i in range(1, len(df)):
        # Detecteer sweep setup
        setup = detect_liquidity_sweep(df, i, sl_buf, rr, filters)
        if not setup:
            continue

        # Pas filters toe
        if filters.bos_confirm and not confirm_bos(df, setup, filters.bos_window):
            logger.debug(f"BOS niet bevestigd voor setup op {setup.sweep_ts}")
            continue

        if filters.direction != "both" and setup.direction != filters.direction:
            continue

        if filters.regime is not None and setup.regime != filters.regime:
            continue

        # Bereken entry, SL, TP
        entry_price = float(df.iloc[i]["close"])
        sl_price, tp_price = calc_sl_tp(entry_price, setup.liq_level, sl_buf, rr, setup.direction)

        # Maak een Position (wordt later omgezet in een Trade)
        position = Position(
            direction=setup.direction,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            size=0.0,  # Wordt later berekend in to_position()
            entry_ts=df.index[i],
            regime=setup.regime,
        )
        positions.append(position)
        logger.debug(f"Setup gedetecteerd: {setup.direction} @ {entry_price} | SL: {sl_price} | TP: {tp_price}")

    return positions