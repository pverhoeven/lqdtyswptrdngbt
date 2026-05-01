"""
feeds/okx_feed.py — Live candle feed via OKX WebSocket + lokale SMC-buffer.

Interface identiek aan BinanceFeed: gebruik warmup() → poll() in de trading loop.

OKX WebSocket aandachtspunten:
- Candles: `confirm == "1"` = gesloten candle
- Candles worden in omgekeerde volgorde geleverd (nieuwste eerst)
- Reconnect-logica met exponential backoff (1s → 2s → 4s → max 30s)
- Heartbeat: ping elke 25s, verwacht pong

Gebruik:
    feed = OKXFeed(cfg)
    feed.warmup()        # verbind WebSocket en vul buffer initieel

    result = feed.poll() # None als geen nieuwe candle
    if result:
        ohlc_row, smc_row = result
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections import deque

import pandas as pd

logger = logging.getLogger(__name__)

_PING_INTERVAL = 25      # seconden tussen heartbeats
_MAX_BACKOFF   = 30      # maximale reconnect wachttijd in seconden


class OKXFeed:
    """
    Live OKX WebSocket candle feed met lokale SMC-buffer.

    Parameters
    ----------
    cfg : dict
        Volledige config dict.
    symbol : str, optional
        OKX instrument-ID (bijv. "ETH-USDT-SWAP"). Standaard: cfg["derivatives"]["symbol"].
    """

    def __init__(self, cfg: dict, symbol: str | None = None) -> None:
        try:
            import websocket  # noqa: F401
        except ImportError:
            raise ImportError(
                "websocket-client niet geïnstalleerd. "
                "Voer uit: pip install websocket-client"
            )

        okx_cfg = cfg["okx"]
        drv_cfg = cfg["derivatives"]
        self._inst_id      = symbol or drv_cfg["symbol"]  # bijv. "BTC-USDT-SWAP"
        self._swing_length = cfg["smc"]["swing_length"]
        self._buffer_size  = self._swing_length * 10

        signal_tf = cfg["data"]["timeframes"]["signal"]  # "15min"
        match = re.match(r"(\d+)", signal_tf)
        minutes = int(match.group(1)) if match else 15
        self._channel = f"candle{minutes}m"              # "candle15m"
        self._interval_minutes = minutes

        # Kies public WS URL op basis van testnet-vlag (candle channel is publiek)
        is_testnet = okx_cfg.get("testnet", True)
        if is_testnet:
            self._ws_url = "wss://wseeapap.okx.com:8443/ws/v5/public"
        else:
            self._ws_url = "wss://wseea.okx.com:8443/ws/v5/public"

        self._buffer: deque[dict] = deque(maxlen=self._buffer_size)
        self._last_candle_ts: pd.Timestamp | None = None

        self._candle_queue: queue.Queue = queue.Queue()
        self._ws_thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Initialisatie
    # ------------------------------------------------------------------

    def warmup(self) -> None:
        """
        Start WebSocket-verbinding en vul de buffer initieel via REST.
        Blokkeert tot de verbinding actief is.
        """
        # websocket-client logt zelf ook op ERROR-niveau bij drops; dat is ruis
        logging.getLogger("websocket").setLevel(logging.CRITICAL)
        self._fill_buffer_via_rest()
        self._running = True
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True, name="OKXFeedWS"
        )
        self._ws_thread.start()
        logger.info("OKX WebSocket gestart voor %s/%s", self._inst_id, self._channel)

    # ------------------------------------------------------------------
    # Poll (identieke interface als BinanceFeed)
    # ------------------------------------------------------------------

    def poll(self) -> tuple[pd.Series, pd.Series] | None:
        """
        Retourneert (ohlc_row, smc_row) van de nieuwste gesloten candle,
        of None als er nog geen nieuwe candle is.
        """
        try:
            candle_dict = self._candle_queue.get_nowait()
        except queue.Empty:
            logger.info("poll: queue leeg — geen gesloten candle ontvangen via WebSocket")
            return None

        closed_ts = pd.Timestamp(candle_dict["open_time"], unit="ms", tz="UTC")

        if self._last_candle_ts is not None and closed_ts <= self._last_candle_ts:
            logger.debug(
                "poll: dubbele candle overgeslagen (closed_ts=%s <= last=%s)",
                closed_ts, self._last_candle_ts,
            )
            return None

        self._buffer.append(candle_dict)
        self._last_candle_ts = closed_ts
        logger.debug("Nieuwe OKX candle: %s", closed_ts)

        df_buffer = self._buffer_to_df()
        smc_row   = self._compute_smc(df_buffer, closed_ts)
        ohlc_row  = df_buffer.loc[closed_ts]
        return ohlc_row, smc_row

    # ------------------------------------------------------------------
    # Buffer vullen via REST (voor warmup)
    # ------------------------------------------------------------------

    def _fill_buffer_via_rest(self) -> None:
        """Haal historische candles op via OKX REST API voor initiële buffer."""
        try:
            import requests
            url = "https://eea.okx.com/api/v5/market/history-candles"
            params = {
                "instId": self._inst_id,
                "bar":    self._channel.replace("candle", ""),  # "15m"
                "limit":  str(self._buffer_size),
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])

            # OKX levert candles omgekeerd (nieuwste eerst)
            for row in reversed(data):
                if row[8] == "1":   # alleen gesloten candles
                    self._buffer.append(_parse_rest_candle(row))

            if self._buffer:
                self._last_candle_ts = pd.Timestamp(
                    self._buffer[-1]["open_time"], unit="ms", tz="UTC"
                )
            logger.info(
                "OKX buffer gevuld via REST: %d candles, laatste: %s",
                len(self._buffer), self._last_candle_ts,
            )
        except Exception as exc:
            logger.warning("OKX REST warmup mislukt: %s", exc)

    def _refill_gaps_via_rest(self) -> None:
        """Haal de laatste candles op na reconnect om eventuele gaps te dichten."""
        try:
            import requests
            url = "https://eea.okx.com/api/v5/market/history-candles"
            params = {
                "instId": self._inst_id,
                "bar":    self._channel.replace("candle", ""),
                "limit":  "5",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])

            # Gebruik lokale cutoff zodat self._last_candle_ts NIET wordt bijgewerkt
            # hier. poll() doet dat zelf, anders filtert het alle gap-fill candles weg.
            cutoff = self._last_candle_ts
            added = 0
            for row in reversed(data):
                if row[8] != "1":
                    continue
                candle = _parse_rest_candle(row)
                ts = pd.Timestamp(candle["open_time"], unit="ms", tz="UTC")
                if cutoff is None or ts > cutoff:
                    self._candle_queue.put(candle)
                    cutoff = ts
                    added += 1

            if added:
                logger.info(
                    "REST gap-fill na reconnect: %d nieuwe candle(s) voor %s",
                    added, self._inst_id,
                )
        except Exception as exc:
            logger.debug("REST gap-fill mislukt: %s", exc)

    # ------------------------------------------------------------------
    # WebSocket loop (draait in achtergrond-thread)
    # ------------------------------------------------------------------

    def _ws_loop(self) -> None:
        """Verbind WebSocket met automatische reconnect en exponential backoff."""
        import websocket

        backoff = 1
        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = self._on_error,
                    on_close   = self._on_close,
                )
                self._ws = ws
                ping_thread = threading.Thread(
                    target=self._ping_loop, args=(ws,), daemon=True
                )
                ping_thread.start()
                connected_at = time.monotonic()
                ws.run_forever()
                if time.monotonic() - connected_at > 60:
                    backoff = 1  # reset alleen na stabiele sessie
                if self._running:
                    self._refill_gaps_via_rest()

            except Exception as exc:
                logger.warning("WebSocket crash: %s", exc)

            if not self._running:
                break

            logger.warning(
                "WebSocket verbroken. Reconnect over %ds…", backoff
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    def _ping_loop(self, ws) -> None:
        """Stuur elke 25s een tekst-ping naar OKX (vereist door OKX protocol)."""
        while self._running:
            time.sleep(_PING_INTERVAL)
            try:
                ws.send("ping")
            except Exception:
                break

    def _on_open(self, ws) -> None:
        subscribe_msg = json.dumps({
            "op":   "subscribe",
            "args": [{"channel": self._channel, "instId": self._inst_id}],
        })
        ws.send(subscribe_msg)
        logger.info(
            "OKX WebSocket verbonden. Geabonneerd op %s/%s",
            self._channel, self._inst_id,
        )

    def _on_message(self, ws, message: str) -> None:
        if message == "pong":
            return
        try:
            msg = json.loads(message)

            # Log events (subscribe confirm, errors) op INFO
            if "event" in msg:
                logger.info("WS event: %s", msg)
                return

            data = msg.get("data", [])
            if not data:
                logger.debug("WS bericht zonder data: %s", message[:200])
                return

            for row in data:
                if len(row) >= 9 and row[8] == "1":  # confirm == "1" = gesloten
                    candle = {
                        "open_time": int(row[0]),
                        "open":      float(row[1]),
                        "high":      float(row[2]),
                        "low":       float(row[3]),
                        "close":     float(row[4]),
                        "volume":    float(row[5]),
                    }
                    self._candle_queue.put(candle)
                    logger.info("Gesloten candle in queue gezet: ts=%s", row[0])

        except Exception as exc:
            logger.warning("WebSocket bericht parse fout: %s — raw: %s", exc, message[:200])

    def _on_error(self, ws, error) -> None:
        logger.warning("WebSocket fout: %s", error)

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info(
            "WebSocket gesloten: %s %s", close_status_code, close_msg
        )

    # ------------------------------------------------------------------
    # Buffer → DataFrame → SMC
    # ------------------------------------------------------------------

    def _buffer_to_df(self) -> pd.DataFrame:
        df = pd.DataFrame(list(self._buffer))
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("open_time").sort_index()
        return df.astype(float)

    def _compute_smc(
        self,
        df: pd.DataFrame,
        target_ts: pd.Timestamp,
    ) -> pd.Series:
        try:
            from src.smc.signals import compute_signals
            signals_df = compute_signals(df, swing_length=self._swing_length)
            if target_ts in signals_df.index:
                return signals_df.loc[target_ts]
        except Exception as exc:
            logger.warning("SMC berekening mislukt: %s", exc)
        return _empty_smc_row()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_rest_candle(row: list) -> dict:
    """Converteer OKX REST candle-rij naar dict."""
    return {
        "open_time": int(row[0]),
        "open":      float(row[1]),
        "high":      float(row[2]),
        "low":       float(row[3]),
        "close":     float(row[4]),
        "volume":    float(row[5]),
    }


def _empty_smc_row() -> pd.Series:
    cols = [
        "ob", "ob_top", "ob_bottom", "ob_pct", "ob_mitigated_idx",
        "liq", "liq_level", "liq_end_idx", "liq_swept_idx",
        "bos", "choch", "structure_level", "structure_broken_idx", "atr",
    ]
    return pd.Series(
        {c: 0.0 if c in ("ob", "liq", "bos", "choch") else float("nan") for c in cols}
    )
