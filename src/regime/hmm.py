"""
hmm.py — HMM 2-state regime detector op 4h data.

Gedrag:
- Features: log returns + ATR ratio (huidige ATR / N-periode gemiddelde)
- 2 states: bullish (1) / bearish (0)
- Traint op in-sample data, slaat model op als pickle
- Laadt model voor inferentie (predict op nieuwe data)
- State-labeling: de state met de hoogste gemiddelde log return = bullish

Aannames:
- Input: 4h OHLCV DataFrame met DatetimeIndex (UTC)
- hmmlearn GaussianHMM met diag covariance type
"""

import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from hmmlearn import hmm  # type: ignore[import]
from scipy.special import logsumexp  # type: ignore[import]
from scipy.stats import multivariate_normal  # type: ignore[import]
from sklearn.preprocessing import StandardScaler  # type: ignore[import]

from src.config_loader import load_config

logger = logging.getLogger(__name__)

_MODEL_FILENAME = "hmm_regime_model.pkl"


# ---------------------------------------------------------------------------
# Publieke interface
# ---------------------------------------------------------------------------

def train(
    df_4h: pd.DataFrame,
    cfg: dict | None = None,
    save_path: str | None = None,
) -> "RegimeModel":
    """
    Train het HMM regime model op 4h data.

    Parameters
    ----------
    df_4h : pd.DataFrame
        4h OHLCV DataFrame (in-sample periode).
    cfg : dict, optional
    save_path : str, optional
        Pad om het model op te slaan. Standaard: data/processed/hmm_regime_model.pkl

    Returns
    -------
    RegimeModel
    """
    if cfg is None:
        cfg = load_config()

    rcfg = cfg["regime"]
    features = _extract_features(df_4h, rcfg)

    logger.info("HMM trainen: %d observaties, %d features", len(features), features.shape[1])

    scaler = StandardScaler()
    X = scaler.fit_transform(features)

    model = hmm.GaussianHMM(
        n_components=rcfg["n_states"],
        covariance_type="diag",
        n_iter=rcfg["n_iter"],
        random_state=rcfg["random_state"],
    )
    model.fit(X)

    logger.info("HMM geconvergeerd: %s", model.monitor_.converged)

    # Bepaal welke state bullish is (hoogste gemiddelde log return)
    states = model.predict(X)
    log_returns = features[:, 0]  # eerste feature = log return
    state_mean_returns = {
        s: log_returns[states == s].mean()
        for s in range(rcfg["n_states"])
    }
    bullish_state = max(state_mean_returns, key=state_mean_returns.get)
    logger.info(
        "State gemiddelde log returns: %s → bullish state = %d",
        {s: f"{v:.6f}" for s, v in state_mean_returns.items()},
        bullish_state,
    )

    regime_model = RegimeModel(
        hmm_model=model,
        scaler=scaler,
        bullish_state=bullish_state,
        feature_config=rcfg,
    )

    if save_path is None:
        processed_dir = Path(cfg["data"]["paths"]["processed"])
        save_path = str(processed_dir / _MODEL_FILENAME)

    regime_model.save(save_path)
    logger.info("Model opgeslagen: %s", save_path)

    return regime_model


def load_model(
    cfg: dict | None = None,
    load_path: str | None = None,
    symbol: str | None = None,
) -> "RegimeModel":
    """
    Laad een eerder getraind regime model.

    Parameters
    ----------
    cfg : dict, optional
    load_path : str, optional
        Expliciet pad. Standaard: data/processed/hmm_regime_model.pkl
    symbol : str, optional
        Als opgegeven, wordt eerst gezocht naar hmm_regime_model_{symbol}.pkl.
        Indien niet gevonden: fallback naar generiek model.

    Returns
    -------
    RegimeModel
    """
    if cfg is None:
        cfg = load_config()

    if load_path is None:
        processed_dir = Path(cfg["data"]["paths"]["processed"])
        if symbol:
            sym_path = processed_dir / f"hmm_regime_model_{symbol}.pkl"
            load_path = str(sym_path if sym_path.exists() else processed_dir / _MODEL_FILENAME)
        else:
            load_path = str(processed_dir / _MODEL_FILENAME)

    return RegimeModel.load(load_path)


def predict_regimes(
    df_4h: pd.DataFrame,
    regime_model: "RegimeModel",
) -> pd.Series:
    """
    Voorspel regime voor elke candle in df_4h.

    Parameters
    ----------
    df_4h : pd.DataFrame
        4h OHLCV DataFrame.
    regime_model : RegimeModel

    Returns
    -------
    pd.Series
        Boolean Series (True = bullish, False = bearish), zelfde index als df_4h.
        De eerste ATR-warmup candles zijn NaN.
    """
    return regime_model.predict(df_4h)


