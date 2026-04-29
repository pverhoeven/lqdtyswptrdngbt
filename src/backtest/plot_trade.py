import plotly.graph_objects as go
from plotly.subplots import make_subplots

def plot_trade_interactive(trade, df_15m, lookback_candles=20, lookforward_candles=10):
    """
    Plot een single trade met Plotly (interactief).
    """
    # Haal data op
    start_idx = df_15m.index.get_loc(trade.entry_time) - lookback_candles
    end_idx = df_15m.index.get_loc(trade.exit_time) + lookforward_candles
    df_plot = df_15m.iloc[start_idx:end_idx].copy()

    # Maak candlesticks
    fig = go.Figure(data=[go.Candlestick(
        x=df_plot.index,
        open=df_plot['open'],
        high=df_plot['high'],
        low=df_plot['low'],
        close=df_plot['close'],
        name='OHLC'
    )])

    # Voeg entry/exit/SL/TP toe
    fig.add_hline(
        y=trade.entry_price,
        line_dash="dash",
        line_color="blue",
        annotation_text=f"Entry: {trade.entry_price:.2f}",
        annotation_position="top left"
    )
    fig.add_hline(
        y=trade.exit_price,
        line_dash="dash",
        line_color="green" if trade.outcome == "win" else "red",
        annotation_text=f"Exit: {trade.exit_price:.2f} ({trade.outcome})",
        annotation_position="top left"
    )
    fig.add_hline(
        y=trade.sl_price,
        line_dash="dot",
        line_color="purple",
        annotation_text=f"SL: {trade.sl_price:.2f}",
        annotation_position="top left"
    )
    fig.add_hline(
        y=trade.tp_price,
        line_dash="dot",
        line_color="orange",
        annotation_text=f"TP: {trade.tp_price:.2f}",
        annotation_position="top left"
    )

    # Voeg metrics toe als tekst
    metrics_text = (
        f"<b>Trade Metrics</b><br>"
        f"Direction: {trade.direction}<br>"
        f"PnL: {trade.pnl_capital:+.2f} USDT<br>"
        f"Costs: {trade.fee_cost:.2f} USDT<br>"
        f"Net PnL: {trade.pnl_capital + trade.fee_cost:.2f} USDT"
    )
    fig.add_annotation(
        xref="paper", yref="paper",
        x=0.02, y=0.95,
        text=metrics_text,
        showarrow=False,
        bgcolor="white",
        bordercolor="black",
        borderwidth=1
    )

    # Layout
    fig.update_layout(
        title=f"Trade: {trade.entry_time} → {trade.exit_time}",
        yaxis_title="Price (USDT)",
        xaxis_rangeslider_visible=False
    )

    fig.write_image(f"trade_{trade.entry_time.strftime('%Y%m%d_%H%M')}.png", scale=2)  # scale=2 voor hogere resolutie



