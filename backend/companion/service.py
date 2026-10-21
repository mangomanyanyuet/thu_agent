"""陪伴会话的服务层：内存会话表 + 落库。"""
from __future__ import annotations

import time
from threading import Lock

from models import RiskFlag, now_iso
from storage import add_risk_flag
from tracer import phase as trace_phase

from .engine import CompanionEngine
from .state import Phase, SessionState

_lock = Lock()
_sessions: dict[str, SessionState] = {}
_engines: dict[str, CompanionEngine] = {}


def create_session(user_id: str) -> SessionState:
    sid = f"cmp_{int(time.time() * 1000) % 10**8:08d}"
    st = SessionState(sid, user_id)
    with _lock:
        _sessions[sid] = st
        _engines[sid] = CompanionEngine(st)
    return st


def get(sid: str) -> SessionState | None:
    return _sessions.get(sid)


def engine_of(sid: str) -> CompanionEngine | None:
    return _engines.get(sid)


def drop(sid: str) -> None:
    with _lock:
        _sessions.pop(sid, None)
        _engines.pop(sid, None)


def set_consent(sid: str, consent: dict) -> SessionState:
    st = _sessions[sid]
    trace_phase(sid, st.phase.value, Phase.QUALITY.value, reason="知情同意已确认")
    st.consent = consent
    st.phase = Phase.QUALITY
    if not consent.get("video", True):
        st.note_degradation("用户未授权摄像头，已降级为纯语音模式")
    if not consent.get("audio", True):
        st.note_degradation("用户未授权麦克风，已降级为纯文本模式")
    return st


def set_quality(sid: str, quality: dict) -> SessionState:
    """PRD §6.3.2 质量前置检测 / §6.4 降级。"""
    st = _sessions[sid]
    st.quality = quality
    if not quality.get("face_visible"):
        st.note_degradation("启动时未检测到人脸，已降级为纯语音模式")
    elif quality.get("low_light"):
        st.note_degradation("光线不足，面部识别置信度下降")
    if quality.get("multi_person"):
        st.note_degradation("检测到多人入镜，已降级为纯语音模式")
    trace_phase(sid, st.phase.value, Phase.OPENING.value,
                reason=f"质检完成（人脸={quality.get('face_visible')}，"
                       f"低光={quality.get('low_light')}，多人={quality.get('multi_person')}）")
    st.phase = Phase.OPENING
    # 开场白随本次响应一起送达用户，静默计时从这里起算：
    # 否则用户从头到尾一句话都没说时，seconds_since_user() 会一直停在会话创建时刻。
    st.mark_activity()
    return st


def persist_risk(st: SessionState) -> None:
    """风险事件落库（与主线 risk_flag 表共用）。"""
    if st.risk_level == "none" or not st.risk_evidence:
        return
    for ev in st.risk_evidence:
        add_risk_flag(RiskFlag(
            id=None, user_id=st.user_id, session_id=st.session_id,
            ts=now_iso(), source="companion", evidence=ev[:200],
            level="high" if st.risk_level == "high" else "low",
            cssrs_dimension="",
        ))


# ------------------------------------------------------------------ 会话收尾
# 存 sid -> 收尾结果。前端可能在报告页反复刷新/重进，重复调用必须返回
# **同一个结果**（含 daily_record），否则前端第二次拿到空记录。
_finalized: dict[str, dict] = {}


