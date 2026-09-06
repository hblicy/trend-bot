# -*- coding: utf-8 -*-
"""
趋势判断机器人配置文件
"""
import os
from dotenv import load_dotenv

load_dotenv()  # 加载 .env 文件中的环境变量

# ========= 交易所设置 =========
EXCHANGE = 'binance'  # 使用的交易所

# ========= 监控设置 =========
SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']  # 监控的交易对
TIMEFRAME = '1d'  # 时间周期，1d表示日线
LOOKBACK = 400  # 获取的K线数量

# ========= 技术指标参数 =========
# 主周期指标参数
EMA_PERIOD = 100  # EMA均线周期（原来是200-SMA）
MACD_FAST = 8   # MACD快线参数（原来是12）
MACD_SLOW = 17  # MACD慢线参数（原来是26）
MACD_SIGNAL = 6  # MACD信号线参数（原来是9）
ADX_PERIOD = 14  # ADX计算周期

# 快周期二次确认参数
FAST_TIMEFRAME = '4h'  # 快周期时间单位
FAST_EMA_PERIOD = 50  # 快周期 EMA均线周期
FAST_MACD_FAST = 6   # 快周期 MACD快线参数
FAST_MACD_SLOW = 13  # 快周期 MACD慢线参数
FAST_MACD_SIGNAL = 4  # 快周期 MACD信号线参数
FAST_ADX_PERIOD = 10  # 快周期 ADX计算周期

# 市场阶段检测参数
EMA20_PERIOD = 20  # 20日EMA周期
EMA50_PERIOD = 50  # 50日EMA周期
RSI_PERIOD = 14     # RSI周期

# 市场阶段阈值
ADX_STRONG_TREND = 35  # 强势趋势 ADX 阈值
RSI_OVERBOUGHT = 80    # RSI 超买阈值
RSI_EXHAUSTION = 70    # 圆弧顶RSI阈值（比超买低一些）
RSI_OVERSOLD = 20      # RSI 超卖阈值
UPPER_SHADOW_FACTOR = 2.0  # 上影线长度因子（相对于实体）
LOWER_SHADOW_FACTOR = 2.0  # 下影线长度因子（相对于实体）
PRICE_VOLATILITY = 0.02   # 价格波动率阈值（影线长度相对于收盘价）
REVERSAL_CONFIRM_DAYS = 2  # 底部反转确认天数（连续站上20EMA）
EMA_DEVIATION = 0.15   # 价格偏离20EMA的百分比阈值
DROP_THRESHOLD = 0.10  # 顶部回落幅度阈值
HIGH_LOOKBACK = 60     # 创新高的回溯天数
DROP_LOOKFORWARD = 3   # 回落观察的未来天数

# 布林带参数
BB_LENGTH = 20         # 布林带周期
BB_STD = 2.0           # 布林带标准差倍数

# 是否启用市场阶段检测
ENABLE_MARKET_PHASE_DETECTION = True
# 是否启用底部检测
ENABLE_BOTTOM_DETECTION = True

# ========= 监控设置 =========
# 监控间隔（分钟）
MONITOR_INTERVAL = 15  # 实时监控间隔，建议15-30分钟

# 定时报告时间（24小时制，每8小时一次）
FIRST_REPORT_HOUR = 8    # 第一次报告时间：上午8点
SECOND_REPORT_HOUR = 16  # 第二次报告时间：下午4点
THIRD_REPORT_HOUR = 0    # 第三次报告时间：凌晨0点
REPORT_MINUTE = 0
# 报告时间所在的时区；运行时每分钟按此时区判断，不依赖机器本地时区
REPORT_TIMEZONE = 'Asia/Shanghai'

# 是否启用趋势变化告警
ENABLE_TREND_CHANGE_ALERT = True

# 市场阶段告警冷却时间（小时），同一信号在此时间内不会重复推送
MARKET_PHASE_ALERT_COOLDOWN_HOURS = 6

# ========= 通知设置 =========
# 企业微信机器人Webhook地址，替换为你的实际地址
WECHAT_WEBHOOK = os.environ.get('TREND_BOT_WEBHOOK', '')

# ========= 趋势判断规则 =========
# 市场阶段检测策略总结：
# "10万以上急跌"属于 加速顶 → 竭尽 → 破位 典型结构；
# 把「超买 + 背离 + 巨量长上影」设为冲顶告警，
# 把「跌破 20EMA/50EMA + ADX 失效 + MACD 翻负」设为破位确认，
# 就能在未来出现类似 12-17、或 1-20 的行情时，第一时间收到提示并提前做好风控。

# 进场规则（严格）
# ADX 动态阈值：波动率 <2% 为 20，<4% 为 25，否则为 30
# 上升趋势：价格 > 100-EMA 且 MACD柱 > 0 且 ADX 达标 且 +DI > -DI
# 下降趋势：价格 < 100-EMA 且 MACD柱 < 0 且 ADX 达标 且 +DI < -DI

# 出场规则
# 主周期价格反向穿越 100-EMA、ADX < 20，或 MACD柱与 DI 同时反转时降级为盘整

# 快周期二次确认
# 如果4H级别出现反转信号，提前降级为盘整
