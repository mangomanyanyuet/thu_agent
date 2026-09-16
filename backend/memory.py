"""记忆层：长期画像 + 会话摘要 + 风险事实。

对应报告 §2.3 的「混合范式」：
  - 单次会话：全量重放 + 滑动窗口 + 摘要节点
  - 跨天长期：事实抽取 + 增量合并进 user_profile
  - 安全红线：risk_flags 永不压缩、永不摘要，每轮硬注入
"""
from __future__ import annotations

from config import get_settings
from models import ExtractedFacts, UserProfile
from storage import (
    get_profile,
    list_daily_records,
    list_risk_flags,
    list_scale_scores,
    merge_list_field,
)

# ------------------------------------------------------------------ 长期画像
def load_profile(user_id: str) -> UserProfile:
    return get_profile(user_id)


def render_profile(profile: UserProfile) -> str:
    """把画像渲染成可注入上下文的紧凑文本。"""
    if not any([profile.personality_traits, profile.coping_style, profile.social_support,
                profile.known_triggers, profile.events, profile.preferences]):
        return "（尚无长期画像，本轮请多了解用户的生活背景与支持系统）"
    lines = []
    if profile.known_triggers:
        lines.append("已知触发源：" + "、".join(profile.known_triggers[-6:]))
    if profile.social_support:
        lines.append("社会支持：" + "、".join(profile.social_support[-6:]))
    if profile.events:
        lines.append("近期事件：" + "、".join(profile.events[-6:]))
    if profile.coping_style:
        lines.append("应对方式：" + "、".join(profile.coping_style[-5:]))
    if profile.preferences:
        lines.append("沟通偏好：" + "、".join(profile.preferences[-5:]))
    if profile.personality_traits:
        lines.append("人格特质：" + "、".join(personality_traits := profile.personality_traits[-5:]))
    return "\n".join(lines)


def write_facts(user_id: str, facts: ExtractedFacts) -> None:
    """事实抽取后的增量写入（去重 + 截断）。"""
    if facts.profile_facts:
        merge_list_field(user_id, "social_support", facts.profile_facts, cap=30)
    if facts.triggers:
        merge_list_field(user_id, "known_triggers", facts.triggers, cap=30)
    if facts.events:
        merge_list_field(user_id, "events", facts.events, cap=20)
    if facts.preferences:
        merge_list_field(user_id, "preferences", facts.preferences, cap=10)
    # 人格特质/应对方式是最慢的变量，cap 给小值：它们不该频繁变动，
    # 堆太多会让噪声盖过真正稳定的信号（render_profile 只展示后 5 条）。
    if facts.personality_traits:
        merge_list_field(user_id, "personality_traits", facts.personality_traits, cap=8)
    if facts.coping_style:
        merge_list_field(user_id, "coping_style", facts.coping_style, cap=8)


# ------------------------------------------------------------------ 结构化上下文
def build_scale_trend(user_id: str) -> str:
    """量表趋势：趋势比绝对值更有临床意义。"""
    out = []
    for name in ("PHQ-9", "GAD-7", "PSS-4"):
        hist = list_scale_scores(user_id, name, limit=30)
        if not hist:
            continue
        last = hist[-1]
        arrow = "↑" if last.delta_vs_last > 0 else ("↓" if last.delta_vs_last < 0 else "→")
        out.append(
            f"{name}: {last.total_score}({last.severity}) {arrow}{last.delta_vs_last:+d} "
            f"vs上次, 基线差 {last.delta_vs_baseline:+d}"
        )
    return "\n".join(out) if out else "（暂无量表记录）"


def load_risk_context(user_id: str, limit: int = 5) -> str:
    """风险上下文：永不压缩，原话保留。"""
    flags = list_risk_flags(user_id=user_id, min_level=1, limit=limit)
    if not flags:
        return "（无历史风险记录）"
    lines = []
    for f in reversed(flags):
        handled = "已处理" if f.handled else "待处理"
        lines.append(f"- [{f.level}/{f.source}/{handled}] {f.evidence[:80]}（{f.ts}）")
    return "\n".join(lines)


