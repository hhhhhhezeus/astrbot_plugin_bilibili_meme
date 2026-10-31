import asyncio
import importlib.util
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = None


def setup_stubs():
    global DATA_DIR
    for old in [
        key for key in sys.modules if key == "astrtest" or key.startswith("astrtest.")
    ]:
        del sys.modules[old]
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.agent.message",
    ):
        sys.modules[name] = types.ModuleType(name)

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            return DATA_DIR

    class TextPart:
        def __init__(self, text):
            self.text = text

        def mark_as_temp(self):
            return self

    def decorator(*args, **kwargs):
        return lambda func: func

    filt = types.SimpleNamespace(
        command=decorator, llm_tool=decorator, on_llm_request=decorator
    )
    sys.modules["astrbot.api"].AstrBotConfig = dict
    sys.modules["astrbot.api.event"].AstrMessageEvent = object
    sys.modules["astrbot.api.event"].filter = filt
    sys.modules["astrbot.api.star"].Context = object
    sys.modules["astrbot.api.star"].Star = Star
    sys.modules["astrbot.api.star"].StarTools = StarTools
    sys.modules["astrbot.core.agent.message"].TextPart = TextPart
    spec = importlib.util.spec_from_file_location(
        "astrtest", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["astrtest"] = pkg
    spec.loader.exec_module(pkg)
    return importlib.import_module("astrtest.main")


class Event:
    unified_msg_origin = "qq:group:test"
    message_str = "请解释测试梗"

    def get_sender_id(self):
        return "tester"

    def plain_result(self, text):
        return text


class Context:
    async def get_current_chat_provider_id(self, umo):
        return "session-provider"

    async def llm_generate(self, **kwargs):
        self.last = kwargs
        return types.SimpleNamespace(
            completion_text=json.dumps(
                {
                    "is_meme": True,
                    "content": "测试梗",
                    "meaning": "梗义",
                    "confidence": 0.9,
                    "taboo": {"level": "none"},
                }
            )
        )


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global DATA_DIR
        self.temp = tempfile.TemporaryDirectory()
        DATA_DIR = Path(self.temp.name)
        self.m = setup_stubs()
        self.ctx = Context()
        self.plugin = self.m.BilibiliMeme(self.ctx, {})

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_learn_library_and_context_injection(self):
        client = MagicMock()
        client.video_info.return_value = {
            "bvid": "BV11kn86mEjg",
            "cid": 1,
            "aid": 2,
            "title": "测试视频",
            "owner": "作者",
            "view": 1,
            "duration": 30,
            "danmaku_total": 0,
        }
        client.comments.return_value = []
        client.subtitle.return_value = {
            "ok": True,
            "lines": [(0, "测试字幕")],
            "lang": "zh",
        }
        client.tags.return_value = []
        client.danmaku.return_value = []
        with patch.object(self.m, "BiliClient", return_value=client) as factory:
            factory.bursts.return_value = []
            outputs = [
                x async for x in self.plugin.learn_command(Event(), "BV11kn86mEjg")
            ]
        self.assertIn("已写入", outputs[0])
        self.assertEqual(self.ctx.last["chat_provider_id"], "session-provider")
        self.assertEqual(self.plugin._search("测试梗")[0][1], "梗义")
        req = types.SimpleNamespace(extra_user_content_parts=[])
        self.plugin.config["inject_matched_memes"] = True
        await self.plugin.inject_known_memes(Event(), req)
        self.assertIn("测试梗", req.extra_user_content_parts[0].text)
        self.assertIn(
            "梗义", [x async for x in self.plugin.library_command(Event(), "测试梗")][0]
        )

    async def test_save_failure_does_not_claim_success(self):
        with patch.object(
            self.plugin, "_save_record", side_effect=OSError("disk error")
        ):
            result = await self.plugin._upsert_jargon(
                {"content": "x", "meaning": "y", "evidence_messages": "[]"}
            )
        self.assertIn("失败", result)
        self.assertNotIn("已写入", result)

    async def test_weekly_uses_separate_configured_provider(self):
        self.plugin.config["weekly_provider_id"] = "weekly-provider"
        result = await self.plugin._llm_capability("llm.generate", prompt="test")
        self.assertTrue(result["success"])
        self.assertEqual(self.ctx.last["chat_provider_id"], "weekly-provider")
        await self.plugin.initialize()
        self.assertIsNotNone(self.plugin._weekly_task)

    async def test_cooldown_and_disabled(self):
        async def work():
            return "ok"

        self.assertEqual(await self.plugin._run(Event(), work), "ok")
        self.assertIn("冷却", await self.plugin._run(Event(), work))
        self.plugin.config["enabled"] = False
        self.assertIn("禁用", await self.plugin._run(Event(), work))

    async def test_weekly_multiple_memes_written_and_sensitive_skipped(self):
        async def generate(**kwargs):
            self.assertEqual(kwargs["chat_provider_id"], "weekly-provider")
            return types.SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "memes": [
                            {
                                "content": "一号梗",
                                "meaning": "含义一",
                                "confidence": 0.9,
                                "taboo": {"level": "none"},
                            },
                            {
                                "content": "二号梗",
                                "meaning": "含义二",
                                "confidence": 0.8,
                                "taboo": {"level": "mild"},
                            },
                            {
                                "content": "敏感梗",
                                "meaning": "含义三",
                                "confidence": 0.9,
                                "taboo": {"level": "strong"},
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            )

        self.ctx.llm_generate = generate
        self.plugin.config["weekly_provider_id"] = "weekly-provider"
        client = MagicMock()
        client.video_info.return_value = {
            "cid": 1,
            "aid": 2,
            "title": "断网补全计划150",
        }
        client.subtitle.return_value = {"lines": [(1, "测试字幕")]}
        client.comments.return_value = []
        await self.plugin._weekly_learn_video(
            client, {"bvid": "BV1234567890"}, self.plugin._settings().plugin
        )
        self.assertEqual(self.plugin._search("一号梗")[0][1], "含义一")
        self.assertEqual(self.plugin._search("二号梗")[0][1], "含义二")
        self.assertFalse(self.plugin._search("敏感梗"))

    async def test_summary_and_tool_share_request_provider(self):
        async def generate(**kwargs):
            self.assertEqual(kwargs["chat_provider_id"], "session-provider")
            return types.SimpleNamespace(
                completion_text=json.dumps(
                    {"one_line": "视频摘要", "key_points": [], "reliability": 0.8}
                )
            )

        self.ctx.llm_generate = generate
        client = MagicMock()
        client.video_info.return_value = {
            "bvid": "BV11kn86mEjg",
            "cid": 1,
            "aid": 2,
            "title": "视频",
            "owner": "UP",
            "duration": 10,
            "view": 3,
        }
        client.subtitle.return_value = {
            "lines": [(0, "字幕")],
            "ok": True,
            "lang": "中文",
        }
        client.comments.return_value = []
        client.danmaku.return_value = []
        self.plugin.config["command_cooldown_seconds"] = 0
        with patch.object(self.m, "BiliClient", return_value=client) as factory:
            factory.segments_for.return_value = 1
            factory.bursts.return_value = []
            first = [
                x async for x in self.plugin.summary_command(Event(), "BV11kn86mEjg")
            ]
            second = [
                x async for x in self.plugin.summary_tool(Event(), "BV11kn86mEjg")
            ]
        self.assertIn("视频摘要", first[0])
        self.assertIn("视频摘要", second[0])
