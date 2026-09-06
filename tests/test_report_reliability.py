import datetime
import copy
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import trend_detector as td


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
        response = Mock()
        response.raise_for_status.side_effect = td.requests.HTTPError("503")
        post.return_value = response

        self.assertIs(td.send_wechat_notification("report"), False)

    @patch.object(td.requests, "post", side_effect=RuntimeError("network down"))
    @patch.object(td, "WECHAT_WEBHOOK", "https://example.invalid/webhook")
    def test_failed_webhook_is_reported_as_failure(self, post):
        self.assertIs(td.send_wechat_notification("report"), False)
        post.assert_called_once()


class ReportScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(
            2026, 9, 6, 8, 5, tzinfo=ZoneInfo("Asia/Shanghai")
        )

    def test_failed_report_remains_retryable(self):
        attempts = iter([False, True])
        fired = {}
        reports = {8: ("上午8点", lambda: next(attempts))}

        self.assertFalse(td._run_due_report(self.now, reports, fired, 15))
        self.assertNotIn(8, fired)
        self.assertTrue(td._run_due_report(self.now, reports, fired, 15))
        self.assertEqual(datetime.date(2026, 9, 6), fired[8])

    def test_successful_report_is_not_repeated(self):
        calls = []
        fired = {}
        reports = {8: ("上午8点", lambda: calls.append("sent") or True)}

        self.assertTrue(td._run_due_report(self.now, reports, fired, 15))
        self.assertFalse(td._run_due_report(self.now, reports, fired, 15))
        self.assertEqual(["sent"], calls)


class ScheduledReportStateTests(unittest.TestCase):
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

    @staticmethod
    def _frame():
        now = datetime.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        index = pd.date_range(end=now, periods=4, freq="D")
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
    ):
        frame = self._frame()
        symbols = symbols or ["BTC/USDT"]
        fetch_ohlcv = fetch_ohlcv or (lambda *_args, **_kwargs: frame.copy())

        if mark_phase is None:
            def mark_phase(symbol, *_args, **_kwargs):
                td.market_phase_states[symbol]["bottom_reversal"] = True
                td.bull_market_signals[symbol] = {"signal": "bull_test"}
                td.bear_market_signals[symbol] = {"signal": "bear_test"}

        with patch.object(td, "SYMBOLS", symbols), patch.object(
            td, "EMA_PERIOD", 1
        ), patch.object(td, "FAST_EMA_PERIOD", 1), patch.object(
            td, "init_exchange", return_value=object()
        ), patch.object(td, "fetch_ohlcv", side_effect=fetch_ohlcv), patch.object(
            td, "_calculate_indicators", side_effect=lambda data, _timeframe: data
        ), patch.object(td, "determine_trend", return_value="盘整"), patch.object(
            td, "check_market_phases", side_effect=mark_phase
        ), patch.object(td, "fetch_realtime_price", return_value=101.5), patch.object(
            td, "send_wechat_notification", return_value=send_result
        ) as send_notification, patch.object(
            td, "save_market_phase_states"
        ), patch("builtins.print"):
            result = td.run_once(is_scheduled_report=True, report_type="上午8点")
            return result, send_notification

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
        frame = self._frame()

        def fetch_ohlcv(_exchange, symbol, timeframe=None):
            if symbol == "ETH/USDT" and timeframe is None:
                return None
            return frame.copy()

        result, send_notification = self._run_scheduled_analysis(
            True,
            symbols=["BTC/USDT", "ETH/USDT"],
            fetch_ohlcv=fetch_ohlcv,
        )

        self.assertIs(result, False)
        send_notification.assert_not_called()

    def test_missing_fast_timeframe_cancels_scheduled_report(self):
        frame = self._frame()

        def fetch_ohlcv(_exchange, _symbol, timeframe=None):
            if timeframe == td.FAST_TIMEFRAME:
                return None
            return frame.copy()

        result, send_notification = self._run_scheduled_analysis(
            True,
            fetch_ohlcv=fetch_ohlcv,
        )

        self.assertIs(result, False)
        send_notification.assert_not_called()


if __name__ == "__main__":
    unittest.main()
