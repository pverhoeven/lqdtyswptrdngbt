# Implementatieplan: productie-stabiele bot met OKX live trading

## Uitgangspunt

De architectuur is al goed opgezet: `AbstractBroker` maakt exchange-swapping één
regelwijziging. OKX live trading is een directe uitbreiding, geen refactor van de
bestaande paper trading loop.

---

## Fase 1 — Circuit breaker & dagelijkse loss limit

**Bestanden:** `src/trading/order_manager.py` + `config/config.yaml`

Circuit breaker logica in `OrderManager.on_signal()` en `on_candle()`.

### Triggers

| Trigger | Gedrag |
|---|---|
| N consecutive losses | Pauze tot einde dag |
| Dagelijks verlies > X% | Stop tot UTC 00:00 |
| Drawdown > Y% van startkapitaal | Harde stop (herstart vereist) |

### Nieuwe config-sectie

```yaml
risk:
  circuit_breaker:
    max_consecutive_losses: 3
    max_daily_loss_pct: 3.0      # % van startkapitaal
    max_drawdown_pct: 10.0       # harde noodstop
```

### Implementatie

`OrderManager` krijgt een `CircuitBreakerState` dataclass die bijhoudt:
consecutive losses, dagelijks P&L, peak equity. Bij elke `on_signal()` wordt
eerst gecheckt of de breaker open staat.

---

## Fase 2 — Retry + backoff + stale-candle detectie

**Bestand:** `src/feeds/binance_feed.py`

`_fetch_klines()` wordt vervangen door `_fetch_with_retry()`:
- Max 3 pogingen, exponential backoff: 2s → 4s → 8s
- Bij `requests.Timeout` of 5xx: retry
- Bij 4xx: direct fout gooien (niet retrien)
- Bij 3 mislukte pogingen: log critical + notificatie sturen

Stale-candle check in `PaperTrader._on_candle_close()`:
- Vergelijk `closed_ts` met verwachte timestamp op basis van `_next_candle_close()`
- Afwijking > 2 candles → log warning, sla candle over

---

## Fase 3 — OKX account, dependency en configuratie

### 3a. OKX account en API-sleutels

1. Maak een demo-account aan via `https://www.okx.com/en/demo-trading`
2. Genereer API-sleutels via **Account → API Management**:
   - API Key, Secret Key, Passphrase (OKX-specifiek, verplicht)
3. Zet rechten op: **Trade** (geen Withdraw nodig)

**Verificatie:** balance-ophaling werkt via `python-okx` SDK.

### 3b. Dependency

```bash
pip install python-okx
```

Toevoegen aan `requirements.txt`:
```
python-okx>=0.4.0
```

### 3c. Configuratie uitbreiden

```yaml
okx:
  api_key:     ""           # of via env: OKX_API_KEY
  api_secret:  ""           # OKX_API_SECRET
  passphrase:  ""           # OKX_PASSPHRASE
  testnet:     true         # false = live handel

derivatives:
  symbol:        "BTC-USDT-SWAP"  # perpetual swap
  leverage:      5                # 1–125x
  margin_mode:   "isolated"       # "isolated" afgebakend risico; "cross" = gehele balance als margin
  position_mode: "net_mode"       # "net_mode" = one-way (long of short, niet beide tegelijk)
```

> Credentials nooit in `config.yaml` committen — gebruik environment variables of
> `.env` (al in `.gitignore`).

---

## Fase 4 — `OKXBroker` implementeren

**Bestand:** `src/trading/broker/okx.py`

Implementeert `AbstractBroker` via de `python-okx` SDK (niet raw REST).

### Endpoints per methode

| Methode | OKX endpoint | Aandachtspunten |
|---|---|---|
| `place_order()` | `POST /api/v5/trade/order` | Limit order + `attachAlgoOrds` voor SL/TP |
| `on_candle()` | `GET /api/v5/trade/orders-pending` | Polling per candle voor fills + sluitingen |
| `open_orders()` | `GET /api/v5/trade/positions` | Vertaal OKX positie-object naar `Order` dataclass |
| `closed_orders()` | `GET /api/v5/trade/orders-history` | Paginering nodig voor langere periodes |
| `equity()` | `GET /api/v5/account/balance` | Gebruik `totalEq` uit response |
| `cancel_order()` | `POST /api/v5/trade/cancel-order` | OKX `ordId` meegeven |

### Contract-grootte correctie

OKX BTC-USDT-SWAP: **1 contract = 0.01 BTC**

De `size`-berekening in `AbstractBroker` geeft BTC-eenheden terug. Omzetten:

```python
contracts = max(1, round(size_in_btc / 0.01))
```

Dit is een kritisch verschil met de paper broker — geen omzetting hier betekent
orders die factor 100 te groot of te klein zijn.

### Leverage en margin instellen (éénmalig bij opstart)

```python
trade_api.set_leverage(
    instId="BTC-USDT-SWAP",
    lever=str(cfg["derivatives"]["leverage"]),
    mgnMode=cfg["derivatives"]["margin_mode"],
)
```

### SL/TP als attached algo-orders

OKX ondersteunt `attachAlgoOrds` bij het plaatsen van een order — de exchange
bewaakt dan zelf de SL/TP, ook als de bot offline gaat. Dit is de meest
betrouwbare aanpak voor derivaten.

### Rate limits

OKX: 60 order-requests per 2 seconden. Bij polling op candle-close ruim
voldoende; geen throttling nodig voor normaal gebruik.

### Funding rate

