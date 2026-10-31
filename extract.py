"""梗抽取：把 B站 视频的多源信号交给本地 LLM，产出结构化梗卡。

设计要点
- 只喂"人话"信号（标题/简介/标签/弹幕爆发点/热评），不喂原始弹幕流，省 token
- 强制 JSON 输出 + 宽松解析（模型偶尔会包 ```json 或加解释）
- 显式判定 taboo（地域黑/地狱笑话/侮辱性），敏感梗默认不入库
- 显式判定 freshness（当红/过气），便于后续衰减
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("bilibili_meme.extract")

# 评论里指示"这视频讲得清楚"的信号
_POSITIVE = (
    "讲得全面",
    "起源",
    "捋清楚",
    "课代表",
    "省流",
    "科普",
    "详细",
    "谢谢",
    "受教",
    "了解了",
    "原来",
)
# 评论里指示"乱讲/不对"的信号
_NEGATIVE = ("瞎讲", "不对", "错的", "胡说", "误导", "扯淡", "编的", "错了")

_JUNK_DANMAKU = (
    "哈哈哈",
    "233",
    "666",
    "前方高能",
    "第一",
    "打卡",
    "来了",
    "好听",
    "笑死",
    "绷不住",
    "泪目",
)


def score_candidate(cand: dict[str, Any], comments: list[dict[str, Any]]) -> float:
    """给候选视频打分，用于「同一梗多个视频」时选源。

    参考：弹幕量（热度）、评论元信号（讲得清不清楚）、标题形态（是不是梗指南）、播放量。
    """
    import math

    score = 0.0
    score += math.log1p(max(0, cand.get("danmaku", 0))) * 0.6
    score += math.log1p(max(0, cand.get("view", 0))) * 0.25

    title = str(cand.get("title", ""))
    if any(k in title for k in ("什么梗", "梗指南", "是什么梗", "梗科普")):
        score += 2.0
    if "梗" in title:
        score += 0.5

    text = " ".join(str(c.get("message", "")) for c in comments)
    score += 0.35 * sum(1 for k in _POSITIVE if k in text)
    score -= 0.8 * sum(1 for k in _NEGATIVE if k in text)
    return round(score, 3)


def pick_comments(
    comments: list[dict[str, Any]], limit: int = 14
) -> list[dict[str, Any]]:
    """挑"有信息量"的评论：优先长文本 + 高赞，过滤纯情绪短句。"""
    scored: list[tuple[float, dict[str, Any]]] = []
    for c in comments:
        msg = str(c.get("message", "")).strip()
        if len(msg) < 8:
            continue
        weight = len(msg) * 0.02 + min(3.0, (c.get("like", 0) or 0) ** 0.5 * 0.15)
        if any(k in msg for k in _POSITIVE):
            weight += 1.5
        if any(k in msg for k in _NEGATIVE):
            weight += 0.8  # 负面也要看，帮模型判断可信度
        scored.append((weight, c))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [c for _, c in scored[:limit]]


def clean_danmaku(samples: list[str]) -> list[str]:
    """去掉纯情绪弹幕，保留带信息的。"""
    out = []
    for s in samples:
        s = str(s).strip()
        if not s or len(s) < 4:
            continue
        if any(s == j or s.startswith(j) for j in _JUNK_DANMAKU):
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z]{2,}", s):
            continue
        out.append(s)
    return out


def build_prompt(
    *,
    term_hint: str,
    video: dict[str, Any],
    tags: list[str],
    bursts: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    subtitle: dict[str, Any] | None = None,
    max_subtitle_chars: int = 20000,
) -> str:
    """组装抽取提示词。

    有字幕时把讲解正文一并喂进去 —— 那是"梗的来历"的唯一可靠来源。
    """
    sub_lines = (subtitle or {}).get("lines") or []
    sub_text = ""
    if sub_lines:
        rendered = "\n".join(txt for _, txt in sub_lines)
        if len(rendered) > max_subtitle_chars:
            rendered = rendered[:max_subtitle_chars] + "…（后略）"
        sub_text = rendered
    burst_lines = []
    for b in bursts:
        cleaned = clean_danmaku(b.get("samples") or [])
        if not cleaned:
            continue
        burst_lines.append(
            f"  [{b['start_s']}-{b['end_s']}s, {b['count']}条] "
            + " / ".join(cleaned[:4])
        )
    comment_lines = [
        f"  [{c.get('like', 0)}赞] {str(c.get('message', '')).replace(chr(10), ' ')[:160]}"
        for c in comments
    ]

    return f"""你在帮一个群聊 AI「麦麦」整理网络黑话（梗）资料。下面是 B站 一个讲梗的视频的可用信息。
请判断"这是不是一个真实的、可被日常使用的梗"，并整理成结构化资料。

# 待确认的梗
{term_hint}

# 视频
标题：{video.get("title", "")}
UP主：{video.get("owner", "")} ｜ 时长：{video.get("duration", 0)}秒 ｜ 播放：{video.get("view", 0)}
简介：{(video.get("desc") or "(无)")[:300]}
标签：{("、".join(tags) if tags else "(无)")}

# 字幕（UP主讲解正文{"，共 " + str(len(sub_lines)) + " 条" if sub_lines else "：未获取到"}）
{sub_text if sub_text else "（该视频没有可用字幕，只能根据弹幕和评论推断）"}

# 弹幕爆发点（弹幕突然变多的地方 = 视频的笑点/关键画面）
{chr(10).join(burst_lines) if burst_lines else "  (无)"}