def persist_daily_from_chat(
    user_id: str, text: str, emotion: dict | None = None, risk_level: str = ""
) -> None:
    """把普通文字对话沉淀为当日记录。

    为什么需要：daily_record 原来只有「陪伴会话 /finish」和「App 主动上报」
    两个写入点，纯文字聊天的用户永远没有每日记录 —— 于是
    `load_daily_context` 恒返回「（暂无每日记录）」，注入 prompt 的
    「今日与近日多模态观察」永远是空的，纵向个性化（跟自己前几天比）
    在主力场景下完全失效。

    同日多次对话合并为一条（source=chat），不堆多条。
    摘要走规则生成，不额外增加一次 LLM 调用。
    """
    from datetime import date

    from models import DailyRecord
    from storage import add_daily_record, find_daily_record, update_daily_record

    if not text.strip():
        return
    day = date.today().isoformat()
    emo = emotion or {}
    label = str(emo.get("primary_emotion") or "").strip()

    rec = find_daily_record(user_id, day, source="chat")
    na = dict((rec.narrative_analysis if rec else None) or {})
    na["source"] = "chat"

    turns = int(na.get("turns", 0)) + 1
    # 主导情绪：取当日出现次数最多的标签
    dist = dict(na.get("emotion_distribution") or {})
    if label:
        dist[label] = dist.get(label, 0) + 1
    dominant = max(dist, key=dist.get) if dist else ""

    _ORDER = ["none", "low", "moderate", "high", "imminent"]
    risk = str(na.get("risk_level") or "none")
    if risk_level in _ORDER and _ORDER.index(risk_level) > _ORDER.index(risk):
        risk = risk_level

    # 累积用户原话，但要封顶，避免长会话把记录撑爆
    _MAX = 2000
    prev_t = (rec.transcript if rec else "") or ""
    transcript = (prev_t + ("\n" if prev_t else "") + text.strip())[-_MAX:]

    na.update(turns=turns, emotion_distribution=dist, risk_level=risk)

    summary = f"对话 {turns} 轮"
    if dominant:
        summary += f"；主导情绪 {dominant}"
    if risk != "none":
        summary += f"；最高风险 {risk}"

    if rec is not None:
        rec.transcript = transcript
        rec.narrative = summary
        rec.narrative_analysis = na
        rec.modality_summary = summary
        update_daily_record(rec)
    else:
        add_daily_record(DailyRecord(
            id=None, user_id=user_id, date=day,
            transcript=transcript, narrative=summary,
            narrative_analysis=na, modality_summary=summary,
        ))


def load_daily_context(user_id: str) -> tuple[str, bool]:
    """最近几天的多模态摘要。"""
    recs = list_daily_records(user_id, limit=3)
    if not recs:
        return "（暂无每日记录）", False
    lines = []
    inconsistent = False
    for r in recs:
        lines.append(f"- {r.date}: {r.modality_summary[:160]}")
        if r.inconsistency:
            inconsistent = True
            lines.append("  ⚠ 检测到自述与表情/语音不一致，可能存在掩饰，请谨慎探询")
    return "\n".join(lines), inconsistent


# ------------------------------------------------------------------ 风险趋势
# 陪伴会话用自己的三级（none/watch/high），与主线五级对齐后才可比较
_COMPANION_LEVEL = {"none": 0, "watch": 1, "high": 3}
_LEVEL_NUM = {"none": 0, "low": 1, "moderate": 2, "high": 3, "imminent": 4}
_NUM_NAME = ["none", "low", "moderate", "high", "imminent"]


def _level_num(level: str) -> int:
    if level in _COMPANION_LEVEL:
        return _COMPANION_LEVEL[level]
    return _LEVEL_NUM.get(level, 0)


def build_risk_trend(user_id: str, days: int = 14) -> dict:
    """跨天风险趋势：把每日记录与风险事件按天聚合，看走向而非单点。

    返回：
        series     —— [{date, level, level_num, evidence, sources}]，按日期升序
        latest     —— 最近一天等级名
        trend      —— up / flat / down（近 3 天线性方向）
        peak       —— 区间内最高等级
        high_days  —— 区间内 high 及以上的天数
        consecutive—— 当前连续 high 及以上天数（0 表示最近一天不是）
    """
    recs = list_daily_records(user_id, limit=days)
    flags = [f for f in list_risk_flags(user_id=user_id, min_level=1, limit=200)]

    # 按天归集：一天可能既有陪伴记录又有风险事件，取最大值（只升不降）
    by_day: dict[str, dict] = {}
    for r in recs:
        na = r.narrative_analysis or {}
        lvl = na.get("risk_level") or "none"
        by_day[r.date] = {
            "date": r.date,
            "level": "none",
            "level_num": 0,
            "sources": [],
            "evidence": [],
            "dominant_emotion": na.get("dominant_emotion") or "",
        }
        _merge_day(by_day[r.date], lvl, "companion",
                   "; ".join(na.get("risk_evidence") or []))

    for f in flags:
        d = str(f.ts or "")[:10]
        if not d:
            continue
        if d not in by_day:
            by_day[d] = {"date": d, "level": "none", "level_num": 0,
                         "sources": [], "evidence": [], "dominant_emotion": ""}
        _merge_day(by_day[d], f.level, f.source, f.evidence)

    series = sorted(by_day.values(), key=lambda x: x["date"])
    if not series:
        return {"series": [], "latest": "none", "trend": "flat", "peak": "none",
                "high_days": 0, "consecutive_high_days": 0, "days": 0}

    # 近 3 天走向
    tail = series[-3:]
    trend = "flat"
    if len(tail) >= 2:
        d = tail[-1]["level_num"] - tail[0]["level_num"]
        trend = "up" if d > 0 else ("down" if d < 0 else "flat")

    peak = max(s["level_num"] for s in series)
    high_days = sum(1 for s in series if s["level_num"] >= 3)

    consecutive = 0
    for s in reversed(series):
        if s["level_num"] >= 3:
            consecutive += 1
        else:
            break

    return {
        "series": series,
        "latest": _NUM_NAME[series[-1]["level_num"]],
        "trend": trend,
        "peak": _NUM_NAME[peak],
        "high_days": high_days,
        "consecutive_high_days": consecutive,
        "days": len(series),
    }


