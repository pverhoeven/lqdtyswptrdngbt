"""
scripts/pages/scanner.py — SMC Setup Scanner pagina voor het Streamlit dashboard.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.scanner.chart_generator import generate_setup_chart
from src.scanner.daily_scanner import DailySetup, run_daily_scan
from src.secrets_loader import load_secrets

load_secrets()

_CFG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "config.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _price(v: float) -> str:
    if v >= 10_000:
        return f"${v:,.0f}"
    elif v >= 100:
        return f"${v:,.1f}"
    else:
        return f"${v:,.2f}"


def _validate_with_mistral(
    setup: DailySetup, score: int, chart_bytes: bytes | None = None
) -> str:
    """Stuur de setup (+ optionele chart) naar Mistral en geef de beoordeling terug."""
    import base64
    import requests as req

    api_key = os.environ.get("MISTRAL_API_KEY", "")
    if not api_key:
        return (
            "⚠️ Geen MISTRAL_API_KEY gevonden. "
            "Stel de omgevingsvariabele in en herstart het dashboard."
        )

    rr = abs(setup.tp - setup.entry_zone) / max(abs(setup.entry_zone - setup.sl), 1e-8)
    confluences_text = "\n".join(f"  • {c}" for c in setup.confluences) or "  • geen"

    prompt = f"""Beoordeel de volgende SMC trading setup beknopt en praktisch.

Setup:
- Symbol: {setup.symbol} | Richting: {'LONG' if setup.direction == 'long' else 'SHORT'}
- Type: {setup.setup_type} | {setup.fase} — {setup.fase_label}
- Sterren: {setup.stars}/3 | Score: {score} punten
- Huidige prijs: ${setup.current_price:,.2f}
- Entry: ${setup.entry_zone:,.2f} | SL: ${setup.sl:,.2f} | TP: ${setup.tp:,.2f}
- Risk/Reward: 1:{rr:.1f} | Afstand tot entry: {setup.distance_pct:.1%}

Confluences:
{confluences_text}"""

    if chart_bytes:
        prompt += """

De chart hierboven toont de recente prijsactie met de SMC niveaus (FVG vlakken, \
BoS/CHoCH stippellijnen, Daily Support/Resistance badges, entry/SL/TP lijnen).
Gebruik zowel de bovenstaande data als de chart voor je beoordeling."""

    prompt += """

Geef een beoordeling in precies 4 punten:
1. Verdict: Valide / Twijfelachtig / Niet valide — en waarom in één zin
2. Sterkste argument vóór deze setup
3. Grootste risico of zwakke punt
4. Aanbeveling: Neem de trade / Wacht op bevestiging / Sla over"""

    system_msg = (
        "Je bent een expert in Smart Money Concepts (SMC) trading. "
        "Geef altijd beknopte, eerlijke beoordelingen in het Nederlands. "
        "Wees kritisch — niet elke setup is de moeite waard."
    )

    if chart_bytes:
        b64 = base64.b64encode(chart_bytes).decode()
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": f"data:image/png;base64,{b64}"},
        ]
        model = "pixtral-12b-2409"
    else:
        user_content = prompt
        model = "mistral-small-latest"

    try:
        resp = req.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 400,
                "temperature": 0.3,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        return f"❌ API fout: {exc}"


def _score(setup: DailySetup) -> int:
    """Bereken een numerieke score (0–70+) voor rangschikking en weergave."""
    fase_pts = {"FASE 3": 30, "FASE 2": 20, "FASE 1": 10}.get(setup.fase, 0)
    star_pts = setup.stars * 10
    dist_pts = int(10 * max(0.0, 1.0 - setup.distance_pct / 0.06))
    conf_pts = len(setup.confluences)          # bonus: 1 pt per confluence
    return fase_pts + star_pts + dist_pts + conf_pts


@st.cache_data(ttl=300, show_spinner=False)
def _scan(timeframe: str) -> list[DailySetup]:
    with open(_CFG_PATH) as f:
        cfg = yaml.safe_load(f)
    return run_daily_scan(cfg, timeframe)


def _setup_card(setup: DailySetup, idx: int, score: int) -> None:
    is_long = setup.direction == "long"
    dir_color = "#26A69A" if is_long else "#EF5350"
    arrow = "▲ LONG" if is_long else "▼ SHORT"
    fase_color = {
        "FASE 3": "#4CAF50",
        "FASE 2": "#FF9800",
        "FASE 1": "#2196F3",
    }.get(setup.fase, "#888888")
    stars = "⭐" * setup.stars
    selected = st.session_state.get("selected_idx") == idx
    border = "#42A5F5" if selected else "#252545"
    # Scorekleur: groen ≥55, geel ≥35, grijs lager
    score_color = "#4CAF50" if score >= 55 else "#FF9800" if score >= 35 else "#888"

    st.markdown(
        f"""
