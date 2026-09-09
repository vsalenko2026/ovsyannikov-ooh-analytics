import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ws import periods


class TestPeriods(unittest.TestCase):
    def test_period_end_weekly_is_sunday(self):
        # 17.07.2026 — пятница, старт основной программы
        self.assertEqual(periods.period_end(dt.date(2026, 7, 17), periods.PERIOD_WEEKLY),
                         dt.date(2026, 7, 19))

    def test_period_end_monthly_is_last_day(self):
        self.assertEqual(periods.period_end(dt.date(2026, 2, 3), periods.PERIOD_MONTHLY),
                         dt.date(2026, 2, 28))

    def test_anchor_start_and_end(self):
        self.assertEqual(periods.detect_week_anchor(["2026-02-02", "2026-02-09"]), "start")
        self.assertEqual(periods.detect_week_anchor(["2026-02-08T00:00:00Z"]), "end")

    def test_mixed_dates_are_rejected(self):
        # Молча достраивать границы по разнородным датам нельзя.
        with self.assertRaises(ValueError):
            periods.detect_week_anchor(["2026-02-02", "2026-02-05"])

    def test_week_bounds_by_anchor(self):
        self.assertEqual(periods.week_bounds(dt.date(2026, 7, 13), "start"),
                         (dt.date(2026, 7, 13), dt.date(2026, 7, 19)))
        self.assertEqual(periods.week_bounds(dt.date(2026, 7, 19), "end"),
                         (dt.date(2026, 7, 13), dt.date(2026, 7, 19)))

    def test_is_closed(self):
        self.assertTrue(periods.is_closed(dt.date(2026, 9, 6), dt.date(2026, 9, 9)))
        self.assertFalse(periods.is_closed(dt.date(2026, 9, 13), dt.date(2026, 9, 9)))


if __name__ == "__main__":
    unittest.main()
