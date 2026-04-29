import pandas as pd

from strategy.sweep_strategy import SweepSetup, SweepFilters, confirm_bos


def apply_filters(setup: SweepSetup, df_15m: pd.DataFrame, i, filters: SweepFilters):
    """
    Pas filters toe op een sweep setup.
    Retourneert True als de setup voldoet aan alle filters.
    """
    if filters.direction != "both" and setup.direction != filters.direction:
        return False
    if filters.regime and setup.regime != filters.regime:
        return False
    if filters.bos_confirm:
        if not confirm_bos(df_15m, setup, filters.bos_window):
            return False
    return True