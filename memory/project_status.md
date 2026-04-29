---
name: project_status
description: Huidige bouw-status van de BTC SMC Trader bot — welke features zijn af, wat staat open
type: project
---

BTC SMC Trader — een volledig zelf-gebouwde algo trading bot op OKX perpetuals met SMC (Smart Money Concepts) signalen.

**Kern architectuur (klaar):**
- 15m SMC lifecycle (OB → Sweep → CHoCH → Entry)
- 4h HMM regime filter (bullish/bearish)
- Paper broker + OKX live broker
- Backtest engine + metrics
- Multi-coin support (BTC, ETH, SOL)
- Circuit breaker (max losses, dagelijks verlies, drawdown)
- Telegram notificaties

**Features toegevoegd:**

Feature 1 — Trailing Stop / Breakeven:
- `config/config.yaml`: `risk.trailing_stop` (enabled, breakeven_at_r=1.0, trail_after_r=2.0, trail_step_r=0.5)
- `src/backtest/engine.py`: `_OpenTrade._current_sl` + `_update_sl()` — trailing werkt in backtest
- `src/trading/broker/paper.py`: `PaperBroker(trailing_cfg=...)` + `_update_trailing()` — trailing in paper trading
- `src/trading/broker/okx.py`: `_TrailingState`, `_fetch_algo_id()`, `_amend_algo_sl()` — best-effort exchange algo amendment

Feature 2 — Funding Rate Filter:
- `src/trading/funding_rate.py`: `FundingRateFilter` + `build_funding_filter(cfg)` — gecached OKX public API
- `src/trading/order_manager.py`: controleert funding filter in `on_signal()` vóór order plaatsing
- `config/config.yaml`: `derivatives.funding_rate_filter` (enabled=false, max_long_rate=0.0003, etc.)

Feature 3 — Walk-Forward Validatie:
- `src/backtest/walk_forward.py`: `run_walk_forward()` + `summarize()` — rollend train/test venster
- `scripts/run_walk_forward.py`: CLI met per-venster tabel + interpretatie

**Implementatieplan fases (alle klaar):**
- ✅ Fase 1: Circuit breaker + dagelijkse loss limit (`src/trading/order_manager.py`)
- ✅ Fase 2: Retry + backoff + stale-candle detectie (`src/feeds/binance_feed.py`)
- ✅ Fase 3: OKX account/config (`config/config.yaml` + `src/trading/broker/okx.py`)
- ✅ Fase 4: OKXBroker + SL/TP algo-orders (`src/trading/broker/okx.py`)
- ✅ Fase 5: OKX WebSocket feed met reconnect + heartbeat (`src/feeds/okx_feed.py`)
- ✅ Fase 6: Telegram notificaties (`src/notifications/notifier.py`)
- ✅ Fase 7: State persistentie — `save_state`/`load_state` (paper) + `reconcile()` (OKX)
- ✅ Fase 8: Live trader entrypoint (`scripts/run_live_trader.py`) + testnet checklist

**Live dashboard:**
- ✅ Streamlit dashboard gebouwd (`scripts/streamlit_dashboard.py`, 272 regels)
- Gebruik: `streamlit run scripts/streamlit_dashboard.py`

Feature 4 — Partial exits (50% @ 1R, rest trailing):
- `config/config.yaml`: `risk.partial_exit` (enabled=false, exit_r=1.0, exit_fraction=0.5, move_sl_to_be=true)
- `src/trading/broker/paper.py`: `_check_partial_exit()` + `_partial_taken` dict + state persistentie
- `src/backtest/engine.py`: `_OpenTrade._check_partial()` + gecorrigeerde fee-berekening in `_close_trade()`
- OKX broker: partial exits NIET ondersteund (vereist reduce-only orders — future work)

Feature 5 — Telegram heartbeat:
- `config/config.yaml`: `notifications.telegram.heartbeat_hours: 4`
- `src/notifications/notifier.py`: `notify_heartbeat(equity, open_positions, wins, losses)`
- `src/trading/order_manager.py`: `send_heartbeat()` + `open_count()`
- `src/trading/paper_trader.py`: heartbeat-timing in `PaperTrader` en `MultiCoinTrader` (aggregated)

Bug fixes (zelfde sessie):
- `scripts/run_live_trader.py`: funding filter was niet aangesloten — `build_funding_filter(cfg)` nu doorgegeven aan `OrderManager` (single en multi-coin)
- `scripts/run_live_trader.py`: `trailing_cfg` en `partial_exit_cfg` nu correct doorgegeven aan `PaperBroker` (was eerder vergeten)

**Open (medium prioriteit):**
- Unfilled limit order timeout in paper broker (OKX heeft TTL al)
- Partial exits voor OKX live broker (reduce-only orders — complex)

**Open (lage prioriteit):**
- Volatility-scaled positiegroottes (Kelly/ATR)
- Performance attribution (uur, regime)
- Handmatige bevestigingsmode (Telegram /approve)

**Testnet checklist:** nog niet volledig doorgelopen (zie `docs/implementatieplan.md`)

**Why:** Gebruiker wil de bot uitbreiden naar productie-niveau met robuuste risk management en statistische validatie van de edge.
**How to apply:** Bij nieuwe features: check de open lijst hierboven; update deze memory na elke sessie.
