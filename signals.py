#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""signals.py

牛/熊市信号及关键形态检测函数，
从 trend_detector.py 拆分以降低主文件体积。
"""
from __future__ import annotations

import logging
import datetime
import pandas as pd

import config as cfg

# 将配置中的全局常量注入本模块命名空间，保持旧代码兼容
for _k, _v in cfg.__dict__.items():
    if _k.isupper():
        globals()[_k] = _v

logger = logging.getLogger(__name__)

__all__ = [
    "detect_bull_market_signals",
    "detect_bear_market_signals",
    "detect_blowoff",
    "detect_exhaustion_top",
    "detect_breakdown",
    "detect_panic_sell",
    "detect_capitulation",
    "detect_bottom_reversal",
]

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _get_completed_slice(df: pd.DataFrame, tf_seconds: int = 86_400, *, as_of_utc=None):
    """返回指定时点已收盘的最新两根 K 线的负索引。"""
    cutoff = (as_of_utc or datetime.datetime.utcnow()) - pd.Timedelta(seconds=tf_seconds)
    completed = df.index.searchsorted(cutoff, side="right")
    if completed == 0:
        return None, None
    current = completed - len(df) - 1
    return current, current - 1 if completed > 1 else None


def _get_completed_weekly_ma50(df: pd.DataFrame, *, as_of_utc=None) -> pd.DataFrame:
    """用已收盘的周日日 K 计算 MA50，保留缺失的最近一周。"""
    closed_by = pd.Timestamp(as_of_utc or datetime.datetime.utcnow()) - pd.Timedelta(days=1)
    latest_sunday = pd.offsets.Week(weekday=6).rollback(closed_by.normalize())
    sunday = (df.index.dayofweek == 6) & (df.index <= closed_by)
    weekly_df = df.loc[sunday, "close"].asfreq("W-SUN").to_frame()
    if latest_sunday not in weekly_df.index:
        weekly_df.loc[latest_sunday, "close"] = float("nan")
    weekly_df["MA50"] = weekly_df["close"].rolling(window=50).mean()
    return weekly_df

# ---------------------------------------------------------------------------
# 牛/熊市路径监控
# ---------------------------------------------------------------------------

_BULL_SIGNAL_MESSAGES = {
    "bull_early_sign": "日线收盘高于 MA200。",
    "bull_confirmed": "日线 MA50 上穿 MA200，收盘高于 MA200 和 MA365。",
    "bull_pullback_test": "近 10 根日 K 最低价低于两条均线中较高一条的 102%，最新收盘高于 MA200、MA365 和前一日收盘。",
    "bull_final_verdict": "周线收盘高于 MA50。",
    "bull_reinforcement": "周线收盘高于 MA50，MA50 较上周上升。",
}
_BEAR_SIGNAL_MESSAGES = {
    "bear_warning": "日线收盘低于 MA200。",
    "bear_confirmed": "日线 MA50 下穿 MA200，收盘低于 MA200 和 MA365。",
    "bearish_retest": "近 10 根日 K 最高价高于两条均线中较低一条的 98%，最新收盘低于 MA200、MA365 和前一日收盘。",
    "bear_final_verdict": "周线收盘低于 MA50。",
    "bear_reinforcement": "周线收盘低于 MA50，MA50 较上周下降。",
}


def detect_bull_market_signals(df: pd.DataFrame, *, as_of_utc=None) -> dict:
    """检测牛市信号，返回 dict(signal, level, message)。"""
    try:
        if len(df) < 365:
            return {"signal": None, "level": None, "message": None}

        for ma, win in (("MA200", 200), ("MA365", 365), ("MA50", 50)):
            if ma not in df.columns:
                df[ma] = df["close"].rolling(window=win).mean()

        curr_idx, prev_idx = _get_completed_slice(df, as_of_utc=as_of_utc)
        if prev_idx is None:
            return {"signal": None, "level": None, "message": None}
        current, prev = df.iloc[curr_idx], df.iloc[prev_idx]

        weekly_df = _get_completed_weekly_ma50(df, as_of_utc=as_of_utc)
        if len(weekly_df) < 2:
            return {"signal": None, "level": None, "message": None}
        current_weekly, prev_weekly = weekly_df.iloc[-1], weekly_df.iloc[-2]

        result = {"signal": None, "level": None, "message": None}

        # ① 早期信号
        if current["close"] > current["MA200"]:
            result = {
                "signal": "bull_early_sign",
                "level": "green",
                "message": _BULL_SIGNAL_MESSAGES["bull_early_sign"],
            }

        # ② 确认转强
        daily_mas = current[["MA200", "MA365"]]
        upper_ma = daily_mas.max() if daily_mas.notna().all() else float("nan")
        golden_cross = current["MA50"] > current["MA200"] and prev["MA50"] <= prev["MA200"]
        if current["close"] > upper_ma and golden_cross:
            result = {
                "signal": "bull_confirmed",
                "level": "blue",
                "message": _BULL_SIGNAL_MESSAGES["bull_confirmed"],
            }

        # ③ 回踩测试 (Fix #2: 用安全索引，排除未收盘 K 线)
        pos = len(df) + curr_idx if curr_idx < 0 else curr_idx
        slice_start = max(0, pos - 9)
        recent_low = df["low"].iloc[slice_start : pos + 1].min()
        pulled_back = recent_low < upper_ma * 1.02
        held = current["close"] > upper_ma
        turning_up = current["close"] > prev["close"]
        if pulled_back and held and turning_up:
            result = {
                "signal": "bull_pullback_test",
                "level": "blue",
                "message": _BULL_SIGNAL_MESSAGES["bull_pullback_test"],
            }

        # ④ 最终判决
        if current_weekly["close"] > current_weekly["MA50"]:
            result = {
                "signal": "bull_final_verdict",
                "level": "golden",
                "message": _BULL_SIGNAL_MESSAGES["bull_final_verdict"],
            }

        # ⑤ 趋势强化
        if (
            current_weekly["close"] > current_weekly["MA50"]
            and current_weekly["MA50"] > prev_weekly["MA50"]
        ):
            result = {
                "signal": "bull_reinforcement",
                "level": "golden",
                "message": _BULL_SIGNAL_MESSAGES["bull_reinforcement"],
            }

        return result
    except Exception as exc:
        logger.error(f"牛市信号检测失败: {exc}")
        return {"signal": None, "level": None, "message": None}


def detect_bear_market_signals(df: pd.DataFrame, *, as_of_utc=None) -> dict:
    """检测熊市信号，逻辑与牛市对称。"""
    try:
        if len(df) < 365:
            return {"signal": None, "level": None, "message": None}

        for ma, win in (("MA200", 200), ("MA365", 365), ("MA50", 50)):
            if ma not in df.columns:
                df[ma] = df["close"].rolling(window=win).mean()

        curr_idx, prev_idx = _get_completed_slice(df, as_of_utc=as_of_utc)
        if prev_idx is None:
            return {"signal": None, "level": None, "message": None}
        current, prev = df.iloc[curr_idx], df.iloc[prev_idx]

        weekly_df = _get_completed_weekly_ma50(df, as_of_utc=as_of_utc)
        if len(weekly_df) < 2:
            return {"signal": None, "level": None, "message": None}
        current_weekly, prev_weekly = weekly_df.iloc[-1], weekly_df.iloc[-2]

        result = {"signal": None, "level": None, "message": None}

        # ① 警示
        if current["close"] < current["MA200"]:
            result = {
                "signal": "bear_warning",
                "level": "yellow",
                "message": _BEAR_SIGNAL_MESSAGES["bear_warning"],
            }

        # ② 确认转弱
        daily_mas = current[["MA200", "MA365"]]
        lower_ma = daily_mas.min() if daily_mas.notna().all() else float("nan")
        death_cross = current["MA50"] < current["MA200"] and prev["MA50"] >= prev["MA200"]
        if current["close"] < lower_ma and death_cross:
            result = {
                "signal": "bear_confirmed",
                "level": "red",
                "message": _BEAR_SIGNAL_MESSAGES["bear_confirmed"],
            }

        # ③ 反弹测试 (Fix #2: 用安全索引，排除未收盘 K 线)
        pos = len(df) + curr_idx if curr_idx < 0 else curr_idx
        slice_start = max(0, pos - 9)
        recent_high = df["high"].iloc[slice_start : pos + 1].max()
        rebounded = recent_high > lower_ma * 0.98
        failed = current["close"] < lower_ma
        turning_down = current["close"] < prev["close"]
        if rebounded and failed and turning_down:
            result = {
                "signal": "bearish_retest",
                "level": "red",
                "message": _BEAR_SIGNAL_MESSAGES["bearish_retest"],
            }

        # ④ 最终判决
        if current_weekly["close"] < current_weekly["MA50"]:
            result = {
                "signal": "bear_final_verdict",
                "level": "dark_red",
                "message": _BEAR_SIGNAL_MESSAGES["bear_final_verdict"],
            }

        # ⑤ 趋势强化
        if (
            current_weekly["close"] < current_weekly["MA50"]
            and current_weekly["MA50"] < prev_weekly["MA50"]
        ):
            result = {
                "signal": "bear_reinforcement",
                "level": "black",
                "message": _BEAR_SIGNAL_MESSAGES["bear_reinforcement"],
            }

        return result
    except Exception as exc:
        logger.error(f"熊市信号检测失败: {exc}")
        return {"signal": None, "level": None, "message": None}

# ---------------------------------------------------------------------------
# 形态检测函数（顶/破位/底）
# ---------------------------------------------------------------------------

def detect_blowoff(row, df: pd.DataFrame | None = None, *, safe_idx: int = -1) -> bool:
    """冲顶（上影 + RSI 超买 + MACD 背离）。
    safe_idx: 当前已收盘 K 线的负索引，前一根为 safe_idx - 1。
    """
    try:
        long_tail = (
            row.get("upper_shadow", 0) > UPPER_SHADOW_FACTOR * row.get("body", 0)
            and row.get("upper_shadow", 0) > PRICE_VOLATILITY * row["close"]
        )
        rsi_overbought = row.get("RSI", 0) > RSI_OVERBOUGHT
        macd_divergence = False
        if df is not None and "MACDh" in df.columns and abs(safe_idx - 1) <= len(df):
            macd_divergence = row["MACDh"] < df["MACDh"].iloc[safe_idx - 1]
        return long_tail and rsi_overbought and macd_divergence
    except Exception as exc:
        logger.error(f"detect_blowoff 失败: {exc}")
        return False


def detect_exhaustion_top(df: pd.DataFrame, *, check_future: bool = False, safe_idx: int = -1) -> bool:
    """圆弧顶检测。safe_idx 统一未收盘保护索引。
    lookback 窗口也截止到 safe_idx 位置，排除未收盘数据。
    """
    try:
        if len(df) < HIGH_LOOKBACK + DROP_LOOKFORWARD:
            return False
        idx_current = safe_idx if not check_future else -(DROP_LOOKFORWARD + 1)
        row = df.iloc[idx_current]
        # 将负索引转为正索引，方便切片
        pos = len(df) + idx_current if idx_current < 0 else idx_current
        window_start = max(0, pos - HIGH_LOOKBACK + 1)
        new_high = row["high"] >= df["high"].iloc[window_start:pos + 1].max()
        ema_dev = (row["close"] - row["EMA20"]) / row["EMA20"] >= EMA_DEVIATION
        rsi_high = row["RSI"] >= RSI_EXHAUSTION
        if check_future and len(df) > DROP_LOOKFORWARD:
            future_price = df["close"].iloc[-1]
            price_drop = (row["close"] - future_price) / row["close"] >= DROP_THRESHOLD
            return new_high and ema_dev and rsi_high and price_drop
        return new_high and ema_dev and rsi_high
    except Exception as exc:
        logger.error(f"detect_exhaustion_top 失败: {exc}")
        return False


def detect_breakdown(row, df: pd.DataFrame | None = None, *, safe_idx: int = -1) -> bool:
    """破位检测：跌破 20EMA + 动能转负。
    safe_idx: 当前已收盘 K 线的负索引，前一根为 safe_idx - 1。
    """
    try:
        close_below = row["close"] < row["EMA20"]
        ema_turn = False
        if df is not None and "EMA20" in df.columns and abs(safe_idx - 1) <= len(df):
            ema_turn = row["EMA20"] < df["EMA20"].iloc[safe_idx - 1]
        adx_fade = row.get("ADX", 0) < 25
        macd_neg = row.get("MACDh", 0) < 0
        return close_below and (adx_fade or macd_neg) and ema_turn
    except Exception as exc:
        logger.error(f"detect_breakdown 失败: {exc}")
        return False


def detect_panic_sell(row, df: pd.DataFrame | None = None, *, safe_idx: int = -1) -> bool:
    """加速下跌检测。
    safe_idx: 当前已收盘 K 线的负索引，前一根为 safe_idx - 1。
    """
    try:
        ema_div = False
        if df is not None and abs(safe_idx - 1) <= len(df):
            prev = df.iloc[safe_idx - 1]
            ema_div = (row["EMA20"] < row["EMA50"]) and (
                (row["EMA20"] - row["EMA50"]) < (prev["EMA20"] - prev["EMA50"])
            )
        adx_strong = row.get("ADX", 0) >= ADX_STRONG_TREND
        if df is not None and "ADX" in df.columns and abs(safe_idx - 1) <= len(df):
            adx_strong = adx_strong and row["ADX"] > df["ADX"].iloc[safe_idx - 1]
        price_decline = False
        if df is not None and abs(safe_idx - 1) <= len(df):
            price_decline = row["close"] < df["close"].iloc[safe_idx - 1]
        downtrend = row["close"] < row["EMA20"] < row["EMA50"]
        return ema_div and adx_strong and price_decline and downtrend
    except Exception as exc:
        logger.error(f"detect_panic_sell 失败: {exc}")
        return False


def detect_capitulation(row, df: pd.DataFrame | None = None, *, safe_idx: int = -1) -> bool:
    """砸盘探底检测。
    safe_idx: 当前已收盘 K 线的负索引。
    """
    try:
        long_lower = (
            row.get("lower_shadow", 0) > LOWER_SHADOW_FACTOR * row.get("body", 0)
            and row.get("lower_shadow", 0) > PRICE_VOLATILITY * row["close"]
        )
        rsi_oversold = row.get("RSI", 100) < RSI_OVERSOLD
        rsi_div, volume_surge, macd_imp = False, False, False
        if df is not None and len(df) > 5:
            # 转正索引
            idx = len(df) + safe_idx if safe_idx < 0 else safe_idx
            if idx >= 5:
                price_new_low = row["close"] < df["close"].iloc[idx - 5 : idx].min()
                rsi_not_new_low = row["RSI"] > df["RSI"].iloc[idx - 5 : idx].min()
                rsi_div = price_new_low and rsi_not_new_low
                volume_surge = row["volume"] > 1.5 * df["volume"].iloc[idx - 5 : idx].mean()
            if "MACDh" in df.columns and idx > 0:
                macd_imp = df["MACDh"].iloc[idx] > df["MACDh"].iloc[idx - 1]
        return (long_lower or rsi_oversold) and (rsi_div or macd_imp) and volume_surge
    except Exception as exc:
        logger.error(f"detect_capitulation 失败: {exc}")
        return False


def detect_bottom_reversal(row, df: pd.DataFrame | None = None, *, safe_idx: int = -1) -> bool:
    """底部反转检测。
    safe_idx: 当前已收盘 K 线的负索引，回看窗口截止到此位置。
    """
    try:
        if df is None or len(df) < REVERSAL_CONFIRM_DAYS + 1:
            return False
        # 转正索引，窗口 = [end_pos - REVERSAL_CONFIRM_DAYS, end_pos)
        end_pos = len(df) + safe_idx + 1 if safe_idx < 0 else safe_idx + 1
        start_pos = end_pos - REVERSAL_CONFIRM_DAYS
        if start_pos < 0:
            return False
        days_above = (df["close"].iloc[start_pos:end_pos] > df["EMA20"].iloc[start_pos:end_pos]).all()
        ema50_flat_or_up = df["EMA50"].iloc[end_pos - 1] >= df["EMA50"].iloc[start_pos]
        macd_positive = row.get("MACDh", -1) > 0
        return days_above and ema50_flat_or_up and macd_positive
    except Exception as exc:
        logger.error(f"detect_bottom_reversal 失败: {exc}")
        return False