Elke 8 uur een fee of credit op open posities. Optioneel te loggen via
`GET /api/v5/account/bills`. Relevant bij langlopende posities.

---

## Fase 5 — OKX live data feed (WebSocket)

**Bestand:** `src/feeds/okx_feed.py`

Voor live trading is een WebSocket stream nodig — REST polling is te traag en
minder betrouwbaar dan de Binance feed.

**WebSocket endpoint:** `wss://ws.okx.com:8443/ws/v5/public`
**Kanaal:** `candle15m` (of `candle1m` voor fijnere granulariteit)

```python
{"op": "subscribe", "args": [{"channel": "candle15m", "instId": "BTC-USDT-SWAP"}]}
```

OKX WebSocket-specifieke aandachtspunten:
- Candles worden in **omgekeerde volgorde** geleverd (nieuwste eerst)
- Een candle is gesloten als het veld `confirm == "1"`
- **Reconnect-logica verplicht**: verbinding dropt regelmatig; automatische
  herverbinding met exponential backoff (1s → 2s → 4s → max 30s)
- Heartbeat: stuur elke 25 seconden `{"op": "ping"}`, verwacht `"pong"` terug

---

## Fase 6 — Notificaties (Telegram)

**Bestand:** `src/notifications/notifier.py`

Slanke `Notifier` klasse via Telegram Bot API (`requests.post`).

| Event | Bericht |
|---|---|
| Trade opened | Symbool, richting, entry / SL / TP, contracten |
| Trade closed | Win/loss, P&L, huidige equity |
| Circuit breaker triggered | Reden, staat van bot |
| API/WebSocket error (na retries) | Beschrijving van fout |
| Bot gestart / gestopt | Timestamp, filter, kapitaal |
| Liquidatiewaarschuwing | Als prijs liquidatieprijs nadert |

```yaml
notifications:
  telegram:
    enabled: true
    bot_token: ""    # TELEGRAM_BOT_TOKEN via env
    chat_id:   ""    # TELEGRAM_CHAT_ID via env
```

`Notifier` is een optionele dependency van `OrderManager` en `PaperTrader`.
Als `enabled: false` of token leeg: no-op.

---

## Fase 7 — State persistentie

**Bestanden:** `src/trading/broker/paper.py` + `src/trading/broker/okx.py`

**PaperBroker:** `save_state(path)` → JSON met open/pending orders + kapitaal.
Bij startup: `load_state(path)` als bestand bestaat.

**OKXBroker:** bij startup `reconcile()` — haalt open posities op van exchange
en vergelijkt met lokale JSONL log. Discrepanties worden gelogd en gemeld via
notificatie. Voorkomt dubbele orders bij herstart.

State file: `logs/state_<datum>.json`

---

## Fase 8 — Live trader entrypoint + testnet checklist

**Bestand:** `scripts/run_live_trader.py`

```
python scripts/run_live_trader.py --filter regime_long --exchange okx
```

### Testnet checklist (doorlopen vóór live kapitaal)

- [ ] Balance-ophaling werkt
- [ ] Leverage en margin mode worden correct ingesteld bij opstart
- [ ] Een limit order wordt geplaatst en verschijnt in OKX interface
- [ ] SL/TP zijn zichtbaar als attached algo-orders
- [ ] Contract-grootte klopt (controleer in OKX interface)
- [ ] Order fill wordt gedetecteerd in `on_candle`
- [ ] Positie sluiting (SL of TP geraakt) wordt correct verwerkt
- [ ] `equity()` klopt na een gesloten trade
- [ ] Circuit breaker stopt nieuwe orders na N consecutive losses
- [ ] Telegram notificaties komen binnen bij trade events
- [ ] Geen dubbele orders bij herstart van de trader
- [ ] WebSocket reconnect werkt na kunstmatige verbrekng

---

## Aandachtspunten derivaten

| Punt | Detail |
|---|---|
| **Contract-grootte** | 1 BTC-USDT-SWAP contract = 0.01 BTC; omzetting verplicht |
| **Leverage** | Met `isolated` margin is risico per positie afgebakend, maar bij hoge leverage snel geliquideerd |
| **Liquidatieprijs** | Monitoren en notificeren als prijs liquidatieniveau nadert |
| **Funding rate** | Elke 8 uur; loggen voor correcte P&L-berekening op langere posities |
| **Rate limits** | 60 order-requests / 2s; voldoende voor candle-close polling |
| **Secrets beheer** | `python-dotenv` of env vars; nooit credentials in `config.yaml` |
| **Position mode** | `net_mode` (one-way): vereenvoudigt long/short beheer; geen hedging |

---

## Volgorde van implementatie

| Stap | Fase | Geschatte tijd | Prioriteit |
|---|---|---|---|
| 1 | Circuit breaker + daily loss limit | ~2-3 uur | Eerst — vangt catastrofaal verlies op |
| 2 | Retry + stale-candle check | ~1 uur | Veerkracht bij API-uitval |
| 3 | OKX account + config | ~1 uur | Vereiste voor Fase 4-5 |
| 4 | Telegram notificaties | ~1-2 uur | Vroeg erin — bewaking tijdens OKX testen |
| 5 | OKXBroker (testnet) | ~3-4 uur | Meeste werk |
| 6 | OKX WebSocket feed | ~2-3 uur | Vervangt REST polling |
| 7 | Live trader entrypoint | ~1 uur | |
| 8 | State persistentie | ~2 uur | |
| 9 | Testnet checklist doorlopen | — | Vóór live kapitaal |