def finalize_session(sid: str) -> dict:
    """会话结束：一次做完「沉淀 + 更新画像 + 更新趋势」。

    陪伴会话是唯一输入入口，所以收尾时要把这一轮的产出写进所有下游：

    1. **daily_record** —— 当日记录（自述 + 多模态聚合 + 风险等级）
    2. **risk_flag**    —— 风险事件（永不压缩）
    3. **user_profile** —— 事实抽取，增量更新长期画像
    4. **baseline_emotion** —— 情绪基线（用于判断"相对恶化"）

    幂等：同一 sid 重复调用不会重复落库（前端可能在报告页反复刷新）。
    """
    st = _sessions.get(sid)
    if st is None:
        raise KeyError(sid)
    eng = _engines.get(sid)
    if eng is None:
        raise KeyError(sid)

    report = eng.build_report()

    if sid in _finalized:
        from memory import build_risk_trend, render_risk_trend

        cached = _finalized[sid]
        # 趋势会随后续会话变化，这里用最新趋势刷新，其余保持首次结果不变
        cached["risk_trend"] = build_risk_trend(st.user_id)
        cached["risk_trend_text"] = render_risk_trend(cached["risk_trend"])
        return cached

    # 1) 风险落库
    persist_risk(st)

    # 2) 沉淀为当日记录（同一天多次陪伴 -> 合并，不堆多条）
    from storage import add_daily_record, find_daily_record, update_daily_record
    from .daily_bridge import merge_daily_record, session_to_daily_record

    rec = session_to_daily_record(st, report)
    prev = find_daily_record(st.user_id, rec.date, source="companion")
    if prev is not None:
        rec = merge_daily_record(prev, rec)
        update_daily_record(rec)
    else:
        rec.id = add_daily_record(rec)

    # 3) 事实抽取 -> 长期画像（无安全语义，可关思考提速）
    facts_dict = _extract_and_write_facts(st)

    # 4) 情绪基线：指数滑动平均，保证"相对恶化"有个参照系
    baseline = _update_baseline(st, report)

    import tracer

    tracer.memory_write(sid, st.user_id, facts_dict, baseline)
    tracer.log("session.finalize",
               f"会话收尾：落每日记录 #{getattr(rec, 'id', '?')}"
               f"，风险 {st.risk_level}，轮次 {st.turn_count}",
               session_id=sid, user_id=st.user_id,
               daily_record_id=getattr(rec, "id", None),
               risk_level=st.risk_level, turn_count=st.turn_count,
               baseline=baseline)

    result = _pack_result(st, report, rec, facts_dict, baseline)
    _finalized[sid] = result
    return result


def _pack_result(st: SessionState, report: dict, rec,
                 facts: dict | None = None, baseline: dict | None = None) -> dict:
    from memory import build_risk_trend, render_risk_trend
    trend = build_risk_trend(st.user_id)
    return {
        "session_id": st.session_id,
        "user_id": st.user_id,
        "report": report,
        "daily_record": rec.__dict__ if rec else None,
        "facts": facts or {},
        "baseline_emotion": baseline or {},
        "risk_trend": trend,
        "risk_trend_text": render_risk_trend(trend),
    }


def _extract_and_write_facts(st: SessionState) -> dict:
    """从整段会话里抽长期事实，写进 user_profile。

    用整段对话而不是单轮 —— 单轮信息太碎，容易抽出噪声事实。
    """
    from llm import structured
    from memory import write_facts
    from models import ExtractedFacts

    conv = "\n".join(
        f"用户：{t.get('text', '')}" for t in st.turns if t.get("text")
    ).strip()
    if not conv:
        return {}

    sys_ = (
        "你是记忆抽取器。从这段对话中抽取长期有效的事实，只输出 JSON："
        "profile_facts(稳定的生活事实/重要他人/社会支持), triggers(触发源), "
        "events(近期事件), preferences(沟通偏好)。"
        "只抽取原文明确提到的内容，不要推断。没有就给空数组。"
    )
    try:
        facts = structured(ExtractedFacts, sys_, conv, thinking=False)
    except Exception:  # noqa: BLE001
        return {}
    write_facts(st.user_id, facts)
    return facts.model_dump()


def _update_baseline(st: SessionState, report: dict) -> dict:
    """更新情绪基线（valence / arousal 的指数滑动平均）。

    报告 §7.2：判断「相对他自己的恶化」比绝对数值更有意义，
    而相对需要一条基线。新样本权重 0.3，避免单日波动把基线带跑。
    """
    from storage import get_profile, upsert_profile

    t2 = report.get("tier2_detail") or {}
    v = t2.get("avg_valence")
    a = t2.get("avg_arousal")
    if v is None and a is None:
        return {}

    p = get_profile(st.user_id)
    base = dict(p.baseline_emotion or {})
    alpha = 0.3
    if v is not None:
        base["valence"] = round(base.get("valence", v) * (1 - alpha) + v * alpha, 3)
    if a is not None:
        base["arousal"] = round(base.get("arousal", a) * (1 - alpha) + a * alpha, 3)
    base["samples"] = int(base.get("samples", 0)) + 1
    p.baseline_emotion = base
    upsert_profile(p)
    return base
