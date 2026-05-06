"""
feeds/okx_multi_feed.py — Multi-pair sweep scanner via OKX WebSocket.

Bewaakt meerdere XPERP instrumenten tegelijk via één WebSocket verbinding.
Roept een callback aan bij elke gedetecteerde liquidity sweep — geen orders.

Gebruik:
    scanner = SweepScanner(inst_ids, cfg, on_sweep=callback)
    scanner.start()   # blokkeert; gebruik stop() vanuit signal handler
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd
import requests

from src.signals.detector import SweepDetector, SweepSignal
from src.signals.filters import SweepFilters

logger = logging.getLogger(__name__)

_PING_INTERVAL  = 25   # seconden tussen heartbeats naar OKX
_MAX_BACKOFF    = 30   # maximale reconnect-wachttijd in seconden
_REST_BASE      = "https://eea.okx.com"
_WS_URL         = "wss://wseea.okx.com:8443/ws/v5/business"
_REST_DELAY     = 0.15  # seconden tussen REST-warmup calls (rate limiting)
_MIN_CANDLES    = 20   # minimale buffer voor SMC-berekening


@dataclass
class _PairState:
    inst_id:  str
    buffer:   deque = field(default_factory=deque)
    detector: SweepDetector = field(default=None)
    last_ts:  pd.Timestamp | None = None


class SweepScanner:
    """
    Bewaakt meerdere OKX XPERP instrumenten op liquidity sweeps.

    Parameters
    ----------
    inst_ids : list[str]
        OKX instrument-IDs om te monitoren.
    cfg : dict
        Volledige config dict.
    on_sweep : Callable[[str, SweepSignal], None]
        Callback bij gedetecteerde sweep: (inst_id, signal).
    """

    def __init__(
        self,
        inst_ids:  list[str],
        cfg:       dict,
        on_sweep:  Callable[[str, SweepSignal], None],
    ) -> None:
        try:
            import websocket  # noqa: F401
        except ImportError:
            raise ImportError(
                "websocket-client niet geïnstalleerd. "
                "Voer uit: pip install websocket-client"
            )

        self._cfg      = cfg
        self._on_sweep = on_sweep
        self._running  = False
        self._ws       = None

        signal_tf = cfg["data"]["timeframes"]["signal"]
        match = re.match(r"(\d+)", signal_tf)
        minutes = int(match.group(1)) if match else 15
        self._channel = f"candle{minutes}m"

        swing_length = cfg["smc"]["swing_length"]
        buffer_size  = swing_length * 10

        scanner_cfg     = cfg.get("scanner", {})
        sweep_rejection = scanner_cfg.get("sweep_rejection", True)

        filters = SweepFilters(
            direction    = "both",
            regime       = False,
            bos_confirm  = False,
            sweep_rejection = sweep_rejection,
            atr_filter   = False,
        )

        reward_ratio  = cfg.get("risk", {}).get("reward_ratio",  1.5)
        sl_buffer_pct = cfg.get("risk", {}).get("sl_buffer_pct", 0.5)

        self._pairs: dict[str, _PairState] = {}
        for inst_id in inst_ids:
            state = _PairState(
                inst_id  = inst_id,
                buffer   = deque(maxlen=buffer_size),
                detector = SweepDetector(
                    filters       = filters,
                    reward_ratio  = reward_ratio,
                    sl_buffer_pct = sl_buffer_pct,
                ),
            )
            self._pairs[inst_id] = state

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start de scanner. Blokkeert tot stop() wordt aangeroepen."""
        self._warmup_all()
        self._running = True
        self._ws_loop()

    def stop(self) -> None:
        """Sluit WebSocket en stop de lus."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Warmup via REST
    # ------------------------------------------------------------------

    def _warmup_all(self) -> None:
        logger.info("Warmup voor %d instrumenten...", len(self._pairs))
        for inst_id, state in self._pairs.items():
            self._fill_buffer(inst_id, state)
            time.sleep(_REST_DELAY)
        logger.info("Warmup voltooid.")

    def _fill_buffer(self, inst_id: str, state: _PairState) -> None:
        try:
            bar    = self._channel.replace("candle", "")  # "15m"
            resp   = requests.get(
                f"{_REST_BASE}/api/v5/market/history-candles",
                params={"instId": inst_id, "bar": bar, "limit": str(state.buffer.maxlen)},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])

            for row in reversed(data):
                if row[8] == "1":
                    state.buffer.append(_parse_candle(row))

            if state.buffer:
                state.last_ts = pd.Timestamp(
                    state.buffer[-1]["open_time"], unit="ms", tz="UTC"
                )
            logger.debug(
                "Buffer gevuld voor %s: %d candles, laatste: %s",
                inst_id, len(state.buffer), state.last_ts,
            )
        except Exception as exc:
            logger.warning("Warmup mislukt voor %s: %s", inst_id, exc)

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    def _ws_loop(self) -> None:
        import websocket
        logging.getLogger("websocket").setLevel(logging.CRITICAL)

        backoff = 1
        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    _WS_URL,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = self._on_error,
                    on_close   = self._on_close,
                )
                self._ws = ws
                threading.Thread(
                    target=self._ping_loop, args=(ws,), daemon=True
                ).start()
                connected_at = time.monotonic()
                ws.run_forever()
                if time.monotonic() - connected_at > 60:
                    backoff = 1
            except Exception as exc:
                logger.warning("WebSocket crash: %s", exc)

            if not self._running:
                break

            logger.warning("WebSocket verbroken. Reconnect over %ds...", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    def _ping_loop(self, ws) -> None:
        while self._running:
            time.sleep(_PING_INTERVAL)
            try:
                ws.send("ping")
            except Exception:
                break

    def _on_open(self, ws) -> None:
        args = [
            {"channel": self._channel, "instId": inst_id}
            for inst_id in self._pairs
        ]
        ws.send(json.dumps({"op": "subscribe", "args": args}))
        logger.info(
            "Geabonneerd op %d instrumenten via %s", len(args), self._channel
        )

    def _on_message(self, ws, message: str) -> None:
        if message == "pong":
            return
        try:
            msg = json.loads(message)
            if "event" in msg:
                logger.debug("WS event: %s", msg)
                return

            inst_id = msg.get("arg", {}).get("instId")
            if not inst_id or inst_id not in self._pairs:
                return

            for row in msg.get("data", []):
                if len(row) >= 9 and row[8] == "1":
                    self._process_candle(inst_id, _parse_candle(row))

        except Exception as exc:
            logger.warning("WS parse fout: %s", exc)

    def _on_error(self, ws, error) -> None:
        logger.warning("WS fout: %s", error)

    def _on_close(self, ws, code, msg) -> None:
        logger.info("WS gesloten: %s %s", code, msg)

    # ------------------------------------------------------------------
    # Candle verwerken per pair
    # ------------------------------------------------------------------

    def _process_candle(self, inst_id: str, candle: dict) -> None:
        state = self._pairs[inst_id]
        ts    = pd.Timestamp(candle["open_time"], unit="ms", tz="UTC")

        if state.last_ts is not None and ts <= state.last_ts:
            return

        state.buffer.append(candle)
        state.last_ts = ts

        if len(state.buffer) < _MIN_CANDLES:
            return

        df = _buffer_to_df(state.buffer)

        try:
            from src.smc.signals import compute_signals
            signals_df = compute_signals(
                df, swing_length=self._cfg["smc"]["swing_length"]
            )
            if ts not in signals_df.index:
                return
            smc_row  = signals_df.loc[ts]
            ohlc_row = df.loc[ts]
        except Exception as exc:
            logger.debug("SMC mislukt voor %s: %s", inst_id, exc)
            return

        signal = state.detector.on_candle(ohlc_row, smc_row, regime=None)
        if signal:
            logger.info("Sweep: %s  %s", inst_id, signal)
            try:
                self._on_sweep(inst_id, signal)
            except Exception as exc:
                logger.warning("on_sweep callback fout: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_candle(row: list) -> dict:
    return {
        "open_time": int(row[0]),
        "open":      float(row[1]),
        "high":      float(row[2]),
        "low":       float(row[3]),
        "close":     float(row[4]),
        "volume":    float(row[5]),
    }


def _buffer_to_df(buffer: deque) -> pd.DataFrame:
    df = pd.DataFrame(list(buffer))
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("open_time").sort_index().astype(float)
