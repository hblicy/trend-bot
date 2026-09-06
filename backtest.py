#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py
============
评估 trend_detector.py 在过去一年的两类信号精确率：
1. 趋势信号（上升 / 下跌）
2. 市场阶段信号（冲顶、圆弧顶、破位、加速下跌、砸盘探底、底部反转）

阈值与窗口均使用 trend_detector.py 中已经定义的常量：
- DROP_THRESHOLD     (0.10)  用作涨/跌幅判断阈值
- DROP_LOOKFORWARD   (3)     用作阶段信号观测窗口
- HOLD_DAYS (=5)     用作趋势信号观测窗口

运行：python backtest.py
"""
from __future__ import annotations

import datetime as dt
import sys
from typing import Tuple

import ccxt
import pandas as pd

import config as cfg
import trend_detector as td

# 回测区间（天）
DAYS_TO_BACKTEST = 365

# --------- 参数取自 trend_detector 配置 ---------
TREND_WINDOW = 5                       # 与 backtest 1.0 保持一致
PHASE_WINDOW = 10    # 阶段信号观察窗口改为 10 天
THRESHOLD = cfg.DROP_THRESHOLD         # 10%
MAX_CANDLES = cfg.LOOKBACK + DAYS_TO_BACKTEST + PHASE_WINDOW + 30

# ------------- 结果计数器 -------------
class Counters:
    def __init__(self):
        self.tp = 0
        self.fp = 0

    def add(self, correct: bool):
        if correct:
            self.tp += 1
        else:
            self.fp += 1

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

# 初始化交易所
ex = getattr(ccxt, cfg.EXCHANGE)({"enableRateLimit": True})


def fetch_ohlcv_paginated(
    exchange,
    symbol: str,
    timeframe: str,
    *,
    since: int,
    total_limit: int,
    page_limit: int = 1000,
):
    """按时间戳向前分页获取 OHLCV，返回去重且升序的数据。"""
    if total_limit <= 0:
        return []
    if page_limit <= 0:
        raise ValueError("page_limit 必须大于 0")

    rows_by_timestamp = {}
    cursor = since
    while len(rows_by_timestamp) < total_limit:
        request_limit = min(page_limit, total_limit - len(rows_by_timestamp))
        page = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=request_limit,
            since=cursor,
        )
        if not page:
            break

        timestamps = []
        for row in page:
            if not row:
                raise ValueError(f"{symbol} {timeframe} 返回了空 K 线记录")
            timestamp = int(row[0])
            timestamps.append(timestamp)
            if timestamp >= since:
                rows_by_timestamp[timestamp] = row

        next_cursor = max(timestamps) + 1
        if next_cursor <= cursor:
            raise RuntimeError(
                f"{symbol} {timeframe} 分页游标未前进: "
                f"cursor={cursor}, next_cursor={next_cursor}"
            )
        cursor = next_cursor

        if len(page) < request_limit:
            break

    ordered_timestamps = sorted(rows_by_timestamp)
    return [rows_by_timestamp[ts] for ts in ordered_timestamps[:total_limit]]


def _validate_fast_coverage(
    fast_df: pd.DataFrame,
    first_day: pd.Timestamp,
    last_day: pd.Timestamp,
) -> None:
    """确保 4H 数据覆盖回测所需日线区间。"""
    if fast_df is None or fast_df.empty:
        raise RuntimeError("快周期回测数据为空")

    required_end = last_day + pd.Timedelta(hours=20)
    actual_start = fast_df.index.min()
    actual_end = fast_df.index.max()
    if actual_start > first_day or actual_end < required_end:
        raise RuntimeError(
            "快周期回测数据未覆盖目标区间: "
            f"需要 {first_day} 至 {required_end}，"
            f"实际 {actual_start} 至 {actual_end}"
        )

    expected_days = pd.date_range(
        first_day.normalize(),
        last_day.normalize(),
        freq="D",
    )
    available_days = pd.DatetimeIndex(fast_df.index).normalize().unique()
    missing_days = expected_days.difference(available_days)
    if not missing_days.empty:
        preview = ", ".join(day.strftime("%Y-%m-%d") for day in missing_days[:5])
        raise RuntimeError(f"快周期回测数据缺少完整日期: {preview}")


# ----------- 评价函数 -----------

def _trend_ok(direction: str, entry: float, future: pd.DataFrame) -> bool:
    """判断趋势信号是否命中 2% 阈值"""
    if future.empty:
        return False
    if direction == "上升":
        rise = (future["high"].max() - entry) / entry
        return rise >= 0.02
    if direction == "下跌":
        drop = (entry - future["low"].min()) / entry
        return drop >= 0.02
    return False


def _phase_ok(direction: str, entry: float, future: pd.DataFrame) -> bool:
    """判断阶段信号是否在 PHASE_WINDOW 内达到 DROP_THRESHOLD"""
    if future.empty:
        return False
    if direction == "down":
        drop = (entry - future["low"].min()) / entry
        return drop >= THRESHOLD
    if direction == "up":
        rise = (future["high"].max() - entry) / entry
        return rise >= THRESHOLD
    return False


from typing import List, Dict


def backtest_symbol(sym: str) -> Tuple[Counters, Counters, List[Dict], List[Dict]]:
    since = int((dt.datetime.utcnow() - dt.timedelta(days=DAYS_TO_BACKTEST + cfg.LOOKBACK + 30)).timestamp() * 1000)
    ohlcv = ex.fetch_ohlcv(sym, timeframe="1d", limit=MAX_CANDLES, since=since)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    df = td.calculate_indicators(df, "main")

    # --- Fix #1: 拉取 4h 快周期数据，与实盘口径对齐 ---
    fast_df = None
    try:
        fast_limit = MAX_CANDLES * 6  # 1d ≈ 6 根 4h
        fast_ohlcv = fetch_ohlcv_paginated(
            ex,
            sym,
            cfg.FAST_TIMEFRAME,
            since=since,
            total_limit=fast_limit,
        )
        fast_df = pd.DataFrame(fast_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if fast_df.empty:
            raise RuntimeError("快周期回测数据为空")
        fast_df["timestamp"] = pd.to_datetime(fast_df["timestamp"], unit="ms")
        fast_df.set_index("timestamp", inplace=True)

        first_required_day = df.index[0]
        last_required_day = df.index[len(df) - PHASE_WINDOW - 1]
        _validate_fast_coverage(fast_df, first_required_day, last_required_day)
        if len(fast_df) < cfg.FAST_EMA_PERIOD:
            raise RuntimeError(
                f"快周期回测数据不足: {len(fast_df)} < {cfg.FAST_EMA_PERIOD}"
            )
        fast_df = td.calculate_indicators(fast_df, "fast")
    except Exception as err:
        raise RuntimeError(f"{sym} 快周期回测数据获取或覆盖验证失败: {err}") from err

    trend_ct = Counters()
    phase_ct = Counters()
    top_events: List[Dict] = []   # 保存顶部事件
    bot_events: List[Dict] = []   # 保存底部事件

    prev_trend = None
    for i in range(cfg.LOOKBACK, len(df) - PHASE_WINDOW):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]

        # --- Fix #1 round 2: 日线时间戳是开盘时间 (00:00 UTC) ---
        # 要找该日线内最后一根已收盘的 4h K 线，需要查找:
        #   4h.index >= day_open_ts  AND  4h.index < day_close_ts
        # 其中 day_close_ts = day_open_ts + 24h
        fast_row = None
        if fast_df is not None:
            day_open_ts = row.name
            day_close_ts = day_open_ts + pd.Timedelta(hours=24)
            mask = (fast_df.index >= day_open_ts) & (fast_df.index < day_close_ts)
            if mask.any():
                fast_row = fast_df.loc[mask].iloc[-1]

        trend = td.determine_trend(row, prev_trend, fast_row, prev_row)
        prev_trend = trend

        # future slices
        future_trend = df.iloc[i + 1 : i + 1 + TREND_WINDOW]
        future_phase = df.iloc[i + 1 : i + 1 + PHASE_WINDOW]

        # ---- 趋势统计 ----
        if trend in ("上升", "下跌"):
            trend_ct.add(_trend_ok(trend, row["close"], future_trend))

        # ---- 阶段信号检测 ----
        # 注意：只传 df[:i+1] 以避免前视偏差
        hist_df = df.iloc[:i+1]
        blow  = td.detect_blowoff(row, hist_df)
        exhst = td.detect_exhaustion_top(hist_df)
        brk   = td.detect_breakdown(row, hist_df)
        panic = td.detect_panic_sell(row, hist_df)
        cap   = td.detect_capitulation(row, hist_df)
        rev   = td.detect_bottom_reversal(row, hist_df)

        if blow or exhst or brk:
            phase_ct.add(_phase_ok("down", row["close"], future_phase))
            sig = "blowoff" if blow else ("exhaustion" if exhst else "breakdown")
            top_events.append({"symbol": sym, "signal": sig, "time": row.name, "price": row["close"]})
        elif panic or cap or rev:
            phase_ct.add(_phase_ok("up", row["close"], future_phase))
            sig = "panic_sell" if panic else ("capitulation" if cap else "bottom_reversal")
            bot_events.append({"symbol": sym, "signal": sig, "time": row.name, "price": row["close"]})

    return trend_ct, phase_ct, top_events, bot_events


def main() -> int:
    overall_trend = Counters()
    overall_phase = Counters()

    print(f"回测区间  : {DAYS_TO_BACKTEST} 天")
    print(f"趋势窗口  : {TREND_WINDOW} 天  (±2%)")
    print(f"阶段窗口  : {PHASE_WINDOW} 天  (阈值 {THRESHOLD*100:.0f}%)\n")

    top_all, bot_all = [], []
    for sym in cfg.SYMBOLS:
        t_ct, p_ct, tops, bots = backtest_symbol(sym)
        overall_trend.tp += t_ct.tp
        overall_trend.fp += t_ct.fp
        overall_phase.tp += p_ct.tp
        overall_phase.fp += p_ct.fp
        top_all.extend(tops)
        bot_all.extend(bots)
        print(f"{sym:<10} 趋势 P={t_ct.precision:.2%}  阶段 P={p_ct.precision:.2%}")

    print("\n=== OVERALL ===")
    print(f"趋势信号 Precision : {overall_trend.precision:.2%}  (TP={overall_trend.tp} FP={overall_trend.fp})")
    print(f"阶段信号 Precision : {overall_phase.precision:.2%}  (TP={overall_phase.tp} FP={overall_phase.fp})")

    # 输出顶部/底部事件列表
    if top_all:
        print("\n=== TOP EVENTS ===")
        for ev in top_all:
            print(f"{ev['symbol']:<8} {ev['signal']:<14} {ev['time']:%Y-%m-%d}  price={ev['price']:.2f}")
    if bot_all:
        print("\n=== BOTTOM EVENTS ===")
        for ev in bot_all:
            print(f"{ev['symbol']:<8} {ev['signal']:<14} {ev['time']:%Y-%m-%d}  price={ev['price']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
