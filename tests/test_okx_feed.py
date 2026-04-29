"""
tests/test_okx_feed.py — Regressie en smoke tests voor OKXFeed.

Critical: gap-fill na WebSocket reconnect mag candles NIET stilletjes laten vallen.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.feeds.okx_feed import OKXFeed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INTERVAL_MS = 15 * 60 * 1000  # 15 minuten in ms


@pytest.fixture
def cfg() -> dict:
    return {
        "derivatives": {"symbol": "BTC-USDT-SWAP"},
        "smc":         {"swing_length": 20},
        "data":        {"timeframes": {"signal": "15min"}},
    }


def _mk_rest_row(ts_ms: int, close: float = 100.0, confirm: str = "1") -> list:
    """OKX REST candle format: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]."""
    return [
        str(ts_ms),
        str(close),          # open
        str(close + 1.0),    # high
        str(close - 1.0),    # low
        str(close),          # close
        "10.0",              # volume
        "0", "0",            # volCcy, volCcyQuote (niet gebruikt)
        confirm,             # "1" = gesloten candle
    ]


def _mk_response(rows: list) -> MagicMock:
    """Mock een requests.get response met OKX-format payload."""
    resp = MagicMock()
    resp.json.return_value     = {"data": rows}
    resp.raise_for_status.return_value = None
    return resp


def _seed_feed(cfg: dict, last_ts_ms: int) -> OKXFeed:
    """Maak een OKXFeed met _last_candle_ts ingesteld op last_ts_ms (geen WS thread)."""
    feed = OKXFeed(cfg)
    feed._last_candle_ts = pd.Timestamp(last_ts_ms, unit="ms", tz="UTC")
    return feed


# ---------------------------------------------------------------------------
# REGRESSIE: gap-fill mag self._last_candle_ts NIET bijwerken
# ---------------------------------------------------------------------------

def test_gap_fill_does_not_advance_last_candle_ts(cfg):
    """
    Bug die werd gefixt: _refill_gaps_via_rest werkte self._last_candle_ts bij naar
    de nieuwste candle. Daarna gooide poll() alle gap-fill candles weg via de
    'closed_ts <= self._last_candle_ts' check.

    Fix: cutoff is een lokale variabele; self._last_candle_ts blijft op de oude
    waarde tot poll() de candles daadwerkelijk verwerkt.
    """
    base_ts = 1_700_000_000_000
    feed = _seed_feed(cfg, base_ts)
    initial_ts = feed._last_candle_ts

    # OKX levert nieuwste eerst. 4 nieuwe candles + 1 al gezien.
    rest_rows = [
        _mk_rest_row(base_ts + 4 * _INTERVAL_MS, close=104.0),
        _mk_rest_row(base_ts + 3 * _INTERVAL_MS, close=103.0),
        _mk_rest_row(base_ts + 2 * _INTERVAL_MS, close=102.0),
        _mk_rest_row(base_ts + 1 * _INTERVAL_MS, close=101.0),
        _mk_rest_row(base_ts,                    close=100.0),  # al gezien
    ]

    with patch("requests.get", return_value=_mk_response(rest_rows)):
        feed._refill_gaps_via_rest()

    # KERN VAN DE FIX: _last_candle_ts is NIET veranderd door gap-fill.
    assert feed._last_candle_ts == initial_ts, (
        "gap-fill heeft _last_candle_ts bijgewerkt — poll() zou alle candles "
        "weggooien (dit is de bug die we hebben gefixt)"
    )


def test_gap_fill_enqueues_only_new_candles(cfg):
    """Alleen candles met ts > _last_candle_ts mogen in de queue belanden."""
    base_ts = 1_700_000_000_000
    feed = _seed_feed(cfg, base_ts)

    rest_rows = [
        _mk_rest_row(base_ts + 3 * _INTERVAL_MS),
        _mk_rest_row(base_ts + 2 * _INTERVAL_MS),
        _mk_rest_row(base_ts + 1 * _INTERVAL_MS),
        _mk_rest_row(base_ts),               # al gezien — moet overslaan
        _mk_rest_row(base_ts - _INTERVAL_MS),  # ouder — moet overslaan
    ]

    with patch("requests.get", return_value=_mk_response(rest_rows)):
        feed._refill_gaps_via_rest()

    assert feed._candle_queue.qsize() == 3

    # Volgorde: oudste eerst (chronologisch), zodat poll() ze in tijd-volgorde verwerkt
    seen_ts = []
    while not feed._candle_queue.empty():
        seen_ts.append(feed._candle_queue.get_nowait()["open_time"])
    assert seen_ts == [
        base_ts + 1 * _INTERVAL_MS,
        base_ts + 2 * _INTERVAL_MS,
        base_ts + 3 * _INTERVAL_MS,
    ]


def test_gap_fill_skips_unconfirmed_candles(cfg):
    """Candles met confirm != '1' (lopende candle) mogen niet worden opgenomen."""
    base_ts = 1_700_000_000_000
    feed = _seed_feed(cfg, base_ts)

    rest_rows = [
        _mk_rest_row(base_ts + 2 * _INTERVAL_MS, confirm="0"),  # lopend — skip
        _mk_rest_row(base_ts + 1 * _INTERVAL_MS, confirm="1"),  # closed — ok
    ]

    with patch("requests.get", return_value=_mk_response(rest_rows)):
        feed._refill_gaps_via_rest()

    assert feed._candle_queue.qsize() == 1
    assert feed._candle_queue.get_nowait()["open_time"] == base_ts + 1 * _INTERVAL_MS


def test_gap_fill_does_not_touch_buffer(cfg):
    """
    Gap-fill mag NIET zelf naar self._buffer schrijven — poll() doet dat als de
    candle daadwerkelijk wordt verwerkt. Anders krijg je dubbele entries in het
    buffer (en dus verkeerde SMC-berekeningen).
    """
    base_ts = 1_700_000_000_000
    feed = _seed_feed(cfg, base_ts)
    assert len(feed._buffer) == 0

    rest_rows = [
        _mk_rest_row(base_ts + 1 * _INTERVAL_MS),
        _mk_rest_row(base_ts + 2 * _INTERVAL_MS),
    ]

    with patch("requests.get", return_value=_mk_response(rest_rows)):
        feed._refill_gaps_via_rest()

    assert len(feed._buffer) == 0, (
        "gap-fill schreef naar _buffer; poll() voegt dezelfde candle later "
        "nogmaals toe → duplicates in SMC-data"
    )


def test_gap_fill_then_poll_processes_all_candles(cfg):
    """
    End-to-end regressie: na gap-fill moet poll() ALLE gemiste candles teruggeven.

    Dit is de scenario die fout ging: WebSocket reconnect → REST gap-fill van 4
    candles → poll() retourneert None voor alle 4 (bug) → 4 signalen verloren.
    """
    base_ts = 1_700_000_000_000
    feed = _seed_feed(cfg, base_ts)

    rest_rows = [
        _mk_rest_row(base_ts + 3 * _INTERVAL_MS, close=103.0),
        _mk_rest_row(base_ts + 2 * _INTERVAL_MS, close=102.0),
        _mk_rest_row(base_ts + 1 * _INTERVAL_MS, close=101.0),
    ]
    with patch("requests.get", return_value=_mk_response(rest_rows)):
        feed._refill_gaps_via_rest()

    # poll() drie keer aanroepen — moet drie keer non-None retourneren
    processed_ts = []
    for _ in range(3):
        result = feed.poll()
        assert result is not None, "gap-fill candle werd door poll() gefilterd"
        ohlc_row, _smc_row = result
        processed_ts.append(ohlc_row.name)

    # Volgorde: chronologisch oplopend
    assert processed_ts == [
        pd.Timestamp(base_ts + 1 * _INTERVAL_MS, unit="ms", tz="UTC"),
        pd.Timestamp(base_ts + 2 * _INTERVAL_MS, unit="ms", tz="UTC"),
        pd.Timestamp(base_ts + 3 * _INTERVAL_MS, unit="ms", tz="UTC"),
    ]

    # Vierde poll() — geen meer candles
    assert feed.poll() is None

    # _last_candle_ts is nu bijgewerkt door poll() (niet door gap-fill)
    assert feed._last_candle_ts == pd.Timestamp(
        base_ts + 3 * _INTERVAL_MS, unit="ms", tz="UTC"
    )


def test_websocket_duplicates_after_gap_fill_are_filtered(cfg):
    """
    Realistische sequence: gap-fill enqueued een candle, daarna pusht de
    her-verbonden WebSocket dezelfde candle ook. poll() moet de duplicaat
    filteren via de _last_candle_ts check.
    """
    base_ts = 1_700_000_000_000
    feed = _seed_feed(cfg, base_ts)

    rest_rows = [_mk_rest_row(base_ts + 1 * _INTERVAL_MS, close=101.0)]
    with patch("requests.get", return_value=_mk_response(rest_rows)):
        feed._refill_gaps_via_rest()

    # poll() verwerkt de gap-fill candle
    assert feed.poll() is not None

    # WebSocket pusht dezelfde candle (zoals na reconnect kan gebeuren)
    duplicate_candle = {
        "open_time": base_ts + 1 * _INTERVAL_MS,
        "open":  101.0, "high": 101.5, "low": 100.5,
        "close": 101.0, "volume": 10.0,
    }
    feed._candle_queue.put(duplicate_candle)

    # poll() moet de duplicaat herkennen en None retourneren
    assert feed.poll() is None