def align_regimes_to_15m(
    regimes_4h: pd.Series,
    df_15m: pd.DataFrame,
) -> pd.Series:
    """
    Downsample 4h regime-labels naar 15m index via forward fill.

    Elke 15m candle krijgt het regime van de 4h candle die het meest recent
    afgesloten is op dat moment.

    Parameters
    ----------
    regimes_4h : pd.Series
        Boolean Series met 4h index (True = bullish).
    df_15m : pd.DataFrame
        15m DataFrame waarvan we de index gebruiken.

    Returns
    -------
    pd.Series
        Boolean Series met 15m index.
    """
    # Shift 1 positie zodat het label van de 4h-bar die sluit om t+4h
    # pas beschikbaar is vanaf de volgende 4h-open (geen look-ahead).
    # Voorbeeld: bar 00:00–04:00 → label wordt zichtbaar vanaf 04:00.
    aligned = regimes_4h.shift(1).reindex(df_15m.index, method="ffill")
    return aligned


# ---------------------------------------------------------------------------
# Feature extractie
# ---------------------------------------------------------------------------

def _extract_features(df_4h: pd.DataFrame, rcfg: dict) -> np.ndarray:
    """
    Bereken features voor het HMM:
    - log return van close
    - ATR ratio: huidige ATR / rolling mean van ATR

    Returns
    -------
    np.ndarray
        Shape (n_valid, 2). Rijen met NaN worden verwijderd.
    """
    atr_period    = rcfg["atr_period"]     # 14
    atr_ma_period = rcfg["atr_ma_period"]  # 50

    log_returns = np.log(df_4h["close"] / df_4h["close"].shift(1))
    atr         = _compute_atr(df_4h, atr_period)
    atr_ma      = atr.rolling(atr_ma_period).mean()
    atr_ratio   = atr / atr_ma

    features_df = pd.DataFrame({
        "log_return": log_returns,
        "atr_ratio":  atr_ratio,
    }, index=df_4h.index)

    # Verwijder warmup-rijen (NaN)
    features_df = features_df.dropna()

    return features_df.values


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Bereken Average True Range."""
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    return tr.rolling(period).mean()


# ---------------------------------------------------------------------------
# Forward-filtering decoder (causal — geen look-ahead)
# ---------------------------------------------------------------------------

def _forward_decode(model: hmm.GaussianHMM, X: np.ndarray) -> np.ndarray:
    """
    Causal state-decoding via het forward-filtering algoritme.

    Retourneert argmax_i P(state_t = i | obs_1 … obs_t) voor elke t.
    In tegenstelling tot Viterbi (model.predict) gebruikt dit géén
    toekomstige observaties, waardoor er geen look-ahead bias is.
    """
    n_samples, _ = X.shape
    n_states = model.n_components

    # Log-emissiewaarschijnlijkheden: shape (n_samples, n_states)
    log_emis = np.zeros((n_samples, n_states))
    for s in range(n_states):
        # covariance_type="diag" → covars_ heeft shape (n_states, n_features)
        cov = np.diag(model.covars_[s])
        log_emis[:, s] = multivariate_normal.logpdf(X, mean=model.means_[s], cov=cov)

    log_transmat = np.log(model.transmat_ + 1e-300)

    # Forward pass in log-ruimte
    log_alpha = np.empty((n_samples, n_states))
    log_alpha[0] = np.log(model.startprob_ + 1e-300) + log_emis[0]

    for t in range(1, n_samples):
        # log_alpha[t-1, :, None] + log_transmat is (n_states, n_states);
        # logsumexp over axis=0 geeft de marginale over vorige states.
        log_alpha[t] = log_emis[t] + logsumexp(
            log_alpha[t - 1, :, np.newaxis] + log_transmat, axis=0
        )

    return np.argmax(log_alpha, axis=1)


# ---------------------------------------------------------------------------
# RegimeModel klasse
# ---------------------------------------------------------------------------

class RegimeModel:
    """Container voor het getrainde HMM model + scaler + metadata."""

    def __init__(
        self,
        hmm_model: hmm.GaussianHMM,
        scaler: StandardScaler,
        bullish_state: int,
        feature_config: dict,
    ) -> None:
        self._hmm        = hmm_model
        self._scaler     = scaler
        self._bullish    = bullish_state
        self._feat_cfg   = feature_config

    def predict(self, df_4h: pd.DataFrame) -> pd.Series:
        """
        Voorspel regime (True = bullish) voor alle candles in df_4h.
        Warmup-candles (NaN features) krijgen pd.NA.
        """
        atr_period    = self._feat_cfg["atr_period"]
        atr_ma_period = self._feat_cfg["atr_ma_period"]
        # Warmup: ATR heeft atr_period-1 NaN, atr_ma heeft atr_ma_period-1 meer NaN
        warmup        = (atr_period - 1) + (atr_ma_period - 1)

        log_returns = np.log(df_4h["close"] / df_4h["close"].shift(1))
        atr         = _compute_atr(df_4h, atr_period)
        atr_ma      = atr.rolling(atr_ma_period).mean()
        atr_ratio   = atr / atr_ma

        features_df = pd.DataFrame({
            "log_return": log_returns,
            "atr_ratio":  atr_ratio,
        }, index=df_4h.index)

        valid_mask = features_df.notna().all(axis=1)
        valid_features = features_df[valid_mask].values

        X = self._scaler.transform(valid_features)
        raw_states = _forward_decode(self._hmm, X)
        is_bullish = raw_states == self._bullish

        # Maak resultaat-series met NaN voor warmup-candles
        result = pd.Series(pd.NA, index=df_4h.index, dtype="boolean")
        result[valid_mask] = is_bullish
        return result

    def init_forward_state(self, df_4h: pd.DataFrame) -> np.ndarray:
        """
        Run forward filter op df_4h, retourneer log_alpha van de laatste stap (n_states,).

        Gebruikt als eenmalige initialisatie; hergebruik via forward_step() is causal.
        """
        atr_period    = self._feat_cfg["atr_period"]
        atr_ma_period = self._feat_cfg["atr_ma_period"]

        log_returns = np.log(df_4h["close"] / df_4h["close"].shift(1))
        atr         = _compute_atr(df_4h, atr_period)
        atr_ma      = atr.rolling(atr_ma_period).mean()
        atr_ratio   = atr / atr_ma

        features_df = pd.DataFrame({
            "log_return": log_returns,
            "atr_ratio":  atr_ratio,
        }, index=df_4h.index).dropna()

        if len(features_df) == 0:
            raise ValueError("Geen geldige features in df_4h voor forward-filter initialisatie.")

        X = self._scaler.transform(features_df.values)

        n_states     = self._hmm.n_components
        log_transmat = np.log(self._hmm.transmat_ + 1e-300)

        log_emis = np.zeros((len(X), n_states))
        for s in range(n_states):
            log_emis[:, s] = multivariate_normal.logpdf(
                X, mean=self._hmm.means_[s], cov=np.diag(self._hmm.covars_[s])
            )

        log_alpha = np.log(self._hmm.startprob_ + 1e-300) + log_emis[0]
        for t in range(1, len(X)):
            log_alpha = log_emis[t] + logsumexp(
                log_alpha[:, np.newaxis] + log_transmat, axis=0
            )

        return log_alpha  # shape (n_states,)

    def forward_step(self, x_scaled: np.ndarray, log_alpha: np.ndarray) -> np.ndarray:
        """
        Één causale forward stap: alpha_t gegeven alpha_{t-1} en nieuwe observatie x.

        Parameters
        ----------
        x_scaled : np.ndarray, shape (n_features,) — al geschaald via scaler
        log_alpha : np.ndarray, shape (n_states,) — toestand van vorige stap

        Returns
        -------
        np.ndarray, shape (n_states,)
        """
        log_transmat = np.log(self._hmm.transmat_ + 1e-300)
        log_emis = np.array([
            multivariate_normal.logpdf(
                x_scaled, mean=self._hmm.means_[s], cov=np.diag(self._hmm.covars_[s])
            )
            for s in range(self._hmm.n_components)
        ])
        return log_emis + logsumexp(log_alpha[:, np.newaxis] + log_transmat, axis=0)

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(self, f)
        Path(tmp).replace(path)

    @classmethod
    def load(cls, path: str) -> "RegimeModel":
        if not Path(path).exists():
            raise FileNotFoundError(
                f"Geen regime model gevonden op {path}. "
                "Voer eerst run_backtest.py --set in_sample uit."
            )
        with open(path, "rb") as f:
            return pickle.load(f)

    @property
    def bullish_state(self) -> int:
        return self._bullish

    @property
    def n_states(self) -> int:
        return self._hmm.n_components
