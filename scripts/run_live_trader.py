"""
scripts/run_live_trader.py — Start de live trading loop op OKX of paper.

Gebruik:
    python scripts/run_live_trader.py --filter long_only --exchange paper
    python scripts/run_live_trader.py --filter long_only --exchange okx
    python scripts/run_live_trader.py --filter regime_long --exchange paper --multi-coin

Credentials via environment variables (nooit in config.yaml):
    OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Filter opties:
    baseline      geen filters (alle sweeps)
    regime        alleen sweeps in lijn met HMM regime
    long_only     alleen bearish sweeps → long
    short_only    alleen bullish sweeps → short
    bos10         wacht op BOS bevestiging binnen 10 candles
    regime_long   regime + alleen long
    regime_short  regime + alleen short

Exchange opties:
    okx           live OKX broker (BTC-USDT-SWAP, OKX WebSocket feed)
    paper         paper broker + Binance feed (testen zonder live orders)

Multi-coin:
    --multi-coin  Trade alle coins uit config.yaml [coins]-sectie (paper en okx).
                  Elke coin krijgt een eigen risicoprofiel op basis van de tier
                  (large_cap = 1% risk, mid_cap = 0.5% risk).
                  Bij okx: één gedeeld account, equity éénmaal opgehaald.

Stop met: Ctrl+C
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.secrets_loader import load_secrets

load_secrets()

from src.config_loader import load_config
from src.notifications.notifier import Notifier
from src.signals.detector import SweepDetector
from src.signals.filters import SweepFilters
from src.trading.broker.paper import PaperBroker
from src.trading.funding_rate import build_funding_filter
from src.trading.order_manager import AccountCircuitBreaker, OrderManager
from src.trading.paper_trader import CoinRunner, MultiCoinTrader, PaperTrader, RegimeProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

_FILTER_PRESETS: dict[str, SweepFilters] = {
    "baseline":      SweepFilters(direction="both"),
    "regime":        SweepFilters(regime=True),
    "long_only":     SweepFilters(direction="long"),
    "short_only":    SweepFilters(direction="short"),
    "bos10":         SweepFilters(bos_confirm=True, bos_window=10),
    "bos20":         SweepFilters(bos_confirm=True, bos_window=20),
    "regime_long":   SweepFilters(regime=True, direction="long"),
    "regime_short":  SweepFilters(regime=True, direction="short"),
    "regime_bos10":  SweepFilters(regime=True, bos_confirm=True, bos_window=10),
    "long_bos10":    SweepFilters(direction="long",  bos_confirm=True, bos_window=10),
    "short_bos10":   SweepFilters(direction="short", bos_confirm=True, bos_window=10),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Live trading loop starten.")
    parser.add_argument(
        "--filter",
        default="baseline",
        choices=list(_FILTER_PRESETS.keys()),
        help="Welke sweep-filter gebruiken (standaard: baseline)",
    )
    parser.add_argument(
        "--exchange",
        default="okx",
        choices=["okx", "paper"],
        help="Broker: 'okx' (live) of 'paper' (simulatie, standaard: okx)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Startkapitaal in USDT — alleen voor paper mode (standaard: uit config)",
    )
    parser.add_argument(
        "--no-regime",
        action="store_true",
        help="HMM regime provider uitschakelen",
    )
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--multi-coin",
        action="store_true",
        help="Trade alle coins uit config.yaml [coins]-sectie (paper en okx)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    # -----------------------------------------------------------------------
    # Multi-coin modus (paper only)
    # -----------------------------------------------------------------------
    if args.multi_coin:
        _run_multi_coin(cfg, args)
        return

    # --- Broker + feed ---
    if args.exchange == "okx":
        from src.feeds.okx_feed import OKXFeed
        from src.trading.broker.okx import OKXBroker

        broker = OKXBroker(cfg)
        broker.reconcile()   # check state bij herstart
        feed   = OKXFeed(cfg)

        symbol  = cfg["derivatives"]["symbol"]
        fee_pct = 0.0   # OKX rekent fees via funding/spread; P&L via exchange

    else:  # paper
        from src.feeds.binance_feed import BinanceFeed
        from src.trading.broker.paper import PaperBroker

        if args.capital is not None:
            cfg["risk"]["capital_initial"] = args.capital

        ts_cfg       = cfg.get("risk", {}).get("trailing_stop", {})
        trailing_cfg = ts_cfg if ts_cfg.get("enabled", False) else {}
        pe_cfg       = cfg.get("risk", {}).get("partial_exit", {})
        partial_cfg  = pe_cfg if pe_cfg.get("enabled", False) else {}

        broker = PaperBroker(
            initial_capital  = cfg["risk"]["capital_initial"],
            fee_pct          = cfg["backtest"]["fee_pct"],
            max_open         = cfg["risk"]["max_open_trades"],
            trailing_cfg     = trailing_cfg,
            partial_exit_cfg = partial_cfg,
        )

        # Laad eerder opgeslagen state als die bestaat
        state_file = Path("logs") / "paper_state.json"
        broker.load_state(state_file)

        feed   = BinanceFeed(cfg)
        symbol = cfg["data"]["symbol"]

    # --- Notifier ---
    notifier = Notifier.from_cfg(cfg)

    # --- Detector ---
    filters = _FILTER_PRESETS[args.filter]
    detector = SweepDetector(
        filters       = filters,
        reward_ratio  = cfg["risk"]["reward_ratio"],
        sl_buffer_pct = cfg["risk"]["sl_buffer_pct"],
    )

    # --- OrderManager met circuit breaker ---
    cb_cfg         = cfg.get("risk", {}).get("circuit_breaker")
    funding_filter = build_funding_filter(cfg)
    order_manager  = OrderManager(
        broker         = broker,
        symbol         = symbol,
        risk_pct       = cfg["risk"]["risk_per_trade_pct"],
        max_open       = cfg["risk"]["max_open_trades"],
        cb_cfg         = cb_cfg,
        notifier       = notifier,
        funding_filter = funding_filter,
    )

    # --- Regime provider ---
    regime_provider = None
    if filters.regime and not args.no_regime:
        model_path = (
            Path(cfg["data"]["paths"]["processed"]) / "hmm_regime_model.pkl"
        )
        if model_path.exists():
            regime_provider = RegimeProvider(cfg)
        else:
            print(
                f"Geen regime model op {model_path}.\n"
                f"Train eerst: python scripts/run_backtest.py --set in_sample\n"
                f"Of gebruik --no-regime om zonder regime filter te draaien."
            )
            sys.exit(1)

    # --- Start ---
    notifier.notify_started(symbol, args.filter, broker.equity())

    print(f"\n{'='*55}")
    print(f"  LIVE TRADER GESTART  [{args.exchange.upper()}]")
    print(f"  Symbool:  {symbol}")
    print(f"  Filter:   {args.filter}")
    print(f"  Equity:   {broker.equity():.2f} USDT")
    print(f"  CB:       {cb_cfg or 'uitgeschakeld'}")
    if cfg["notifications"]["telegram"]["enabled"]:
        print(f"  Telegram: aan")
    print(f"{'='*55}\n")

    heartbeat_hours = cfg.get("notifications", {}).get("telegram", {}).get("heartbeat_hours")
    trader = PaperTrader(
        feed            = feed,
        detector        = detector,
        order_manager   = order_manager,
        regime_provider = regime_provider,
        heartbeat_hours = heartbeat_hours,
    )

    try:
        trader.start()
    finally:
        notifier.notify_stopped(broker.equity())
        # Sla paper state op bij stoppen
        if args.exchange == "paper":
            broker.save_state(Path("logs") / "paper_state.json")


def _run_multi_coin(cfg: dict, args) -> None:
    """Start multi-coin trading (paper of OKX) voor alle coins in config."""
    from src.feeds.binance_feed import BinanceFeed

    is_okx = args.exchange == "okx"

    if is_okx:
        from src.feeds.okx_feed import OKXFeed
        from src.trading.broker.okx import OKXBroker

    coins    = cfg.get("coins", [{"symbol": cfg["data"]["symbol"], "tier": "large_cap"}])
    tiers    = cfg.get("risk_tiers", {})
    filters  = _FILTER_PRESETS[args.filter]
    notifier = Notifier.from_cfg(cfg)

    total_cap    = args.capital if args.capital is not None else cfg["risk"]["capital_initial"]
    n_coins      = len(coins)
    cap_per_coin = total_cap / n_coins   # alleen relevant voor paper

    # Gedeelde account-niveau circuit breaker (alleen relevant voor OKX: één account)
    account_cb: AccountCircuitBreaker | None = None

    exchange_label = "OKX" if is_okx else "PAPER"
    print(f"\n{'='*55}")
    print(f"  MULTI-COIN TRADER [{exchange_label}] — {n_coins} coins")
    if not is_okx:
        print(f"  Totaal kapitaal:  {total_cap:.0f} USDT")
        print(f"  Per coin:         {cap_per_coin:.0f} USDT")
    print(f"  Filter:           {args.filter}")
    for coin in coins:
        tier     = coin.get("tier", "large_cap")
        tier_cfg = tiers.get(tier, {})
        risk_pct = tier_cfg.get("risk_per_trade_pct", cfg["risk"]["risk_per_trade_pct"])
        sym_label = coin.get("swap_symbol", coin["symbol"]) if is_okx else coin["symbol"]
        lev_label = f"  lev={coin.get('leverage', cfg['derivatives']['leverage'])}x" if is_okx else ""
        print(f"  {sym_label:<16} tier={tier:<12} risk={risk_pct:.1f}%/trade{lev_label}")
    print(f"{'='*55}\n")

    runners: list[CoinRunner] = []
    brokers: list = []   # bijhouden voor equity bij afsluiten

    for coin in coins:
        sym      = coin["symbol"]                           # Binance symbool (BTCUSDT)
        swap_sym = coin.get("swap_symbol", sym)             # OKX symbool  (BTC-USDT-SWAP)
        tier     = coin.get("tier", "large_cap")
        tier_cfg = tiers.get(tier, {})
        risk_pct = tier_cfg.get("risk_per_trade_pct", cfg["risk"]["risk_per_trade_pct"])
        cb_cfg   = tier_cfg.get("circuit_breaker", cfg["risk"].get("circuit_breaker"))

        if is_okx:
            leverage = coin.get("leverage") or cfg["derivatives"]["leverage"]
            broker = OKXBroker(cfg, symbol=swap_sym, leverage=leverage)
            broker.reconcile()
            feed   = OKXFeed(cfg, symbol=swap_sym)
            order_sym = swap_sym

            # Maak gedeelde account CB aan na de eerste broker (equity = totaal account)
            if account_cb is None:
                acb_cfg = cfg.get("risk", {}).get("circuit_breaker")
                if acb_cfg:
                    account_cb = AccountCircuitBreaker(
                        max_daily_loss_pct = acb_cfg.get("max_daily_loss_pct", 3.0),
                        max_drawdown_pct   = acb_cfg.get("max_drawdown_pct", 10.0),
                        start_capital      = broker.equity(),
                    )
        else:
            ts_cfg      = cfg.get("risk", {}).get("trailing_stop", {})
            pe_cfg      = cfg.get("risk", {}).get("partial_exit", {})
            broker = PaperBroker(
                initial_capital  = cap_per_coin,
                fee_pct          = cfg["backtest"]["fee_pct"],
                max_open         = cfg["risk"]["max_open_trades"],
                trailing_cfg     = ts_cfg if ts_cfg.get("enabled", False) else {},
                partial_exit_cfg = pe_cfg if pe_cfg.get("enabled", False) else {},
            )
            feed      = BinanceFeed(cfg, symbol=sym)
            order_sym = sym

        brokers.append(broker)

        detector = SweepDetector(
            filters       = filters,
            reward_ratio  = cfg["risk"]["reward_ratio"],
            sl_buffer_pct = cfg["risk"]["sl_buffer_pct"],
        )

        order_manager = OrderManager(
            broker         = broker,
            symbol         = order_sym,
            risk_pct       = risk_pct,
            max_open       = cfg["risk"]["max_open_trades"],
            cb_cfg         = cb_cfg,
            notifier       = notifier,
            account_cb     = account_cb,
            funding_filter = build_funding_filter(cfg),
        )

        regime_provider = None
        if filters.regime and not args.no_regime:
            model_path = (
                Path(cfg["data"]["paths"]["processed"])
                / f"hmm_regime_model_{sym}.pkl"
            )
            fallback_path = (
                Path(cfg["data"]["paths"]["processed"])
                / "hmm_regime_model.pkl"
            )
            if model_path.exists() or fallback_path.exists():
                regime_provider = RegimeProvider(cfg, symbol=sym)
            else:
                print(
                    f"Geen regime model voor {sym}. "
                    "Train eerst via run_backtest.py of gebruik --no-regime."
                )
                sys.exit(1)

        runners.append(CoinRunner(
            symbol          = order_sym,
            feed            = feed,
            detector        = detector,
            order_manager   = order_manager,
            regime_provider = regime_provider,
        ))

    symbols_str = ", ".join(r.symbol for r in runners)
    if is_okx:
        startup_equity = brokers[0].equity() if brokers else 0.0
    else:
        startup_equity = sum(b.equity() for b in brokers)
    notifier.send(
        f"[BOT GESTART — MULTI-COIN]\n"
        f"Coins: {symbols_str}\n"
        f"Filter: {args.filter}\n"
        f"Kapitaal: {startup_equity:.2f} USDT"
    )

    heartbeat_hours = cfg.get("notifications", {}).get("telegram", {}).get("heartbeat_hours")
    trader = MultiCoinTrader(
        runners,
        notifier        = notifier,
        heartbeat_hours = heartbeat_hours,
    )
    try:
        trader.start()
    finally:
        if is_okx:
            # Alle OKX-brokers delen één account; equity éénmaal ophalen
            final_eq = brokers[0].equity() if brokers else 0.0
        else:
            final_eq = sum(r.order_manager.stats.current_capital for r in runners)
        notifier.notify_stopped(final_eq)


if __name__ == "__main__":
    main()
