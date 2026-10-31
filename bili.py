"""B站 API 客户端：WBI 签名 / 搜索 / 视频信息 / 评论 / 弹幕(protobuf) / 时间轴爆发点。

只依赖标准库，不引入额外依赖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

logger = logging.getLogger("bilibili_meme.bili")

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# WBI 用的 64 位重排表（B站前端常量，稳定多年）
_MIXIN_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


class BiliError(RuntimeError):
    """B站接口异常（含风控 -352）。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BiliClient:
    """极简 B站 只读客户端。所有请求串行 + 限速，避免触发风控。"""

    def __init__(
        self, *, timeout: float = 20.0, rate_limit: float = 1.2, sessdata: str = ""
    ) -> None:
        self.timeout = timeout
        self.rate_limit = max(0.0, rate_limit)
        self._last_request = 0.0
        self._mixin_key: str | None = None
        self._mixin_at = 0.0
        self._cookie: str = ""
        self._sessdata: str = (sessdata or "").strip()

    # ---------------- 基础请求 ----------------

    def _throttle(self) -> None:
        gap = self.rate_limit - (time.monotonic() - self._last_request)
        if gap > 0:
            time.sleep(gap)
        self._last_request = time.monotonic()

    def _fetch(self, url: str) -> bytes:
        self._throttle()
        headers = dict(UA)
        if self._cookie:
            headers["Cookie"] = self._cookie
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                req, timeout=self.timeout
            ) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise BiliError("B站响应超过大小限制")
                return raw
        except urllib.error.HTTPError as exc:
            raise BiliError(f"HTTP {exc.code}: {url[:120]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BiliError(f"网络错误: {exc}") from exc

    def _raw_json(self, url: str) -> dict[str, Any]:
        """不校验 code 的原始 JSON（nav 未登录时返回 -101，但 wbi_img 仍可用）。"""
        raw = self._fetch(url)
        try:
            data = json.loads(raw.decode("utf-8", "ignore"))
        except json.JSONDecodeError as exc:
            raise BiliError(f"响应不是 JSON: {raw[:80]!r}") from exc
        if not isinstance(data, dict):
            raise BiliError("响应结构异常")
        return data

    def _get_json(self, url: str) -> dict[str, Any]:
        data = self._raw_json(url)
        code = data.get("code")
        if code == -352:
            raise BiliError("触发风控(-352)，请降低频率或稍后再试")
        if code != 0:
            raise BiliError(f"接口返回 code={code} msg={data.get('message')}")
        return data

    def _ensure_cookie(self) -> str:
        """匿名指纹 cookie（buvid3/buvid4）。缺了它部分接口会直接 -101/-352。"""
        if self._cookie:
            return self._cookie
        try:
            spi = self._raw_json("https://api.bilibili.com/x/frontend/finger/spi")
            data = spi.get("data") or {}
            b3, b4 = str(data.get("b_3", "")), str(data.get("b_4", ""))
            if b3:
                self._cookie = f"buvid3={b3}; buvid4={b4}"
                if self._sessdata:
                    self._cookie += f"; SESSDATA={self._sessdata}"
        except BiliError as exc:
            logger.debug("取 buvid 失败(继续裸请求): %s", exc)
        return self._cookie

    # ---------------- WBI 签名 ----------------

    def _get_mixin_key(self, ttl: float = 3600.0) -> str:
        if self._mixin_key and (time.time() - self._mixin_at) < ttl:
            return self._mixin_key
        self._ensure_cookie()
        data = self._raw_json("https://api.bilibili.com/x/web-interface/nav")
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError(f"拿不到 WBI 密钥（nav code={data.get('code')}）")
        raw = img + sub
        self._mixin_key = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
        self._mixin_at = time.time()
        return self._mixin_key

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 WBI 规则签名，返回带 wts / w_rid 的新参数。"""
        signed = {k: v for k, v in params.items() if v is not None}
        signed["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(signed.items()))
        # B站要求过滤这几个字符
        query = "".join(ch for ch in query if ch not in "!'()*")
        signed["w_rid"] = hashlib.md5(
            (query + self._get_mixin_key()).encode()
        ).hexdigest()
        return signed

    def _wbi_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.bilibili.com{path}?" + urllib.parse.urlencode(
            self._sign(params)
        )
        return self._get_json(url)

    # ---------------- 业务接口 ----------------

    @staticmethod
    def segments_for(duration_s: int, cap: int = 8) -> int:
        """按时长算弹幕分片数（每片约 6 分钟），避免长视频只取到开头。"""
        return max(1, min(cap, -(-int(duration_s or 0) // 360)))

    def search_videos(
        self, keyword: str, limit: int = 5, duration: int | None = None
    ) -> list[dict[str, Any]]:
        """搜索视频，返回 [{bvid, title, danmaku, review, author}]。

        duration: B站 时长筛选（1=<10分钟, 2=10-30, 3=30-60, 4=>60），None 不限。
        """
        params: dict[str, Any] = {"search_type": "video", "keyword": keyword, "page": 1}
        if duration:
            params["duration"] = duration
        data = self._wbi_get("/x/web-interface/wbi/search/type", params)
        result = (data.get("data") or {}).get("result") or []
        items: list[dict[str, Any]] = []
        for it in result[: max(1, limit)]:
            title = str(it.get("title", ""))
            for marker in ('<em class="keyword">', "</em>"):
                title = title.replace(marker, "")
            items.append(
                {
                    "bvid": it.get("bvid", ""),
                    "title": title,
                    "danmaku": int(it.get("video_review") or 0),
                    "review": int(it.get("review") or 0),
                    "author": it.get("author", ""),
                }
            )
        return items

    def recent_uploads(
        self, author_uid: int, *, since: int, max_pages: int = 5
    ) -> list[dict[str, Any]]:
        """Fetch a bounded newest-first UP archive; fail on incomplete pagination."""
        if (
            isinstance(author_uid, bool)
            or not isinstance(author_uid, int)
            or author_uid <= 0
        ):
            raise ValueError("UP 主 UID 必须为正整数")
        if not 1 <= max_pages <= 20:
            raise ValueError("页数上限无效")
        result = []
        for page in range(1, max_pages + 1):
            data = self._wbi_get(
                "/x/space/wbi/arc/search",
                {
                    "mid": author_uid,
                    "pn": page,
                    "ps": 30,
                    "order": "pubdate",
                },
            )
            body = data.get("data")
            if not isinstance(body, dict) or not isinstance(body.get("list"), dict):
                raise BiliError("UP 主投稿列表响应结构异常")
            videos = body["list"].get("vlist")
            if not isinstance(videos, list):
                raise BiliError("UP 主投稿列表缺少视频数据")
            for item in videos:
                if not isinstance(item, dict):
                    raise BiliError("UP 主投稿记录结构异常")
                if item.get("mid") is not None and str(item["mid"]) != str(author_uid):
                    raise BiliError("投稿作者 UID 与请求不一致")
                stamp = item.get("created")
                if isinstance(stamp, bool) or not isinstance(stamp, int):
                    raise BiliError("投稿时间缺失，无法判断是否已完整扫描")
                result.append(
                    {
                        "bvid": item.get("bvid"),
                        "title": item.get("title"),
                        "author_uid": author_uid,
                        "published_at": stamp,
                    }
                )
            if len(videos) < 30 or (
                videos and all(v["created"] < since for v in videos)
            ):
                return result
        raise BiliError("投稿超过分页上限，未确认完整性；请缩短时间范围或提高上限")

    def video_info(self, bvid: str) -> dict[str, Any]:
        """视频元信息（aid/cid/标题/简介/时长/UP主）。"""
        data = self._get_json(
            "https://api.bilibili.com/x/web-interface/view?"
            + urllib.parse.urlencode({"bvid": bvid})
        )
        v = data["data"]
        pages = v.get("pages") or []
        return {
            "bvid": v.get("bvid", bvid),
            "aid": v.get("aid"),
            "cid": v.get("cid") or (pages[0]["cid"] if pages else None),
            "title": v.get("title", ""),
            "desc": v.get("desc", ""),
            "duration": int(v.get("duration") or 0),
            "owner": (v.get("owner") or {}).get("name", ""),
            "view": int((v.get("stat") or {}).get("view") or 0),
            "danmaku_total": int((v.get("stat") or {}).get("danmaku") or 0),
            "reply_total": int((v.get("stat") or {}).get("reply") or 0),
        }

    def tags(self, bvid: str) -> list[str]:
        try:
            data = self._get_json(
                f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
            )
            return [
                str(t.get("tag_name", ""))
                for t in (data.get("data") or [])
                if t.get("tag_name")
            ]
        except BiliError as exc:
            logger.debug("取标签失败(忽略): %s", exc)
            return []

    def comments(self, aid: int, pages: int = 2) -> list[dict[str, Any]]:
        """热门评论，返回 [{like, message, is_up}]。"""
        out: list[dict[str, Any]] = []
        cursor = 0
        for _ in range(max(1, pages)):
            data = self._get_json(
                f"https://api.bilibili.com/x/v2/reply/main?oid={aid}&type=1&mode=3&ps=20&next={cursor}"
            )
            body = data.get("data") or {}
            for r in body.get("replies") or []:
                content = r.get("content") or {}
                msg = str(content.get("message") or "").strip()
                if not msg:
                    continue
                out.append(
                    {
                        "like": int(r.get("like") or 0),
                        "message": msg,
                        "rcount": int(r.get("rcount") or 0),
                    }
                )
            cursor = int((body.get("cursor") or {}).get("next") or 0)
            if not cursor:
                break
        return out

    # ---------------- 弹幕 ----------------

    @staticmethod
    def _read_varint(buf: bytes, idx: int) -> tuple[int, int]:
        result = shift = 0
        while True:
            if idx >= len(buf):
                raise ValueError("varint 越界")
            byte = buf[idx]
            idx += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return result, idx

    @classmethod
    def parse_danmaku(cls, raw: bytes) -> list[tuple[int, str]]:
        """解析 DanmakuElem 列表，返回 [(progress_ms, 内容)]。

        proto 字段：1=id, 2=progress(ms), 3=mode, 4=fontsize, 5=color,
                    6=midHash, 7=content, 8=ctime
        """
        elems: list[tuple[int, str]] = []
        idx = 0
        while idx < len(raw):
            try:
                tag, idx = cls._read_varint(raw, idx)
                field, wire = tag >> 3, tag & 0x07
                if wire != 2:
                    break
                length, idx = cls._read_varint(raw, idx)
                seg = raw[idx : idx + length]
                idx += length
            except ValueError:
                break
            if field != 1:  # 只要 DanmakuElem
                continue
            progress, content = 0, None
            j = 0
            while j < len(seg):
                try:
                    t, j = cls._read_varint(seg, j)
                    f, w = t >> 3, t & 0x07
                    if w == 0:
                        val, j = cls._read_varint(seg, j)
                        if f == 2:
                            progress = val
                    elif w == 2:
                        ln, j = cls._read_varint(seg, j)
                        payload = seg[j : j + ln]
                        j += ln
                        if f == 7:
                            content = payload.decode("utf-8", "ignore")
                    else:
                        break
                except (ValueError, IndexError):
                    break
            if content:
                elems.append((progress, content.strip()))
        return elems

    def danmaku(self, cid: int, segments: int = 2) -> list[tuple[int, str]]:
        """按分片拉弹幕（每片约 6 分钟）。"""
        out: list[tuple[int, str]] = []
        for seg in range(1, max(1, segments) + 1):
            try:
                raw = self._fetch(
                    f"https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid={cid}&segment_index={seg}"
                )
            except BiliError as exc:
                logger.debug("弹幕分片 %d 失败(忽略): %s", seg, exc)
                continue
            out.extend(self.parse_danmaku(raw))
        return out

    @staticmethod
    def bursts(
        elems: Iterable[tuple[int, str]],
        *,
        window_ms: int = 10_000,
        top: int = 4,
        samples: int = 4,
    ) -> list[dict[str, Any]]:
        """按时间窗统计弹幕密度，返回弹幕爆发点（= 笑点/关键画面）。"""
        buckets: dict[int, list[str]] = {}
        for progress, content in elems:
            buckets.setdefault(progress // window_ms, []).append(content)
        ranked = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)[
            : max(1, top)
        ]
        ranked.sort(key=lambda kv: kv[0])  # 按时间顺序回排
        return [
            {
                "start_s": win * window_ms // 1000,
                "end_s": (win + 1) * window_ms // 1000,
                "count": len(items),
                "samples": items[:samples],
            }
            for win, items in ranked
        ]

    # ---------------- 字幕 / 短链 ----------------

    def expand_short_link(self, url: str) -> str:
        """Resolve only known B站 short-link hosts, without following arbitrary redirects."""
        found = _BV_RE.search(url)
        if found:
            return found.group(1)
        match = re.search(r"(https://(?:b23\.tv|bili2233\.cn)/[^\s]+)", url)
        if not match:
            return ""
        current = match.group(1)
        opener = urllib.request.build_opener(_NoRedirect())
        for _ in range(4):
            parts = urllib.parse.urlsplit(current)
            if (
                parts.scheme != "https"
                or parts.hostname not in ("b23.tv", "bili2233.cn")
                or parts.username
                or parts.password
                or parts.port not in (None, 443)
            ):
                return ""
            self._throttle()
            try:
                req = urllib.request.Request(current, headers=UA)
                with opener.open(req, timeout=self.timeout):
                    return ""
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    return ""
                target = urllib.parse.urljoin(current, exc.headers.get("Location", ""))
            except (urllib.error.URLError, TimeoutError, ValueError):
                return ""
            destination = urllib.parse.urlsplit(target)
            if (
                destination.scheme == "https"
                and destination.hostname in ("www.bilibili.com", "m.bilibili.com")
                and not destination.username
                and not destination.password
            ):
                found = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", destination.path)
                return found.group(1) if found else ""
            current = target
        return ""

    def _subtitle_json(self, url: str) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError as exc:
            raise BiliError("字幕地址端口无效") from exc
        if (
            parsed.scheme != "https"
            or not (host == "hdslb.com" or host.endswith(".hdslb.com"))
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
        ):
            raise BiliError("字幕地址不在允许的 HTTPS 域名范围内")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise BiliError("字幕地址不允许重定向")

        self._throttle()
        try:
            req = urllib.request.Request(url, headers=dict(UA))
            with urllib.request.build_opener(NoRedirect()).open(
                req, timeout=self.timeout
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise BiliError("字幕响应超过大小限制")
                payload = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise BiliError("字幕下载失败或格式无效") from exc
        if not isinstance(payload, dict):
            raise BiliError("字幕响应结构异常")
        return payload

    def subtitle(self, bvid: str, cid: int) -> dict[str, Any]:
        """取字幕。返回 {ok, need_login, lang, lines:[(from_s, text)]}。

        未登录时 B站 返回 need_login_subtitle=True 且 subtitles 为空，
        此时只能靠弹幕/评论降级。
        """
        try:
            data = self._wbi_get("/x/player/wbi/v2", {"bvid": bvid, "cid": cid})
        except BiliError as exc:
            logger.debug("取字幕失败: %s", exc)
            return {"ok": False, "need_login": False, "lang": "", "lines": []}

        body = data.get("data") or {}
        sub = body.get("subtitle") or {}
        subs = sub.get("subtitles") or []
        need_login = bool(body.get("need_login_subtitle"))
        if not subs:
            return {"ok": False, "need_login": need_login, "lang": "", "lines": []}

        # 人工字幕优先，中文优先
        def rank(item: dict[str, Any]) -> tuple[int, int]:
            """排序键：中文优先，其次人工字幕优先于 AI 字幕。"""
            lan = str(item.get("lan") or "")
            is_ai = 1 if str(item.get("subtitle_type") or "") in ("1",) else 0
            zh = 0 if lan.startswith("zh") else 1
            return (zh, is_ai)

        pick = sorted(subs, key=rank)[0]
        url = str(pick.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return {"ok": False, "need_login": need_login, "lang": "", "lines": []}
        try:
            payload = self._subtitle_json(url)
        except BiliError as exc:
            logger.debug("字幕正文下载失败: %s", exc)
            return {"ok": False, "need_login": need_login, "lang": "", "lines": []}
        lines: list[tuple[float, str]] = []
        for item in payload.get("body") or []:
            text = str(item.get("content") or "").strip()
            if text:
                lines.append((float(item.get("from") or 0), text))
        return {
            "ok": bool(lines),
            "need_login": need_login,
            "lang": str(pick.get("lan_doc") or pick.get("lan") or ""),
            "lines": lines,
        }