def _merge_day(day: dict, level: str, source: str, evidence: str) -> None:
    n = _level_num(level)
    if n > day["level_num"]:
        day["level_num"] = n
        day["level"] = level if level in _NUM_NAME else _NUM_NAME[n]
    if source and source not in day["sources"]:
        day["sources"].append(source)
    if evidence and evidence not in day["evidence"]:
        day["evidence"].append(evidence)


def render_risk_trend(t: dict) -> str:
    """把风险趋势渲染成可注入上下文的一行文本。"""
    if not t.get("series"):
        return "（暂无风险趋势数据）"
    arrow = {"up": "上升 ↑", "flat": "平稳 →", "down": "下降 ↓"}.get(t["trend"], "")
    bits = [
        f"近 {t['days']} 天：当前 {t['latest']}，趋势{arrow}，"
        f"区间峰值 {t['peak']}"
    ]
    if t["high_days"]:
        bits.append(f"high 及以上 {t['high_days']} 天")
    if t["consecutive_high_days"] >= 2:
        bits.append(f"⚠ 已连续 {t['consecutive_high_days']} 天处于 high 及以上")
    # 最近 3 天的明细（含原话片段，供模型判断语境）
    recent = t["series"][-3:]
    detail = "；".join(
        f"{s['date']}:{s['level']}" + (f"（{(s['evidence'] or [''])[0][:40]}）" if s["evidence"] else "")
        for s in recent
    )
    return "，".join(bits) + "。最近：" + detail


def load_companion_context(user_id: str, limit: int = 5) -> str:
    """近期陪伴会话摘要（纵向个性化：用户最近几次都聊了什么、情绪如何）。"""
    recs = list_daily_records(user_id, limit=limit)
    comps = [r for r in recs
             if (r.narrative_analysis or {}).get("source") == "companion"]
    if not comps:
        return "（暂无历史陪伴会话）"
    lines = []
    for r in comps:
        na = r.narrative_analysis or {}
        bits = [f"- {r.date}"]
        if na.get("dominant_emotion"):
            bits.append(f"主导情绪 {na['dominant_emotion']}")
        if na.get("avg_valence") is not None:
            bits.append(f"愉悦度 {na['avg_valence']:+.2f}")
        if na.get("themes"):
            bits.append("主题：" + "、".join(na["themes"][:3]))
        if na.get("risk_level") and na["risk_level"] != "none":
            bits.append(f"风险 {na['risk_level']}")
        lines.append(" ".join(bits))
    return "\n".join(lines)


# ------------------------------------------------------------------ 摘要压缩
SUMMARY_PROMPT = """你是心理健康对话的会话记录员。请把"已有的历史摘要"与"新增对话"合并成一段新的摘要。

硬性要求：
1. 必须原样保留所有与自杀/自伤/绝望/告别相关的原话，不得改写、不得省略、不得概括。
2. 保留：用户提到的关键人物、事件、时间、承诺过的行动、会谈进行到哪一步。
3. 用中文，第三人称，不超过 {max_chars} 字。
4. 不添加原文没有的信息，不做临床诊断。
"""


def summarize_history(prev_summary: str, new_messages: list[dict[str, str]],
                      max_chars: int | None = None) -> str:
    """滚动摘要。风险相关内容通过 prompt 硬约束保留；
    同时 risk_flags 已独立落库双保险，摘要丢了也不影响安全判定。"""
    if not new_messages:
        return prev_summary
    max_chars = max_chars or 600
    from llm import complete

    conv = "\n".join(f"{m['role']}: {m['content']}" for m in new_messages)
    prompt = (
        f"已有摘要：\n{prev_summary or '（无）'}\n\n"
        f"新增对话：\n{conv}\n\n请输出合并后的新摘要："
    )
    try:
        out = complete(SUMMARY_PROMPT.format(max_chars=max_chars),
                       [{"role": "user", "content": prompt}])
        return out.strip()[: max_chars * 2]
    except Exception:  # noqa: BLE001
        return (prev_summary + "\n" + conv)[-max_chars * 2 :]


def context_budget_report(ctx: str) -> dict[str, int]:
    """简单的上下文预算统计，便于前端展示与调优。"""
    s = get_settings()
    return {
        "chars": len(ctx),
        "approx_tokens": len(ctx) // 3,
        "limit_chars": s.ctx_max_chars,
        "usage_pct": min(100, int(len(ctx) / max(s.ctx_max_chars, 1) * 100)),
    }
