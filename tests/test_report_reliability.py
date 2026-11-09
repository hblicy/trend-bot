import datetime
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import trend_detector as td


def _btc_signal_frame():
    index = pd.date_range(end="2026-11-09", periods=401, freq="D")
    close = pd.Series([79500.0] * 150 + [74200.0] * 251, index=index)
    close.loc[["2026-11-01", "2026-11-07", "2026-11-08", "2026-11-09"]] = [
        76700.0, 75867.59, 74851.44, 74829.40,
    ]
    return pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close, "volume": 100.0,
        "EMA": close, "EMA20": close, "EMA50": close, "RSI": 50.0, "MACDh": 0.0,
    })


class WeeklyCloseTests(unittest.TestCase):
    def test_sunday_close_is_available_at_monday_utc_midnight(self):
        frame = _btc_signal_frame()
        sunday_is_latest = frame.iloc[:-1]
        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock:
            clock.utcnow.return_value = datetime.datetime(2026, 11, 8, 23, 59, 59)
            self.assertEqual(
                "bull_final_verdict",
                td.detect_bull_market_signals(sunday_is_latest.copy())["signal"],
            )
            self.assertFalse(td.is_weekly_ma50_bearish(sunday_is_latest))

            clock.utcnow.return_value = datetime.datetime(2026, 11, 9)
            for daily_bars in (sunday_is_latest, frame):
                with self.subTest(last_open=daily_bars.index[-1]):
                    self.assertEqual(
                        "bear_reinforcement",
                        td.detect_bear_market_signals(daily_bars.copy())["signal"],
                    )
                    self.assertTrue(td.is_weekly_ma50_bearish(daily_bars))

    def test_missing_sunday_cannot_turn_saturday_into_a_weekly_close(self):
        frame = _btc_signal_frame().drop(pd.Timestamp("2026-11-08"))
        frame.loc["2026-11-07", "close"] = 74000.0
        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock:
            clock.utcnow.return_value = datetime.datetime(2026, 11, 9, 0, 1)
            self.assertFalse(td.is_weekly_ma50_bearish(frame))

    def test_missing_latest_sunday_does_not_reuse_the_previous_week_signal(self):
        frame = _btc_signal_frame().drop(pd.Timestamp("2026-11-08"))
        frame.loc["2026-11-01", "close"] = 74200.0
        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock:
            clock.utcnow.return_value = datetime.datetime(2026, 11, 10, 0, 1)
            self.assertIsNone(td.detect_bear_market_signals(frame)["signal"])

    def test_daily_signal_requiring_ma365_waits_for_completed_history(self):
        frame = _btc_signal_frame().tail(365).copy()
        price_columns = ["open", "high", "low", "close"]
        frame.loc["2026-11-07", price_columns] = 74200.0
        frame["macd_hist_col"] = "MACDh"
        inverse = frame.copy()
        inverse[price_columns] = 150000.0 - inverse[price_columns]
        as_of = datetime.datetime(2026, 11, 9, 0, 1)
        for detect, candles, expected in (
            (td.detect_bull_market_signals, frame, "bull_early_sign"),
            (td.detect_bear_market_signals, inverse, "bear_warning"),
        ):
            with self.subTest(expected=expected):
                result = detect(candles, as_of_utc=as_of)
                self.assertTrue(pd.isna(candles.iloc[-2]["MA365"]))
                self.assertEqual(expected, result["signal"])


