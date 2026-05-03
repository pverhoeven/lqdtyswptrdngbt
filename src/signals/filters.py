"""
signals/filters.py — Filter-configuratie voor sweep-detectie.

Gedeeld tussen backtest (sweep_engine) en live trading.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class SweepFilters:
    """
    Welke filters actief zijn bij sweep-detectie.
    Alle filters standaard uit — één voor één aanzetten.

    Attributes
    ----------
    regime : bool
        HMM regime moet overeenkomen met sweep-richting.
        Bullish regime → alleen long. Bearish regime → alleen short.
    direction : str
        "long", "short", of "both".
    bos_confirm : bool
        BOS in sweep-richting moet verschijnen binnen bos_window candles.
    bos_window : int
        Aantal candles na sweep om BOS te zoeken.
    """
    regime:             bool = False
    direction:          str  = "both"   # "long" | "short" | "both"
    bos_confirm:        bool = False
    bos_window:         int  = 10
    atr_filter:         bool = False
    atr_window:         int  = 14       # rolling window voor ATR gemiddelde
    sweep_rejection:    bool = False    # long: sweep-candle sluit groen (rejection van low); short: rood
    pre_sweep_lookback: int  = 0        # 0 = uit; N = prijs moet N candles geleden hoger liggen (long)
    micro_bos_tf:       str | None = None  # "3min" | "5min" — wacht op BoS op lagere TF na 15m sweep
    micro_bos_window:   int  = 20          # max lagere-TF candles na sweep

    def __post_init__(self) -> None:
        if self.direction not in ("long", "short", "both", "dynamic"):
            raise ValueError(
                f"direction moet 'long', 'short', 'both' of 'dynamic' zijn, niet '{self.direction}'"
            )
        if self.bos_window < 1:
            raise ValueError("bos_window moet minimaal 1 zijn")
        if self.atr_window < 1:
            raise ValueError("atr_window moet minimaal 1 zijn")
        if self.pre_sweep_lookback < 0:
            raise ValueError("pre_sweep_lookback moet 0 of positief zijn")
        if self.micro_bos_window < 1:
            raise ValueError("micro_bos_window moet minimaal 1 zijn")

    def allows(self, direction: str) -> bool:
        """True als deze richting door het direction-filter komt."""
        return self.direction == "both" or self.direction == direction

    @classmethod
    def from_config(cls, cfg: dict) -> "SweepFilters":
        """Bouw SweepFilters vanuit de 'filters' sectie van config.yaml."""
        f = cfg.get("filters", {})
        return cls(
            direction        = f.get("direction",        "both"),
            regime           = f.get("regime",           False),
            bos_confirm      = f.get("bos_confirm",      False),
            bos_window       = f.get("bos_window",       10),
            atr_filter       = f.get("atr_filter",       False),
            atr_window       = f.get("atr_window",       14),
            micro_bos_tf     = f.get("micro_bos_tf",     None),
            micro_bos_window = f.get("micro_bos_window", 20),
        )

    def __str__(self) -> str:
        parts = []
        if self.regime:
            parts.append("regime")
        if self.direction == "dynamic":
            parts.append("dynamic_200ma")
        elif self.direction != "both":
            parts.append(f"{self.direction}_only")
        if self.bos_confirm:
            parts.append(f"bos{self.bos_window}")
        if self.atr_filter:
            parts.append(f"atr{self.atr_window}")
        if self.sweep_rejection:
            parts.append("rejection")
        if self.pre_sweep_lookback > 0:
            parts.append(f"trend{self.pre_sweep_lookback}")
        if self.micro_bos_tf:
            tf_label = self.micro_bos_tf.replace("min", "m")
            parts.append(f"micro_bos_{tf_label}_w{self.micro_bos_window}")
        return "+".join(parts) if parts else "baseline"