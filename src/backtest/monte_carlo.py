import numpy as np
from src.backtest.metrics import compute_metrics, equity_curve, compute_metrics_from_equity


def monte_carlo(trades, n_simulations=1000, initial_capital=10000):
    if not trades:
        return {
            'sharpe': [0.0, 0.0, 0.0],
            'win_rate': [0.0, 0.0, 0.0],
            'max_drawdown': [0.0, 0.0, 0.0],
            'profit_factor': [0.0, 0.0, 0.0],
            'total_return': [0.0, 0.0, 0.0],
        }

    # Bereken win rate en profit factor één keer (blijven constant)
    wins = sum(1 for t in trades if t.outcome == "win")
    losses = len(trades) - wins
    win_rate = wins / len(trades) if trades else 0.0
    total_wins = sum(t.pnl_capital for t in trades if t.outcome == "win" and t.pnl_capital > 0)
    total_losses = abs(sum(t.pnl_capital for t in trades if t.outcome == "loss" and t.pnl_capital < 0))
    profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

    results = {
        'sharpe': [],
        'win_rate': [],  # Blijft constant
        'max_drawdown': [],
        'profit_factor': [],  # Blijft constant
        'total_return': [],
    }

    for _ in range(n_simulations):
        # Shuffle de trades
        shuffled_trades = np.random.permutation(trades).tolist()

        # Bereken equity curve voor geshuffelde trades
        equity = equity_curve(shuffled_trades, initial_capital)
        metrics = compute_metrics_from_equity(equity, initial_capital)

        results['sharpe'].append(metrics.sharpe_ratio)
        results['win_rate'].append(win_rate)  # Constant
        results['max_drawdown'].append(metrics.max_drawdown)
        results['profit_factor'].append(profit_factor)  # Constant
        results['total_return'].append(metrics.total_return)

    # Retourneer percentielen
    return {
        'sharpe': np.percentile(results['sharpe'], [5, 50, 95]),
        'win_rate': [win_rate, win_rate, win_rate],  # Constant
        'max_drawdown': np.percentile(results['max_drawdown'], [5, 50, 95]),
        'profit_factor': [profit_factor, profit_factor, profit_factor],  # Constant
        'total_return': np.percentile(results['total_return'], [5, 50, 95]),
    }