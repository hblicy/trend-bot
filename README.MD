# Trend Detection Bot v1.2

基于 Python 和 CCXT 的多币种趋势与市场阶段检测机器人。

该项目旨在通过量化指标自动识别加密货币市场的趋势方向（上升/下跌/盘整）以及关键的市场阶段（如冲顶、筑底、破位等），并通过企业微信实时推送告警。

## 核心功能

### 1. 趋势监控
系统默认监控 **BTC, ETH, SOL, BNB**（可配置）的趋势状态：
- **判断逻辑**：基于 100-EMA、MACD、ADX 及 DMI 指标的组合判定。
- **快慢周期结合**：引入 4H 周期作为"快周期"进行二次确认，有效过滤假信号。主周期和快周期均有未收盘 K 线保护，避免未完成数据污染信号。
- **状态分类**：
  - 🟢 **上升 (Uptrend)**：价格强力站上均线，动能向上。
  - 🔴 **下跌 (Downtrend)**：价格跌破均线，动能向下。
  - ⚪ **盘整 (Sideways)**：趋势不明确或动能不足。

### 2. 市场阶段识别 (Advanced)
除基础趋势外，系统内置了对特定市场微结构的识别算法：
- **顶部信号**：冲顶 (Blow-off Top)、圆弧顶 (Exhaustion)、破位 (Breakdown)。
- **底部信号**：恐慌抛售 (Panic Sell)、探底 (Capitulation)、底部反转 (Bottom Reversal)。
- **识别因子**：结合 RSI 超买超卖、K 线影线长度、波动率及乖离率等多维因子。
- **告警策略**：同类信号消失后可再次触发，并受默认 6 小时冷却限制。定时报告内首次出现的阶段信号随报告一并送达，不再单独重复推送。

### 3. 智能告警
- **定时报告**：每日 08:00, 16:00, 00:00（Asia/Shanghai）推送全市场趋势报告。整点后有宽限窗口；分析或 Webhook 发送失败时会在窗口内继续重试，只有成功送达才记为本时段已完成。
- **实时告警**：当检测到趋势发生实质性反转（需满足确认次数）时，立即触发 Webhook 告警。
- **趋势状态持久化**：趋势状态自动保存到 `trend_states.json`，重启后不丢失基线。
- **通知渠道**：默认支持企业微信 (WeChat Work)。

### 4. 回测系统
内置 `backtest.py` 回测脚本，可对过去 365 天的历史数据进行验证：
- 评估趋势信号的准确率（是否捕捉到 >2% 的波幅）。
- 评估市场阶段信号的有效性（是否在窗口期内达到 >10% 的涨跌幅）。
- 回测已消除前视偏差（每个检测点只使用历史数据）。

---

## 安装与使用

### 环境要求
- Python 3.9+

### 安装步骤
1. 克隆项目或下载源码。
2. 创建虚拟环境并安装依赖：
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
   > **注意**：技术指标（EMA/RSI/MACD/ADX/BBands）已使用纯 pandas + numpy 实现，
   > 无需额外安装 `pandas-ta`，兼容 Python 3.9 及以上版本。

### 配置

复制 `.env` 文件并填入你的 Webhook 地址：

```bash
cp .env.example .env
# 编辑 .env，设置 TREND_BOT_WEBHOOK
```

`.env` 文件内容：
```
TREND_BOT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key
```

> ⚠️ `.env` 文件已在 `.gitignore` 中排除，不会被提交到版本控制。

### 运行机器人
```bash
python trend_detector.py
```
程序启动后会立即执行一次分析，随后进入后台循环，按计划任务和监控间隔运行。

### 运行回测
```bash
python backtest.py
```
脚本将输出每个币种的趋势判断准确率及关键行情节点的统计数据。

---

## 配置说明 (`config.py`)

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `EXCHANGE` | 交易所 (CCXT ID) | `binance` |
| `SYMBOLS` | 监控币种列表 | `['BTC/USDT', ...]` |
| `TIMEFRAME` | 主分析周期 | `1d` |
| `FAST_TIMEFRAME` | 快周期（二次确认） | `4h` |
| `MONITOR_INTERVAL` | 实时轮询间隔 (分钟) | `15` |
| `ENABLE_MARKET_PHASE_DETECTION` | 是否开启阶段检测 | `True` |

### 环境变量

| 变量 | 说明 | 必填 |
|------|------|------|
| `TREND_BOT_WEBHOOK` | 企业微信 Webhook 地址 | ✅ |

通过 `.env` 文件或系统环境变量设置。

---

## 策略逻辑详解

### 1. 进场条件 (Entry)
- **多头 (Long)**：
  - Price > EMA (100)
  - MACD Hist > 0
  - ADX 高于动态阈值：波动率 <2% 时为 20，<4% 时为 25，否则为 30
  - +DI > -DI
- **空头 (Short)**：
  - Price < EMA (100)
  - MACD Hist < 0
  - ADX 高于动态阈值：波动率 <2% 时为 20，<4% 时为 25，否则为 30
  - +DI < -DI

### 2. 出场/降级 (Exit)
- 主周期价格反向穿越 100-EMA、ADX 低于 20，或 MACD 柱反向且 DI 交叉时，趋势降级为“盘整”。
- 4H 快周期出现 MACD 柱反向且 DI 交叉时，会提前降级为“盘整”。

### 3. 特殊阶段
- **冲顶 (Blow-off)**：RSI > 80 + 长上影线 + MACD 柱走弱。
- **破位 (Breakdown)**：跌破 20EMA 且均线拐头 + (ADX 衰减 或 MACD 翻负)。
- **反弹修复**：底部反转条件成立，但周线 MA50 仍向下时，推送「修复信号」而非「机会信号」。

---

## 目录结构

```
trend-bot_v1.2/
├── trend_detector.py     # 主程序，核心循环与状态管理
├── config.py             # 配置文件（从 .env 加载敏感信息）
├── indicators.py         # 技术指标计算库
├── signals.py            # 市场微结构信号检测
├── backtest.py           # 历史数据回测工具
├── .env                  # 环境变量（Webhook 等，不入版本控制）
├── .gitignore            # 排除 .env、日志、缓存
├── trend_states.json     # 趋势状态持久化
├── market_phase_states.json  # 市场阶段状态持久化
├── requirements.txt      # Python 依赖
├── start.sh / stop.sh / restart.sh / status.sh  # 运维脚本
└── README.MD             # 本文档
```