<div style="border:1px solid {border}; border-radius:10px; padding:14px;
            background:#0e0e1e; margin-bottom:4px;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
    <span style="color:{dir_color}; font-weight:700; font-size:1.05em;">{setup.symbol}</span>
    <div style="display:flex; gap:6px; align-items:center;">
      <span style="color:{score_color}; font-weight:700; font-size:0.82em;">{score} pts</span>
      <span style="background:{fase_color}22; color:{fase_color}; border:1px solid {fase_color}66;
                   padding:2px 10px; border-radius:10px; font-size:0.72em;
                   font-weight:600;">{setup.fase}</span>
    </div>
  </div>
  <div style="color:{dir_color}; font-size:0.85em; margin-bottom:4px;">{arrow} &nbsp; {stars}</div>
  <div style="color:#888; font-size:0.78em; margin-bottom:8px;">{setup.setup_type}</div>
  <div style="font-size:0.83em; line-height:1.8;">
    <span style="color:#aaa;">Entry</span>&nbsp;
    <b style="color:#42A5F5;">{_price(setup.entry_zone)}</b>
    &nbsp;&nbsp;
    <span style="color:#aaa;">SL</span>&nbsp;
    <b style="color:#EF5350;">{_price(setup.sl)}</b>
    &nbsp;&nbsp;
    <span style="color:#aaa;">TP</span>&nbsp;
    <b style="color:#66BB6A;">{_price(setup.tp)}</b>
  </div>
  <div style="color:#555; font-size:0.72em; margin-top:4px;">Afstand: {setup.distance_pct:.1%}</div>
