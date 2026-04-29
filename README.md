# BTC SMC Trader

Backtesting én live/paper trading van een Smart Money Concepts strategie op BTC/USDT.

**Kernhypothese:** OB → sweep → CHoCH → retest geeft een positieve edge wanneer het HMM-regime bullish of bearish is.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Backtest

```bash
python scripts/download_data.py      # ~5 min
python scripts/build_cache.py        # ~30–60 min (eenmalig)
python scripts/run_backtest.py --set in_sample
python scripts/run_backtest.py --set oos   # alleen na bevroren parameters
```

### Paper trading (geen echte orders)

```bash
python scripts/run_paper_trader.py --filter regime_long
```

### Live trading (OKX)

Zet eerst je credentials in een `.env` bestand (zie **Credentials** hieronder), daarna:

```bash
python scripts/run_live_trader.py --filter regime_long --exchange okx
```

Stop met `Ctrl+C`.

#### Filter opties

| Filter | Beschrijving |
|--------|-------------|
| `baseline` | geen filters (alle sweeps) |
| `regime` | alleen sweeps in lijn met HMM regime |
| `long_only` | alleen bearish sweeps → long |
| `short_only` | alleen bullish sweeps → short |
| `bos10` | wacht op BOS bevestiging binnen 10 candles |
| `regime_long` | regime + alleen long |
| `regime_short` | regime + alleen short |

## Credentials

API-sleutels en het Telegram bot token worden **nooit** in `config.yaml` of in versiebeheer opgeslagen.

De applicatie ondersteunt twee manieren om secrets te laden — lokaal via `.env`, op een server via OCI Vault:

| Omgeving | Methode |
|----------|---------|
| Lokaal | `.env` bestand (python-dotenv) |
| Oracle Cloud instance | OCI Vault Secret Management |

De keuze wordt automatisch gemaakt op basis van de omgevingsvariabele `OCI_VAULT_OCID`: als die gezet is, wordt OCI Vault gebruikt; anders `.env`.

### Lokaal (.env)

```bash
cp .env.example .env
# Vul je eigen waarden in .env
```

De `.env` staat in `.gitignore` en wordt nooit gecommit.

**OKX API-sleutel aanmaken:** OKX → Account → API Management → Create API Key. Kies rechten: `Trade` (en optioneel `Read`). Sla de passphrase direct op — OKX toont hem maar één keer.

**Telegram bot aanmaken:** stuur `/newbot` naar `@BotFather`. Je `chat_id` vind je via:
```
https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
```
(stuur eerst een bericht naar je bot zodat er een update verschijnt)

### Oracle Cloud (OCI Vault)

#### 1. Vault aanmaken

OCI Console → *Security → Vault → Create Vault*. Noteer het **Vault OCID**.

#### 2. Secrets aanmaken

Maak in de vault vijf secrets aan (type: *Plaintext*) met exact deze namen:

| Secret naam | Inhoud |
|-------------|--------|
| `OKX_API_KEY` | OKX API key |
| `OKX_API_SECRET` | OKX API secret |
| `OKX_PASSPHRASE` | OKX passphrase |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |

#### 3. Instance Principal instellen

De instance moet toestemming krijgen om de secrets te lezen zonder dat er een config-bestand op de server staat.

**Dynamic Group aanmaken** (OCI Console → *Identity → Dynamic Groups*):
```
instance.id = 'ocid1.instance.oc1.eu-amsterdam-1.<jouw-instance-ocid>'
```

**IAM Policy toevoegen** (OCI Console → *Identity → Policies*):
```
Allow dynamic-group <naam-dynamic-group> to read secret-family in compartment <jouw-compartment>
```

#### 4. Vault OCID instellen op de instance

Voeg toe aan je systemd service of `~/.bashrc`:

```bash
export OCI_VAULT_OCID=ocid1.vault.oc1.eu-amsterdam-1.<jouw-vault-ocid>
```

Daarna werkt de bot zonder `.env` bestand op de server:

```bash
python scripts/run_live_trader.py --filter regime_long --exchange okx
```

## Deployment op Oracle Cloud (Docker)

Vereisten: OCI Vault en Instance Principal zijn al geconfigureerd (zie **Credentials → Oracle Cloud** hierboven), Docker is geïnstalleerd op de instance.

### Eenmalige installatie

```bash
# SSH naar de instance
ssh ubuntu@<oracle-ip>

# Docker installeren (alleen eerste keer)
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
# Log opnieuw in zodat de groepswijziging actief wordt

# OCI_VAULT_OCID permanent instellen
echo 'export OCI_VAULT_OCID=ocid1.vault.oc1.eu-amsterdam-1.<jouw-vault-ocid>' >> ~/.bashrc
source ~/.bashrc

# Repo clonen
git clone git@github.com:<jouw-naam>/btc-smc-trader.git
cd btc-smc-trader
```

### Bot starten

```bash
docker compose up -d --build
docker compose logs -f   # logs volgen (Ctrl+C om te stoppen)
```

De bot herstart automatisch na een crash of reboot.

### Updates doorvoeren

```bash
git pull
docker compose up -d --build
```

## Projectstructuur

```
btc_smc_trader/
├── .env.example              ← template voor credentials (wél in git)
├── config/config.yaml        ← alle tuneable parameters
├── data/
│   ├── raw/                  ← 1m parquet per jaar (gegenereerd)
│   ├── processed/            ← 15m en 4h parquet (gegenereerd)
│   └── smc_cache/            ← SMC output per kwartaal (gegenereerd)
├── src/
│   ├── config_loader.py
│   ├── data/
│   │   ├── downloader.py     ← Binance API + rate limiting + checkpoint
│   │   ├── aggregator.py     ← upsampling 1m → 15m/4h
│   │   └── cache.py          ← SMC cache bouwen en laden
│   ├── feeds/
│   │   ├── binance_feed.py   ← WebSocket feed voor paper trading
│   │   └── okx_feed.py       ← WebSocket feed voor live trading
│   ├── notifications/
│   │   └── notifier.py       ← Telegram notificaties
│   ├── regime/hmm.py         ← HMM 2-state regime detector
│   ├── smc/
│   │   ├── signals.py        ← SMC library wrapper
│   │   └── lifecycle.py      ← state machine OB→sweep→CHoCH→entry
│   ├── trading/
│   │   ├── order_manager.py
│   │   └── broker/
│   │       ├── paper.py      ← paper broker (geen echte orders)
│   │       └── okx.py        ← OKX broker (live handel)
│   └── backtest/
│       ├── engine.py         ← backtest loop
│       └── metrics.py        ← Sharpe, drawdown, win rate, etc.
└── scripts/
    ├── download_data.py
    ├── build_cache.py
    ├── run_backtest.py
    ├── run_paper_trader.py
    └── run_live_trader.py
```

## In-sample / Out-of-sample

| Set | Periode | Gebruik |
|-----|---------|---------|
| In-sample | 2019–2022 | HMM trainen, parameters tunen (max 3 rondes) |
| Out-of-sample | 2023–2024 | Éénmalig na bevroren parameters |

**OOS-data is afgegrendeld in code.** `allow_oos=True` alleen voor de finale evaluatie.

## Interpretatie resultaten

| Resultaat | Conclusie |
|-----------|-----------|
| Sharpe > 1.0, > 100 trades op OOS | Edge aanwezig |
| Sharpe 0.5–1.0 | Zwak signaal, parameters onderzoeken |
| Sharpe < 0.5 of < 50 trades | Kernhypothese herzien |
