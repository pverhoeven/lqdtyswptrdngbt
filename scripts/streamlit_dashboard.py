"""
scripts/streamlit_dashboard.py — Web dashboard voor de trading bot.

Gebruik:
    streamlit run scripts/streamlit_dashboard.py

Opties via URL query params:
    ?log=logs/trades_20260428.jsonl
    ?refresh=15
"""

from __future__ import annotations

import json
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_LOG_DIR = Path("logs")

st.set_page_config(
    page_title="BTC SMC Trader",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar — instellingen
# ---------------------------------------------------------------------------

st.sidebar.title("BTC SMC Trader")
st.sidebar.markdown("---")

log_files = sorted(_LOG_DIR.glob("trades_*.jsonl"), reverse=True)
log_options = [str(p) for p in log_files]

if not log_options:
    st.sidebar.warning("Geen logbestanden gevonden in `logs/`.")
    selected_log = None
else:
    selected_log = st.sidebar.selectbox(
        "Logbestand",
        log_options,
        index=0,
        format_func=lambda p: Path(p).name,
    )

refresh_interval = st.sidebar.slider("Refresh (seconden)", 5, 120, 30)

st.sidebar.markdown("---")
st.sidebar.caption(f"Laatste update: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)

# ---------------------------------------------------------------------------
# Log lezen & state berekenen
# ---------------------------------------------------------------------------

@st.cache_data(ttl=5)
def read_log(path: str) -> list[dict]:
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return events


def build_state(events: list[dict]) -> dict:
    signals: list[dict] = []
    closed:  list[dict] = []
    open_orders: dict[str, dict] = {}

    for ev in events:
        t = ev.get("event")
        if t == "signal":
            signals.append(ev)
        elif t == "order_placed":
            open_orders[ev["order_id"]] = ev
        elif t == "trade_closed":
            closed.append(ev)
            open_orders.pop(ev.get("order_id", ""), None)

    wins      = sum(1 for c in closed if (c.get("pnl") or 0) > 0)
    losses    = sum(1 for c in closed if (c.get("pnl") or 0) <= 0)
    total_pnl = sum(c.get("pnl") or 0 for c in closed)
    total     = wins + losses
    win_rate  = wins / total if total > 0 else 0.0

    return {
        "signals":     signals,
        "closed":      closed,
        "open_orders": list(open_orders.values()),
        "wins":        wins,
        "losses":      losses,
        "total_pnl":   total_pnl,
        "win_rate":    win_rate,
        "n_signals":   len(signals),
        "n_placed":    len(signals),   # benadering
        "n_trades":    total,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts_str: str | None) -> str:
    if not ts_str:
        return "–"
    try:
        return datetime.fromisoformat(ts_str).strftime("%m-%d %H:%M")
    except Exception:
        return ts_str[:16]


def _pnl_color(pnl: float) -> str:
    return "green" if pnl > 0 else "red" if pnl < 0 else "gray"


# ---------------------------------------------------------------------------
# Regime & sentiment
# ---------------------------------------------------------------------------

_COINS = [
    {"name": "BTC", "inst_id": "BTC-USDT-SWAP"},
    {"name": "ETH", "inst_id": "ETH-USDT-SWAP"},
    {"name": "SOL", "inst_id": "SOL-USDT-SWAP"},
]

_HMM_PATH = Path("data/processed/hmm_regime_model.pkl")
_OKX_BASE = "https://www.okx.com"


@st.cache_data(ttl=300)
def fetch_4h_candles(inst_id: str, limit: int = 200) -> pd.DataFrame | None:
    try:
        r = requests.get(
            f"{_OKX_BASE}/api/v5/market/history-candles",
            params={"instId": inst_id, "bar": "4H", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        rows = [
            {
                "timestamp": pd.Timestamp(int(row[0]), unit="ms", tz="UTC"),
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[5]),
            }
            for row in reversed(data["data"])
        ]
        return pd.DataFrame(rows).set_index("timestamp")
    except Exception:
        return None


@st.cache_data(ttl=300)
def fetch_fear_greed() -> dict | None:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        r.raise_for_status()
        entry = r.json()["data"][0]
        return {"value": int(entry["value"]), "label": entry["value_classification"]}
    except Exception:
        return None


@st.cache_data(ttl=300)
def fetch_funding_rate(inst_id: str) -> float | None:
    try:
        r = requests.get(
            f"{_OKX_BASE}/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        return float(data["data"][0]["fundingRate"])
    except Exception:
        return None


@st.cache_resource
def _load_hmm_model():
    if not _HMM_PATH.exists():
        return None
    try:
        with open(_HMM_PATH, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _compute_regime(coin_name: str, df: pd.DataFrame | None) -> tuple[str, str, str]:
    """Retourneert (label, kleur, methode)."""
    if df is None or len(df) < 51:
        return "Onbekend", "gray", "–"

    if coin_name == "BTC":
        model = _load_hmm_model()
        if model is not None:
            try:
                regimes = model.predict(df)
                valid = regimes.dropna()
                if len(valid) > 0:
                    is_bull = bool(valid.iloc[-1])
                    return ("Bullish", "#00cc66", "HMM 4h") if is_bull else ("Bearish", "#ff4444", "HMM 4h")
            except Exception:
                pass

    # SMA50 fallback voor ETH/SOL (en BTC als model faalt)
    sma50 = df["close"].rolling(50).mean().iloc[-1]
    if pd.isna(sma50):
        return "Onbekend", "gray", "–"
    is_bull = float(df["close"].iloc[-1]) > float(sma50)
    return ("Bullish", "#00cc66", "SMA50 4h") if is_bull else ("Bearish", "#ff4444", "SMA50 4h")


# ---------------------------------------------------------------------------
# Weergave
# ---------------------------------------------------------------------------

st.title("BTC SMC Trader — Live Dashboard")

if selected_log is None:
    st.info("Start de trading bot om logbestanden te genereren.")
    if auto_refresh:
        time.sleep(refresh_interval)
        st.rerun()
    st.stop()

events = read_log(selected_log)
state  = build_state(events)

# --- KPI metrics ---
col1, col2, col3, col4, col5 = st.columns(5)

col1.metric("Signalen", state["n_signals"])
col2.metric("Trades",   state["n_trades"])

pnl_delta = f"{state['total_pnl']:+.2f} USDT"
col3.metric("Totaal P&L", pnl_delta)

col4.metric("Win-rate", f"{state['win_rate']:.1%}", f"{state['wins']}W / {state['losses']}L")
col5.metric("Open posities", len(state["open_orders"]))

st.divider()

# --- Regime & Sentiment ---
st.subheader("Marktregime & Sentiment")

r_cols = st.columns(5)

for i, coin in enumerate(_COINS):
    df_4h = fetch_4h_candles(coin["inst_id"])
    label, color, method = _compute_regime(coin["name"], df_4h)
    with r_cols[i]:
        st.markdown(f"**{coin['name']} Regime**")
        st.markdown(
            f"<span style='color:{color}; font-size:1.3em; font-weight:bold'>{label}</span>",
            unsafe_allow_html=True,
        )
        st.caption(method)

fg = fetch_fear_greed()
with r_cols[3]:
    st.markdown("**Fear & Greed**")
    if fg:
        val = fg["value"]
        if val <= 25:
            fg_color = "#ff4444"
        elif val <= 45:
            fg_color = "#ff8c00"
        elif val <= 55:
            fg_color = "#aaaaaa"
        elif val <= 75:
            fg_color = "#88cc44"
        else:
            fg_color = "#00cc66"
        st.markdown(
            f"<span style='color:{fg_color}; font-size:1.3em; font-weight:bold'>"
            f"{val} — {fg['label']}</span>",
            unsafe_allow_html=True,
        )
        st.caption("alternative.me")
    else:
        st.caption("Niet beschikbaar")

fr = fetch_funding_rate("BTC-USDT-SWAP")
with r_cols[4]:
    st.markdown("**BTC Funding Rate**")
    if fr is not None:
        fr_color = "#00cc66" if fr > 0 else "#ff4444" if fr < 0 else "#aaaaaa"
        caption = "Longs betalen shorts" if fr > 0 else "Shorts betalen longs" if fr < 0 else "Neutraal"
        st.markdown(
            f"<span style='color:{fr_color}; font-size:1.3em; font-weight:bold'>"
            f"{fr * 100:+.4f}%</span>",
            unsafe_allow_html=True,
        )
        st.caption(caption)
    else:
        st.caption("Niet beschikbaar")

st.divider()

# --- Open posities ---
st.subheader("Open posities")

if state["open_orders"]:
    df_open = pd.DataFrame(state["open_orders"])
    cols_open = ["order_id", "symbol", "side", "status", "entry_price", "sl_price", "tp_price", "size"]
    cols_open = [c for c in cols_open if c in df_open.columns]

    def _style_side(val: str) -> str:
        v = str(val).upper()
        if v in ("LONG", "BUY"):
            return "color: #00cc66; font-weight: bold"
        if v in ("SHORT", "SELL"):
            return "color: #ff4444; font-weight: bold"
        return ""

    styled = df_open[cols_open].style.map(_style_side, subset=["side"] if "side" in cols_open else [])
    st.dataframe(styled, use_container_width=True, hide_index=True)
else:
    st.caption("Geen open posities.")

st.divider()

# --- Recente trades & signalen naast elkaar ---
col_trades, col_signals = st.columns(2)

with col_trades:
    st.subheader("Recente trades")
    if state["closed"]:
        rows = []
        for c in reversed(state["closed"][-20:]):
            pnl = c.get("pnl") or 0
            rows.append({
                "Tijdstip": _fmt_ts(c.get("closed_at") or c.get("timestamp")),
                "ID":        c.get("order_id", "–"),
                "Side":      c.get("side", "–"),
                "Entry":     c.get("entry_price"),
                "Exit":      c.get("close_price"),
                "P&L":       round(pnl, 2),
            })
        df_trades = pd.DataFrame(rows)

        def _style_pnl(val):
            try:
                v = float(val)
                if v > 0:
                    return "color: #00cc66; font-weight: bold"
                if v < 0:
                    return "color: #ff4444; font-weight: bold"
            except Exception:
                pass
            return ""

        def _style_side_col(val):
            v = str(val).upper()
            if v in ("LONG", "BUY"):
                return "color: #00cc66"
            if v in ("SHORT", "SELL"):
                return "color: #ff4444"
            return ""

        styled_trades = df_trades.style.map(_style_pnl, subset=["P&L"]).map(_style_side_col, subset=["Side"])
        st.dataframe(styled_trades, use_container_width=True, hide_index=True)
    else:
        st.caption("Nog geen afgesloten trades.")

with col_signals:
    st.subheader("Recente signalen")
    if state["signals"]:
        rows = []
        for s in reversed(state["signals"][-20:]):
            rows.append({
                "Tijdstip": _fmt_ts(s.get("signal_ts") or s.get("timestamp")),
                "Dir":      s.get("direction", "–").upper(),
                "Entry":    s.get("entry_price"),
                "SL":       s.get("sl_price"),
                "TP":       s.get("tp_price"),
                "Filter":   s.get("filter", "–"),
            })
        df_sigs = pd.DataFrame(rows)

        def _style_dir(val):
            v = str(val).upper()
            if v == "LONG":
                return "color: #00cc66"
            if v == "SHORT":
                return "color: #ff4444"
            return ""

        styled_sigs = df_sigs.style.map(_style_dir, subset=["Dir"])
        st.dataframe(styled_sigs, use_container_width=True, hide_index=True)
    else:
        st.caption("Nog geen signalen gedetecteerd.")

st.divider()

# --- P&L curve ---
if state["closed"]:
    st.subheader("P&L curve")
    pnl_values = [c.get("pnl") or 0 for c in state["closed"]]
    cumulative  = pd.Series(pnl_values).cumsum()
    df_pnl = pd.DataFrame({
        "Trade":       range(1, len(cumulative) + 1),
        "Cumulatief P&L (USDT)": cumulative.values,
    }).set_index("Trade")
    st.line_chart(df_pnl, use_container_width=True)

# --- Auto-refresh ---
if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
