#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""indicators.py

技术指标计算模块，使用纯 pandas/numpy 实现，无需 pandas_ta 依赖。
提供单一 API:
    calculate_indicators(df: pd.DataFrame, timeframe: str = 'main')
"""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import datetime

import config as cfg  # 全局配置

# 将配置文件中的大写常量注入当前模块命名空间
for _k, _v in cfg.__dict__.items():
    if _k.isupper():
        globals()[_k] = _v

logger = logging.getLogger(__name__)

__all__ = [
    "calculate_indicators",
]


# ---------------------------------------------------------------------------
# 纯 pandas/numpy 实现的技术指标
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, length: int) -> pd.Series:
    """EMA (指数移动平均)"""
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """RSI (相对强弱指标)"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD (指数平滑异同移动平均线)
    返回 DataFrame 包含 MACD, MACDs (signal), MACDh (histogram)
    """
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({
        "MACD": macd_line,
        "MACDs": signal_line,
        "MACDh": histogram,
    }, index=series.index)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.DataFrame:
    """ADX (平均趋向指标) + DMP/DMN"""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    # True Range
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional Movement
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=high.index)

    # Smoothed averages (Wilder's smoothing)
    atr = tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean() / atr.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    return pd.DataFrame({
        "ADX": adx,
        "DMP": plus_di,
        "DMN": minus_di,
    }, index=high.index)


def _bbands(series: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands (布林带)"""
    mid = series.rolling(window=length).mean()
    # ddof=0 与 TA-Lib / TradingView 布林带口径一致（pandas 默认 ddof=1 会略宽）
    rolling_std = series.rolling(window=length).std(ddof=0)
    upper = mid + std * rolling_std
    lower = mid - std * rolling_std
    bandwidth = ((upper - lower) / mid.replace(0, np.nan)) * 100
    return pd.DataFrame({
        "BBL": lower,
        "BBM": mid,
        "BBU": upper,
        "BBB": bandwidth,
    }, index=series.index)


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def calculate_indicators(df: pd.DataFrame, timeframe: str = "main") -> pd.DataFrame:  # noqa: C901
    """计算技术指标。

    Args:
        df: OHLCV DataFrame，必须包含 open/high/low/close/volume 列。
        timeframe: 'main' 为日线主周期；'fast' 为 4h 快周期。

    Returns:
        添加技术指标后的 DataFrame（就地修改并返回）。
    """
    try:
        # ---- 周期相关参数 ----
        if timeframe == "fast":
            ema_period = FAST_EMA_PERIOD
            macd_fast, macd_slow, macd_signal = FAST_MACD_FAST, FAST_MACD_SLOW, FAST_MACD_SIGNAL
            adx_period = FAST_ADX_PERIOD
        else:
            ema_period = EMA_PERIOD
            macd_fast, macd_slow, macd_signal = MACD_FAST, MACD_SLOW, MACD_SIGNAL
            adx_period = ADX_PERIOD

        # ---- EMA ----
        df["EMA"] = _ema(df["close"], ema_period)

        # ---- 波动率 (20 日收益率 std * 100) ----
        df["volatility"] = df["close"].pct_change().rolling(20).std() * 100
        df["volatility"] = df["volatility"].fillna(2.0)

        # ---- 相对成交量 (使用前 20 根已收盘 K 线的平均成交量作为基准) ----
        df["rel_volume"] = df["volume"] / df["volume"].shift(1).rolling(20).mean()
        df["rel_volume"] = df["rel_volume"].fillna(1.0)

        # ---- MACD ----
        macd = _macd(df["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal)
        df["MACD"] = macd["MACD"]
        df["MACDs"] = macd["MACDs"]
        df["MACDh"] = macd["MACDh"]

        df["macd_col"], df["macd_signal_col"], df["macd_hist_col"] = "MACD", "MACDs", "MACDh"

        # ---- ADX ----
        adx = _adx(df["high"], df["low"], df["close"], length=adx_period)
        df["ADX"] = adx["ADX"]
        df["DMP"] = adx["DMP"]
        df["DMN"] = adx["DMN"]

        df["adx_col"], df["plus_di_col"], df["minus_di_col"] = "ADX", "DMP", "DMN"

        # ---- 主周期额外指标 ----
        if timeframe == "main":
            df["EMA20"] = _ema(df["close"], EMA20_PERIOD)
            df["EMA50"] = _ema(df["close"], EMA50_PERIOD)
            df["RSI"] = _rsi(df["close"], RSI_PERIOD)

            df["body"] = (df["close"] - df["open"]).abs()
            df["upper_shadow"] = df["high"] - df[["open", "close"]].max(axis=1)
            df["lower_shadow"] = df[["open", "close"]].min(axis=1) - df["low"]

            bb = _bbands(df["close"], length=BB_LENGTH, std=BB_STD)
            df["BBL"] = bb["BBL"]
            df["BBM"] = bb["BBM"]
            df["BBU"] = bb["BBU"]
            df["BBB"] = bb["BBB"]

        return df
    except Exception as exc:
        logger.error(f"calculate_indicators 失败: {exc}")
        return df
