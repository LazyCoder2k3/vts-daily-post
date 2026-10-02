"""Kiểm thử luồng snapshot/post khi GitHub Actions chạy trễ hoặc /today lỗi."""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402

TZ = main.TZ
CFG = {"daily_ride_factor": 0.25, "daily_swim_factor": 4, "daily_ride_types": ["ride"], "daily_swim_types": ["swim"],
       "start": date(2026, 9, 15), "end": date(2026, 10, 14), "anniv": date(2026, 10, 15),
       "founded_year": 2018, "target_km": 45, "hashtags": "#x", "show_daily_km": True,
       "llm_provider": "none"}


def fake_now(y, m, d, hh, mm):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(y, m, d, hh, mm, tzinfo=tz or TZ)
    return _DT


def ranking(a, b):
    return [{"rank": 1, "nick": "A", "total": a, "days": 3}, {"rank": 2, "nick": "B", "total": b, "days": 2}]


TODAY_ROWS = [{"nick": "A", "distance": 5.0, "type": "run"}, {"nick": "B", "distance": 8.0, "type": "ride"}]


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(main, "SNAP_DIR", root / "snapshots"),
            mock.patch.object(main, "POST_DIR", root / "posts"),
            mock.patch.object(main, "RAW_DIR", root / "raw"),
            mock.patch.object(main, "notify"),
        ]
        self.notify = None
        for p in self.patches:
            m = p.start()
            if p.attribute == "notify":
                self.notify = m
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def run_snapshot(self, now, rank=(10.0, 5.0), today=TODAY_ROWS, today_exc=None):
        ft = mock.Mock(side_effect=today_exc) if today_exc else mock.Mock(return_value=(today, "raw-today"))
        with mock.patch.object(main, "datetime", fake_now(*now)), \
                mock.patch.object(main, "fetch_ranking", return_value=(ranking(*rank), "raw")), \
                mock.patch.object(main, "fetch_today", ft):
            main.cmd_snapshot(CFG)
        return ft

    def snap(self, d):
        return main.load_snapshot(date.fromisoformat(d))

    def test_on_time_uses_today(self):
        ft = self.run_snapshot((2026, 10, 1, 23, 50))
        s = self.snap("2026-10-01")
        self.assertFalse(s["late"])
        self.assertEqual(s["warnings"], [])
        self.assertEqual(s["daily"]["active"], 2)
        self.assertAlmostEqual(s["daily"]["sum"], 5.0 + 8.0 * 0.25)
        ft.assert_called_once()

    def test_late_run_is_attributed_to_previous_day_and_skips_today(self):
        ft = self.run_snapshot((2026, 10, 2, 4, 28))
        s = self.snap("2026-10-01")
        self.assertIsNone(self.snap("2026-10-02"))
        self.assertTrue(s["late"])
        self.assertIsNone(s["daily"])
        self.assertTrue(s["warnings"])
        ft.assert_not_called()
        self.notify.assert_called_once()

    def test_late_run_never_overwrites_on_time_snapshot(self):
        self.run_snapshot((2026, 10, 1, 23, 50), rank=(10.0, 5.0))
        self.run_snapshot((2026, 10, 2, 4, 28), rank=(99.0, 5.0))
        s = self.snap("2026-10-01")
        self.assertFalse(s["late"])
        self.assertEqual(s["rows"][0]["total"], 10.0)

    def test_later_on_time_run_replaces_earlier_on_time_run(self):
        self.run_snapshot((2026, 10, 1, 23, 20), rank=(10.0, 5.0))
        self.run_snapshot((2026, 10, 1, 23, 50), rank=(12.0, 5.0))
        self.assertEqual(self.snap("2026-10-01")["rows"][0]["total"], 12.0)

    def test_today_failure_keeps_ranking_and_warns(self):
        self.run_snapshot((2026, 10, 1, 23, 50), today_exc=RuntimeError("timeout"))
        s = self.snap("2026-10-01")
        self.assertIsNone(s["daily"])
        self.assertEqual(len(s["rows"]), 2)
        self.assertIn("timeout", s["warnings"][0])

    def test_today_failure_on_second_run_keeps_daily_from_first_run(self):
        self.run_snapshot((2026, 10, 1, 23, 20))
        self.run_snapshot((2026, 10, 1, 23, 50), today_exc=RuntimeError("timeout"))
        s = self.snap("2026-10-01")
        self.assertEqual(s["daily"]["active"], 2)
        self.assertTrue(s["warnings"])

    def test_post_estimates_daily_from_ranking_diff_when_snapshot_is_late(self):
        self.run_snapshot((2026, 9, 30, 23, 50), rank=(10.0, 5.0))
        self.run_snapshot((2026, 10, 2, 4, 28), rank=(17.0, 5.0))  # trễ: ngày 1/10
        with mock.patch.object(main, "today_vn", return_value=date(2026, 10, 2)):
            main.cmd_post(CFG, dry_run=False)
        note, text = (c.args[0] for c in self.notify.call_args_list[-2:])
        self.assertIn("ƯỚC TÍNH", note)
        self.assertIn("A – 7,0 km", text)
        self.assertIn("hôm qua", text.lower())

    def test_post_warns_when_yesterday_snapshot_is_missing(self):
        self.run_snapshot((2026, 9, 29, 23, 50))
        with mock.patch.object(main, "today_vn", return_value=date(2026, 10, 2)):
            main.cmd_post(CFG, dry_run=False)
        note = self.notify.call_args_list[-2].args[0]
        self.assertIn("2026-10-01", note)
        self.assertIn("CŨ", note)

    def test_post_on_time_snapshot_has_no_warning(self):
        self.run_snapshot((2026, 10, 1, 23, 50))
        with mock.patch.object(main, "today_vn", return_value=date(2026, 10, 2)):
            main.cmd_post(CFG, dry_run=False)
        note = self.notify.call_args_list[-2].args[0]
        self.assertNotIn("⚠️", note)


if __name__ == "__main__":
    unittest.main()
