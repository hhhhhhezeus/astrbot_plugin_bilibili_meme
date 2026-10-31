import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if "bilibili_meme" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "bilibili_meme", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
from bilibili_meme.weekly_runner import WeeklyRunner, previous_week


class WeeklyRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_and_restart_do_not_repeat_completed_video(self):
        now = datetime(2026, 11, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        start, end = previous_week(now)
        calls = []
        failures = {"BV1234567891"}

        async def discover(uid, since):
            if uid != 646732586:
                return []
            return [
                {
                    "bvid": "BV1234567890",
                    "title": "断网补全计划150",
                    "author_uid": uid,
                    "published_at": start,
                },
                {
                    "bvid": "BV1234567891",
                    "title": "断网补全计划151",
                    "author_uid": uid,
                    "published_at": end - 1,
                },
            ]

        async def process(video):
            calls.append(video["bvid"])
            if video["bvid"] in failures:
                raise RuntimeError("test failure")

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "weekly.json"
            first = await WeeklyRunner(path).run(now, discover, process)
            self.assertEqual(first["completed"], ["BV1234567890"])
            self.assertEqual(len(first["failed"]), 1)
            failures.clear()
            second = await WeeklyRunner(path).run(now, discover, process)
            self.assertEqual(second["completed"], ["BV1234567891"])
            self.assertEqual(calls.count("BV1234567890"), 1)

    async def test_incomplete_week_remains_pending_across_restart(self):
        now = datetime(2026, 11, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

        async def discover(uid, since):
            raise RuntimeError("unavailable")

        async def process(video):
            pass

        with tempfile.TemporaryDirectory() as d:
            runner = WeeklyRunner(Path(d) / "state.json")
            await runner.run(now, discover, process)
            self.assertEqual(runner._load()["pending_end"], previous_week(now)[1])
            later = datetime(2026, 11, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
            with self.assertRaisesRegex(ValueError, "历史周任务"):
                await WeeklyRunner(runner.path).run(later, discover, process)

    def test_shanghai_calendar_week(self):
        now = datetime(2026, 11, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        start, end = previous_week(now)
        self.assertEqual(
            datetime.fromtimestamp(start, ZoneInfo("Asia/Shanghai")).isoformat(),
            "2026-10-26T00:00:00+08:00",
        )
        self.assertEqual(end - start, 7 * 86400)

    async def test_batches_over_one_hundred_without_losing_remainder(self):
        now = datetime(2026, 11, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        start, end = previous_week(now)

        async def discover(uid, since):
            if uid != 4009817:
                return []
            return [
                {
                    "bvid": "BV" + str(i).zfill(10),
                    "title": "梗指南",
                    "author_uid": uid,
                    "published_at": start + 1,
                }
                for i in range(101)
            ]

        async def process(video):
            pass

        with tempfile.TemporaryDirectory() as d:
            runner = WeeklyRunner(Path(d) / "state.json")
            first = await runner.run(now, discover, process)
            self.assertEqual(len(first["completed"]), 100)
            self.assertEqual(len(first["failed"]), 1)
            self.assertEqual(runner._load()["pending_end"], end)
            second = await runner.run(now, discover, process)
            self.assertEqual(len(second["completed"]), 1)
            self.assertFalse(second["failed"])
            self.assertEqual(runner._load()["last_completed_end"], end)
            self.assertNotIn("pending_end", runner._load())
