---
name: Paper trading live op Oracle instance
description: bos20 paper trader draait live op Oracle Cloud instance — mijlpaal en context voor forward-testing
type: project
---

bos20 paper trader actief op Oracle Cloud instance (gestart ~2026-05-03).

**Why:** Na uitgebreide backtest-validatie (causal shift geverifieerd, fill-rate en slippage stress-tests doorstaan) is de stap naar forward-testing gezet.

**Setup:** `python scripts/run_paper_trader.py --filter bos20`, OKX testnet, causal_shift=true cache, bos_window=20, 1% risico per trade, R:R 1.5.

**Succes-criterium:** Sharpe >1.5 na 6 maanden paper trading. Boven 2.0 zou een verrassing zijn.

**How to apply:** Bij vragen over live performance, degradatie t.o.v. backtest, of beslissing om over te gaan naar live trading — refereer aan dit mijlpaal en het forward-testing doel.
