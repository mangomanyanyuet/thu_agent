"""让 LLM 在生成回复的同时，隐式输出本轮用户的文本情绪判定。

动机
----
原先文本情绪是关键词匹配（`fusion.TEXT_KEYWORDS`），问题：
- 覆盖不全（"我撑不住了""心里空落落的"这类表达一个词都命中不了）
- 无法识别反讽、否定、程度
- 与长期对话路径不一致（那边已经用 LLM 判定情绪了）

但单独再加一次 LLM 调用会让单轮延迟翻倍（实测约 +6s）。
所以改成**一次调用、两份产出**：模型先输出对用户说的话，再输出一段
机器可读的情绪判定，后端解析后只把前者展示给用户，后者进融合。

为什么不直接用 JSON 整个包住回复
--------------------------------
本项目的 vLLM 模型（`kaiwu-llm-model`）被 GRPO 训练成总爱输出 JSON，
且包裹 key 不固定（见 `llm.unwrap_reply`）。若要求整体 JSON，
回复文本容易被转义/截断。用 `<reply>` / `<emotion>` 标签分段更稳，
且解析失败时还能整段退回给用户（不至于把对话搞坏）。

兜底原则
--------
解析失败一律退回关键词匹配，**绝不因为拿不到情绪就中断对话**。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 与 fusion.EMOTION_PROTOTYPE 的中文标签保持一致
VALID_LABELS = ("平静", "愉悦", "焦虑", "低落", "烦躁", "疲惫", "紧张", "惊讶")

_REPLY_RE = re.compile(r"<reply>(.*?)</reply>", re.S | re.I)
_EMOTION_RE = re.compile(r"<emotion>(.*?)</emotion>", re.S | re.I)

# 输出规范：拼在 role prompt 后面
OUTPUT_SPEC = """

## 输出格式（严格遵守，这是硬性要求）

分两段输出。第一段是你要对用户说的话，第二段是对用户此刻情绪的判定，
第二段**不会展示给用户**，只用于系统分析。

<reply>
（这里只写你要对用户说的话，1-3 句，不要写分析、不要写括号说明）
</reply>
<emotion>
{{"label":"标签","valence":数值,"arousal":数值,"confidence":数值,"intensity":数值}}
</emotion>

字段说明：
- label：只能是 {labels} 之一，根据**用户这句话**体现的情绪判断
- valence：[-1, 1]，-1 极度消极，0 中性，1 极度积极
- arousal：[-1, 1]，-1 平静/低迷，0 适中，1 激动/亢奋
- confidence：[0, 1]，你对这个判断的把握
- intensity：[0, 10]，情绪强度

注意：
1. 两段标签必须成对出现，且 <emotion> 内是**合法 JSON**，不要加注释或多余文字。
2. 如果用户表达隐晦、反讽或前后矛盾，按你的最佳判断填，并降低 confidence。
3. <reply> 里绝不能出现 JSON、标签、分析过程。
""".replace("{labels}", "、".join(VALID_LABELS))


def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def normalize_emotion(d: dict) -> dict | None:
    """校验并规整情绪字段；不合法返回 None。"""
    if not isinstance(d, dict):
        return None
    label = str(d.get("label") or "").strip()
    # 模型偶尔输出英文或带空格，做一次宽松匹配
    if label not in VALID_LABELS:
        alt = {"neutral": "平静", "joy": "愉悦", "happy": "愉悦", "calm": "平静",
               "sadness": "低落", "sad": "低落", "anxiety": "焦虑", "anxious": "焦虑",
               "anger": "烦躁", "angry": "烦躁", "fear": "紧张", "tired": "疲惫",
               "surprise": "惊讶", "shame": "低落", "numbness": "平静"}
        label = alt.get(label.lower(), "")
        if not label:
            # 退而求其次：子串匹配
            for v in VALID_LABELS:
                if v in label:
                    label = v
                    break
    if label not in VALID_LABELS:
        return None
    return {
        "label": label,
        "valence": round(_clamp(d.get("valence"), -1.0, 1.0, 0.0), 3),
        "arousal": round(_clamp(d.get("arousal"), -1.0, 1.0, 0.0), 3),
        "confidence": round(_clamp(d.get("confidence"), 0.0, 1.0, 0.5), 3),
        "intensity": _clamp(d.get("intensity"), 0.0, 10.0, 5.0),
    }


def parse_reply_and_emotion(raw: str) -> tuple[str, dict | None]:
    """从模型输出里拆出「给用户的回复」和「情绪判定」。

    返回 (reply, emotion)。emotion 为 None 表示没解析到（调用方应兜底）。
    reply 保证是字符串（可能为空，由调用方再兜底）。
    """
    if not raw:
        return "", None

    text = raw.strip()

    reply_m = _REPLY_RE.search(text)
    reply = reply_m.group(1).strip() if reply_m else ""

    emotion: dict | None = None
    emo_m = _EMOTION_RE.search(text)
    if emo_m:
        blob = emo_m.group(1).strip()
        # 模型可能带代码围栏
        blob = re.sub(r"^```(?:json)?|```$", "", blob, flags=re.M).strip()
        try:
            emotion = normalize_emotion(json.loads(blob))
        except json.JSONDecodeError:
            # 围栏里可能还有前后缀文字，退一步取第一个 {...}
            i, j = blob.find("{"), blob.rfind("}")
            if i >= 0 and j > i:
                try:
                    emotion = normalize_emotion(json.loads(blob[i:j + 1]))
                except json.JSONDecodeError:
                    emotion = None

    if not reply:
        # 没拿到 <reply>：把 emotion 段和残留标签去掉，剩下的当回复
        rest = _EMOTION_RE.sub("", text)
        rest = _REPLY_RE.sub("", rest)
        rest = re.sub(r"</?(?:reply|emotion)>", "", rest, flags=re.I)
        reply = rest.strip()

    return reply, emotion
