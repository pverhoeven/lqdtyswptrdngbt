# OKX Live Trading — Implementatieplan

## Doel

De bestaande paper trading setup uitbreiden met een `OKXBroker` die live derivaten (BTC-USDT-SWAP perpetual) kan traden via de OKX exchange. De `AbstractBroker` interface is al klaar; dit plan beschrijft wat er verder nodig is.

---

## Stap 1 — OKX account en API-sleutels

**Wat:** Aanmaken van een OKX demo-account en API-credentials.

**Hoe:**
1. Ga naar `https://www.okx.com/en/demo-trading` en maak een demo-account aan.
2. Genereer API-sleutels via **Account → API Management**:
   - API Key
   - Secret Key
   - Passphrase (OKX-specifiek, verplicht)
3. Zet rechten op: **Trade** (geen Withdraw nodig).

**Verificatie:** API-sleutels werken met een simpele balance-check via `python-okx`.

---

## Stap 2 — Dependency installeren

```bash
pip install python-okx
```

Voeg toe aan `requirements.txt`:
```
python-okx>=0.4.0
```

---

## Stap 3 — Configuratie uitbreiden

Voeg toe aan `config/config.yaml`:

```yaml
okx:
  api_key:     ""           # uit OKX API Management
  api_secret:  ""
  passphrase:  ""
  testnet:     true         # false = live handel

derivatives:
  symbol:        "BTC-USDT-SWAP"  # perpetual swap
  leverage:      5                # 1–125x
  margin_mode:   "isolated"       # "isolated" of "cross"
  position_mode: "net_mode"       # "net_mode" (one-way) of "long_short_mode"
```

**Let op:** Credentials nooit in git. Gebruik environment variables of een `.env` bestand (al in `.gitignore`).

---

## Stap 4 — `OKXBroker` implementeren

**Bestand:** `src/trading/broker/okx.py`

Implementeert `AbstractBroker` (zelfde interface als `PaperBroker`).

### Kernlogica per methode

| Methode | OKX endpoint | Aandachtspunten |
|---------|-------------|-----------------|
| `place_order` | `POST /api/v5/trade/order` | Limit order + attached TP/SL als algo orders |
| `on_candle` | `GET /api/v5/trade/orders-pending` | Polling op elke candle voor fill/close events |
| `open_orders` | `GET /api/v5/trade/positions` | Vertaal OKX positie-object naar `Order` dataclass |
| `closed_orders` | `GET /api/v5/trade/orders-history` | Paginering nodig voor lange periodes |
| `equity` | `GET /api/v5/account/balance` | Haal `totalEq` op uit response |
| `cancel_order` | `POST /api/v5/trade/cancel-order` | OKX orderId meegeven |

### Contract-grootte correctie

OKX BTC-USDT-SWAP: **1 contract = 0.01 BTC**

De `size` berekening in `base.py` geeft BTC-eenheden terug. Omzetten naar contracten:

```python
contracts = round(size_in_btc / 0.01)  # minimaal 1 contract
```

### Leverage en margin instellen (éénmalig bij opstart)

```python
# Via python-okx SDK
trade_api.set_leverage(instId="BTC-USDT-SWAP", lever="5", mgnMode="isolated")
```

---

## Stap 5 — OKX live data feed

**Bestand:** `src/feeds/okx_feed.py`

De `BinanceFeed` haalt historische data op. Voor live trading is een WebSocket stream nodig.

**WebSocket endpoint:** `wss://ws.okx.com:8443/ws/v5/public`  
**Kanaal:** `candle1m` (1-minuut kaarsen, sluit zodra de candle volledig is)

```python
# Abonneren op candle stream
{"op": "subscribe", "args": [{"channel": "candle1m", "instId": "BTC-USDT-SWAP"}]}
```

De feed roept na elke gesloten candle `broker.on_candle()` aan — identiek aan de backtest flow.

---

## Stap 6 — Paper trader aanpassen voor live gebruik

In `scripts/run_paper_trader.py` (of nieuw `scripts/run_live_trader.py`):

```python
from src.trading.broker.okx import OKXBroker

broker = OKXBroker(cfg["okx"], cfg["derivatives"])
# Vervangt PaperBroker — rest van de loop blijft ongewijzigd
```

---

## Stap 7 — Testen op testnet

Checklist vóór live kapitaal:

- [ ] Balance-ophaling werkt
- [ ] Leverage en margin mode worden correct ingesteld
- [ ] Een limit order wordt geplaatst en verschijnt in OKX interface
- [ ] SL/TP worden als attached orders zichtbaar
- [ ] Order fill wordt gedetecteerd in `on_candle`
- [ ] Positie sluiting (SL of TP geraakt) wordt correct verwerkt
- [ ] `equity()` klopt na een gesloten trade
- [ ] Geen dubbele orders bij herstart van de trader

---

## Aandachtspunten

| Punt | Detail |
|------|--------|
| **Funding rate** | Elke 8 uur een fee/credit; relevant voor langlopende posities. Optioneel te loggen. |
| **Rate limits** | OKX: 60 order-requests per 2 seconden; bij `on_candle` polling voldoende. |
| **Liquidatieprijs** | Met `isolated` margin is risico afgebakend, maar bij hoge leverage snel bereikt. |
| **Reconnect** | WebSocket verbinding vereist automatische reconnect-logica bij drops. |
| **Secrets beheer** | Gebruik `python-dotenv` of omgevingsvariabelen; nooit credentials in `config.yaml` committen. |

---

## Volgorde samengevat

```
Stap 1  Account + API-sleutels (demo)
Stap 2  python-okx installeren
Stap 3  config.yaml uitbreiden
Stap 4  OKXBroker implementeren
Stap 5  OKX live feed (WebSocket)
Stap 6  Live trader script aanpassen
Stap 7  End-to-end testen op testnet
        ↓
        Live zetten (testnet → false)
```
