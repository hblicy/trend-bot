#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加密货币趋势判断机器人

该程序从交易所拉取 BTC、ETH、SOL 最近 400 根 1D K 线，计算以下指标：
1. 200-SMA（方向）
2. MACD（动能）
3. ADX / +DI / -DI（趋势强度）

基于下述规则给出 Up / Down / Sideways 判断：
• 价 > 200-SMA 且 MACD 柱>0 且 ADX>25 且 +DI>-DI → Uptrend
• 价 < 200-SMA 且 MACD 柱<0 且 ADX>25 且 +DI<-DI → Downtrend
• 其余 → Sideways

定时（默认 4 小时）循环监控并输出最新结果
"""

import ccxt
import pandas as pd
import requests
import schedule
import time
import datetime
import logging
from logging.handlers import RotatingFileHandler
import sys
import concurrent.futures
from zoneinfo import ZoneInfo
import json
import os
from indicators import calculate_indicators as _calculate_indicators

# 公开别名，供 backtest.py 等外部模块调用
calculate_indicators = _calculate_indicators
from signals import (
    detect_bull_market_signals,
    detect_bear_market_signals,
    detect_blowoff,
    detect_exhaustion_top,
    detect_breakdown,
    detect_panic_sell,
    detect_capitulation,
    detect_bottom_reversal,
)
import config as cfg  # 显式导入配置模块，避免命名空间污染

# 将配置中全部大写常量注入当前模块的全局命名空间，保持后续代码兼容
for _k, _v in cfg.__dict__.items():
    if _k.isupper():
        globals()[_k] = _v

# 配置日志输出
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler('trend_detector.log', maxBytes=5_000_000, backupCount=3)
    ]
)
logger = logging.getLogger(__name__)

# 定义趋势状态文件路径
TREND_STATE_FILE = 'trend_states.json'
# 市场阶段状态文件（持久化牛/熊信号，避免重启重复告警）
MARKET_PHASE_STATE_FILE = 'market_phase_states.json'

def save_trend_states(trends):
    """将趋势状态保存到JSON文件
    
    Args:
        trends: 趋势状态字典，键为交易对名称，值为趋势状态
    """
    try:
        # 将趋势状态转换为可序列化的格式
        serializable_trends = {}
        for symbol, trend in trends.items():
            serializable_trends[symbol] = trend
        
        # 写入JSON文件
        with open(TREND_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable_trends, f, ensure_ascii=False, indent=2)
        
        logger.info(f"趋势状态已保存到 {TREND_STATE_FILE}")
    except Exception as e:
        logger.error(f"保存趋势状态失败: {str(e)}")

def load_trend_states():
    try:
        if os.path.exists(TREND_STATE_FILE):
            with open(TREND_STATE_FILE, 'r', encoding='utf-8') as f:
                trends = json.load(f)
            logger.info(f"从 {TREND_STATE_FILE} 加载趋势状态成功")
            return trends
        else:
            logger.info(f"趋势状态文件 {TREND_STATE_FILE} 不存在，将创建新文件")
            # Create empty file immediately
            with open(TREND_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({}, f)
            return {}
    except Exception as e:
        logger.error(f"加载趋势状态失败: {str(e)}")
        return {}

# ---------------- 市场阶段状态持久化 ----------------

def save_market_phase_states(states: dict):
    """保存市场阶段状态到磁盘，将 datetime 对象转换为可序列化格式"""
    def _to_serializable(obj):
        """递归地将 dict / list 中的 datetime 对象转换为 ISO 字符串"""
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        elif isinstance(obj, (list, tuple)):
            return [_to_serializable(i) for i in obj]
        elif isinstance(obj, dict):
            return {k: _to_serializable(v) for k, v in obj.items()}
        else:
            return obj

    try:
        serializable_states = _to_serializable(states)
        with open(MARKET_PHASE_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable_states, f, ensure_ascii=False, indent=2)
        logger.info(f"市场阶段状态已保存到 {MARKET_PHASE_STATE_FILE}")
    except Exception as e:
        logger.error(f"保存市场阶段状态失败: {e}")


def load_market_phase_states() -> dict:
    """加载市场阶段状态，如果文件不存在则返回空 dict"""
    try:
        if os.path.exists(MARKET_PHASE_STATE_FILE):
            with open(MARKET_PHASE_STATE_FILE, 'r', encoding='utf-8') as f:
                states = json.load(f)
            logger.info(f"从 {MARKET_PHASE_STATE_FILE} 加载市场阶段状态成功")
            return states
        # 初始化空文件
        with open(MARKET_PHASE_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        logger.info(f"市场阶段状态文件 {MARKET_PHASE_STATE_FILE} 不存在，已创建")
        return {}
    except Exception as e:
        logger.error(f"加载市场阶段状态失败: {e}")
        return {}

def init_exchange():
    """初始化交易所对象"""
    try:
        ex = getattr(ccxt, EXCHANGE)({'enableRateLimit': True})
        ex.load_markets()
        logger.info(f"初始化{EXCHANGE}交易所成功")
        return ex
    except Exception as e:
        logger.error(f"初始化交易所失败: {str(e)}")
        return None

def fetch_realtime_price(ex, symbol):
    """获取实时价格 (ticker last price)。

    Args:
        ex: ccxt 交易所对象
        symbol: 交易对名称

    Returns:
        float | None
    """
    try:
        ticker = ex.fetch_ticker(symbol)
        return ticker.get('last')
    except Exception as e:
        logger.warning(f"获取 {symbol} 实时价格失败: {e}")
        return None


def is_weekly_ma50_bearish(df: pd.DataFrame) -> bool:
    """Return True when the completed weekly trend is still bearish."""
    try:
        if df is None or len(df) < 365:
            return False

        weekly_df = df.resample("W-SUN").agg(
            {
                "close": "last",
                "high": "max",
                "low": "min",
                "open": lambda x: x.iloc[0] if len(x) > 0 else None,
            }
        )
        if weekly_df.empty:
            return False

        now_utc = datetime.datetime.utcnow()
        last_ts = weekly_df.index[-1].to_pydatetime()
        if (now_utc - last_ts).total_seconds() < 7 * 86_400:
            weekly_df = weekly_df.iloc[:-1]

        if len(weekly_df) < 51:
            return False

        weekly_ma50 = weekly_df["close"].rolling(window=50).mean()
        current_close = weekly_df["close"].iloc[-1]
        current_ma50 = weekly_ma50.iloc[-1]
        prev_ma50 = weekly_ma50.iloc[-2]

        if pd.isna(current_ma50) or pd.isna(prev_ma50):
            return False

        return current_close < current_ma50 and current_ma50 < prev_ma50
    except Exception as e:
        logger.warning(f"检测周线 MA50 偏空状态失败: {e}")
        return False


# 市场阶段 → 建议行动 映射表
_PHASE_ADVICE = {
    'blowoff':        ('⚡ 冲顶', '准备落袋，注意剧烈波动'),
    'exhaustion':     ('🏔️ 圆弧顶', '准备减仓，评估回调风险'),
    'breakdown':      ('📉 破位', '考虑出场或减仓'),
    'panic_sell':     ('🔻 加速下跌', '继续观望，等待探底信号'),
    'capitulation':   ('💥 砸盘探底', '关注反转信号，可小仓试探'),
    'bottom_reversal':('🔄 底部反转', '考虑分批建仓，设好止损'),
}

# 牛/熊市信号名 → 默认文案 (Fix: 重启后 in-memory dict 丢失时的 fallback)
_BULL_SIGNAL_MESSAGES = {
    'bull_early_sign':     '早期信号: 价格突破日线MA200，可尝试底仓建仓',
    'bull_confirmed':      '确认转强: MA50 上穿 MA200 且价格站稳 MA200/365',
    'bull_pullback_test':  '回踩 MA200/365 获得支撑并再次向上',
    'bull_final_verdict':  '周线 MA50 突破，牛市长期确认',
    'bull_reinforcement':  '周线 MA50 向上拐头，牛市深化',
}
_BEAR_SIGNAL_MESSAGES = {
    'bear_warning':        '价格跌破 MA200，警示',
    'bear_confirmed':      '死亡交叉且跌破 MA200/365',
    'bearish_retest':      '反弹受阻 MA200/365 再次下跌',
    'bear_final_verdict':  '周线 MA50 跌破，熊市长期确认',
    'bear_reinforcement':  '周线 MA50 向下拐头，熊市加深',
}

def _get_phase_summary(symbol: str) -> str:
    """根据 market_phase_states 生成当前币种的阶段摘要（一行文本）。
    如果没有任何活跃阶段则返回空字符串。
    重启后 in-memory 的 bull/bear_market_signals 会丢失，
    此时通过 persistent state 里存的 signal name 查 fallback 映射表。
    """
    state = market_phase_states.get(symbol, {})
    if not state:
        return ''

    # 检查牛市信号 — 优先用 in-memory，fallback 用 signal name 查表
    bull_sig_name = state.get('bull_signal')
    if bull_sig_name:
        sig = bull_market_signals.get(symbol)
        if sig and sig.get('message'):
            return f"📈 {sig['message']}"
        # fallback: 从 signal name 查静态映射
        fallback_msg = _BULL_SIGNAL_MESSAGES.get(bull_sig_name)
        if fallback_msg:
            return f"📈 {fallback_msg}"

    # 检查熊市信号
    bear_sig_name = state.get('bear_signal')
    if bear_sig_name:
        sig = bear_market_signals.get(symbol)
        if sig and sig.get('message'):
            return f"📉 {sig['message']}"
        fallback_msg = _BEAR_SIGNAL_MESSAGES.get(bear_sig_name)
        if fallback_msg:
            return f"📉 {fallback_msg}"

    # 检查形态信号 (按严重程度排序)
    for key in ['breakdown', 'blowoff', 'exhaustion',
                 'bottom_reversal', 'capitulation', 'panic_sell']:
        if state.get(key):
            label, advice = _PHASE_ADVICE[key]
            return f"{label} — {advice}"
    return ''


def fetch_ohlcv(ex, symbol, timeframe=None, *, max_retries: int = 3, backoff_secs: int = 2):
    """获取 K 线数据，带自动重试。

    Args:
        ex: ccxt 交易所对象
        symbol: 交易对名称，如 'BTC/USDT'
        timeframe: K 线周期，默认为配置中的 TIMEFRAME
        max_retries: 最大重试次数
        backoff_secs: 首次失败后的等待秒数，之后指数退避

    Returns:
        pd.DataFrame | None
    """
    if timeframe is None:
        timeframe = TIMEFRAME

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"第 {attempt}/{max_retries} 次获取 {symbol} {timeframe} K 线（{LOOKBACK} 根）")
            ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=LOOKBACK)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            if attempt < max_retries:
                wait = backoff_secs * (2 ** (attempt - 1))
                logger.warning(f"获取 {symbol} 失败: {e}，{wait}s 后重试 …")
                time.sleep(wait)
            else:
                logger.error(f"获取 {symbol} {timeframe} K 线重试{max_retries}次仍失败: {e}")
    return None

# --------- 技术信号 ----------  
def in_up(row, prev_row):
    """上升趋势进场信号，需要当前和前一蜡烛都满足条件"""
    try:
        # 检查必要的列是否存在
        if 'close' not in row or 'EMA' not in row:
            return False
            
        # 价格大于EMA
        price_above_ema = row['close'] > row['EMA']
        
        # MACD柱为正
        macd_positive = False
        if 'MACDh' in row:
            macd_positive = row['MACDh'] > 0
        
        # ADX大于25
        adx_strong = False
        if 'ADX' in row and 'volatility' in row:
            # 动态设置ADX阈值：波动率低时使用较低阈值，波动率高时使用较高阈值
            adx_threshold = 20 if row['volatility'] < 2 else (25 if row['volatility'] < 4 else 30)
            adx_strong = row['ADX'] > adx_threshold
        elif 'ADX' in row:  # 兼容旧数据，没有volatility时使用默认阈值
            adx_strong = row['ADX'] > 25
        
        # +DI大于-DI
        di_positive = False
        if 'DMP' in row and 'DMN' in row:
            di_positive = row['DMP'] > row['DMN']
        
        # 成交量确认 - 上升趋势中成交量应该高于平均成交量
        volume_confirms = True  # 默认为True，避免无法计算时阻止信号
        if 'rel_volume' in row:
            # 上升趋势中，如果成交量明显低于平均值，则不确认趋势
            # 这里使用0.8作为阈值，低于平均成交量的80%认为是弱势成交量
            volume_confirms = row['rel_volume'] >= 0.8
        
        # 识别是否为“突破”时刻（即上一根 K 线还在平均线以下）
        is_breakout = not (prev_row['close'] > prev_row['EMA'])
        
        # 上涨突破需要成交量配合
        if price_above_ema and macd_positive and is_breakout:
            # 如果是突破 EMA，则需要较大的成交量
            if 'rel_volume' in row:
                volume_confirms = row['rel_volume'] > 1.2  # 突破时需要 20% 以上的放量
        
        # 最终判断逻辑：
        # 1. 如果是突破，则必须满足成交量放大 (volume_confirms) 且 ADX/DI 走强
        # 2. 如果之前已经在趋势中 (非 breakout)，则维持保守的两根蜡烛确认逻辑
        
        if is_breakout:
            return price_above_ema and macd_positive and adx_strong and di_positive and volume_confirms
        else:
            prev_price_above_ema = prev_row['close'] > prev_row['EMA']
            prev_macd_positive = prev_row['MACDh'] > 0
            prev_adx_strong = prev_row['ADX'] > 20 # 维持趋势时 ADX 要求略低
            prev_di_positive = prev_row['DMP'] > prev_row['DMN']
            return price_above_ema and macd_positive and adx_strong and di_positive and volume_confirms and prev_price_above_ema and prev_macd_positive and prev_adx_strong and prev_di_positive
    except Exception as e:
        logger.error(f"上升趋势判断失败: {str(e)}")
        return False  

def in_down(row, prev_row):
    """下跌趋势进场信号，需要当前和前一蜡烛都满足条件"""
    try:
        # 检查必要的列是否存在
        if 'close' not in row or 'EMA' not in row:
            return False
            
        # 价格小于EMA
        price_below_ema = row['close'] < row['EMA']
        
        # MACD柱为负
        macd_negative = False
        if 'MACDh' in row:
            macd_negative = row['MACDh'] < 0
        
        # 基于波动率的动态ADX阈值
        adx_strong = False
        if 'ADX' in row and 'volatility' in row:
            # 动态设置ADX阈值：波动率低时使用较低阈值，波动率高时使用较高阈值
            adx_threshold = 20 if row['volatility'] < 2 else (25 if row['volatility'] < 4 else 30)
            adx_strong = row['ADX'] > adx_threshold
        elif 'ADX' in row:  # 兼容旧数据，没有volatility时使用默认阈值
            adx_strong = row['ADX'] > 25
        
        # +DI小于-DI
        di_negative = False
        if 'DMP' in row and 'DMN' in row:
            di_negative = row['DMP'] < row['DMN']
        
        # 成交量确认 - 下跌趋势中成交量应该高于平均成交量
        volume_confirms = True  # 默认为True，避免无法计算时阻止信号
        if 'rel_volume' in row:
            # 对于下跌趋势，通常需要放量，但也可能出现缩量下跌
            # 为了不过滤掉有效的下跌信号，使用较低的阈值
            volume_confirms = row['rel_volume'] >= 0.7
        
        # 识别是否为“突破（跌破）”时刻
        is_breakdown = not (prev_row['close'] < prev_row['EMA'])

        # 下跌突破需要成交量配合
        if price_below_ema and macd_negative and is_breakdown:
            # 如果是跌破 EMA，则需要较大的成交量
            if 'rel_volume' in row:
                volume_confirms = row['rel_volume'] > 1.1  # 跌破时需要 10% 以上的放量
        
        if is_breakdown:
            return price_below_ema and macd_negative and adx_strong and di_negative and volume_confirms
        else:
            prev_price_below_ema = prev_row['close'] < prev_row['EMA']
            prev_macd_negative = prev_row['MACDh'] < 0
            prev_adx_strong = prev_row['ADX'] > 20
            prev_di_negative = prev_row['DMP'] < prev_row['DMN']
            return price_below_ema and macd_negative and adx_strong and di_negative and volume_confirms and prev_price_below_ema and prev_macd_negative and prev_adx_strong and prev_di_negative
    except Exception as e:
        logger.error(f"下跌趋势判断失败: {str(e)}")
        return False  

def exit_signal(row, current_pos):
    """退出信号
    
    Args:
        row: 数据行
        current_pos: 1 多仓；-1 空仓
    """
    try:
        # 检查必要的列是否存在
        if 'MACDh' not in row or 'DMP' not in row or 'DMN' not in row:
            return False
            
        if current_pos == 1:  # 多仓的退出  
            macd_negative = row['MACDh'] < 0
            di_negative = row['DMP'] < row['DMN']
            return macd_negative and di_negative
            
        if current_pos == -1:  # 空仓的退出  
            macd_positive = row['MACDh'] > 0
            di_positive = row['DMP'] > row['DMN']
            return macd_positive and di_positive
            
        return False
    except Exception as e:
        logger.error(f"退出信号判断失败: {str(e)}")
        return False

def determine_trend(row, prev_trend=None, fast_row=None, prev_main_row=None):
    """基于行数据判断趋势
    
    Args:
        row: DataFrame的一行数据（主周期）
        prev_trend: 上一个趋势状态，用于出场规则
        fast_row: 快周期的数据行，用于二次确认
        prev_main_row: 主周期的前一行数据，用于确认
        
    Returns:
        str: 趋势类型 ('上升', '下跌', '盘整')
    """
    try:
        # 如果有快周期数据，检查是否有反转信号
        fast_reversal = False
        if fast_row is not None:
            # 判断快周期是否有反转信号
            if prev_trend == '上升' and exit_signal(fast_row, 1):
                fast_reversal = True
                logger.info(f"快周期出现上升趋势反转信号")
            elif prev_trend == '下跌' and exit_signal(fast_row, -1):
                fast_reversal = True
                logger.info(f"快周期出现下跌趋势反转信号")
        
        if fast_reversal:
            return '盘整'
        
        # 当前趋势状态的初步判断
        current_trend = '盘整'  # 默认为盘整
        
        # 检查进场条件
        if in_up(row, prev_main_row):
            current_trend = '上升'
        elif in_down(row, prev_main_row):
            current_trend = '下跌'
            
        # 如果有明确的趋势，返回该趋势
        if current_trend != '盘整':
            return current_trend
            
        # 如果当前判断为盘整，但存在前一个趋势，检查是否应该维持前一个趋势
        if prev_trend and prev_trend != '盘整':
            # Fix #4: 增强退出条件 — EMA 穿越 + ADX 衰减
            adx_val = row.get('ADX', 30)
            if prev_trend == '上升':
                # 价格跌破 EMA 或 ADX 衰减到 20 以下，降级
                if row['close'] < row['EMA']:
                    logger.info(f"上升趋势退出: 价格跌破 EMA")
                    return '盘整'
                if adx_val < 20:
                    logger.info(f"上升趋势退出: ADX 衰减至 {adx_val:.1f}")
                    return '盘整'
                if not exit_signal(row, 1):
                    logger.info(f"维持上升趋势状态")
                    return '上升'
            elif prev_trend == '下跌':
                # 价格站上 EMA 或 ADX 衰减到 20 以下，降级
                if row['close'] > row['EMA']:
                    logger.info(f"下跌趋势退出: 价格站上 EMA")
                    return '盘整'
                if adx_val < 20:
                    logger.info(f"下跌趋势退出: ADX 衰减至 {adx_val:.1f}")
                    return '盘整'
                if not exit_signal(row, -1):
                    logger.info(f"维持下跌趋势状态")
                    return '下跌'
        
        # 如果都不满足，返回盘整
        return '盘整'
    except Exception as e:
        logger.error(f"判断趋势失败: {str(e)}")
        return '盘整'

def send_wechat_notification(text):
    """发送企业微信通知
    
    Args:
        text: 要发送的纯文本内容
    """
    text = text.rstrip()

    # Fix: 空 webhook 短路，避免无意义的失败请求噪音
    if not WECHAT_WEBHOOK:
        logger.debug("WECHAT_WEBHOOK 未配置，跳过通知")
        return
    try:
        data = {"msgtype": "text", "text": {"content": text}}
        response = requests.post(WECHAT_WEBHOOK, json=data, timeout=10)
        response.raise_for_status()
        logger.info("企业微信通知发送成功")
    except Exception as e:
        logger.error(f"发送企业微信通知失败: {str(e)}")

def run_once(is_scheduled_report=False, report_type=None):
    """执行一次完整的趋势分析
    
    Args:
        is_scheduled_report: 是否为定时报告
        report_type: 报告类型（早间/晚间）
    """
    try:
        if is_scheduled_report:
            logger.info(f"开始执行{report_type}定时趋势分析")
        else:
            logger.info("开始执行趋势分析")
        
        # 初始化交易所
        exchange = init_exchange()
        if not exchange:
            logger.error("交易所初始化失败，无法继续")
            return
        
        # 存储各交易对的分析结果
        results: list[dict] = []

        def process_symbol(symbol: str):
            """并发处理单个交易对，返回结果或 None"""
            logger.info(f"分析 {symbol} 趋势 …")
            main_df = fetch_ohlcv(exchange, symbol)
            if main_df is None or len(main_df) < EMA_PERIOD:
                logger.warning(f"{symbol} 主周期数据不足，跳过")
                return None
            main_df = _calculate_indicators(main_df, 'main')

            # 快周期
            fast_df = None
            try:
                fast_df = fetch_ohlcv(exchange, symbol, timeframe=FAST_TIMEFRAME)
                if fast_df is not None and len(fast_df) >= FAST_EMA_PERIOD:
                    fast_df = _calculate_indicators(fast_df, 'fast')
                else:
                    fast_df = None
            except Exception as err:
                logger.error(f"{symbol} 快周期获取失败: {err}")
                fast_df = None

            if len(main_df) < 2:
                return None

            # --- 关键修改：防止信号重绘 ---
            # 判断最后一根 K 线是否已收盘
            # 如果 (当前时间 - 最后时间戳) < K线周期，说明最后一根 K 线正在行进中，不可信
            # 此时应使用倒数第二根 (iloc[-2]) 作为"当前"信号行
            # 否则使用倒数第一根 (iloc[-1])
            
            # 解析周期秒数 (简单处理 1d, 4h)
            tf_seconds = 86400  # 默认 1d
            if TIMEFRAME.endswith('h'):
                tf_seconds = int(TIMEFRAME[:-1]) * 3600
            elif TIMEFRAME.endswith('m'):
                tf_seconds = int(TIMEFRAME[:-1]) * 60
                
            last_ts = main_df.index[-1].to_pydatetime().replace(tzinfo=None) # 确保无时区
            now_ts = datetime.datetime.utcnow()
            
            # 使用更安全的逻辑：如果当前时间还没超过 (最后时间戳 + 周期)，说明该K线未完成
            time_diff = (now_ts - last_ts).total_seconds()
            
            # 决定使用的索引
            if time_diff < tf_seconds:
                # 最后一根未收盘，使用倒数第二根
                safe_main_idx = -2
                safe_prev_idx = -3
                logger.debug(f"{symbol} 最新K线未收盘 (diff={time_diff:.0f}s < {tf_seconds}s)，使用上一个已收盘K线")
            else:
                # 最后一根已收盘
                safe_main_idx = -1
                safe_prev_idx = -2
            
            # 确保索引有效
            if abs(safe_prev_idx) > len(main_df):
                 logger.warning(f"{symbol} 数据不足，无法回溯安全索引")
                 return None

            last_main_row = main_df.iloc[safe_main_idx]
            prev_main_row = main_df.iloc[safe_prev_idx]
            
            # 快周期也做未收盘保护，避免未完成的 4H K 线污染信号
            last_fast_row = None
            if fast_df is not None and len(fast_df) >= 2:
                fast_tf_seconds = 4 * 3600  # 4h = 14400s
                fast_last_ts = fast_df.index[-1].to_pydatetime().replace(tzinfo=None)
                fast_time_diff = (now_ts - fast_last_ts).total_seconds()
                if fast_time_diff < fast_tf_seconds:
                    last_fast_row = fast_df.iloc[-2]  # 未收盘，用上一根
                    logger.debug(f"{symbol} 快周期最新K线未收盘，使用上一根")
                else:
                    last_fast_row = fast_df.iloc[-1]
            prev_trend = previous_trends.get(symbol.split('/')[0])
            trend = determine_trend(last_main_row, prev_trend, last_fast_row, prev_main_row)

            coin = symbol.split('/')[0]

            if ENABLE_MARKET_PHASE_DETECTION:
                check_market_phases(coin, main_df, safe_idx=safe_main_idx)

            # 获取实时价格 (Fix #3)
            rt_price = fetch_realtime_price(exchange, symbol)

            # 获取阶段摘要 (Fix #1)
            phase_summary = _get_phase_summary(coin)

            emoji = {'上升': '', '下跌': '', '盘整': ''}[trend]
            try:
                return {
                    'symbol': coin,
                    'trend': trend,
                    'emoji': emoji,
                    'price': last_main_row['close'],
                    'realtime_price': rt_price,
                    'phase_summary': phase_summary,
                    'ema': last_main_row['EMA'],
                    'macd_hist': last_main_row.get('MACDh'),
                    'adx': last_main_row.get('ADX'),
                    'plus_di': last_main_row.get('DMP'),
                    'minus_di': last_main_row.get('DMN')
                }
            except Exception as e:
                logger.error(f"格式化 {symbol} 结果失败: {e}")
                return None

        # ---------------------------------------------------------
        # 串行执行分析 (移除线程池以保证 CCXT 稳定性)
        # ---------------------------------------------------------
        for symbol in SYMBOLS:
            res = process_symbol(symbol)
            if res:
                results.append(res)
        
        # 没有结果则返回
        if not results:
            logger.warning("没有可用的分析结果")
            return
        
        # 生成推送消息
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if is_scheduled_report and report_type:
            text = f"加密货币{report_type}趋势报告 \n>{timestamp}\n\n"
        else:
            text = f"加密货币趋势监控 \n>{timestamp}\n\n"
        
        # 添加每个交易对的结果
        for r in results:
            # 实时价 + 日线收盘价
            rt = r.get('realtime_price')
            if rt is not None:
                text += f"{r['symbol']} {r['trend']}\n"
                text += f"  实时: ${rt:,.2f} | 日线收盘: ${r['price']:,.2f}\n"
            else:
                text += f"{r['symbol']} {r['trend']} ${r['price']:,.2f}\n"
            # 阶段建议 (Fix #1)
            phase = r.get('phase_summary', '')
            if phase:
                text += f"  {phase}\n"
            text += "\n"
        
        text = text.rstrip()

        # 打印到控制台
        print("\n" + "=" * 50)
        print(text)
        print("=" * 50)
        
        # 发送到企业微信
        if is_scheduled_report:
            send_wechat_notification(text)
            logger.info(f"发送{report_type if report_type else '定时'}报告成功")
        
        # 检查趋势变化并发送告警
        if not is_scheduled_report:
            check_trend_changes(results)
            
            # 在日志中记录市场阶段状态
            if ENABLE_MARKET_PHASE_DETECTION:
                logger.info(f"市场阶段状态: {market_phase_states}")
        
        # 打印详细指标数据
        print("\n详细指标数据:")
        for r in results:
            print(f"\n{r['symbol']} ({r['trend']})")
            print(f"  价格: {r['price']:.2f}")
            print(f"  100-EMA: {r['ema']:.2f}")
            
            # 安全打印可能为None的值
            if r['macd_hist'] is not None:
                print(f"  MACD柱: {r['macd_hist']:.4f}")
            else:
                print(f"  MACD柱: None")
                
            if r['adx'] is not None:
                print(f"  ADX: {r['adx']:.2f}")
            else:
                print(f"  ADX: None")
                
            if r['plus_di'] is not None:
                print(f"  +DI: {r['plus_di']:.2f}")
            else:
                print(f"  +DI: None")
                
            if r['minus_di'] is not None:
                print(f"  -DI: {r['minus_di']:.2f}")
            else:
                print(f"  -DI: None")
        
        # 在分析完成后持久化市场阶段状态，防止重启后重复告警
        save_market_phase_states(market_phase_states)
        logger.info("趋势分析完成")
        
    except Exception as e:
        logger.error(f"执行趋势分析时发生异常: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())

# 存储每个交易对的上一次趋势状态
previous_trends = load_trend_states()

# 存储趋势变化的确认计数
trend_change_counters = {}

# 存储上次告警时间
last_trend_alert_times = {}

# 趋势变化确认所需的次数
TREND_CHANGE_CONFIRMATION_COUNT = 3

# 趋势变化告警的最小间隔时间（小时）
TREND_ALERT_MIN_INTERVAL_HOURS = 6

# 信号确认所需的次数（防止误报）
SIGNAL_CONFIRMATION_COUNT = 2

# 关键转折信号确认次数
CRITICAL_PATTERN_CONFIRMATION_COUNT = 1

# 初始化延迟次数（跳过前N次运行的信号）
INITIALIZATION_DELAY_COUNT = 2

# 记录系统已运行的次数，用于初始化延迟
system_run_count = 0

# 存储信号确认计数器
signal_confirmation_counters = {}

def check_trend_changes(results):
    """检查趋势变化并发送通知
    
    Args:
        results: 当前趋势结果列表
    """
    global previous_trends, trend_change_counters, last_trend_alert_times
    
    # 如果未启用趋势变化告警，直接返回
    if not ENABLE_TREND_CHANGE_ALERT:
        return
        
    # 如果是首次运行，初始化上一次趋势状态
    if not previous_trends:
        for r in results:
            symbol = r['symbol']
            previous_trends[symbol] = r['trend']
            trend_change_counters[symbol] = 0
            last_trend_alert_times[symbol] = datetime.datetime.now() - datetime.timedelta(hours=TREND_ALERT_MIN_INTERVAL_HOURS)  # 初始化为可立即告警
        logger.info("初始化趋势状态")
        # 立即落盘，防止未发生趋势变更就重启时丢失基线
        save_trend_states(previous_trends)
        return
    
    # 检查趋势变化
    potential_changes = []
    confirmed_changes = []
    current_time = datetime.datetime.now()
    
    for r in results:
        symbol = r['symbol']
        current_trend = r['trend']
        
        # 确保交易对在我们的记录中
        if symbol not in previous_trends:
            previous_trends[symbol] = current_trend
            trend_change_counters[symbol] = 0
            last_trend_alert_times[symbol] = current_time - datetime.timedelta(hours=TREND_ALERT_MIN_INTERVAL_HOURS)
            continue
        
        # 如果趋势发生变化
        if previous_trends[symbol] != current_trend:
            # 增加确认计数
            if symbol not in trend_change_counters:
                trend_change_counters[symbol] = 0
            trend_change_counters[symbol] += 1
            
            logger.info(f"{symbol} 趋势变化 {previous_trends[symbol]} -> {current_trend} 确认计数: {trend_change_counters[symbol]}/{TREND_CHANGE_CONFIRMATION_COUNT}")
            
            # 记录潜在变化
            potential_changes.append({
                'symbol': symbol,
                'previous_trend': previous_trends[symbol],
                'current_trend': current_trend,
                'price': r['price'],
                'count': trend_change_counters[symbol]
            })
            
            # 检查是否达到确认次数
            if trend_change_counters[symbol] >= TREND_CHANGE_CONFIRMATION_COUNT:
                # 检查是否满足最小告警间隔
                time_since_last_alert = current_time - last_trend_alert_times.get(symbol, datetime.datetime.min)
                min_interval = datetime.timedelta(hours=TREND_ALERT_MIN_INTERVAL_HOURS)
                
                if time_since_last_alert >= min_interval:
                    # 添加到确认变化列表
                    alert_price = r.get('realtime_price')
                    if alert_price is None:
                        alert_price = r['price']

                    confirmed_changes.append({
                        'symbol': symbol,
                        'previous_trend': previous_trends[symbol],
                        'current_trend': current_trend,
                        'price': alert_price
                    })
                    
                    # 更新上次告警时间
                    last_trend_alert_times[symbol] = current_time
                    
                    # 更新趋势状态并重置计数器
                    previous_trends[symbol] = current_trend
                    trend_change_counters[symbol] = 0
                    # 持久化趋势状态，防止重启丢失
                    save_trend_states(previous_trends)
                else:
                    logger.info(f"{symbol} 趋势变化已确认但未达到最小告警间隔 (还需 {(min_interval - time_since_last_alert).total_seconds() / 3600:.1f} 小时)")
        else:
            # 如果趋势没有变化，重置计数器
            trend_change_counters[symbol] = 0
    
    # 如果有确认的趋势变化，发送通知
    if confirmed_changes:
        # 生成通知消息
        timestamp = current_time.strftime('%Y-%m-%d %H:%M:%S')
        alert_text = f"加密货币趋势变化告警 \n>{timestamp}\n\n"
        
        for change in confirmed_changes:
            alert_text += f"{change['symbol']}\n"
            alert_text += f"价格: ${change['price']:,.2f}\n"
            alert_text += f"趋势: {change['previous_trend']} -> {change['current_trend']}\n\n"
        
        alert_text = alert_text.rstrip()

        # 打印到控制台
        print("\n" + "!" * 50)
        print(alert_text)
        print("!" * 50)
        
        # 发送到企业微信
        send_wechat_notification(alert_text)
        logger.info(f"发送趋势变化告警: {', '.join([c['symbol'] for c in confirmed_changes])}")
    
    # 如果有潜在变化但未确认，记录日志
    elif potential_changes:
        symbols_with_changes = [f"{c['symbol']}({c['count']}/{TREND_CHANGE_CONFIRMATION_COUNT})" for c in potential_changes]
        logger.info(f"潜在趋势变化未达到确认次数: {', '.join(symbols_with_changes)}")

# 存储每个交易对的市场阶段状态（从磁盘恢复）（从磁盘恢复，避免重启后重复告警）
market_phase_states = load_market_phase_states()

# 存储熊市信号检测结果
bear_market_signals = {}

# 存储牛市信号检测结果
bull_market_signals = {}

def check_market_phases(symbol, main_df, *, safe_idx: int = -1):
    """检测市场阶段并发送告警
    熊市来临和牛市开启信号只监控BTC
    
    Args:
        symbol: 交易对名称
        main_df: 主周期数据帧
        safe_idx: 经过未收盘保护的安全索引 (Fix #4: 统一口径)
    
    Returns:
        dict: 市场阶段检测结果
    """
    global market_phase_states
    
    # 如果未启用市场阶段检测，直接返回
    if not ENABLE_MARKET_PHASE_DETECTION:
        return None
        
    # 确保有足够的数据
    if len(main_df) < 5:
        return None
    
    # 获取最新数据行 — 使用与趋势判断相同的安全索引 (Fix #4)
    current_row = main_df.iloc[safe_idx]
    
    # ========== 冷启动保护 ==========
    # 如果这个 symbol 是首次被检测，我们只初始化状态，不发送告警
    # 这可以避免启动时因状态文件为空而发送大量过期告警
    is_cold_start = symbol not in market_phase_states
    
    # 检测各种市场阶段
    is_blowoff = detect_blowoff(current_row, main_df, safe_idx=safe_idx)  # 冲顶信号
    is_exhaustion = detect_exhaustion_top(main_df, safe_idx=safe_idx)  # 圆弧顶信号
    is_breakdown = detect_breakdown(current_row, main_df, safe_idx=safe_idx)  # 破位
    
    # 检测熊市和牛市信号 (只针对BTC)
    bear_signal = {'signal': None, 'level': None, 'message': None}
    bull_signal = {'signal': None, 'level': None, 'message': None}
    
    # 只对BTC进行牛熊市信号监控
    if symbol.split('/')[0].upper() == 'BTC':
        bear_signal = detect_bear_market_signals(main_df)
        bull_signal = detect_bull_market_signals(main_df)
    
    # 检测底部形成阶段
    is_panic_sell = False
    is_capitulation = False
    is_bottom_reversal = False
    
    if ENABLE_BOTTOM_DETECTION:
        is_panic_sell = detect_panic_sell(current_row, main_df, safe_idx=safe_idx)  # A. 加速下跌
        is_capitulation = detect_capitulation(current_row, main_df, safe_idx=safe_idx)  # B. 砸盘探底
        is_bottom_reversal = detect_bottom_reversal(current_row, main_df, safe_idx=safe_idx)  # C. 扭转向上
    
    # 初始化市场阶段状态
    if symbol not in market_phase_states:
        market_phase_states[symbol] = {
            'blowoff': False,
            'exhaustion': False,
            'breakdown': False,
            'panic_sell': False,
            'capitulation': False,
            'bottom_reversal': False,
            'last_alerts': {},   # Fix #3: 按信号类型独立冷却
            'bear_signal': None,
            'bull_signal': None
        }
    # Fix #3: 兼容旧格式迁移 — 将单一 last_alert 迁移为 last_alerts dict
    if 'last_alert' in market_phase_states[symbol] and 'last_alerts' not in market_phase_states[symbol]:
        old_val = market_phase_states[symbol].pop('last_alert')
        market_phase_states[symbol]['last_alerts'] = {} if old_val is None else {'_legacy': old_val}
    elif 'last_alerts' not in market_phase_states[symbol]:
        market_phase_states[symbol]['last_alerts'] = {}
    
    # 初始化熊市和牛市信号状态
    if symbol not in bear_market_signals:
        bear_market_signals[symbol] = None
        
    if symbol not in bull_market_signals:
        bull_market_signals[symbol] = None
    
    # 检查是否有新的市场阶段变化
    phase_changed = False
    phase_type = None
    
    # 检查熊市和牛市信号变化 (只针对BTC)
    bear_signal_changed = False
    bull_signal_changed = False
    
    # 只对BTC检查信号变化
    if symbol.split('/')[0].upper() == 'BTC':
        bear_signal_changed = bear_signal['signal'] is not None and bear_signal['signal'] != market_phase_states[symbol]['bear_signal']
        bull_signal_changed = bull_signal['signal'] is not None and bull_signal['signal'] != market_phase_states[symbol]['bull_signal']
    
    # 检测市场阶段变化（按优先级排序）
    if bull_signal_changed:
        phase_changed = True
        phase_type = '牛市信号'
        market_phase_states[symbol]['bull_signal'] = bull_signal['signal']
        bull_market_signals[symbol] = bull_signal
    elif bear_signal_changed:
        phase_changed = True
        phase_type = '熊市信号'
        market_phase_states[symbol]['bear_signal'] = bear_signal['signal']
        bear_market_signals[symbol] = bear_signal
    elif is_blowoff and not market_phase_states[symbol]['blowoff']:
        phase_changed = True
        phase_type = '冲顶'
        market_phase_states[symbol]['blowoff'] = True
    elif is_exhaustion and not market_phase_states[symbol]['exhaustion']:
        phase_changed = True
        phase_type = '圆弧顶'
        market_phase_states[symbol]['exhaustion'] = True
    elif is_breakdown and not market_phase_states[symbol]['breakdown']:
        phase_changed = True
        phase_type = '破位'
        market_phase_states[symbol]['breakdown'] = True
    elif is_panic_sell and not market_phase_states[symbol]['panic_sell']:
        phase_changed = True
        phase_type = '加速下跌'
        market_phase_states[symbol]['panic_sell'] = True
    elif is_capitulation and not market_phase_states[symbol]['capitulation']:
        phase_changed = True
        phase_type = '砸盘探底'
        market_phase_states[symbol]['capitulation'] = True
    elif is_bottom_reversal and not market_phase_states[symbol]['bottom_reversal']:
        phase_changed = True
        phase_type = '底部反转'
        market_phase_states[symbol]['bottom_reversal'] = True
    
    # --- 状态重置逻辑 (Fix #2) ---
    # 当信号消失时重置状态，允许下次再次触发
    # 配合 MARKET_PHASE_ALERT_COOLDOWN_HOURS (默认6h) 防止短时间内重复推送
    if not phase_changed:
        if not is_blowoff: market_phase_states[symbol]['blowoff'] = False
        if not is_exhaustion: market_phase_states[symbol]['exhaustion'] = False
        if not is_breakdown: market_phase_states[symbol]['breakdown'] = False
        if not is_panic_sell: market_phase_states[symbol]['panic_sell'] = False
        if not is_capitulation: market_phase_states[symbol]['capitulation'] = False
        if not is_bottom_reversal: market_phase_states[symbol]['bottom_reversal'] = False
        # 牛/熊信号消失时也清理，避免定时报告残留过期建议
        if bull_signal['signal'] is None and market_phase_states[symbol].get('bull_signal'):
            market_phase_states[symbol]['bull_signal'] = None
        if bear_signal['signal'] is None and market_phase_states[symbol].get('bear_signal'):
            market_phase_states[symbol]['bear_signal'] = None
    
    # 如果有新的市场阶段变化，发送告警
    # 注意：冷启动时（首次检测该 symbol）不发送告警，只初始化状态
    if phase_changed and not is_cold_start:
        # Fix #3: 按信号类型独立冷却
        last_alerts = market_phase_states[symbol].get('last_alerts', {})
        cooldown_hours = getattr(cfg, 'MARKET_PHASE_ALERT_COOLDOWN_HOURS', 6)
        last_alert_time = last_alerts.get(phase_type)
        if last_alert_time:
            # 尝试将 ISO 字符串转换为 datetime（从 JSON 加载时可能是字符串）
            if isinstance(last_alert_time, str):
                try:
                    last_alert_time = datetime.datetime.fromisoformat(last_alert_time)
                except ValueError:
                    last_alert_time = None
            if last_alert_time:
                time_since_last = datetime.datetime.now() - last_alert_time
                if time_since_last < datetime.timedelta(hours=cooldown_hours):
                    logger.info(f"{symbol} {phase_type} 信号在冷却期内 (还需 {cooldown_hours - time_since_last.total_seconds()/3600:.1f} 小时)，跳过告警")
                    return {'phase_changed': False, 'phase_type': None}
        
        # Fix #3: 记录该信号类型的最后告警时间
        market_phase_states[symbol].setdefault('last_alerts', {})[phase_type] = datetime.datetime.now()
        
        # 生成告警消息
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if phase_type == '冲顶':
            alert_text = f"**{symbol}** 冲顶信号 \n>{timestamp}\n\n"
            alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
            alert_text += f"RSI: {current_row['RSI']:.2f}\n"
            alert_text += f"特征: 巨量长上影 + 顶背离 + RSI超买\n"
            alert_text += f"建议行动: 准备落袋，发出剧烈波动告警"
        elif phase_type == '圆弧顶':
            # 计算价格偏离20EMA的百分比
            ema_deviation_pct = ((current_row['close'] - current_row['EMA20']) / current_row['EMA20']) * 100
            
            alert_text = f"**{symbol}** 圆弧顶信号 \n>{timestamp}\n\n"
            alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
            alert_text += f"RSI: {current_row['RSI']:.2f}, 偏离20EMA: {ema_deviation_pct:.1f}%\n"
            alert_text += f"特征: 创高价 + 远离均线 + RSI过高\n"
            alert_text += f"建议行动: 准备减仓，评估短期回调风险"
        elif phase_type == '破位':
            alert_text = f"警告信号！{symbol} 出现破位下跌，可能开始反转 \n>{timestamp}\n\n"
            alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
            alert_text += f"MACD: {current_row[current_row['macd_hist_col']]:.4f}\n"
            alert_text += f"特征: 跌破 20EMA + 均线开始下弯 + 动能减弱\n"
            alert_text += f"建议行动: 考虑出场或减仓"
        elif phase_type == '加速下跌':
            alert_text = f"市场警示！{symbol} 进入加速下跌阶段 \n>{timestamp}\n\n"
            alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
            alert_text += f"ADX: {current_row[current_row['adx_col']]:.2f}\n"
            alert_text += f"特征: 20EMA远离50EMA向下张口 + ADX强势升高 + 连续下跌\n"
            alert_text += f"建议行动: 继续观望，等待探底信号"
        elif phase_type == '砸盘探底':
            alert_text = f"关键信号！{symbol} 出现砸盘探底，可能已见底 \n>{timestamp}\n\n"
            alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
            alert_text += f"RSI: {current_row['RSI']:.2f}\n"
            alert_text += f"特征: 巨量长下影 + RSI超卖/底背离 + 交易量爆发\n"
            alert_text += f"建议行动: 关注可能的反转信号，准备小仓位试探"
        elif phase_type == '底部反转':
            coin_name = symbol.split('/')[0].upper()
            weekly_bear_active = is_weekly_ma50_bearish(main_df)
            if weekly_bear_active:
                alert_text = f"修复信号！{symbol} 日线出现反弹修复 \n>{timestamp}\n\n"
                alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
                alert_text += f"MACD: {current_row[current_row['macd_hist_col']]:.4f}\n"
                alert_text += f"特征: 连续{REVERSAL_CONFIRM_DAYS}天站上20EMA + 50EMA走平/上弯 + MACD柱翻正\n"
                alert_text += f"{coin_name} 日线出现反弹修复信号，但周线 MA50 仍向下，长期熊市结构未解除。\n"
                alert_text += "建议行动：仅观察或轻仓试探，等待周线结构改善。"
            else:
                alert_text = f"机会信号！{symbol} 确认底部反转向上 \n>{timestamp}\n\n"
                alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
                alert_text += f"MACD: {current_row[current_row['macd_hist_col']]:.4f}\n"
                alert_text += f"特征: 连续{REVERSAL_CONFIRM_DAYS}天站上20EMA + 50EMA走平/上弯 + MACD柱翻正\n"
                alert_text += f"建议行动: 考虑分批建仓，设置正确止损位"
        elif phase_type == '熊市信号':
            # 熊市信号类型
            bear_signal_type = bear_market_signals[symbol]['signal']
            alert_level = bear_market_signals[symbol]['level']
            
            # 根据不同的熊市信号类型设置不同的警报前缀
            if alert_level == 'yellow':
                prefix = "⚠️ 黄色预警"
            elif alert_level == 'red':
                prefix = "🔴 红色预警"
            elif alert_level == 'dark_red':
                prefix = "⛔ 最终预警"
            elif alert_level == 'black':
                prefix = "⚫ 深度熊市确认"
            else:
                prefix = "🔔 熊市信号"
                
            alert_text = f"{prefix}！{symbol} 熊市路径监控 \n>{timestamp}\n\n"
            alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
            alert_text += f"{bear_market_signals[symbol]['message']}"
        elif phase_type == '牛市信号':
            # 牛市信号类型
            bull_signal_type = bull_market_signals[symbol]['signal']
            alert_level = bull_market_signals[symbol]['level']
            
            # 根据不同的牛市信号类型设置不同的警报前缀
            if alert_level == 'green':
                prefix = "🌱 绿色曙光"
            elif alert_level == 'blue':
                prefix = "🔵 蓝色信号"
            elif alert_level == 'golden':
                prefix = "🌟 金色确认"
            else:
                prefix = "📈 牛市信号"
                
            alert_text = f"{prefix}！{symbol} 牛市路径监控 \n>{timestamp}\n\n"
            alert_text += f"当前价格: ${current_row['close']:,.2f}\n"
            alert_text += f"{bull_market_signals[symbol]['message']}"
        
        # 打印到控制台
        print("\n" + "#" * 50)
        print(alert_text)
        print("#" * 50)
        
        # 发送到企业微信
        send_wechat_notification(alert_text)
        logger.info(f"发送{symbol} {phase_type}阶段告警")
    
    # 返回市场阶段检测结果
    return {
        'blowoff': is_blowoff,
        'exhaustion': is_exhaustion,
        'breakdown': is_breakdown,
        'panic_sell': is_panic_sell,
        'capitulation': is_capitulation,
        'bottom_reversal': is_bottom_reversal,
        'bear_signal': bear_signal['signal'],
        'bull_signal': bull_signal['signal']
    }

def send_first_report():
    """发送第一次定时报告（上午8点）"""
    logger.info("发送上午8点定时报告")
    run_once(is_scheduled_report=True, report_type='上午8点')

def send_second_report():
    """发送第二次定时报告（下午4点）"""
    logger.info("发送下午4点定时报告")
    run_once(is_scheduled_report=True, report_type='下午4点')

def send_third_report():
    """发送第三次定时报告（凌晨0点）"""
    logger.info("发送凌晨0点定时报告")
    run_once(is_scheduled_report=True, report_type='凌晨0点')

def main():
    """主函数"""
    try:
        # 添加启动日志并推送到微信
        startup_message = "趋势判断机器人已启动"
        logger.info(startup_message)
        send_wechat_notification(startup_message)
        
        logger.info("启动加密货币趋势判断机器人")
        logger.info(f"监控交易对: {', '.join(SYMBOLS)}")
        logger.info(f"实时监控间隔: {MONITOR_INTERVAL}分钟")
        logger.info(f"定时报告时间: {FIRST_REPORT_HOUR:02d}:00, {SECOND_REPORT_HOUR:02d}:00, {THIRD_REPORT_HOUR:02d}:00 (每8小时)")
        logger.info(f"趋势变化告警: {'已启用' if ENABLE_TREND_CHANGE_ALERT else '未启用'}")
        
        # 启动时立即执行一次
        run_once()
        
        # 设置定时监控任务
        schedule.every(MONITOR_INTERVAL).minutes.do(run_once)
        
        # Fix #5 round 2: DST 安全的定时报告 —— 每分钟检查一次配置时区的当前时间
        # 不再静态换算为本地时间，而是在运行时动态判断
        report_tz = ZoneInfo(getattr(cfg, 'REPORT_TIMEZONE', 'Asia/Shanghai'))
        _report_hours = {
            FIRST_REPORT_HOUR:  ('上午8点',  send_first_report),
            SECOND_REPORT_HOUR: ('下午4点',  send_second_report),
            THIRD_REPORT_HOUR:  ('凌晨0点',  send_third_report),
        }
        _last_fired_date = {}  # {hour: date}  防止同一小时内重复触发
        
        def _check_report_schedule():
            """每分钟调用，检查配置时区的当前 hour:minute 是否匹配报告时间"""
            now_tz = datetime.datetime.now(tz=report_tz)
            cur_hour, cur_min = now_tz.hour, now_tz.minute
            cur_date = now_tz.date()
            if cur_min != REPORT_MINUTE:
                return
            if cur_hour in _report_hours:
                if _last_fired_date.get(cur_hour) == cur_date:
                    return  # 今天已触发过
                _last_fired_date[cur_hour] = cur_date
                label, fn = _report_hours[cur_hour]
                logger.info(f"定时报告触发: {label} ({report_tz} {cur_hour:02d}:{cur_min:02d})")
                fn()
        
        schedule.every(1).minutes.do(_check_report_schedule)
        logger.info(f"定时报告: 时区={report_tz}, 报告时间={[f'{h:02d}:{REPORT_MINUTE:02d}' for h in _report_hours]}")
        
        # 主循环
        while True:
            schedule.run_pending()
            time.sleep(20)
            
    except KeyboardInterrupt:
        logger.info("用户中断，程序退出")
    except Exception as e:
        logger.error(f"程序运行异常: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
