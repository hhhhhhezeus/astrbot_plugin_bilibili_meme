"""Pure candidate selection for the weekly video workflow.

Discovery and execution are intentionally separate: callers must provide a
trusted numeric UP ID, a bounded publication window, and previously handled IDs.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_BVID = re.compile(r"BV[0-9A-Za-z]{10}\Z")


def select_weekly_videos(
    videos: Iterable[dict[str, Any]],
    *,
    author_uid: int,
    start: int,
    end: int,
    seen: set[str],
    title_pattern: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Select one author's unseen uploads in [start, end), oldest first.

    Invalid or missing identifiers/timestamps are ignored; API responses are
    untrusted. Compile the optional title filter before scanning input.
    """
    if (
        isinstance(author_uid, bool)
        or not isinstance(author_uid, int)
        or author_uid <= 0
    ):
        raise ValueError("author_uid must be a positive integer")
    if start >= end or not 1 <= limit <= 100:
        raise ValueError("invalid window or limit")
    matcher = re.compile(title_pattern) if title_pattern else None
    selected: dict[str, dict[str, Any]] = {}
    for item in videos:
        if not isinstance(item, dict):
            continue
        bvid, title = item.get("bvid"), item.get("title")
        uid, published = item.get("author_uid"), item.get("published_at")
        if not isinstance(bvid, str) or not _BVID.fullmatch(bvid) or bvid in seen:
            continue
        if isinstance(uid, bool) or not isinstance(uid, int) or uid != author_uid:
            continue
        if (
            isinstance(published, bool)
            or not isinstance(published, int)
            or not start <= published < end
        ):
            continue
        if not isinstance(title, str) or (
            matcher is not None and not matcher.search(title)
        ):
            continue
        selected.setdefault(
            bvid,
            {
                "bvid": bvid,
                "title": title,
                "author_uid": uid,
                "published_at": published,
            },
        )
    return sorted(selected.values(), key=lambda x: (x["published_at"], x["bvid"]))[
        :limit
    ]
