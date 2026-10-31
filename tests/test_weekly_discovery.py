import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if "bilibili_meme" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "bilibili_meme", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
from bilibili_meme.bili import BiliClient, BiliError


class DiscoveryTests(unittest.TestCase):
    def test_fetch_and_identity(self):
        client = BiliClient(rate_limit=0)
        with patch.object(
            client,
            "_wbi_get",
            return_value={
                "data": {
                    "list": {
                        "vlist": [
                            {"bvid": "BV1234567890", "title": "x", "created": 101}
                        ]
                    }
                }
            },
        ) as call:
            result = client.recent_uploads(42, since=100)
        self.assertEqual(result[0]["author_uid"], 42)
        self.assertEqual(call.call_args.args[1]["mid"], 42)

    def test_old_pinned_video_does_not_stop_pagination(self):
        client = BiliClient(rate_limit=0)
        first = [{"created": 50, "bvid": "BV1234567890", "title": "置顶"}] + [
            {"created": 150, "bvid": f"BV{i:010d}", "title": "新"} for i in range(29)
        ]
        second = [{"created": 140, "bvid": "BV9999999999", "title": "下一页"}]
        responses = [{"data": {"list": {"vlist": page}}} for page in (first, second)]
        with patch.object(client, "_wbi_get", side_effect=responses) as call:
            found = client.recent_uploads(42, since=100)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(found[-1]["bvid"], "BV9999999999")

    def test_truncated_pages_and_malformed_response_fail(self):
        client = BiliClient(rate_limit=0)
        with patch.object(
            client,
            "_wbi_get",
            return_value={"data": {"list": {"vlist": [{"created": 200}] * 30}}},
        ):
            with self.assertRaises(BiliError):
                client.recent_uploads(42, since=100, max_pages=1)
        with patch.object(client, "_wbi_get", return_value={"data": {}}):
            with self.assertRaises(BiliError):
                client.recent_uploads(42, since=100)