class NotificationTests(unittest.TestCase):
    @patch.object(td.requests, "post")
    @patch.object(td, "WECHAT_WEBHOOK", "")
    def test_empty_webhook_is_reported_as_failure(self, post):
        self.assertIs(td.send_wechat_notification("report"), False)
        post.assert_not_called()

    @patch.object(td.requests, "post")
    @patch.object(td, "WECHAT_WEBHOOK", "https://example.invalid/webhook")
    def test_successful_webhook_is_reported_as_success(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        post.return_value = response

        self.assertIs(td.send_wechat_notification("report"), True)
        post.assert_called_once_with(
            "https://example.invalid/webhook",
            json={"msgtype": "text", "text": {"content": "report"}},
            timeout=10,
        )

    @patch.object(td.requests, "post")
    @patch.object(td, "WECHAT_WEBHOOK", "https://example.invalid/webhook")
    def test_webhook_business_error_is_reported_as_failure(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"errcode": 93000, "errmsg": "invalid webhook"}
        post.return_value = response

        self.assertIs(td.send_wechat_notification("report"), False)

    @patch.object(td.requests, "post")
    @patch.object(td, "WECHAT_WEBHOOK", "https://example.invalid/webhook")
    def test_webhook_http_error_is_reported_as_failure(self, post):
        marker = "synthetic-query-value"
        response = td.requests.Response()
        response.status_code = 503
        response.url = "https://example.invalid/webhook?key=" + marker
        post.return_value = response

        with self.assertLogs(td.logger, "ERROR") as logs:
            self.assertIs(td.send_wechat_notification("report"), False)
        self.assertFalse(any(marker in entry for entry in logs.output), "HTTP 错误日志包含合成凭据")
        self.assertIn("HTTP=503", "\n".join(logs.output))

    @patch.object(td.requests, "post", side_effect=td.requests.ConnectionError("network down"))
    @patch.object(td, "WECHAT_WEBHOOK", "https://example.invalid/webhook")
    def test_failed_webhook_is_reported_as_failure(self, post):
        self.assertIs(td.send_wechat_notification("report"), False)
        post.assert_called_once()

    def test_network_exception_logging_omits_webhook_url(self):
        marker = "synthetic-query-value"
        url = "https://example.invalid/webhook?key=" + marker
        with patch.object(td, "WECHAT_WEBHOOK", url), patch.object(
            td.requests, "post", side_effect=td.requests.ConnectionError(f"request failed for {url}")
        ), self.assertLogs(td.logger, "ERROR") as logs:
            self.assertIs(td.send_wechat_notification("report"), False)
        self.assertFalse(any(marker in entry for entry in logs.output), "日志包含合成的 URL 参数")
        self.assertIn("ConnectionError", "\n".join(logs.output))

    def test_business_error_logging_omits_remote_error_text(self):
        marker = "synthetic-query-value"
        response = Mock()
        response.json.return_value = {"errcode": 93000, "errmsg": f"invalid key {marker}"}
        with patch.object(td, "WECHAT_WEBHOOK", "https://example.invalid/webhook"), patch.object(
            td.requests, "post", return_value=response
        ), self.assertLogs(td.logger, "ERROR") as logs:
            self.assertIs(td.send_wechat_notification("report"), False)
        self.assertFalse(any(marker in entry for entry in logs.output), "日志包含接口回显的合成凭据")
        self.assertIn("93000", "\n".join(logs.output))


class ReportScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(
            2026, 9, 6, 8, 5, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        state = patch.multiple(td, market_phase_states={}, _report_delivery_pending_write=False)
        state.start()
        self.addCleanup(state.stop)

    @patch.object(td, "save_market_phase_states")
    def test_failed_report_remains_retryable(self, save):
        attempts = iter([False, True])
        reports = {8: ("上午8点", lambda: next(attempts))}

        self.assertFalse(td._run_due_report(self.now, reports, 15))
        save.assert_not_called()
        self.assertTrue(td._run_due_report(self.now, reports, 15))
        save.assert_called_once()

    @patch.object(td, "save_market_phase_states")
    def test_successful_report_is_not_repeated(self, _save):
        calls = []
        reports = {8: ("上午8点", lambda: calls.append("sent") or True)}

        self.assertTrue(td._run_due_report(self.now, reports, 15))
        self.assertFalse(td._run_due_report(self.now, reports, 15))
        self.assertEqual(["sent"], calls)

    @patch.object(td, "save_market_phase_states")
    def test_each_report_starts_at_minute_one(self, _save):
        for hour in (0, 8, 16):
            with self.subTest(hour=hour):
                send_report = Mock(return_value=True)
                reports = {hour: ("定时报告", send_report)}
                at_close = self.now.replace(hour=hour, minute=0)

                self.assertFalse(td._run_due_report(at_close, reports, 15))
                send_report.assert_not_called()
                self.assertTrue(td._run_due_report(at_close.replace(minute=1), reports, 15))
                send_report.assert_called_once()

    def test_monitor_yields_to_delayed_report_from_minute_zero(self):
        with patch.object(td, "REPORT_MINUTE", 1), patch.object(
            td.datetime, "datetime", wraps=datetime.datetime
        ) as clock, patch.object(td, "init_exchange", return_value=None) as init_exchange:
            clock.now.return_value = self.now.replace(minute=0)
            self.assertTrue(td.run_once())
        init_exchange.assert_not_called()

    def test_restart_keeps_successful_report_delivery(self):
        reports = {8: ("上午8点", Mock(return_value=True))}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch.object(
            td, "MARKET_PHASE_STATE_FILE", str(Path(directory) / "market_phase_states.json")
        ), patch.object(td, "market_phase_states", {}):
            self.assertTrue(td._run_due_report(self.now, reports, 15))
            td.market_phase_states = td.load_market_phase_states()
            self.assertFalse(td._run_due_report(self.now, reports, 15))
        reports[8][1].assert_called_once()

    def test_failed_report_receipt_write_retries_without_resending(self):
        report = Mock(return_value=True)
        reports = {8: ("上午8点", report)}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            state_file = Path(directory) / "market_phase_states.json"
            state_file.write_text("{}", encoding="utf-8")
            with patch.object(td, "MARKET_PHASE_STATE_FILE", str(state_file)):
                with patch.object(td.os, "replace", side_effect=PermissionError("simulated state write failure")):
                    self.assertTrue(td._run_due_report(self.now, reports, 15))
                self.assertEqual("{}", state_file.read_text(encoding="utf-8"))
                self.assertFalse(td._run_due_report(self.now.replace(hour=9), reports, 15))
                td.market_phase_states = td.load_market_phase_states()
                self.assertFalse(td._run_due_report(self.now, reports, 15))
        report.assert_called_once()

    def test_monitor_started_before_the_hour_yields_before_sending(self):
        frame = _btc_signal_frame().iloc[:-1]
        fast_frame = ScheduledReportStateTests._frame(td.FAST_TIMEFRAME)
        report_tz = ZoneInfo("Asia/Shanghai")
        before = datetime.datetime(2026, 11, 9, 7, 59, 50, tzinfo=report_tz)
        after = datetime.datetime(2026, 11, 9, 8, 0, 25, tzinfo=report_tz)
        now = before

        def fetch(_exchange, _symbol, timeframe=None):
            nonlocal now
            if timeframe is None:
                return frame.copy()
            now = after
            return fast_frame.copy()

        def local_now(tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

        with patch.object(td, "market_phase_states", {}), patch.object(td, "bull_market_signals", {}), patch.object(
            td, "bear_market_signals", {}
        ), patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock, patch.object(
            td, "SYMBOLS", ["BTC/USDT"]
        ), patch.object(td, "FAST_EMA_PERIOD", 1), patch.object(td, "init_exchange", return_value=object()), patch.object(
            td, "fetch_ohlcv", side_effect=fetch
        ), patch.object(td, "_calculate_indicators", side_effect=lambda bars, _: bars), patch.object(
            td, "determine_trend", return_value="盘整"
        ), patch.object(td, "fetch_realtime_price", return_value=None), patch.object(
            td, "check_trend_changes"
        ) as trend_changes, patch.object(td, "save_market_phase_states"), patch.object(
            td, "send_wechat_notification", return_value=True
        ) as send, patch("builtins.print"):
            clock.now.side_effect = local_now
            clock.utcnow.side_effect = lambda: now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            td.check_market_phases("BTC", frame.iloc[:200], send_alert=False)
            self.assertTrue(td.run_once())
        trend_changes.assert_not_called()
        send.assert_not_called()


class TrendChangeDeliveryTests(unittest.TestCase):
    def test_opposite_candidate_starts_its_own_confirmation_count(self):
        snapshot = {"symbol": "BTC", "price": 100.0, "realtime_price": 101.0}
        now = datetime.datetime(2026, 11, 9, 9)
        with patch.object(td, "previous_trends", {"BTC": "盘整"}), patch.object(
            td, "trend_change_candidates", {}
        ), patch.object(td, "last_trend_alert_times", {"BTC": now - datetime.timedelta(days=1)}), patch.object(
            td.datetime, "datetime", wraps=datetime.datetime
        ) as clock, patch.object(td, "save_trend_states"), patch.object(
            td, "send_wechat_notification", return_value=True
        ) as send, patch("builtins.print"):
            clock.now.return_value = now
            for trend in ("上升", "上升", "下跌"):
                td.check_trend_changes([dict(snapshot, trend=trend)])
            send.assert_not_called()
            for _ in range(2):
                td.check_trend_changes([dict(snapshot, trend="下跌")])
            send.assert_called_once()
            self.assertIn("趋势: 盘整 -> 下跌", send.call_args.args[0])

    def test_failed_webhook_can_retry_confirmed_trend_change(self):
        results = [{"symbol": "BTC", "trend": "上升", "price": 100.0, "realtime_price": 101.0}]
        now = datetime.datetime(2026, 11, 9, 8, 18)
        with patch.object(td, "previous_trends", {"BTC": "盘整"}), patch.object(
            td, "trend_change_candidates", {"BTC": ("上升", td.TREND_CHANGE_CONFIRMATION_COUNT - 1)}
        ), patch.object(td, "last_trend_alert_times", {"BTC": now - datetime.timedelta(days=1)}), patch.object(
            td.datetime, "datetime", wraps=datetime.datetime
        ) as clock, patch.object(td, "save_trend_states") as save, patch.object(
            td, "send_wechat_notification", side_effect=[False, True]
        ) as send, patch("builtins.print"):
            clock.now.return_value = now
            td.check_trend_changes(results)
            self.assertEqual("盘整", td.previous_trends["BTC"])
            save.assert_not_called()
            td.check_trend_changes(results)
            td.check_trend_changes(results)
            self.assertEqual("上升", td.previous_trends["BTC"])
        self.assertEqual(2, send.call_count)
        save.assert_called_once()


class ScheduledReportStateTests(unittest.TestCase):
    report_utc = datetime.datetime(2026, 11, 9, 0, 1)

    def setUp(self):
        self.original_phase_states = copy.deepcopy(td.market_phase_states)
        self.original_bull_signals = copy.deepcopy(td.bull_market_signals)
        self.original_bear_signals = copy.deepcopy(td.bear_market_signals)
        self._replace_mapping(
            td.market_phase_states,
            {"BTC": {"bottom_reversal": False}},
        )
        self._replace_mapping(td.bull_market_signals, {"BTC": None})
        self._replace_mapping(td.bear_market_signals, {"BTC": None})

    def tearDown(self):
        self._replace_mapping(td.market_phase_states, self.original_phase_states)
        self._replace_mapping(td.bull_market_signals, self.original_bull_signals)
        self._replace_mapping(td.bear_market_signals, self.original_bear_signals)

    @staticmethod
    def _replace_mapping(target, values):
        target.clear()
        target.update(copy.deepcopy(values))

    @classmethod
    def _frame(cls, timeframe=None):
        freq = "4h" if timeframe == td.FAST_TIMEFRAME else "D"
        index = pd.date_range(end=pd.Timestamp(cls.report_utc).floor(freq), periods=4, freq=freq)
        return pd.DataFrame(
            {
                "open": [97.0, 98.0, 99.0, 100.0],
                "high": [99.0, 100.0, 101.0, 102.0],
                "low": [96.0, 97.0, 98.0, 99.0],
                "close": [98.0, 99.0, 100.0, 101.0],
                "volume": [10.0, 11.0, 12.0, 13.0],
                "EMA": [97.0, 98.0, 99.0, 100.0],
                "MACDh": [0.1, 0.1, 0.1, 0.1],
                "ADX": [30.0, 30.0, 30.0, 30.0],
                "DMP": [25.0, 25.0, 25.0, 25.0],
                "DMN": [15.0, 15.0, 15.0, 15.0],
            },
            index=index,
        )

    def _run_scheduled_analysis(
        self,
        send_result,
        symbols=None,
        fetch_ohlcv=None,
        mark_phase=None,
        realtime_price=101.5,
    ):
        symbols = symbols or ["BTC/USDT"]
        fetch_ohlcv = fetch_ohlcv or (lambda _exchange, _symbol, timeframe=None: self._frame(timeframe))

        if mark_phase is None:
            def mark_phase(symbol, *_args, **_kwargs):
                td.market_phase_states[symbol]["bottom_reversal"] = True
                td.bull_market_signals[symbol] = {"signal": "bull_test"}
                td.bear_market_signals[symbol] = {"signal": "bear_test"}

        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock, patch.object(
            td, "SYMBOLS", symbols
        ), patch.object(
            td, "EMA_PERIOD", 1
        ), patch.object(td, "FAST_EMA_PERIOD", 1), patch.object(
            td, "init_exchange", return_value=object()
        ), patch.object(td, "fetch_ohlcv", side_effect=fetch_ohlcv), patch.object(
            td, "_calculate_indicators", side_effect=lambda data, _timeframe: data
        ), patch.object(td, "determine_trend", return_value="盘整"), patch.object(
            td, "check_market_phases", side_effect=mark_phase
        ), patch.object(td, "fetch_realtime_price", return_value=realtime_price), patch.object(
            td, "send_wechat_notification", return_value=send_result
        ) as send_notification, patch.object(
            td, "save_market_phase_states"
        ), patch("builtins.print"):
            clock.utcnow.return_value = self.report_utc
            clock.now.side_effect = lambda tz=None: (
                self.report_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tz)
                if tz else self.report_utc
            )
            result = td.run_once(is_scheduled_report=True, report_type="上午8点")
            return result, send_notification

    def test_report_uses_configured_fast_timeframe(self):
        daily = self._frame()
        fast = self._frame(td.FAST_TIMEFRAME)
        fast.index = pd.date_range(end=self.report_utc.replace(minute=0), periods=len(fast), freq="h")
        with patch.object(td, "FAST_TIMEFRAME", "1h"):
            result, send = self._run_scheduled_analysis(
                True, fetch_ohlcv=lambda _ex, _symbol, timeframe=None: fast.copy() if timeframe else daily.copy(),
            )
        self.assertIs(result, True)
        send.assert_called_once()

    def test_report_labels_close_without_ticker_and_uses_report_timezone(self):
        result, send = self._run_scheduled_analysis(True, realtime_price=None)
        self.assertIs(result, True)
        report = send.call_args.args[0]
        self.assertIn(">2026-11-09 08:01:00", report)
        self.assertIn("日线收盘价: $100.00（2026-11-09 08:00 +0800）", report)

    def test_first_report_delivery_counts_signals_already_seen_at_cold_start(self):
        frame = _btc_signal_frame().iloc[:-1]
        td.market_phase_states.clear()
        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock:
            clock.utcnow.return_value = self.report_utc
            td.check_market_phases("BTC", frame, send_alert=False)

        result, send = self._run_scheduled_analysis(
            True,
            fetch_ohlcv=lambda _ex, _symbol, timeframe=None: self._frame(timeframe) if timeframe else frame.copy(),
            mark_phase=td.check_market_phases,
        )
        self.assertIs(result, True)
        send.assert_called_once()
        self.assertEqual({"牛市信号", "熊市信号"}, set(td.market_phase_states["BTC"]["last_alerts"]))

    def test_report_starts_cooldown_for_new_subtype_only_once(self):
        earlier = datetime.datetime(2026, 11, 9, 12)
        delivered = datetime.datetime(2026, 11, 9, 16, 1)
        state = {
            "bear_signal": "bear_reinforcement",
            "last_alerts": {"熊市信号": earlier},
            "last_delivered_signals": {"bear_signal": "bear_final_verdict"},
        }
        td.market_phase_states["BTC"] = state
        report = [{"symbol": "BTC", "_reported_phase_keys": ("bear_signal",)}]
        td._mark_reported_phase_changes(copy.deepcopy(td.market_phase_states), report, delivered)
        self.assertEqual(delivered, state["last_alerts"]["熊市信号"])
        self.assertEqual("bear_reinforcement", state["last_delivered_signals"]["bear_signal"])

        td._mark_reported_phase_changes(copy.deepcopy(td.market_phase_states), report, delivered.replace(hour=23))
        self.assertEqual(delivered, state["last_alerts"]["熊市信号"])

    def test_report_bottom_reversal_uses_weekly_bear_context(self):
        frame = _btc_signal_frame().iloc[:-1]
        td.market_phase_states.clear()
        with patch.object(td, "detect_bottom_reversal", return_value=True):
            result, send = self._run_scheduled_analysis(
                True, symbols=["ETH/USDT"],
                fetch_ohlcv=lambda _ex, _symbol, timeframe=None: self._frame(timeframe) if timeframe else frame.copy(),
                mark_phase=td.check_market_phases,
            )
        self.assertIs(result, True)
        report = send.call_args.args[0]
        self.assertIn("周线收盘低于 MA50", report)
        self.assertNotIn("考虑分批建仓", report)

    def test_failed_scheduled_report_restores_phase_state(self):
        before_phase = copy.deepcopy(td.market_phase_states)
        before_bull = copy.deepcopy(td.bull_market_signals)
        before_bear = copy.deepcopy(td.bear_market_signals)

        result, _send_notification = self._run_scheduled_analysis(False)
        self.assertIs(result, False)
        self.assertEqual(before_phase, td.market_phase_states)
        self.assertEqual(before_bull, td.bull_market_signals)
        self.assertEqual(before_bear, td.bear_market_signals)

    def test_successful_scheduled_report_keeps_phase_state(self):
        result, _send_notification = self._run_scheduled_analysis(True)
        self.assertIs(result, True)
        self.assertTrue(td.market_phase_states["BTC"]["bottom_reversal"])
        self.assertEqual("bull_test", td.bull_market_signals["BTC"]["signal"])
        self.assertEqual("bear_test", td.bear_market_signals["BTC"]["signal"])

    def test_successful_scheduled_report_starts_phase_alert_cooldown(self):
        result, _send_notification = self._run_scheduled_analysis(True)

        self.assertIs(result, True)
        last_alert = td.market_phase_states["BTC"]["last_alerts"]["底部反转"]
        self.assertIsInstance(last_alert, datetime.datetime)

    def test_new_phase_is_reported_ahead_of_existing_summary(self):
        self._replace_mapping(
            td.market_phase_states,
            {
                "BTC": {
                    "bottom_reversal": False,
                    "bull_signal": "bull_confirmed",
                    "last_alerts": {},
                }
            },
        )
        self._replace_mapping(
            td.bull_market_signals,
            {
                "BTC": {
                    "signal": "bull_confirmed",
                    "message": "已有牛市摘要",
                }
            },
        )

        def mark_bottom_reversal(symbol, *_args, **_kwargs):
            td.market_phase_states[symbol]["bottom_reversal"] = True

        result, send_notification = self._run_scheduled_analysis(
            True,
            mark_phase=mark_bottom_reversal,
        )

        self.assertIs(result, True)
        sent_text = send_notification.call_args.args[0]
        self.assertIn("底部反转", sent_text)
        self.assertNotIn("已有牛市摘要", sent_text)
        self.assertIn("底部反转", td.market_phase_states["BTC"]["last_alerts"])
        self.assertNotIn("牛市信号", td.market_phase_states["BTC"]["last_alerts"])

    def test_incomplete_scheduled_report_is_not_sent(self):
        def fetch_ohlcv(_exchange, symbol, timeframe=None):
            if symbol == "ETH/USDT" and timeframe is None:
                return None
            return self._frame(timeframe)

        result, send_notification = self._run_scheduled_analysis(
            True,
            symbols=["BTC/USDT", "ETH/USDT"],
            fetch_ohlcv=fetch_ohlcv,
        )

        self.assertIs(result, False)
        send_notification.assert_not_called()

    def test_missing_fast_timeframe_cancels_scheduled_report(self):
        def fetch_ohlcv(_exchange, _symbol, timeframe=None):
            if timeframe == td.FAST_TIMEFRAME:
                return None
            return self._frame(timeframe)

        result, send_notification = self._run_scheduled_analysis(
            True,
            fetch_ohlcv=fetch_ohlcv,
        )

        self.assertIs(result, False)
        send_notification.assert_not_called()

    def test_scheduled_report_waits_for_each_latest_closed_candle(self):
        for stale_timeframe in (None, td.FAST_TIMEFRAME):
            with self.subTest(stale_timeframe=stale_timeframe):
                def fetch_ohlcv(_exchange, _symbol, timeframe=None):
                    frame = self._frame(timeframe)
                    return frame.iloc[:-2] if timeframe == stale_timeframe else frame

                result, send_notification = self._run_scheduled_analysis(True, fetch_ohlcv=fetch_ohlcv)

                self.assertIs(result, False)
                send_notification.assert_not_called()

    def test_scheduled_report_sends_when_latest_closed_candles_are_available(self):
        def fetch_ohlcv(_exchange, _symbol, timeframe=None):
            return self._frame(timeframe).iloc[:-1]

        result, send_notification = self._run_scheduled_analysis(True, fetch_ohlcv=fetch_ohlcv)

        self.assertIs(result, True)
        send_notification.assert_called_once()

    def test_report_includes_daily_bull_and_weekly_bear_and_marks_both_delivered(self):
        frame = _btc_signal_frame().iloc[:-1]
        td.market_phase_states.clear()

        def fetch_ohlcv(_exchange, _symbol, timeframe=None):
            return self._frame(timeframe) if timeframe else frame.copy()

        failed, _ = self._run_scheduled_analysis(
            False, fetch_ohlcv=fetch_ohlcv, mark_phase=td.check_market_phases,
        )
        self.assertIs(failed, False)
        self.assertEqual({}, td.market_phase_states)

        result, send_notification = self._run_scheduled_analysis(
            True, fetch_ohlcv=fetch_ohlcv, mark_phase=td.check_market_phases,
        )
        self.assertIs(result, True)
        send_notification.assert_called_once()
        report = send_notification.call_args.args[0]
        self.assertIn("日线收盘高于 MA200", report)
        self.assertIn("周线收盘低于 MA50，MA50 较上周下降", report)
        state = td.market_phase_states["BTC"]
        self.assertEqual("bull_early_sign", state["bull_signal"])
        self.assertEqual("bear_reinforcement", state["bear_signal"])
        self.assertEqual({"牛市信号", "熊市信号"}, set(state["last_alerts"]))

        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock, patch.object(
            td, "send_wechat_notification"
        ) as send_alert:
            clock.utcnow.return_value = self.report_utc.replace(minute=18)
            clock.now.return_value = clock.utcnow.return_value
            td.check_market_phases("BTC", frame)
        send_alert.assert_not_called()

    def test_standalone_btc_alert_combines_signals_and_labels_the_close(self):
        frame = _btc_signal_frame().iloc[:-1]
        td.market_phase_states.clear()
        td.check_market_phases("BTC", frame.iloc[:200], send_alert=False)

        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock, patch.object(
            td, "send_wechat_notification", side_effect=[False, True]
        ) as send_alert, patch("builtins.print"):
            clock.utcnow.return_value = self.report_utc.replace(minute=18)
            clock.now.return_value = self.report_utc.replace(hour=8, minute=18)
            td.check_market_phases("BTC", frame)
            self.assertEqual({}, td.market_phase_states["BTC"]["last_alerts"])
            td.check_market_phases("BTC", frame)

        self.assertEqual(2, send_alert.call_count)
        alert = send_alert.call_args.args[0]
        self.assertIn("日线收盘价: $74,851.44", alert)
        self.assertIn("2026-11-09 08:00", alert)
        self.assertIn("日线收盘高于 MA200", alert)
        self.assertIn("周线收盘低于 MA50，MA50 较上周下降", alert)
        self.assertEqual(
            {"bull_signal": "bull_early_sign", "bear_signal": "bear_reinforcement"},
            td.market_phase_states["BTC"]["last_delivered_signals"],
        )

    def test_bear_alert_can_send_during_bull_alert_cooldown(self):
        frame = _btc_signal_frame().iloc[:-1]
        td.market_phase_states.clear()
        td.check_market_phases("BTC", frame.iloc[:200], send_alert=False)
        td.market_phase_states["BTC"]["last_alerts"]["牛市信号"] = self.report_utc.replace(hour=8).isoformat()

        with patch.object(td.datetime, "datetime", wraps=datetime.datetime) as clock, patch.object(
            td, "send_wechat_notification", return_value=True
        ) as send_alert, patch("builtins.print"):
            clock.utcnow.return_value = self.report_utc.replace(minute=18)
            clock.now.return_value = self.report_utc.replace(hour=8, minute=18)
            td.check_market_phases("BTC", frame)

        send_alert.assert_called_once()
        alert = send_alert.call_args.args[0]
        self.assertIn("周线偏空", alert)
        self.assertIn("周线收盘低于 MA50，MA50 较上周下降", alert)
        self.assertIn("日线收盘价: $74,851.44", alert)


if __name__ == "__main__":
    unittest.main()
