"""Statistics shared by the study and the backtest.

Performance
-----------
Performance statistics of a daily excess-return series, with their uncertainty.

* `summary`: annualized return, vol, Sharpe and its standard error, Newey-West t-stat of the
  mean, max drawdown, hit rate, skew, kurtosis, worst day.
* `bootstrap_sharpe`: circular block bootstrap confidence interval (keeps autocorrelation).
* `deflated_sharpe`: probability that the true Sharpe exceeds what the best of N trials
  would show by luck (Bailey and Lopez de Prado, 2014). N = number of variants tried.

Returns are summed, not compounded: positions are vol-scaled weights on notional, so the
cumulative P&L is the sum of daily returns on a constant capital.

Forecast evaluation
-------------------
* `qlike`: the QLIKE loss of a variance forecast (Patton, 2011: robust to noise in the realized
  measure, so forecasts are ranked as on the true variance).
* `hac_mean_test`: Newey-West t-test that a series has a positive mean; with loss differentials
  it is the Diebold-Mariano test, with the Clark-West adjustment the nested-model test.

Regressions use Newey-West (HAC, Bartlett kernel) standard errors, implemented with numpy so the
project needs no statsmodels; the tests check the formula against a direct computation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm


# --------------------------------------------------------------------------- regression

def newey_west_ols(y, X, lags: int | None = None) -> dict:
    """OLS with Newey-West standard errors. X without constant; one is added.

    V = (X'X)^-1 S (X'X)^-1,  S = sum_t u_t^2 x_t x_t' + sum_{l=1..L} w_l sum_t u_t u_{t-l}
    (x_t x_{t-l}' + x_{t-l} x_t'),  w_l = 1 - l / (L + 1). Default L = floor(4 (n/100)^(2/9)).
    """
    y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(y)), np.asarray(X, float)])
    n, k = X.shape
    L = int(np.floor(4 * (n / 100.0) ** (2.0 / 9.0))) if lags is None else lags
    if np.linalg.matrix_rank(X) < k:
        raise ValueError("regressors are collinear")
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ X.T @ y
    u = y - X @ beta
    Xu = X * u[:, None]
    S = Xu.T @ Xu
    for lag in range(1, L + 1):
        w = 1.0 - lag / (L + 1.0)
        G = Xu[lag:].T @ Xu[:-lag]
        S += w * (G + G.T)
    V = xtx_inv @ S @ xtx_inv
    se = np.sqrt(np.diag(V))
    r2 = 1.0 - (u @ u) / ((y - y.mean()) @ (y - y.mean())) if n > 1 else np.nan
    return {"beta": beta, "se": se, "t": beta / se, "n": n, "lags": L, "r2": r2}


# --------------------------------------------------------------------------- performance

PERIODS = 252.0
EULER_GAMMA = 0.5772156649


def sharpe(r) -> float:
    """Annualized Sharpe ratio of daily excess returns."""
    r = np.asarray(r, float)
    sd = r.std(ddof=1)
    return float(r.mean() / sd * np.sqrt(PERIODS)) if sd > 0 else np.nan


def max_drawdown(r) -> float:
    """Largest peak-to-trough fall of the cumulative (summed) return, as a positive number."""
    cum = np.cumsum(np.asarray(r, float))
    return float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum, initial=0.0))


def summary(r: pd.Series) -> dict:
    """Headline statistics of a daily return series (NaN days dropped)."""
    r = r.dropna()
    x = r.to_numpy()
    n = len(x)
    sr = sharpe(x)
    skew = float(pd.Series(x).skew())
    kurt = float(pd.Series(x).kurt()) + 3.0
    nw = newey_west_ols(x, np.empty((n, 0))) if n > 10 else None
    return {
        "days": n,
        "ann_return": float(x.mean() * PERIODS),
        "ann_vol": float(x.std(ddof=1) * np.sqrt(PERIODS)),
        "sharpe": sr,
        # Lo (2002) / Mertens: standard error of the Sharpe ratio with non-normal returns
        "sharpe_se": float(np.sqrt((1 - skew * sr / np.sqrt(PERIODS) + (kurt - 1) / 4 * (sr / np.sqrt(PERIODS)) ** 2)
                                   / (n - 1)) * np.sqrt(PERIODS)) if n > 1 else np.nan,
        "t_mean_nw": float(nw["t"][0]) if nw else np.nan,
        "max_drawdown": max_drawdown(x),
        "hit_rate": float((x > 0).mean()),
        "worst_day": float(x.min()),
        "skew": skew,
        "kurtosis": kurt,
    }


def bootstrap_sharpe(r, block: int, draws: int, seed: int) -> tuple[float, float]:
    """5% and 95% quantiles of the Sharpe ratio under a circular block bootstrap."""
    x = np.asarray(pd.Series(r).dropna(), float)
    n = len(x)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(draws, int(np.ceil(n / block))))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(draws, -1)[:, :n] % n
    sample = x[idx]
    sr = sample.mean(axis=1) / sample.std(axis=1, ddof=1) * np.sqrt(PERIODS)
    return float(np.quantile(sr, 0.05)), float(np.quantile(sr, 0.95))


def deflated_sharpe(sr_annual: float, n_trials: int, days: int, skew: float, kurt: float,
                    trial_sharpes_annual=None) -> float:
    """Probability that the true Sharpe is above the expected maximum of `n_trials` lucky ones.

    PSR(SR*) = Phi((SR - SR*) sqrt(T - 1) / sqrt(1 - g3 SR + (g4 - 1)/4 SR^2)), per-period
    Sharpe ratios. SR* = sqrt(V) [(1 - g) Phi^-1(1 - 1/N) + g Phi^-1(1 - 1/(N e))], V the
    variance of the trials' Sharpe ratios (default: the variance of a Sharpe estimate,
    1/(T - 1)). With N = 1, SR* = 0 and this is the probabilistic Sharpe ratio against zero.
    """
    sr = sr_annual / np.sqrt(PERIODS)
    if n_trials <= 1:
        sr0 = 0.0
    else:
        if trial_sharpes_annual is not None and len(trial_sharpes_annual) > 1:
            var = float(np.var(np.asarray(trial_sharpes_annual) / np.sqrt(PERIODS), ddof=1))
        else:
            var = 1.0 / (days - 1)
        sr0 = np.sqrt(var) * ((1 - EULER_GAMMA) * norm.ppf(1 - 1 / n_trials)
                              + EULER_GAMMA * norm.ppf(1 - 1 / (n_trials * np.e)))
    denom = np.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr**2, 1e-12))
    return float(norm.cdf((sr - sr0) * np.sqrt(days - 1) / denom))


# --------------------------------------------------------------------------- forecast evaluation

def qlike(y, h) -> np.ndarray:
    """QLIKE loss of variance forecasts h for realized variances y: y/h - ln(y/h) - 1 (zero when h = y)."""
    r = np.asarray(y, float) / np.asarray(h, float)
    return r - np.log(r) - 1.0


def hac_mean_test(x, lags: int) -> dict:
    """Mean of x, its Newey-West t-statistic (Bartlett, `lags`) and one-sided p-value for mean > 0.

    Same estimator as `newey_west_ols` on a constant: var(mean) = (g0 + 2 sum_l w_l g_l) / n,
    g_l the lag-l autocovariance, w_l = 1 - l / (L + 1).
    """
    x = np.asarray(x, float)
    n = len(x)
    u = x - x.mean()
    s = u @ u / n
    for lag in range(1, lags + 1):
        s += 2.0 * (1.0 - lag / (lags + 1.0)) * (u[lag:] @ u[:-lag]) / n
    t = float(x.mean() / np.sqrt(s / n))
    return {"mean": float(x.mean()), "t": t, "p": float(norm.sf(t)), "n": n}
