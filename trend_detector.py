#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加密货币趋势判断机器人

该程序从交易所拉取 BTC、ETH、SOL、BNB 最近 400 根 1D K 线，计算以下指标：
1. 100-EMA（方向）
2. MACD（动能，默认 8/17/6）
3. ADX / +DI / -DI（趋势强度）
4. 4H 快周期二次确认

基于 config.py 中的进场/出场规则给出 上升 / 下跌 / 盘整。
定时报告：Asia/Shanghai 08:01 / 16:01 / 00:01；实时监控间隔见 MONITOR_INTERVAL。
"""

import ccxt
import pandas as pd
import requests
import schedule
import time
import datetime
import copy
import logging
from logging.handlers import RotatingFileHandler
import sys
import concurrent.futures
from zoneinfo import ZoneInfo
import json
import os
import tempfile
from indicators import calculate_indicators as _calculate_indicators

# 公开别名，供 backtest.py 等外部模块调用
calculate_indicators = _calculate_indicators
from signals import (
    _BEAR_SIGNAL_MESSAGES,
    _BULL_SIGNAL_MESSAGES,
    _get_completed_slice,
    _get_completed_weekly_ma50,
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

def save_market_phase_states(states: dict) -> bool:
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

    temp_path = None
    try:
        serializable_states = _to_serializable(states)
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=os.path.dirname(os.path.abspath(MARKET_PHASE_STATE_FILE)),
            prefix='.market-phase-', delete=False,
        ) as f:
            temp_path = f.name
            json.dump(serializable_states, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, MARKET_PHASE_STATE_FILE)
        temp_path = None
        logger.info(f"市场阶段状态已保存到 {MARKET_PHASE_STATE_FILE}")
        return True
    except Exception:
        logger.exception("保存市场阶段状态失败")
        return False
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                logger.exception("清理市场阶段临时状态失败")


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

_exchange = None


def init_exchange(*, force_reload: bool = False):
    """初始化交易所对象。复用连接，避免每次分析都 load_markets 拖过整点。"""
    global _exchange
    try:
        if _exchange is not None and not force_reload:
            return _exchange
        ex = getattr(ccxt, EXCHANGE)({'enableRateLimit': True})
        ex.load_markets()
        _exchange = ex
        logger.info(f"初始化{EXCHANGE}交易所成功")
        return ex
    except Exception as e:
        _exchange = None
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


def is_weekly_ma50_bearish(df: pd.DataFrame, *, as_of_utc=None):
    """周线数据不足时返回 None，否则返回是否偏空。"""
    try:
        if df is None or len(df) < 365:
            return None

        weekly_df = _get_completed_weekly_ma50(df, as_of_utc=as_of_utc)
        if len(weekly_df) < 51:
            return None

        current_close = weekly_df["close"].iloc[-1]
        current_ma50 = weekly_df["MA50"].iloc[-1]
        prev_ma50 = weekly_df["MA50"].iloc[-2]

        if pd.isna(current_close) or pd.isna(current_ma50) or pd.isna(prev_ma50):
            return None

        return current_close < current_ma50 and current_ma50 < prev_ma50
    except Exception as e:
        logger.warning(f"检测周线 MA50 偏空状态失败: {e}")
        return None


# 市场阶段 → 建议行动 映射表
_PHASE_ADVICE = {
    'blowoff':        ('⚡ 冲顶', '准备落袋，注意剧烈波动'),
    'exhaustion':     ('🏔️ 圆弧顶', '准备减仓，评估回调风险'),
    'breakdown':      ('📉 破位', '考虑出场或减仓'),
    'panic_sell':     ('🔻 加速下跌', '继续观望，等待探底信号'),
    'capitulation':   ('💥 砸盘探底', '关注反转信号，可小仓试探'),
    'bottom_reversal':('🔄 底部反转信号', '日线出现反弹迹象'),
}

_WEEKLY_DATA_UNAVAILABLE = '周线数据不全，暂无法确认周线趋势。'

_PHASE_SUMMARY_PRIORITY = (
    'bull_signal',
    'bear_signal',
    'breakdown',
    'blowoff',
    'exhaustion',
    'bottom_reversal',
    'capitulation',
    'panic_sell',
)


def _get_phase_summary_for_key(symbol: str, state_key: str, *, weekly_bearish=None) -> str:
    """生成指定阶段状态的一行报告摘要。"""
    state = market_phase_states.get(symbol, {})
    if not state:
        return ''

    if state_key == 'bull_signal':
        bull_sig_name = state.get('bull_signal')
        if not bull_sig_name:
            return ''
        sig = bull_market_signals.get(symbol)
        if sig and sig.get('message'):
            return f"📈 {sig['message']}"
        fallback_msg = _BULL_SIGNAL_MESSAGES.get(bull_sig_name)
        return f"📈 {fallback_msg}" if fallback_msg else ''

    if state_key == 'bear_signal':
        bear_sig_name = state.get('bear_signal')
        if not bear_sig_name:
            return ''
        sig = bear_market_signals.get(symbol)
        if sig and sig.get('message'):
            return f"📉 {sig['message']}"
        fallback_msg = _BEAR_SIGNAL_MESSAGES.get(bear_sig_name)
        return f"📉 {fallback_msg}" if fallback_msg else ''

    if state.get(state_key) and state_key in _PHASE_ADVICE:
        if state_key == 'bottom_reversal':
            if weekly_bearish is None:
                return f'🔄 底部反转信号 — {_WEEKLY_DATA_UNAVAILABLE}'
            if weekly_bearish:
                return f"🔄 日线反弹修复 — {_BEAR_SIGNAL_MESSAGES['bear_reinforcement']}"
        label, advice = _PHASE_ADVICE[state_key]
        return f"{label} — {advice}"
    return ''


def _get_phase_summaries(symbol: str, previous_state=None, *, weekly_bearish=None) -> dict[str, str]:
    """选取报告摘要，同时保留并存的多空信号。"""
    summaries = {}
    for state_key in _PHASE_SUMMARY_PRIORITY:
        summary = _get_phase_summary_for_key(symbol, state_key, weekly_bearish=weekly_bearish)
        if summary:
            summaries[state_key] = summary
    if not summaries:
        return {}

    preferred_key = next(iter(summaries))
    if previous_state is not None:
        current_state = market_phase_states[symbol]
        preferred_key = next(
            (key for key in summaries if current_state[key] != previous_state.get(key)),
            preferred_key,
        )
    selected = {preferred_key: summaries[preferred_key]}
    if 'bull_signal' in summaries and 'bear_signal' in summaries:
        selected['bull_signal'] = summaries['bull_signal']
        selected['bear_signal'] = summaries['bear_signal']
    return selected


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
    global _exchange
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
                _exchange = None
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
        return False
    data = {"msgtype": "text", "text": {"content": text}}
    try:
        response = requests.post(WECHAT_WEBHOOK, json=data, timeout=10)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        # 请求异常文本可能包含带凭据的 URL。
        status = exc.response.status_code if exc.response is not None else None
        logger.error("企业微信通知请求失败 (%s, HTTP=%s)", type(exc).__name__, status)
        return False

    errcode = result.get("errcode") if isinstance(result, dict) else None
    if type(errcode) is not int:
        logger.error("企业微信通知返回无效的业务状态")
        return False
    if errcode != 0:
        logger.error("企业微信通知返回业务错误: errcode=%d", errcode)
        return False
    logger.info("企业微信通知发送成功")
    return True


_PHASE_ALERT_TYPES = {
    'blowoff': '冲顶',
    'exhaustion': '圆弧顶',
    'breakdown': '破位',
    'panic_sell': '加速下跌',
    'capitulation': '砸盘探底',
    'bottom_reversal': '底部反转',
    'bear_signal': '熊市信号',
    'bull_signal': '牛市信号',
}
_PATH_SIGNAL_KEYS = {
    _PHASE_ALERT_TYPES['bear_signal']: 'bear_signal',
    _PHASE_ALERT_TYPES['bull_signal']: 'bull_signal',
}


def _record_phase_delivery(state, phase_type, notified_at):
    state.setdefault('last_alerts', {})[phase_type] = notified_at
    state_key = _PATH_SIGNAL_KEYS.get(phase_type)
    if state_key is not None:
        state.setdefault('last_delivered_signals', {})[state_key] = state[state_key]


def _mark_reported_phase_changes(previous_states, report_results, notified_at=None):
    """将定时报告中首次送达或变化的阶段信号计入独立告警冷却。"""
    notified_at = notified_at or datetime.datetime.now()
    for result in report_results:
        symbol = result['symbol']
        current_state = market_phase_states.get(symbol, {})
        previous_state = previous_states.get(symbol, {})
        for state_key in result['_reported_phase_keys']:
            current_value = current_state.get(state_key)
            if current_value:
                phase_type = _PHASE_ALERT_TYPES[state_key]
                if phase_type in _PATH_SIGNAL_KEYS:
                    previous_value = current_state.get('last_delivered_signals', {}).get(state_key)
                else:
                    previous_value = previous_state.get(state_key)
                if current_value != previous_value or not current_state.get('last_alerts', {}).get(phase_type):
                    _record_phase_delivery(current_state, phase_type, notified_at)


def _report_now():
    return datetime.datetime.now(tz=ZoneInfo(REPORT_TIMEZONE))


def _in_report_window(now_tz):
    report_hours = (FIRST_REPORT_HOUR, SECOND_REPORT_HOUR, THIRD_REPORT_HOUR)
    grace = max(10, int(MONITOR_INTERVAL))
    return now_tz.hour in report_hours and now_tz.minute <= REPORT_MINUTE + grace


def _format_candle_close(row):
    duration = pd.Timedelta(seconds=ccxt.Exchange.parse_timeframe(TIMEFRAME))
    close_utc = pd.Timestamp(row.name).tz_localize('UTC') + duration
    close_local = close_utc.tz_convert(ZoneInfo(REPORT_TIMEZONE))
    label = '日线收盘价' if TIMEFRAME == '1d' else f'{TIMEFRAME} K 线收盘价'
    return f"{label}: ${row['close']:,.2f}（{close_local:%Y-%m-%d %H:%M %z}）"

def run_once(is_scheduled_report=False, report_type=None):
    """执行一次完整的趋势分析
    
    Args:
        is_scheduled_report: 是否为定时报告
        report_type: 报告类型（早间/晚间）
    """
    scheduled_state_snapshot = None
    scheduled_report_sent = False
    analysis_utc = datetime.datetime.utcnow()
    if is_scheduled_report:
        scheduled_state_snapshot = (
            copy.deepcopy(market_phase_states),
            copy.deepcopy(bull_market_signals),
            copy.deepcopy(bear_market_signals),
        )

    try:
        if not is_scheduled_report:
            now_tz = _report_now()
            if _in_report_window(now_tz):
                logger.info(
                    f"已进入定时报告窗口 ({now_tz.strftime('%H:%M')} {REPORT_TIMEZONE})，中止本轮非报告分析"
                )
                return True

        if is_scheduled_report:
            logger.info(f"开始执行{report_type}定时趋势分析")
        else:
            logger.info("开始执行趋势分析")
        
        # 初始化交易所
        exchange = init_exchange()
        if not exchange:
            logger.error("交易所初始化失败，无法继续")
            return False
        
        # 存储各交易对的分析结果
        results: list[dict] = []
        tf_seconds = ccxt.Exchange.parse_timeframe(TIMEFRAME)
        fast_tf_seconds = ccxt.Exchange.parse_timeframe(FAST_TIMEFRAME)

        def process_symbol(symbol: str):
            """分析单个交易对，返回结果或 None。"""
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

            if is_scheduled_report and fast_df is None:
                logger.warning(f"{symbol} 快周期数据不足，取消本次定时报告")
                return None

            if not is_scheduled_report and _in_report_window(_report_now()):
                return None

            # 取数开始前已经收盘的 K 线才可以用于这一轮分析。
            safe_main_idx, safe_prev_idx = _get_completed_slice(main_df, tf_seconds, as_of_utc=analysis_utc)
            if safe_prev_idx is None:
                logger.warning(f"{symbol} 已收盘主周期 K 线不足，无法分析")
                return None

            last_main_row = main_df.iloc[safe_main_idx]
            prev_main_row = main_df.iloc[safe_prev_idx]
            
            last_fast_row = None
            if fast_df is not None and len(fast_df) >= 2:
                fast_idx, _ = _get_completed_slice(fast_df, fast_tf_seconds, as_of_utc=analysis_utc)
                if fast_idx is not None:
                    last_fast_row = fast_df.iloc[fast_idx]

            if is_scheduled_report:
                for timeframe, row, seconds in (
                    (TIMEFRAME, last_main_row, tf_seconds),
                    (FAST_TIMEFRAME, last_fast_row, fast_tf_seconds),
                ):
                    interval = pd.Timedelta(seconds=seconds)
                    expected_open = pd.Timestamp(analysis_utc).floor(interval) - interval
                    if row is None or row.name != expected_open:
                        logger.warning(
                            "%s %s 缺少最新已收盘 K 线，预期开盘时间 %s UTC，取到 %s UTC",
                            symbol, timeframe, expected_open, row.name if row is not None else None,
                        )
                        return None

            prev_trend = previous_trends.get(symbol.split('/')[0])
            trend = determine_trend(last_main_row, prev_trend, last_fast_row, prev_main_row)

            coin = symbol.split('/')[0]
            previous_phase_state = None
            if is_scheduled_report:
                previous_phase_state = market_phase_states.get(coin, {}).copy()

            phase_result = None
            if ENABLE_MARKET_PHASE_DETECTION:
                # 定时报告只把阶段写进汇总，不单独推送修复/形态信号，避免整点只收到修复信号
                phase_result = check_market_phases(
                    coin,
                    main_df,
                    safe_idx=safe_main_idx,
                    send_alert=not is_scheduled_report,
                    as_of_utc=analysis_utc,
                )

            # 获取实时价格 (Fix #3)
            rt_price = fetch_realtime_price(exchange, symbol)

            weekly_bearish = phase_result.get('weekly_bearish') if phase_result else None
            phase_summaries = _get_phase_summaries(coin, previous_phase_state, weekly_bearish=weekly_bearish)
            summary_lines = list(phase_summaries.values())
            if phase_result and coin == 'BTC' and weekly_bearish is None and 'bottom_reversal' not in phase_summaries:
                summary_lines.append(_WEEKLY_DATA_UNAVAILABLE)

            emoji = {'上升': '', '下跌': '', '盘整': ''}[trend]
            try:
                return {
                    'symbol': coin,
                    'trend': trend,
                    'emoji': emoji,
                    'price': last_main_row['close'],
                    'close_text': _format_candle_close(last_main_row),
                    'realtime_price': rt_price,
                    'phase_summary': '\n  '.join(summary_lines),
                    '_reported_phase_keys': tuple(phase_summaries),
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
        failed_symbols = []
        for symbol in SYMBOLS:
            res = process_symbol(symbol)
            if not is_scheduled_report and _in_report_window(_report_now()):
                logger.info("实时轮询进入定时报告窗口，结束本轮分析")
                save_market_phase_states(market_phase_states)
                return True
            if res:
                results.append(res)
            else:
                failed_symbols.append(symbol)

        if is_scheduled_report and failed_symbols:
            logger.error(
                "定时报告数据不完整，取消发送并等待重试: %s",
                ", ".join(failed_symbols),
            )
            return False
        
        # 没有结果则返回
        if not results:
            logger.warning("没有可用的分析结果")
            return False
        
        # 生成推送消息
        timestamp = _report_now().strftime('%Y-%m-%d %H:%M:%S')
        if is_scheduled_report and report_type:
            text = f"加密货币{report_type}趋势报告 \n>{timestamp}\n\n"
        else:
            text = f"加密货币趋势监控 \n>{timestamp}\n\n"
        
        # 添加每个交易对的结果
        for r in results:
            # 实时价 + 日线收盘价
            rt = r.get('realtime_price')
            text += f"{r['symbol']} {r['trend']}\n"
            if rt is not None:
                text += f"  实时: ${rt:,.2f}\n"
            text += f"  {r['close_text']}\n"
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
            scheduled_report_sent = send_wechat_notification(text)
            if not scheduled_report_sent:
                logger.error(f"发送{report_type if report_type else '定时'}报告失败")
                return False
            _mark_reported_phase_changes(scheduled_state_snapshot[0], results)
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
        return True
        
    except Exception as e:
        logger.error(f"执行趋势分析时发生异常: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return scheduled_report_sent if is_scheduled_report else False
    finally:
        if is_scheduled_report and not scheduled_report_sent and scheduled_state_snapshot:
            for current, previous in zip(
                (market_phase_states, bull_market_signals, bear_market_signals),
                scheduled_state_snapshot,
            ):
                current.clear()
                current.update(previous)
            logger.info("定时报告未送达，已恢复本轮之前的市场阶段状态")

# 存储每个交易对的上一次趋势状态
previous_trends = load_trend_states()

# 存储每个币种的候选趋势和连续确认次数
trend_change_candidates = {}

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
    global previous_trends, trend_change_candidates, last_trend_alert_times
    
    # 如果未启用趋势变化告警，直接返回
    if not ENABLE_TREND_CHANGE_ALERT:
        return
        
    # 如果是首次运行，初始化上一次趋势状态
    if not previous_trends:
        for r in results:
            symbol = r['symbol']
            previous_trends[symbol] = r['trend']
            trend_change_candidates.pop(symbol, None)
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
            trend_change_candidates.pop(symbol, None)
            last_trend_alert_times[symbol] = current_time - datetime.timedelta(hours=TREND_ALERT_MIN_INTERVAL_HOURS)
            continue
        
        # 如果趋势发生变化
        if previous_trends[symbol] != current_trend:
            # 增加确认计数
            candidate, count = trend_change_candidates.get(symbol, (None, 0))
            count = min(count + 1, TREND_CHANGE_CONFIRMATION_COUNT) if candidate == current_trend else 1
            trend_change_candidates[symbol] = (current_trend, count)
            potential_changes.append(f"{symbol}({count}/{TREND_CHANGE_CONFIRMATION_COUNT})")
            
            logger.info(f"{symbol} 趋势变化 {previous_trends[symbol]} -> {current_trend} 确认计数: {count}/{TREND_CHANGE_CONFIRMATION_COUNT}")
            
            # 检查是否达到确认次数
            if count >= TREND_CHANGE_CONFIRMATION_COUNT:
                # 检查是否满足最小告警间隔
                time_since_last_alert = current_time - last_trend_alert_times.get(symbol, datetime.datetime.min)
                min_interval = datetime.timedelta(hours=TREND_ALERT_MIN_INTERVAL_HOURS)
                
                if time_since_last_alert >= min_interval:
                    # 添加到确认变化列表
                    realtime = r.get('realtime_price')
                    confirmed_changes.append({
                        'symbol': symbol,
                        'previous_trend': previous_trends[symbol],
                        'current_trend': current_trend,
                        'price_text': r['close_text'] if realtime is None else f'实时: ${realtime:,.2f}',
                    })
                else:
                    logger.info(f"{symbol} 趋势变化已确认但未达到最小告警间隔 (还需 {(min_interval - time_since_last_alert).total_seconds() / 3600:.1f} 小时)")
        else:
            # 如果趋势没有变化，重置计数器
            trend_change_candidates.pop(symbol, None)
    
    # 如果有确认的趋势变化，发送通知
    if confirmed_changes:
        # 生成通知消息
        timestamp = _report_now().strftime('%Y-%m-%d %H:%M:%S')
        alert_text = f"加密货币趋势变化告警 \n>{timestamp}\n\n"
        
        for change in confirmed_changes:
            alert_text += f"{change['symbol']}\n"
            alert_text += f"{change['price_text']}\n"
            alert_text += f"趋势: {change['previous_trend']} -> {change['current_trend']}\n\n"
        
        alert_text = alert_text.rstrip()

        # 打印到控制台
        print("\n" + "!" * 50)
        print(alert_text)
        print("!" * 50)
        
        # 发送到企业微信
        if _in_report_window(_report_now()):
            logger.info("趋势变化告警进入定时报告窗口，留待后续轮询")
            return
        if not send_wechat_notification(alert_text):
            logger.warning("趋势变化告警发送失败，将在后续轮询重试")
            return

        for change in confirmed_changes:
            symbol = change['symbol']
            last_trend_alert_times[symbol] = current_time
            previous_trends[symbol] = change['current_trend']
            trend_change_candidates.pop(symbol, None)
        save_trend_states(previous_trends)
        logger.info(f"发送趋势变化告警: {', '.join(c['symbol'] for c in confirmed_changes)}")
    
    # 如果有潜在变化但未确认，记录日志
    elif potential_changes:
        logger.info(f"潜在趋势变化未达到确认次数: {', '.join(potential_changes)}")

# 存储每个交易对的市场阶段状态（从磁盘恢复）（从磁盘恢复，避免重启后重复告警）
market_phase_states = load_market_phase_states()

# 存储熊市信号检测结果
bear_market_signals = {}

# 存储牛市信号检测结果
bull_market_signals = {}

def check_market_phases(symbol, main_df, *, safe_idx: int = -1, send_alert: bool = True, as_of_utc=None):
    """检测市场阶段并发送告警
    熊市来临和牛市开启信号只监控BTC
    
    Args:
        symbol: 交易对名称
        main_df: 主周期数据帧
        safe_idx: 经过未收盘保护的安全索引 (Fix #4: 统一口径)
        send_alert: 是否立即推送阶段告警。定时报告应设为 False，阶段摘要会写入趋势报告。
    
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
    is_btc = symbol.split('/')[0].upper() == 'BTC'
    if is_btc:
        bear_signal = detect_bear_market_signals(main_df, as_of_utc=as_of_utc)
        bull_signal = detect_bull_market_signals(main_df, as_of_utc=as_of_utc)
    
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
    
    phase_state = market_phase_states[symbol]
    previous_phase_state = phase_state.copy()
    previous_bull_message = bull_market_signals.get(symbol)
    previous_bear_message = bear_market_signals.get(symbol)
    weekly_bearish = None
    if is_btc or is_bottom_reversal or phase_state.get('bottom_reversal'):
        weekly_bearish = is_weekly_ma50_bearish(main_df, as_of_utc=as_of_utc)

    path_changes = []
    for key, signal, cache in (
        ('bull_signal', bull_signal, bull_market_signals),
        ('bear_signal', bear_signal, bear_market_signals),
    ):
        current_signal = signal['signal']
        previous_signal = phase_state.get(key)
        if current_signal and current_signal != previous_signal:
            path_changes.append(_PHASE_ALERT_TYPES[key])
        phase_state[key] = current_signal
        cache[symbol] = signal if current_signal else None

    phase_type = None
    if path_changes:
        phase_type = path_changes[0]
    elif is_blowoff and not market_phase_states[symbol]['blowoff']:
        phase_type = '冲顶'
        market_phase_states[symbol]['blowoff'] = True
    elif is_exhaustion and not market_phase_states[symbol]['exhaustion']:
        phase_type = '圆弧顶'
        market_phase_states[symbol]['exhaustion'] = True
    elif is_breakdown and not market_phase_states[symbol]['breakdown']:
        phase_type = '破位'
        market_phase_states[symbol]['breakdown'] = True
    elif is_panic_sell and not market_phase_states[symbol]['panic_sell']:
        phase_type = '加速下跌'
        market_phase_states[symbol]['panic_sell'] = True
    elif is_capitulation and not market_phase_states[symbol]['capitulation']:
        phase_type = '砸盘探底'
        market_phase_states[symbol]['capitulation'] = True
    elif is_bottom_reversal and not market_phase_states[symbol]['bottom_reversal']:
        phase_type = '底部反转'
        market_phase_states[symbol]['bottom_reversal'] = True
    
    # --- 状态重置逻辑 (Fix #2) ---
    # 当信号消失时重置状态，允许下次再次触发
    # 配合 MARKET_PHASE_ALERT_COOLDOWN_HOURS (默认6h) 防止短时间内重复推送
    if phase_type is None:
        if not is_blowoff: market_phase_states[symbol]['blowoff'] = False
        if not is_exhaustion: market_phase_states[symbol]['exhaustion'] = False
        if not is_breakdown: market_phase_states[symbol]['breakdown'] = False
        if not is_panic_sell: market_phase_states[symbol]['panic_sell'] = False
        if not is_capitulation: market_phase_states[symbol]['capitulation'] = False
        if not is_bottom_reversal: market_phase_states[symbol]['bottom_reversal'] = False
    
    # 如果有新的市场阶段变化，发送告警
    # 注意：冷启动时（首次检测该 symbol）不发送告警，只初始化状态
    # 定时报告路径 send_alert=False，避免整点只推修复信号、漏发趋势报告
    if phase_type and not is_cold_start and send_alert:
        last_alerts = phase_state['last_alerts']
        now_local = datetime.datetime.now()
        cooldown_hours = getattr(cfg, 'MARKET_PHASE_ALERT_COOLDOWN_HOURS', 6)
        eligible_types = []
        for alert_type in path_changes or [phase_type]:
            last_alert_time = last_alerts.get(alert_type)
            if isinstance(last_alert_time, str):
                try:
                    last_alert_time = datetime.datetime.fromisoformat(last_alert_time)
                except ValueError:
                    last_alert_time = None
            if last_alert_time:
                time_since_last = now_local - last_alert_time
                if time_since_last < datetime.timedelta(hours=cooldown_hours):
                    logger.info(f"{symbol} {alert_type} 信号在冷却期内 (还需 {cooldown_hours - time_since_last.total_seconds()/3600:.1f} 小时)，跳过告警")
                    continue
            eligible_types.append(alert_type)
        if not eligible_types:
            return {'phase_changed': False, 'phase_type': None}
        phase_type = eligible_types[0]

        # 生成告警消息
        timestamp = _report_now().strftime('%Y-%m-%d %H:%M:%S')
        close_line = f'{_format_candle_close(current_row)}\n'

        if len(eligible_types) > 1:
            alert_text = f"{symbol} 日线与周线信号 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += '\n'.join(
                _get_phase_summary_for_key(symbol, key) for key in ('bull_signal', 'bear_signal')
            )
        elif phase_type == '冲顶':
            alert_text = f"**{symbol}** 冲顶信号 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"RSI: {current_row['RSI']:.2f}\n"
            alert_text += "特征: 上影线较长，RSI 超买，MACD 柱较前一根下降\n"
            alert_text += f"建议行动: 准备落袋，发出剧烈波动告警"
        elif phase_type == '圆弧顶':
            # 计算价格偏离20EMA的百分比
            ema_deviation_pct = ((current_row['close'] - current_row['EMA20']) / current_row['EMA20']) * 100
            
            alert_text = f"**{symbol}** 圆弧顶信号 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"RSI: {current_row['RSI']:.2f}, 偏离20EMA: {ema_deviation_pct:.1f}%\n"
            alert_text += "特征: 最高价达到观察窗口高点，收盘偏离 EMA20 和 RSI 均达到设定阈值\n"
            alert_text += f"建议行动: 准备减仓，评估短期回调风险"
        elif phase_type == '破位':
            alert_text = f"警告信号！{symbol} 出现破位信号 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"MACD: {current_row[current_row['macd_hist_col']]:.4f}\n"
            alert_text += "特征: 收盘低于 EMA20，EMA20 较前一根下降，且 ADX 低于 25 或 MACD 柱为负\n"
            alert_text += f"建议行动: 考虑出场或减仓"
        elif phase_type == '加速下跌':
            alert_text = f"市场警示！{symbol} 进入加速下跌阶段 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"ADX: {current_row[current_row['adx_col']]:.2f}\n"
            alert_text += "特征: 收盘低于 EMA20，EMA20 与 EMA50 的向下差距扩大，ADX 增强且收盘价较前一根下降\n"
            alert_text += f"建议行动: 继续观望，等待探底信号"
        elif phase_type == '砸盘探底':
            alert_text = f"关键信号！{symbol} 出现砸盘探底迹象 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"RSI: {current_row['RSI']:.2f}\n"
            alert_text += "特征: 长下影或 RSI 超卖，伴随 RSI 底背离或 MACD 柱改善，成交量高于前五根均量的 1.5 倍\n"
            alert_text += f"建议行动: 关注可能的反转信号，准备小仓位试探"
        elif phase_type == '底部反转':
            heading = '日线反弹修复' if weekly_bearish else '日线反弹信号'
            alert_text = f"{symbol} {heading} \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"MACD: {current_row[current_row['macd_hist_col']]:.4f}\n"
            alert_text += (
                f"特征: 连续 {REVERSAL_CONFIRM_DAYS} 天收盘高于 EMA20，"
                "EMA50 不低于这段期间的首日值，MACD 柱为正"
            )
            if weekly_bearish is None:
                alert_text += f'\n{_WEEKLY_DATA_UNAVAILABLE}'
            elif weekly_bearish:
                alert_text += f"\n{_BEAR_SIGNAL_MESSAGES['bear_reinforcement']}"
        elif phase_type == '熊市信号':
            # 熊市信号类型
            alert_level = bear_market_signals[symbol]['level']
            
            # 根据不同的熊市信号类型设置不同的警报前缀
            if alert_level == 'yellow':
                prefix = "⚠️ 黄色预警"
            elif alert_level == 'red':
                prefix = "🔴 红色预警"
            elif alert_level in ('dark_red', 'black'):
                prefix = "📉 周线偏空"
            else:
                prefix = "🔔 熊市信号"
                
            alert_text = f"{prefix}！{symbol} 熊市路径监控 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"{bear_market_signals[symbol]['message']}"
        elif phase_type == '牛市信号':
            # 牛市信号类型
            alert_level = bull_market_signals[symbol]['level']
            
            # 根据不同的牛市信号类型设置不同的警报前缀
            if alert_level == 'green':
                prefix = "🌱 绿色曙光"
            elif alert_level == 'blue':
                prefix = "🔵 蓝色信号"
            elif alert_level == 'golden':
                prefix = "📈 周线偏多"
            else:
                prefix = "📈 牛市信号"
                
            alert_text = f"{prefix}！{symbol} 牛市路径监控 \n>{timestamp}\n\n"
            alert_text += close_line
            alert_text += f"{bull_market_signals[symbol]['message']}"
        
        # 打印到控制台
        print("\n" + "#" * 50)
        print(alert_text)
        print("#" * 50)
        
        # 发送到企业微信
        if _in_report_window(_report_now()):
            logger.info("%s 阶段告警进入定时报告窗口，交给定时报处理", symbol)
        elif send_wechat_notification(alert_text):
            for alert_type in eligible_types:
                _record_phase_delivery(phase_state, alert_type, now_local)
            logger.info("发送 %s %s 阶段告警", symbol, '、'.join(eligible_types))
            previous_phase_state = None
        else:
            logger.warning("%s %s 阶段告警发送失败，将在后续轮询重试", symbol, '、'.join(eligible_types))
        if previous_phase_state is not None:
            phase_state.clear()
            phase_state.update(previous_phase_state)
            bull_market_signals[symbol] = previous_bull_message
            bear_market_signals[symbol] = previous_bear_message
    
    # 返回市场阶段检测结果
    return {
        'blowoff': is_blowoff,
        'exhaustion': is_exhaustion,
        'breakdown': is_breakdown,
        'panic_sell': is_panic_sell,
        'capitulation': is_capitulation,
        'bottom_reversal': is_bottom_reversal,
        'bear_signal': bear_signal['signal'],
        'bull_signal': bull_signal['signal'],
        'weekly_bearish': weekly_bearish,
    }

