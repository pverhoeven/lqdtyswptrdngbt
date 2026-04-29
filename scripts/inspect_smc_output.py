"""
scripts/inspect_smc_output.py — Inspecteert de ruwe output van de SMC library.

Wat dit script doet:
- Laadt 1 maand 15m data uit de bestaande cache
- Toont per SMC-kolom: aantal non-NaN waarden, unieke waarden, voorbeeldrijen
- Helpt begrijpen of de lifecycle-verwachtingen kloppen met de werkelijke output

Gebruik:
    python scripts/inspect_smc_output.py
    python scripts/inspect_smc_output.py --months 3
    python scripts/inspect_smc_output.py --year 2021 --month 6
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.config_loader import load_config
from src.data.cache import load_cache

logging.basicConfig(
    level=logging.WARNING,  # stil — we printen zelf
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspecteer ruwe SMC library output."
    )
    parser.add_argument("--months", type=int, default=1,
                        help="Aantal maanden data te inspecteren (standaard: 1)")
    parser.add_argument("--year",  type=int, default=2021,
                        help="Startjaar (standaard: 2021)")
    parser.add_argument("--month", type=int, default=1,
                        help="Startmaand (standaard: 1)")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Periode
    start = pd.Timestamp(f"{args.year}-{args.month:02d}-01", tz="UTC")
    end   = start + pd.DateOffset(months=args.months)
    print(f"\n{'='*60}")
    print(f"  SMC OUTPUT INSPECTIE")
    print(f"  Periode: {start.date()} → {end.date()}")
    print(f"{'='*60}\n")

    # Laad cache
    try:
        cache = load_cache(cfg, start=str(start.date()), end=str(end.date()))
    except FileNotFoundError as e:
        print(f"❌ Cache niet gevonden: {e}")
        print("   Voer eerst: python scripts/build_cache.py")
        sys.exit(1)

    if cache.empty:
        print("❌ Geen data in cache voor deze periode.")
        sys.exit(1)

    print(f"Geladen: {len(cache)} candles  "
          f"({cache.index[0].date()} → {cache.index[-1].date()})\n")

    # --- Kolom-overzicht ---
    print(f"{'─'*60}")
    print(f"  KOLOM-OVERZICHT")
    print(f"{'─'*60}")

    for col in cache.columns:
        series = cache[col]
        non_nan = series.notna().sum()
        pct     = non_nan / len(series) * 100
        unique  = sorted(series.dropna().unique()[:20].tolist())

        # Formatteer unieke waarden kort
        if len(unique) > 10:
            uniq_str = f"{unique[:5]} ... ({len(unique)} uniek)"
        else:
            uniq_str = str(unique)

        print(f"  {col:<25} non-NaN: {non_nan:>5} ({pct:5.1f}%)   uniek: {uniq_str}")

    # --- Detail per kritische kolom ---
    critical = {
        "ob":    "OB signaal (verwacht: 1=bullish, -1=bearish, 0=geen)",
        "liq":   "Liquidity sweep (verwacht: 1=bullish, -1=bearish, 0=geen)",
        "bos":   "Break of Structure (verwacht: 1=bullish, -1=bearish, 0=geen)",
        "choch": "Change of Character (verwacht: 1=bullish, -1=bearish, 0=geen)",
    }

    print(f"\n{'─'*60}")
    print(f"  DETAIL KRITISCHE KOLOMMEN")
    print(f"{'─'*60}")

    for col, beschrijving in critical.items():
        if col not in cache.columns:
            print(f"\n⚠️  {col}: KOLOM ONTBREEKT IN CACHE")
            continue

        series = cache[col]
        print(f"\n{col} — {beschrijving}")

        # Waardenverdeling
        counts = series.value_counts(dropna=False).sort_index()
        for val, cnt in counts.items():
            pct = cnt / len(series) * 100
            label = "NaN" if pd.isna(val) else str(val)
            bar   = "█" * min(int(pct / 2), 30)
            print(f"    {label:>8}:  {cnt:>6}  ({pct:5.1f}%)  {bar}")

        # Voorbeeldrijen waar het signaal actief is (niet 0 en niet NaN)
        active = series[(series != 0) & series.notna()]
        if len(active) == 0:
            print(f"    ⚠️  GEEN ACTIEVE SIGNALEN GEVONDEN")
            print(f"       Dit verklaart mogelijk de 0 trades.")
        else:
            print(f"\n    Eerste 5 actieve candles:")
            sample_idx = active.index[:5]
            sample = cache.loc[sample_idx, [col] + [
                c for c in ["ob_top","ob_bottom","liq_level","structure_level"]
                if c in cache.columns
            ]]
            print(sample.to_string(max_cols=8))

    # --- OB zone analyse ---
    print(f"\n{'─'*60}")
    print(f"  OB ZONE ANALYSE")
    print(f"{'─'*60}")

    if "ob" in cache.columns and "ob_top" in cache.columns:
        ob_signals = cache[cache["ob"] != 0].dropna(subset=["ob"])
        ob_active  = ob_signals[ob_signals["ob"] != 0]

        print(f"  Totaal OB signalen (non-zero, non-NaN): {len(ob_active)}")

        if len(ob_active) > 0:
            # Controleer of ob_top / ob_bottom zinvolle waarden hebben
            has_top    = ob_active["ob_top"].notna().sum()
            has_bottom = ob_active["ob_bottom"].notna().sum() if "ob_bottom" in cache.columns else 0
            print(f"  OB met geldige ob_top:    {has_top}")
            print(f"  OB met geldige ob_bottom: {has_bottom}")

            if has_top > 0:
                print(f"\n  Steekproef OB-zones (eerste 5):")
                cols_show = [c for c in ["ob","ob_top","ob_bottom","ob_pct"] if c in cache.columns]
                print(ob_active[cols_show].head(5).to_string())

    # --- Liquidity analyse ---
    print(f"\n{'─'*60}")
    print(f"  LIQUIDITY SWEEP ANALYSE")
    print(f"{'─'*60}")

    if "liq" in cache.columns:
        liq_active = cache[(cache["liq"] != 0) & cache["liq"].notna()]
        print(f"  Totaal liquidity sweeps: {len(liq_active)}")

        if len(liq_active) > 0:
            bull_liq = (liq_active["liq"] == 1).sum()
            bear_liq = (liq_active["liq"] == -1).sum()
            print(f"  Bullish sweeps (liq=1):  {bull_liq}")
            print(f"  Bearish sweeps (liq=-1): {bear_liq}")

            print(f"\n  Steekproef sweeps (eerste 5):")
            cols_show = [c for c in ["liq","liq_level","liq_swept_idx"] if c in cache.columns]
            print(liq_active[cols_show].head(5).to_string())

    # --- CHoCH timing t.o.v. sweeps ---
    print(f"\n{'─'*60}")
    print(f"  CHOCH ANALYSE")
    print(f"{'─'*60}")

    if "choch" in cache.columns:
        choch_active = cache[(cache["choch"] != 0) & cache["choch"].notna()]
        print(f"  Totaal CHoCH signalen: {len(choch_active)}")

        if len(choch_active) > 0 and "liq" in cache.columns:
            liq_active2 = cache[(cache["liq"] != 0) & cache["liq"].notna()]

            if len(liq_active2) > 0 and len(choch_active) > 0:
                # Hoeveel CHoCH verschijnen binnen N candles na een sweep?
                for window in [3, 8, 20, 50]:
                    count = 0
                    liq_positions = liq_active2.index
                    choch_positions = choch_active.index

                    for liq_ts in liq_positions[:100]:  # sample van 100
                        liq_pos = cache.index.get_loc(liq_ts)
                        window_end = min(liq_pos + window, len(cache) - 1)
                        window_ts = cache.index[window_end]
                        if any((choch_positions > liq_ts) & (choch_positions <= window_ts)):
                            count += 1

                    pct_with_choch = count / min(100, len(liq_positions)) * 100
                    print(f"  CHoCH binnen {window:>3} candles na sweep: "
                          f"{count:>3}/100  ({pct_with_choch:.0f}%)")

    # --- Samenvatting en aanbevelingen ---
    print(f"\n{'─'*60}")
    print(f"  SAMENVATTING")
    print(f"{'─'*60}")

    issues = []

    if "ob" in cache.columns:
        ob_count = ((cache["ob"] != 0) & cache["ob"].notna()).sum()
        if ob_count == 0:
            issues.append("❌ ob: GEEN actieve signalen — lifecycle kan nooit starten")
        elif ob_count < 5:
            issues.append(f"⚠️  ob: slechts {ob_count} signalen — te zeldzaam")
        else:
            print(f"  ✓ ob: {ob_count} actieve signalen")

    if "liq" in cache.columns:
        liq_count = ((cache["liq"] != 0) & cache["liq"].notna()).sum()
        if liq_count == 0:
            issues.append("❌ liq: GEEN sweep signalen")
        else:
            print(f"  ✓ liq: {liq_count} sweep signalen")

    if "choch" in cache.columns:
        choch_count = ((cache["choch"] != 0) & cache["choch"].notna()).sum()
        if choch_count == 0:
            issues.append("❌ choch: GEEN CHoCH signalen")
        else:
            print(f"  ✓ choch: {choch_count} CHoCH signalen")

    if issues:
        print()
        for issue in issues:
            print(f"  {issue}")
        print()
        print("  Aanbeveling: run scripts/debug_lifecycle.py voor verdere diagnose")
    else:
        print("\n  Alle kritische signalen aanwezig.")
        print("  Aanbeveling: run scripts/debug_lifecycle.py om de keten te testen")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()