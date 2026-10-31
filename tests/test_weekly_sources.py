import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "weekly_sources", Path(__file__).resolve().parents[1] / "weekly_sources.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
select = module.select_weekly_videos


class WeeklySelectionTests(unittest.TestCase):
    def test_author_window_series_and_deduplication(self):
        def video(bvid, uid, published, title):
            return {
                "bvid": bvid,
                "author_uid": uid,
                "published_at": published,
                "title": title,
            }

        videos = [
            video("BV1234567890", 42, 199, "断网补全计划151"),
            video("BV1234567891", 42, 100, "断网补全计划150"),
            video("BV1234567892", 99, 120, "断网补全计划152"),
            video("BV1234567893", 42, 200, "断网补全计划153"),
            video("BV1234567894", 42, 120, "其他视频"),
            video("BV1234567891", 42, 100, "断网补全计划150"),
        ]
        result = select(
            videos,
            author_uid=42,
            start=100,
            end=200,
            seen={"BV1234567890"},
            title_pattern="断网补全计划",
        )
        self.assertEqual([x["bvid"] for x in result], ["BV1234567891"])

    def test_invalid_identifiers_and_limits(self):
        self.assertEqual(
            select(
                [{}, {"bvid": "x", "author_uid": 1, "published_at": 5, "title": "x"}],
                author_uid=1,
                start=0,
                end=10,
                seen=set(),
            ),
            [],
        )
        for kwargs in (
            {"author_uid": 0, "start": 0, "end": 1},
            {"author_uid": 1, "start": 1, "end": 1},
        ):
            with self.assertRaises(ValueError):
                select([], seen=set(), **kwargs)