def send_first_report():
    """发送第一次定时报告（上午8点）"""
    logger.info("发送上午8点定时报告")
    return run_once(is_scheduled_report=True, report_type='上午8点')

def send_second_report():
    """发送第二次定时报告（下午4点）"""
    logger.info("发送下午4点定时报告")
    return run_once(is_scheduled_report=True, report_type='下午4点')

def send_third_report():
    """发送第三次定时报告（凌晨0点）"""
    logger.info("发送凌晨0点定时报告")
    return run_once(is_scheduled_report=True, report_type='凌晨0点')


_REPORT_DELIVERIES_KEY = '_report_deliveries'
_report_delivery_pending_write = False


def _run_due_report(now_tz, report_hours, grace_minutes):
    """执行当前宽限窗口内尚未成功发送的定时报告。"""
    global _report_delivery_pending_write
    if _report_delivery_pending_write:
        _report_delivery_pending_write = not save_market_phase_states(market_phase_states)

    cur_hour = now_tz.hour
    cur_min = now_tz.minute
    cur_date = now_tz.date().isoformat()
    if cur_hour not in report_hours:
        return False
    if market_phase_states.get(_REPORT_DELIVERIES_KEY, {}).get(str(cur_hour)) == cur_date:
        return False
    if cur_min < REPORT_MINUTE or cur_min > REPORT_MINUTE + grace_minutes:
        return False

    label, report_fn = report_hours[cur_hour]
    logger.info(
        f"定时报告触发: {label} "
        f"({now_tz.tzinfo} {cur_hour:02d}:{cur_min:02d})"
    )
    if not report_fn():
        logger.error(f"定时报告发送失败，将在宽限窗口内重试: {label}")
        return False

    market_phase_states.setdefault(_REPORT_DELIVERIES_KEY, {})[str(cur_hour)] = cur_date
    _report_delivery_pending_write = not save_market_phase_states(market_phase_states)
    if _report_delivery_pending_write:
        logger.warning("定时报告已送达，送达记录将在下次调度时补写")
    return True

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
        logger.info(
            "定时报告时间: %02d:%02d, %02d:%02d, %02d:%02d (每8小时)",
            FIRST_REPORT_HOUR, REPORT_MINUTE,
            SECOND_REPORT_HOUR, REPORT_MINUTE,
            THIRD_REPORT_HOUR, REPORT_MINUTE,
        )
        logger.info(f"趋势变化告警: {'已启用' if ENABLE_TREND_CHANGE_ALERT else '未启用'}")
        
        # 启动时立即执行一次
        run_once()
        
        # DST 安全的定时报告：运行时按配置时区判断。
        # 必须用宽限窗口，不能卡死在整分 —— 15 分钟监控任务一旦拖过 :00，
        # 严格 minute==0 会漏发趋势报告，只留下监控路径里的修复/形态信号。
        _report_hours = {
            FIRST_REPORT_HOUR:  ('上午8点',  send_first_report),
            SECOND_REPORT_HOUR: ('下午4点',  send_second_report),
            THIRD_REPORT_HOUR:  ('凌晨0点',  send_third_report),
        }
        REPORT_GRACE_MINUTES = max(10, int(MONITOR_INTERVAL))

        def _check_report_schedule():
            """每分钟调用：目标小时内、今天还没发过，就补发报告。"""
            return _run_due_report(
                _report_now(),
                _report_hours,
                REPORT_GRACE_MINUTES,
            )

        # 先注册报告检查，run_pending 时优先于监控任务
        schedule.every(1).minutes.do(_check_report_schedule)
        schedule.every(MONITOR_INTERVAL).minutes.do(run_once)
        logger.info(
            f"定时报告: 时区={REPORT_TIMEZONE}, "
            f"报告时间={[f'{h:02d}:{REPORT_MINUTE:02d}' for h in _report_hours]}, "
            f"宽限={REPORT_GRACE_MINUTES}分钟"
        )
        _check_report_schedule()  # 启动时若已在窗口内立即补发，不等下一分钟
        
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
