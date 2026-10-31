import importlib.util
import io
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


class SubtitleURLTests(unittest.TestCase):
    def test_rejects_untrusted_urls_and_redirects(self):
        client = BiliClient(rate_limit=0)
        for url in (
            "http://hdslb.com/a",
            "https://hdslb.com.evil.test/a",
            "https://127.0.0.1/a",
            "https://evil.test@hdslb.com/a",
            "https://hdslb.com:abc/a",
        ):
            with self.subTest(url=url), self.assertRaises(BiliError):
                client._subtitle_json(url)
        with patch("urllib.request.build_opener") as build:

            class Opener:
                def open(self, req, timeout):
                    redirect = build.call_args.args[0]
                    return redirect.redirect_request(
                        req, None, 302, "", {}, "http://127.0.0.1/"
                    )

            build.return_value = Opener()
            with self.assertRaises(BiliError):
                client._subtitle_json("https://aisubtitle.hdslb.com/a")

    def test_allows_valid_subtitle(self):
        client = BiliClient(rate_limit=0)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(b'{"body": []}')
            self.assertEqual(
                client._subtitle_json("https://aisubtitle.hdslb.com/a"), {"body": []}
            )

    def test_large_subtitle_is_rejected(self):
        client = BiliClient(rate_limit=0)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(
                b"x" * (8 * 1024 * 1024 + 1)
            )
            with self.assertRaises(BiliError):
                client._subtitle_json("https://aisubtitle.hdslb.com/a")

    def test_large_api_response_is_rejected(self):
        client = BiliClient(rate_limit=0)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(
                b"x" * (8 * 1024 * 1024 + 1)
            )
            with self.assertRaises(BiliError):
                client._fetch("https://api.bilibili.com/x")