# 热门评论（体现观众反应和社区认知）
{chr(10).join(comment_lines) if comment_lines else "  (无)"}

# 输出要求
只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。字段：

{{
  "is_meme": true/false,          // 是不是真实存在的网络梗；如果视频在讲别的东西就给 false
  "content": "梗的标准写法（最常用的那种，2-10字）",
  "aliases": ["其它写法或简称", "..."],
  "origin": "这个梗的来历/出处（来自字幕讲解；没有字幕就留空串，不要编）",
  "meaning": "这个梗是什么意思（1-2句，说清含义）",
  "usage": "在什么语境下、怎么用它（1-2句，给出可模仿的句式）",
  "examples": ["来自上面信息里的真实例句", "..."],
  "freshness": "active|fading|dead",   // 当红/正在过气/已死
  "taboo": {{
     "level": "none|mild|strong",
     "reason": "若 mild/strong，说明为什么（如地域歧视、地狱笑话、人身攻击、涉政）"
  }},
  "confidence": 0.0-1.0            // 你对这个整理结果的把握
}}

判定规则：
1. examples 必须是上面材料里出现过的原句，不要自己编。
2. 如果材料不足以说清"含义"或"用法"，把 confidence 压到 0.4 以下。
3. taboo 判定要保守：涉及地域、民族、性别、身体缺陷、灾难、逝者的玩笑，一律不写 none。
5. origin 只能来自字幕内容；没有字幕时必须留空串，绝不允许凭猜测编造来历。
4. 不要把这个视频里的 UP 主名字当成梗。
"""


def parse_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON（容忍代码块和多余解释）。"""
    if not text:
        return None
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, re.S)
    if fence:
        body = fence.group(1)
    else:
        start, end = body.find("{"), body.rfind("}")
        if start >= 0 and end > start:
            body = body[start : end + 1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # 常见问题：尾逗号
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", body))
        except json.JSONDecodeError:
            logger.warning("模型输出无法解析为 JSON: %s", body[:200])
            return None
    return data if isinstance(data, dict) else None


def _mmss(sec: float) -> str:
    total = int(sec)
    return f"{total // 60}:{total % 60:02d}"


def build_video_summary_prompt(
    *,
    video: dict[str, Any],
    subtitle: dict[str, Any],
    bursts: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    max_subtitle_chars: int = 60000,
) -> str:
    """组装"视频理解"提示词。

    有字幕时以字幕为主（这是唯一的讲解正文来源），弹幕爆发点作为"重点位置"的旁证，
    评论只用来补充观众共识。没字幕时明确告知模型信息不足。
    """
    lines = subtitle.get("lines") or []
    sub_text = ""
    if lines:
        rendered = "\n".join(f"[{_mmss(t)}] {txt}" for t, txt in lines)
        if len(rendered) > max_subtitle_chars:
            head = rendered[: max_subtitle_chars // 2]
            tail = rendered[-max_subtitle_chars // 2 :]
            rendered = f"{head}\n…（中间省略，字幕过长）…\n{tail}"
        sub_text = rendered

    burst_lines = []
    for b in bursts:
        samples = clean_danmaku(b.get("samples") or [])
        if samples:
            burst_lines.append(
                f"  [{_mmss(b['start_s'])}-{_mmss(b['end_s'])}, {b['count']}条] "
                + " / ".join(samples[:3])
            )

    has_sub = bool(lines)
    return f"""你在帮一个群聊 AI「麦麦」理解一个 B站 视频，之后再向群友转述。下面是可用信息。

# 视频
标题：{video.get("title", "")}
UP主：{video.get("owner", "")} ｜ 时长：{video.get("duration", 0)}秒 ｜ 播放：{video.get("view", 0)}
简介：{(video.get("desc") or "(无)")[:400]}

# 字幕（{"共 " + str(len(lines)) + " 条，语种 " + str(subtitle.get("lang") or "未知") if has_sub else "未获取到 —— 请看下方限制"})
{sub_text if has_sub else "（没有字幕。B站 的字幕接口需要登录 cookie，本次未授权。）"}

# 弹幕爆发点（弹幕突然变多 = 观众觉得精彩的位置）
{chr(10).join(burst_lines) if burst_lines else "  (无)"}

# 热门评论
{chr(10).join(f"  [{c.get('like', 0)}赞] {str(c.get('message', ''))[:120]}" for c in comments[:10]) or "  (无)"}

# 输出要求
只输出一个 JSON 对象，不要解释、不要 markdown 代码块：

{{
  "one_line": "一句话说清这个视频在讲什么（30字内）",
  "key_points": [
    {{"time": "3:20", "point": "第一个要点（20-40字）"}},
    {{"time": "12:05", "point": "第二个要点"}}
  ],
  "audience": "适合谁看（15字内）",
  "verdict": "值得看|一般|不值得",
  "reliability": 0.0-1.0,
  "note": "若信息不足，在这里说明缺什么；信息充分则留空串"
}}

规则：
1. key_points 给 3-5 条，按视频时间顺序排列。
2. **time 必须来自字幕里的真实时间戳**；没有字幕就不要编时间，time 填 ""。
3. {"字幕可用，reliability 可以给 0.7 以上。" if has_sub else '没有字幕、只有弹幕和评论时，reliability 必须低于 0.4，并在 note 里说明"未获取到字幕，结论仅基于标题和观众反应"。'}
4. 不要复述标题，要说清"实际内容"。
"""
