"""Weekly coordination primitives. Does not start a background task by itself."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .weekly_sources import select_weekly_videos

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
SOURCES = ((646732586, r"断网补全计划\s*\d+"), (4009817, None))


def previous_week(now: datetime) -> tuple[int, int]:
    """Previous calendar week (Monday 00:00 through next Monday), Shanghai time."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(SHANGHAI)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((monday - timedelta(days=7)).timestamp()), int(monday.timestamp())


class WeeklyRunner:
    """Coordinate discovery and processing; checkpoints only completed videos.

    The caller owns the scheduler and supplies async discovery/process callbacks.
    A failed item is deliberately not marked complete and is retried next time.
    """

    def __init__(self, state_path: Path):
        self.path = Path(state_path)
        self.lock = asyncio.Lock()

    def _load(self):
        if not self.path.exists():
            return {"done": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("done"), dict):
            raise ValueError("周任务状态文件无效，请检查备份")
        return value

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    async def run(self, now, discover, process):
        async with self.lock:
            start, end = previous_week(now)
            state = self._load()
            pending_end = state.get("pending_end")
            if pending_end is not None and pending_end != end:
                raise ValueError("存在尚未完成的历史周任务，请先重试该周")
            state["pending_end"] = end
            self._save(state)
            done = state["done"]
            results = {"start": start, "end": end, "completed": [], "failed": []}
            for uid, pattern in SOURCES:
                try:
                    candidates = await discover(uid, start)
                    videos = select_weekly_videos(
                        candidates,
                        author_uid=uid,
                        start=start,
                        end=end,
                        seen=set(done),
                        title_pattern=pattern,
                        limit=100,
                    )
                except Exception as exc:
                    results["failed"].append({"uid": uid, "error": str(exc)[:200]})
                    continue
                for video in videos:
                    try:
                        await process(video)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        results["failed"].append(
                            {"bvid": video["bvid"], "error": str(exc)[:200]}
                        )
                        continue
                    done[video["bvid"]] = {
                        "published_at": video["published_at"],
                        "author_uid": uid,
                    }
                    self._save(state)
                    results["completed"].append(video["bvid"])
                if len(videos) == 100:
                    results["failed"].append(
                        {"uid": uid, "error": "本次达到 100 条上限，将继续处理剩余视频"}
                    )
            if not results["failed"]:
                state["last_completed_end"] = end
                state.pop("pending_end", None)
                self._save(state)
            return results
