"""
DEPRECATED: scripts/debug_lifecycle.py — LifecycleEngine is vervangen door SweepDetector.

Test drie strategieën van simpel naar complex:
  1. SWEEP-ONLY:    liq sweep → directe entry (geen OB, geen CHoCH)
  2. SWEEP+CHOCH:   liq sweep → CHoCH → entry
  3. FULL CHAIN:    OB → sweep → CHoCH → retest → entry (origineel)

Voor elke strategie:
  - Telt hoeveel signalen elke stap produceert
  - Simuleert entries met vaste SL/TP
  - Toont de resulterende trade-frequentie en win/loss verdeling

Dit helpt te identificeren welke stap de bottleneck is.

Gebruik:
    python scripts/debug_lifecycle.py
    python scripts/debug_lifecycle.py --year 2021
    python scripts/debug_lifecycle.py --strategy sweep_only
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.config_loader import load_config
from src.data.cache import load_cache

logging.basicConfig(level=logging.WARNING)


# ---------------------------------------------------------------------------
# Gedeelde risico-instellingen
# ---------------------------------------------------------------------------
SL_BUFFER_PCT = 0.001   # 0.1%
REWARD_RATIO  = 2.0
FEE_PCT       = 0.001   # 0.1% per trade


# ---------------------------------------------------------------------------
# Strategie 1: Sweep-only
# ---------------------------------------------------------------------------

def run_sweep_only(
    df_15m: pd.DataFrame,
    cache: pd.DataFrame,
) -> dict:
    """
    Simpelste mogelijke strategie:
    Bij een bearish liquidity sweep (liq=-1) → long entry op close van die candle.
    Bij een bullish sweep (liq=1) → short entry.
    SL: 0.1% onder/boven entry. TP: 1:2.
    """
    counters = {"sweeps_bull": 0, "sweeps_bear": 0, "entries": 0}
    trades = []
    open_trade = None

    for i, ts in enumerate(df_15m.index):
        if ts not in cache.index:
            continue

        row   = df_15m.loc[ts]
        s_row = cache.loc[ts]
        liq   = _safe_val(s_row.get("liq", 0))

        if liq == 1:
            counters["sweeps_bull"] += 1
        elif liq == -1:
            counters["sweeps_bear"] += 1

        # Check open trade
        if open_trade is not None:
            result = _check_trade(open_trade, row)
            if result:
                trades.append(result)
                open_trade = None

        # Nieuwe entry bij sweep (als geen open trade)
        if open_trade is None and liq != 0 and not pd.isna(liq):
            entry = float(row["close"])
            if liq == -1:   # bearish sweep → long
                sl = entry * (1 - SL_BUFFER_PCT)
                tp = entry + (entry - sl) * REWARD_RATIO
                direction = "long"
            else:            # bullish sweep → short
                sl = entry * (1 + SL_BUFFER_PCT)
                tp = entry - (sl - entry) * REWARD_RATIO
                direction = "short"

            open_trade = {
                "entry": entry, "sl": sl, "tp": tp,
                "direction": direction, "entry_ts": ts,
            }
            counters["entries"] += 1

    return {"counters": counters, "trades": trades}


# ---------------------------------------------------------------------------
# Strategie 2: Sweep + CHoCH
# ---------------------------------------------------------------------------

def run_sweep_choch(
    df_15m: pd.DataFrame,
    cache: pd.DataFrame,
    choch_window: int = 20,
) -> dict:
    """
    Sweep → CHoCH binnen N candles → entry.
    """
    counters = {
        "sweeps": 0,
        "choch_after_sweep": 0,
        "entries": 0,
    }
    trades = []
    open_trade = None
    pending_sweep: dict | None = None   # wacht op CHoCH na sweep

    for i, ts in enumerate(df_15m.index):
        if ts not in cache.index:
            continue

        row   = df_15m.loc[ts]
        s_row = cache.loc[ts]
        liq   = _safe_val(s_row.get("liq", 0))
        choch = _safe_val(s_row.get("choch", 0))

        # Check open trade
        if open_trade is not None:
            result = _check_trade(open_trade, row)
            if result:
                trades.append(result)
                open_trade = None

        # Verval pending sweep als window verlopen
        if pending_sweep is not None:
            if i - pending_sweep["idx"] > choch_window:
                pending_sweep = None

        # Nieuwe sweep
        if liq != 0 and not pd.isna(liq):
            counters["sweeps"] += 1
            pending_sweep = {"liq": liq, "idx": i, "ts": ts}

        # CHoCH na sweep → entry
        if pending_sweep is not None and choch != 0 and not pd.isna(choch):
            liq_dir = pending_sweep["liq"]
            # Sweep swing low (-1) + bullish CHoCH (1) → long
            # Sweep swing high (1) + bearish CHoCH (-1) → short
            if (liq_dir == -1 and choch == 1) or (liq_dir == 1 and choch == -1):
                if open_trade is None:
                    counters["choch_after_sweep"] += 1
                    entry = float(row["close"])
                    direction = "long" if choch == 1 else "short"
                    if direction == "long":
                        sl = entry * (1 - SL_BUFFER_PCT)
                        tp = entry + (entry - sl) * REWARD_RATIO
                    else:
                        sl = entry * (1 + SL_BUFFER_PCT)
                        tp = entry - (sl - entry) * REWARD_RATIO

                    open_trade = {
                        "entry": entry, "sl": sl, "tp": tp,
                        "direction": direction, "entry_ts": ts,
                    }
                    counters["entries"] += 1
                    pending_sweep = None

    return {"counters": counters, "trades": trades}


# ---------------------------------------------------------------------------
# Strategie 3: Full OB chain (origineel, maar met stap-tellers)
# ---------------------------------------------------------------------------

def run_full_chain(
    df_15m: pd.DataFrame,
    cache: pd.DataFrame,
    cfg: dict,
) -> dict:
    """
    Originele OB → sweep → CHoCH → retest keten.
    Telt hoeveel setups elke stap halen.
    """
    from src.smc.lifecycle import LifecycleEngine

    counters = {
        "ob_formed":       0,
        "sweep_occurred":  0,
        "choch_confirmed": 0,
        "entry_valid":     0,
    }

    # Patch lifecycle engine om stap-tellers bij te houden
    engine = _InstrumentedLifecycle(cfg, counters)
    trades = []
    open_trade = None

    for i, ts in enumerate(df_15m.index):
        if ts not in cache.index:
            continue

        row   = df_15m.loc[ts]
        s_row = cache.loc[ts]

        # Check open trade
        if open_trade is not None:
            result = _check_trade(open_trade, row)
            if result:
                trades.append(result)
                open_trade = None

        signals = engine.update(i, row, s_row, regime=True)  # regime altijd True

        for sig in signals:
            if open_trade is None:
                sl = sig.sl_price
                tp = sig.entry_price + abs(sig.entry_price - sl) * REWARD_RATIO
                if sig.direction == "short":
                    tp = sig.entry_price - abs(sig.entry_price - sl) * REWARD_RATIO
                open_trade = {
                    "entry": sig.entry_price, "sl": sl, "tp": tp,
                    "direction": sig.direction, "entry_ts": ts,
                }

    return {"counters": counters, "trades": trades}


# ---------------------------------------------------------------------------
# EQL strategie: gelijke lows sweep → CHoCH → entry
# ---------------------------------------------------------------------------

def run_eql(
    df_15m: pd.DataFrame,
    cache: pd.DataFrame,
    choch_window: int = 30,
) -> dict:
    """
    EQL-gebaseerde strategie:
    - Detecteer sweeps van liquidity pools (liq != 0) als proxy voor EQL/EQH sweep
    - Wacht op CHoCH bevestiging
    - Entry op OB-zone of candle close

    Dit is dezelfde logica als sweep+choch maar met expliciete EQL-framing.
    Gebruikt dezelfde liq kolom maar filtert op 'Swept' index (liq_swept_idx).
    """
    counters = {
        "liq_pools_swept": 0,
        "with_choch": 0,
        "entries": 0,
    }
    trades = []
    open_trade = None
    pending: dict | None = None

    for i, ts in enumerate(df_15m.index):
        if ts not in cache.index:
            continue

        row   = df_15m.loc[ts]
        s_row = cache.loc[ts]
        liq   = _safe_val(s_row.get("liq", 0))
        choch = _safe_val(s_row.get("choch", 0))

        # Check open trade
        if open_trade is not None:
            result = _check_trade(open_trade, row)
            if result:
                trades.append(result)
                open_trade = None

        # Verval pending
        if pending and i - pending["idx"] > choch_window:
            pending = None

        # Liquidity sweep gedetecteerd
        if liq != 0 and not pd.isna(liq):
            counters["liq_pools_swept"] += 1
            pending = {"liq": liq, "idx": i}

        # CHoCH na sweep → entry
        if pending and choch != 0 and not pd.isna(choch):
            liq_dir = pending["liq"]
            if (liq_dir == -1 and choch == 1) or (liq_dir == 1 and choch == -1):
                if open_trade is None:
                    counters["with_choch"] += 1
                    entry = float(row["close"])
                    direction = "long" if choch == 1 else "short"
                    if direction == "long":
                        sl = entry * (1 - SL_BUFFER_PCT * 3)   # iets ruimere SL
                        tp = entry + (entry - sl) * REWARD_RATIO
                    else:
                        sl = entry * (1 + SL_BUFFER_PCT * 3)
                        tp = entry - (sl - entry) * REWARD_RATIO

                    open_trade = {
                        "entry": entry, "sl": sl, "tp": tp,
                        "direction": direction, "entry_ts": ts,
                    }
                    counters["entries"] += 1
                    pending = None

    return {"counters": counters, "trades": trades}


# ---------------------------------------------------------------------------
# Geïnstrumenteerde lifecycle (telt stappen)
# ---------------------------------------------------------------------------

class _InstrumentedLifecycle:
    """Wrapper om LifecycleEngine die stap-tellers bijhoudt."""

    def __init__(self, cfg: dict, counters: dict) -> None:
        from src.smc.lifecycle import LifecycleEngine, Stage
        self._engine   = LifecycleEngine(cfg)
        self._counters = counters
        self._Stage    = Stage
        self._prev_stages: dict[int, str] = {}

    def update(self, i, row, s_row, regime):
        from src.smc.lifecycle import Stage

        # Teller voor OB formed
        before = {id(s): s.stage.name for s in self._engine._active}
        signals = self._engine.update(i, row, s_row, regime)

        after = {id(s): s.stage.name for s in self._engine._active}

        # Tel nieuwe OBs (stage == OB_FORMED en net toegevoegd)
        for s in self._engine._active:
            sid = id(s)
            if s.stage == Stage.OB_FORMED and sid not in before:
                self._counters["ob_formed"] += 1
            elif s.stage == Stage.SWEEP_OCCURRED and before.get(sid) == "OB_FORMED":
                self._counters["sweep_occurred"] += 1
            elif s.stage == Stage.CHOCH_CONFIRMED and before.get(sid) == "SWEEP_OCCURRED":
                self._counters["choch_confirmed"] += 1

        self._counters["entry_valid"] += len(signals)
        return signals


# ---------------------------------------------------------------------------
# Trade simulatie helpers
# ---------------------------------------------------------------------------

def _check_trade(trade: dict, row: pd.Series) -> dict | None:
    """Controleer of SL of TP geraakt is. Retourneert trade-resultaat of None."""
    low   = float(row["low"])
    high  = float(row["high"])

    if trade["direction"] == "long":
        if low <= trade["sl"]:
            return {**trade, "outcome": "loss",
                    "exit_price": trade["sl"],
                    "pnl": (trade["sl"] - trade["entry"]) / trade["entry"]}
        if high >= trade["tp"]:
            return {**trade, "outcome": "win",
                    "exit_price": trade["tp"],
                    "pnl": (trade["tp"] - trade["entry"]) / trade["entry"]}
    else:
        if high >= trade["sl"]:
            return {**trade, "outcome": "loss",
                    "exit_price": trade["sl"],
                    "pnl": (trade["entry"] - trade["sl"]) / trade["entry"]}
        if low <= trade["tp"]:
            return {**trade, "outcome": "win",
                    "exit_price": trade["tp"],
                    "pnl": (trade["entry"] - trade["tp"]) / trade["entry"]}
    return None


def _safe_val(v) -> float:
    try:
        f = float(v)
        return 0.0 if pd.isna(f) else f
    except (TypeError, ValueError):
        return 0.0


def _print_results(name: str, result: dict) -> None:
    trades  = result["trades"]
    cntrs   = result["counters"]
    wins    = [t for t in trades if t["outcome"] == "win"]
    losses  = [t for t in trades if t["outcome"] == "loss"]

    print(f"\n{'─'*50}")
    print(f"  {name}")
    print(f"{'─'*50}")

    # Stap-tellers
    print("  Stap-tellers:")
    for k, v in cntrs.items():
        print(f"    {k:<25} {v:>6}")

    # Trade resultaten
    print(f"\n  Trade resultaten:")
    print(f"    Totaal trades:  {len(trades):>6}")
    print(f"    Wins:           {len(wins):>6}")
    print(f"    Losses:         {len(losses):>6}")

    if trades:
        win_rate = len(wins) / len(trades)
        print(f"    Win rate:       {win_rate:>6.1%}")

        if losses:
            gross_win  = sum(t["pnl"] for t in wins)
            gross_loss = abs(sum(t["pnl"] for t in losses))
            pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
            print(f"    Profit factor:  {pf:>6.2f}")
    else:
        print(f"\n  ⚠️  GEEN TRADES GEGENEREERD")
        # Diagnose hint
        if "sweeps" in cntrs and cntrs["sweeps"] == 0:
            print(f"  → Sweep signalen ontbreken volledig in de cache.")
            print(f"     Controleer inspect_smc_output.py resultaten.")
        elif "sweeps" in cntrs and cntrs.get("choch_after_sweep", 0) == 0:
            print(f"  → Sweeps gevonden ({cntrs['sweeps']}), maar CHoCH volgt nooit na sweep.")
            print(f"     Vergroot choch_window of controleer CHoCH signalen.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debug SMC lifecycle stapsgewijs."
    )
    parser.add_argument("--year",  type=int, default=2021)
    parser.add_argument("--months", type=int, default=6,
                        help="Aantal maanden te analyseren (standaard: 6)")
    parser.add_argument("--strategy", default="all",
                        choices=["all","sweep_only","sweep_choch","full_chain","eql"])
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)

    start = pd.Timestamp(f"{args.year}-01-01", tz="UTC")
    end   = start + pd.DateOffset(months=args.months)

    print(f"\n{'='*50}")
    print(f"  LIFECYCLE DEBUG")
    print(f"  Periode: {start.date()} → {end.date()}")
    print(f"  Strategie: {args.strategy}")
    print(f"{'='*50}")

    # Laad data
    try:
        cache = load_cache(cfg, start=str(start.date()), end=str(end.date()))
    except FileNotFoundError as e:
        print(f"❌ Cache niet gevonden: {e}")
        sys.exit(1)

    processed_dir = Path(cfg["data"]["paths"]["processed"])
    symbol = cfg["data"]["symbol"]
    tf = cfg["data"]["timeframes"]["signal"].replace("min","m")
    path_15m = processed_dir / f"{symbol}_{tf}.parquet"

    if not path_15m.exists():
        print(f"❌ 15m data niet gevonden: {path_15m}")
        sys.exit(1)

    df_15m_full = pd.read_parquet(path_15m)
    df_15m = df_15m_full[
        (df_15m_full.index >= start) &
        (df_15m_full.index <= end)
    ]

    # Gemeenschappelijke index
    common = df_15m.index.intersection(cache.index)
    df_15m = df_15m.loc[common]
    cache  = cache.loc[common]

    print(f"\nData: {len(df_15m)} candles "
          f"({df_15m.index[0].date()} → {df_15m.index[-1].date()})")

    run_all = args.strategy == "all"

    if run_all or args.strategy == "sweep_only":
        result = run_sweep_only(df_15m, cache)
        _print_results("STRATEGIE 1: SWEEP-ONLY", result)

    if run_all or args.strategy == "sweep_choch":
        result = run_sweep_choch(df_15m, cache, choch_window=20)
        _print_results("STRATEGIE 2: SWEEP + CHOCH (window=20)", result)

    if run_all or args.strategy == "eql":
        result = run_eql(df_15m, cache, choch_window=30)
        _print_results("STRATEGIE 3: EQL SWEEP + CHOCH (window=30)", result)

    if run_all or args.strategy == "full_chain":
        result = run_full_chain(df_15m, cache, cfg)
        _print_results("STRATEGIE 4: FULL OB CHAIN (origineel)", result)

    print(f"\n{'='*50}")
    print("  INTERPRETATIE")
    print(f"{'='*50}")
    print("""
  Lees de output van boven naar beneden:

  1. Als sweep_only 0 trades geeft:
     → liq signalen ontbreken. Controleer inspect_smc_output.py.
     → Mogelijk: kolom-mapping verkeerd, of library geeft andere waarden.

  2. Als sweep_only trades geeft maar sweep_choch niet:
     → CHoCH volgt nooit na een sweep.
     → Vergroot choch_window, of CHoCH-signalen zijn zeldzaam/verkeerd.

  3. Als sweep_choch trades geeft maar full_chain niet:
     → OB-detectie is de bottleneck (ob_formed teller laag).
     → Verlaag swing_length of verwijder OB-stap.

  4. Als alle strategieën 0 trades geven:
     → Fundamenteel probleem in cache of kolom-mapping.
     → Controleer inspect_smc_output.py eerst.
""")


if __name__ == "__main__":
    main()