</div>""",
        unsafe_allow_html=True,
    )
    if st.button("Bekijk chart →", key=f"btn_{idx}", use_container_width=True):
        st.session_state.selected_idx = idx
        st.rerun()


# ---------------------------------------------------------------------------
# Pagina layout
# ---------------------------------------------------------------------------

st.title("SMC Setup Scanner")

# Controls
col_tf, col_coin, col_btn, col_info = st.columns([2, 2, 1, 3])
with col_tf:
    timeframe = st.selectbox("Timeframe", ["15m", "1h", "4h", "1d"], key="tf_select")
with col_btn:
    st.write("")
    scan_now = st.button("Scan nu", type="primary", use_container_width=True)

if scan_now:
    _scan.clear()
    st.session_state.pop("selected_idx", None)

with st.spinner(f"Scanning coins op {timeframe}…"):
    try:
        all_setups = _scan(timeframe)
    except Exception as exc:
        st.error(f"Scan mislukt: {exc}")
        st.stop()

# Coin-filter dropdown (gevuld met coins die setups hebben)
coins_with_setups = sorted({s.symbol for s in all_setups})
with col_coin:
    coin_options = ["Alle coins"] + coins_with_setups
    selected_coin = st.selectbox("Coin", coin_options, key="coin_select")

if selected_coin != "Alle coins":
    setups = [s for s in all_setups if s.symbol == selected_coin]
else:
    setups = all_setups

# Sorteer op score (hoogste eerst)
setups = sorted(setups, key=_score, reverse=True)

with col_info:
    st.write("")
    st.caption(
        f"{len(setups)}/{len(all_setups)} setup(s) — cache 5 min — klik **Scan nu** voor verse data"
    )

if not setups:
    st.info("Geen setups gevonden voor de geselecteerde coin en timeframe.")
    st.stop()

# Summary metrics
m1, m2, m3, m4 = st.columns(4)
m1.metric("Totaal setups", len(setups))
m2.metric("FASE 3 — Entry", sum(1 for s in setups if s.fase == "FASE 3"))
m3.metric("FASE 2 — Wacht BoS", sum(1 for s in setups if s.fase == "FASE 2"))
m4.metric("FASE 1 — Watch", sum(1 for s in setups if s.fase == "FASE 1"))

st.divider()

# Twee-koloms layout: setup-kaarten links, grafiek rechts
list_col, chart_col = st.columns([2, 3])

with list_col:
    for i, setup in enumerate(setups):
        _setup_card(setup, i, _score(setup))

with chart_col:
    if "selected_idx" not in st.session_state:
        st.markdown(
            "<div style='color:#555; font-size:0.9em; padding-top:40px; text-align:center;'>"
            "← Selecteer een setup om de chart te zien</div>",
            unsafe_allow_html=True,
        )
    else:
        idx = st.session_state.selected_idx
        if 0 <= idx < len(setups):
            sel = setups[idx]
            is_long = sel.direction == "long"
            dir_color = "#26A69A" if is_long else "#EF5350"
            arrow = "▲ LONG" if is_long else "▼ SHORT"
            st.markdown(
                f"<span style='color:{dir_color}; font-weight:700; font-size:1.05em;'>"
                f"{sel.symbol} &nbsp; {arrow} &nbsp; {'⭐' * sel.stars}</span>"
                f"<span style='color:#aaa; font-size:0.85em;'> &nbsp;·&nbsp; {sel.fase_label}</span>",
                unsafe_allow_html=True,
            )
            try:
                with st.spinner("Chart genereren…"):
                    img = generate_setup_chart(sel, timeframe)
                st.image(img, use_container_width=True)
                st.session_state[f"chart_bytes_{idx}"] = img
            except Exception as exc:
                st.error(f"Chart kon niet worden gegenereerd: {exc}")
                st.session_state.pop(f"chart_bytes_{idx}", None)

            # Metrics onder de grafiek
            rr = abs(sel.tp - sel.entry_zone) / max(abs(sel.entry_zone - sel.sl), 1e-8)
            sl_pct = abs(sel.entry_zone - sel.sl) / sel.entry_zone
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Entry", _price(sel.entry_zone))
            mc2.metric("Stop Loss", _price(sel.sl), delta=f"-{sl_pct:.1%}", delta_color="inverse")
            mc3.metric("Take Profit", _price(sel.tp), delta=f"+{rr:.1f}R")
            mc4.metric("Type", sel.setup_type)

            # Score samenvatting
            fase_pts = {"FASE 3": 30, "FASE 2": 20, "FASE 1": 10}.get(sel.fase, 0)
            star_pts = sel.stars * 10
            dist_pts = int(10 * max(0.0, 1.0 - sel.distance_pct / 0.06))
            conf_pts = len(sel.confluences)
            total    = fase_pts + star_pts + dist_pts + conf_pts

            fase_desc = {
                "FASE 3": "BoS bevestigd — entry klaar",
                "FASE 2": "Sweep gezien — wacht op BoS",
                "FASE 1": "Niveau aanwezig — watch",
            }.get(sel.fase, sel.fase)

            def _bar(pts: int, max_pts: int) -> str:
                pct = int(pts / max_pts * 100) if max_pts else 0
                return (
                    f'<div style="background:#1a1a3a; border-radius:3px; height:4px; margin-top:3px;">'
                    f'<div style="background:#42A5F5; width:{pct}%; height:4px; border-radius:3px;"></div>'
                    f'</div>'
                )

            rows = [
                (f"{sel.fase} — {fase_desc}", fase_pts, 30),
                (f"{'⭐' * sel.stars} &nbsp; {sel.stars} {'ster' if sel.stars == 1 else 'sterren'}", star_pts, 30),
                (f"Afstand {sel.distance_pct:.1%} tot zone", dist_pts, 10),
                (f"{len(sel.confluences)} confluence{'s' if len(sel.confluences) != 1 else ''}", conf_pts, None),
            ]

            score_color = "#4CAF50" if total >= 55 else "#FF9800" if total >= 35 else "#888"
            html = (
                f'<div style="background:#0a0a18; border:1px solid #1a1a3a; border-radius:8px; '
                f'padding:14px; margin-top:10px;">'
                f'<div style="color:#aaa; font-size:0.78em; margin-bottom:10px;">'
                f'Score toelichting &nbsp;—&nbsp; '
                f'<b style="color:{score_color}; font-size:1.1em;">{total} punten</b></div>'
            )
            for label, pts, max_pts in rows:
                html += (
                    f'<div style="margin-bottom:8px;">'
                    f'<div style="display:flex; justify-content:space-between;">'
                    f'<span style="color:#ccc; font-size:0.75em;">{label}</span>'
                    f'<span style="color:#42A5F5; font-size:0.75em; font-weight:600;">+{pts}</span>'
                    f'</div>'
                    + (_bar(pts, max_pts) if max_pts else "")
                    + "</div>"
                )
            html += "</div>"
            st.markdown(html, unsafe_allow_html=True)

            # AI validatie
            ai_key = (
                f"ai_{sel.symbol}_{sel.direction}_{sel.fase}_"
                f"{sel.setup_type}_{sel.entry_zone:.2f}"
            )
            st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
            if st.button("🤖 Valideer met AI (Mistral)", key=f"ai_btn_{idx}",
                         use_container_width=True):
                with st.spinner("Mistral analyseert de setup…"):
                    chart_bytes = st.session_state.get(f"chart_bytes_{idx}")
                    st.session_state[ai_key] = _validate_with_mistral(
                        sel, total, chart_bytes
                    )

            if ai_key in st.session_state:
                verdict_text = st.session_state[ai_key]
                # Bepaal kleur op basis van eerste woord verdict
                low = verdict_text.lower()
                if "niet valide" in low or "sla over" in low:
                    border_c, label_c = "#EF5350", "#EF5350"
                    icon = "❌"
                elif "twijfel" in low or "wacht" in low:
                    border_c, label_c = "#FF9800", "#FF9800"
                    icon = "⚠️"
                else:
                    border_c, label_c = "#4CAF50", "#4CAF50"
                    icon = "✅"

                st.markdown(
                    f'<div style="background:#0a0a18; border:1px solid {border_c}44; '
                    f'border-radius:8px; padding:14px; margin-top:4px;">'
                    f'<div style="color:{label_c}; font-size:0.75em; font-weight:600; '
                    f'margin-bottom:8px;">{icon} AI Validatie — Mistral</div>'
                    f'<div style="color:#ddd; font-size:0.83em; line-height:1.7; '
                    f'white-space:pre-wrap;">{verdict_text}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
