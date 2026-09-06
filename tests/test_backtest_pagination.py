import unittest
from unittest.mock import patch

import pandas as pd

import backtest


FOUR_HOURS_MS = 4 * 60 * 60 * 1000


def make_rows(count):
    return [
        [i * FOUR_HOURS_MS, 100.0, 101.0, 99.0, 100.5, 10.0]
        for i in range(count)
    ]


class FakeExchange:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit, since):
        self.calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "limit": limit,
                "since": since,
            }
        )
        return [row for row in self.rows if row[0] >= since][:limit]


class ScriptedExchange:
    def __init__(self, pages):
        self.pages = iter(pages)

    def fetch_ohlcv(self, _symbol, timeframe, limit, since):
        return next(self.pages)


class BacktestPaginationTests(unittest.TestCase):
    def test_fetch_ohlcv_paginated_collects_all_pages(self):
        exchange = FakeExchange(make_rows(2505))

        rows = backtest.fetch_ohlcv_paginated(
            exchange,
            "BTC/USDT",
            "4h",
            since=0,
            total_limit=2505,
            page_limit=1000,
        )

        self.assertEqual(2505, len(rows))
        self.assertEqual(
            [1000, 1000, 505],
            [call["limit"] for call in exchange.calls],
        )
        timestamps = [row[0] for row in rows]
        self.assertEqual(sorted(set(timestamps)), timestamps)
        self.assertEqual(999 * FOUR_HOURS_MS + 1, exchange.calls[1]["since"])

    def test_fetch_ohlcv_paginated_stops_at_available_history(self):
        exchange = FakeExchange(make_rows(1200))

        rows = backtest.fetch_ohlcv_paginated(
            exchange,
            "BTC/USDT",
            "4h",
            since=0,
            total_limit=2505,
            page_limit=1000,
        )

        self.assertEqual(1200, len(rows))
        self.assertEqual([1000, 1000], [call["limit"] for call in exchange.calls])

    def test_fetch_ohlcv_paginated_deduplicates_overlapping_pages(self):
        rows = make_rows(5)
        exchange = ScriptedExchange(
            [
                rows[:3],
                [rows[2], rows[3]],
                [rows[4]],
            ]
        )

        result = backtest.fetch_ohlcv_paginated(
            exchange,
            "BTC/USDT",
            "4h",
            since=0,
            total_limit=5,
            page_limit=3,
        )

        self.assertEqual([row[0] for row in rows], [row[0] for row in result])

    def test_fetch_ohlcv_paginated_rejects_stagnant_cursor(self):
        rows = make_rows(3)
        exchange = ScriptedExchange([rows, rows])

        with self.assertRaisesRegex(RuntimeError, "分页游标未前进"):
            backtest.fetch_ohlcv_paginated(
                exchange,
                "BTC/USDT",
                "4h",
                since=0,
                total_limit=5,
                page_limit=3,
            )

    @patch.object(backtest.td, "calculate_indicators", side_effect=lambda data, _kind: data)
    @patch.object(backtest, "fetch_ohlcv_paginated", return_value=[])
    def test_backtest_rejects_missing_fast_timeframe_coverage(
        self,
        _fetch_paginated,
        _calculate_indicators,
    ):
        daily_rows = [
            [i * 24 * 60 * 60 * 1000, 100.0, 101.0, 99.0, 100.5, 10.0]
            for i in range(backtest.MAX_CANDLES)
        ]
        exchange = FakeExchange(daily_rows)

        with patch.object(backtest, "ex", exchange):
            with self.assertRaisesRegex(RuntimeError, "快周期"):
                backtest.backtest_symbol("BTC/USDT")

    def test_validate_fast_coverage_rejects_missing_end(self):
        fast_index = pd.date_range("2026-01-01", periods=6, freq="4h")
        fast_df = pd.DataFrame({"close": range(6)}, index=fast_index)

        with self.assertRaisesRegex(RuntimeError, "未覆盖"):
            backtest._validate_fast_coverage(
                fast_df,
                pd.Timestamp("2026-01-01"),
                pd.Timestamp("2026-01-03"),
            )

    def test_validate_fast_coverage_rejects_missing_internal_day(self):
        fast_index = pd.date_range("2026-01-01", "2026-01-03 20:00", freq="4h")
        fast_index = fast_index[fast_index.normalize() != pd.Timestamp("2026-01-02")]
        fast_df = pd.DataFrame({"close": range(len(fast_index))}, index=fast_index)

        with self.assertRaisesRegex(RuntimeError, "缺少完整日期"):
            backtest._validate_fast_coverage(
                fast_df,
                pd.Timestamp("2026-01-01"),
                pd.Timestamp("2026-01-03"),
            )


if __name__ == "__main__":
    unittest.main()
