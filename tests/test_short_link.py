import importlib.util
from pathlib import Path
import sys
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
if "bilibili_meme" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "bilibili_meme", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
from bilibili_meme.bili import BiliClient


class ShortLinkTests(unittest.TestCase):
    def test_bilibili_redirect_extracts_without_following(self):
        headers = Message()
        headers["Location"] = "https://www.bilibili.com/video/BV1234567890?x=1"
        with patch("urllib.request.build_opener") as make:
            make.return_value.open.side_effect = HTTPError(
                "https://b23.tv/x", 302, "redirect", headers, None
            )
            self.assertEqual(
                BiliClient(rate_limit=0).expand_short_link("https://b23.tv/x"),
                "BV1234567890",
            )
            self.assertEqual(make.return_value.open.call_count, 1)

    def test_redirect_to_internal_address_is_never_fetched(self):
        headers = Message()
        headers["Location"] = "http://127.0.0.1/secret"
        with patch("urllib.request.build_opener") as make:
            make.return_value.open.side_effect = HTTPError(
                "https://b23.tv/x", 302, "redirect", headers, None
            )
            self.assertEqual(
                BiliClient(rate_limit=0).expand_short_link("https://b23.tv/x"), ""
            )
            self.assertEqual(make.return_value.open.call_count, 1)
