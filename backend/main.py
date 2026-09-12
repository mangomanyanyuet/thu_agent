"""FastAPI 服务入口。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from config import get_settings  # noqa: E402
from mini import router as mini_router  # noqa: E402
from schemas import (  # noqa: E402
    ChatRequest,
    CompanionConsent,
    CompanionQuality,
    CompanionStart,
    CompanionTurn,
    DailyReportRequest,
    ProfileUpdate,
    ReviewRequest,
    ScaleSubmitRequest,
)

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 小程序适配层：同步接口 + 会话自愈 + 聚合配置（详见 mini.py 文档字符串）
app.include_router(mini_router)


# ------------------------------------------------------------------ 对话
@app.post(f"{settings.api_prefix}/chat")
def chat(req: ChatRequest):
    import graph
    import storage

    sid = req.session_id or graph.new_session(req.user_id)
    if not storage.get_session(sid):
        raise HTTPException(404, "session 不存在")

    result = graph.run_turn(req.user_id, sid, req.text,
                            voice=req.voice.model_dump() if req.voice else None)
    return {"session_id": sid, **result}


@app.post(f"{settings.api_prefix}/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE 流式：先推元数据（风险/情绪），再逐字推回复。

    注意：安全语义要求「先判定、后生成」，所以风险分级结果在首个事件就下发，
    前端可在风险高时立即展示热线卡片，不必等正文流完。
    """
    import graph
    import storage

    sid = req.session_id or graph.new_session(req.user_id)
    if not storage.get_session(sid):
        raise HTTPException(404, "session 不存在")

    result = graph.run_turn(req.user_id, sid, req.text,
                            voice=req.voice.model_dump() if req.voice else None)

    def gen():
        import json
        meta = {k: v for k, v in result.items() if k != "reply"}
        yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
        for ch in result.get("reply", ""):
            yield f"event: delta\ndata: {json.dumps({'t': ch}, ensure_ascii=False)}\n\n"
        yield f"event: done\ndata: {json.dumps({'session_id': sid}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get(f"{settings.api_prefix}/asr/status")
def asr_status():
    """ASR 可用性（前端据此决定麦克风按钮是否可用）。"""
    import asr
    return {"available": asr.asr_available(), "model": asr.ASR_MODEL_SIZE}


@app.post(f"{settings.api_prefix}/asr")
async def asr_transcribe(file: UploadFile = File(...), language: str = "zh"):
    """语音 -> 转写文本 + 声学特征 + 语气判定。

    注意：转写失败时 text 为空，但声学特征与语气仍会返回 ——
    语气是风险判定的多模态输入之一，不应因为 ASR 挂掉而丢失。
    """
    import asr

    data = await file.read()
    if not data:
        raise HTTPException(400, "音频为空")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "音频过大（上限 25MB）")
    try:
        return asr.transcribe(data, language=language)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"语音处理失败：{exc}") from exc


@app.post(f"{settings.api_prefix}/sessions")
def create_session(user_id: str, cbt_stage: str = "none"):
    import graph
    return {"session_id": graph.new_session(user_id, cbt_stage)}


@app.get(f"{settings.api_prefix}/sessions/{{user_id}}")
def sessions_of(user_id: str):
    from storage import list_sessions
    return [s.__dict__ for s in list_sessions(user_id)]


@app.get(f"{settings.api_prefix}/messages/{{session_id}}")
def messages_of(session_id: str):
    from storage import list_messages
    return [m.__dict__ for m in list_messages(session_id)]


# ------------------------------------------------------------------ 人工审核（HITL）
@app.get(f"{settings.api_prefix}/review/pending")
def pending_reviews(min_level: int = 2):
    """待处理风险事件队列（人工审核台数据源）。"""
    from storage import list_risk_flags
    return [f.__dict__ for f in list_risk_flags(unhandled_only=True, min_level=min_level, limit=100)]


@app.post(f"{settings.api_prefix}/review")
def submit_review(req: ReviewRequest):
    """恢复被 interrupt 挂起的会话。"""
    import graph
    from storage import list_risk_flags, mark_risk_handled

    payload = {"action": req.action}
    if req.action == "override":
        payload["reply"] = req.reply

    result = graph.run_turn("", req.session_id, "", resume_payload=payload)

    for f in list_risk_flags(unhandled_only=True, min_level=2, limit=50):
        if f.session_id == req.session_id:
            mark_risk_handled(f.id, req.reviewer, f"{req.action}: {req.reply[:100]}")

    return result


@app.post(f"{settings.api_prefix}/risk/{{flag_id}}/handle")
def handle_flag(flag_id: int, reviewer: str = "human", note: str = ""):
    from storage import mark_risk_handled
    mark_risk_handled(flag_id, reviewer, note)
    return {"ok": True}


# ------------------------------------------------------------------ 每日上报（App 侧）
@app.post(f"{settings.api_prefix}/daily")
def report_daily(req: DailyReportRequest):
    from datetime import date

    import perception
    import storage
    from models import DailyRecord

    day = req.date or date.today().isoformat()
    rec = perception.build_daily_record(req.user_id, day, req.video_uri,
                                        req.transcript, req.narrative,
                                        voice=req.voice.model_dump() if req.voice else None)
    rid = storage.add_daily_record(rec)

    # 多模态通道发现风险 -> 直接落 flag（不等对话）
    lvl, ev = perception.multimodal_risk_signal(rec)
    # 语音语气信号单独再算一次（可能比视频桩函数给出的等级更高）
    if req.voice:
        v_lvl, v_ev = perception.voice_risk_signal(
            req.voice.tone, req.voice.audio_features, req.narrative)
        if v_lvl > lvl:
            lvl, ev = v_lvl, v_ev
    if lvl > 0:
        from models import RiskFlag, now_iso
        level_name = ["none", "low", "moderate", "high", "imminent"][lvl]
        storage.add_risk_flag(RiskFlag(
            id=None, user_id=req.user_id, session_id="daily", ts=now_iso(),
            source="multimodal", evidence=ev, level=level_name,
        ))
    return {"id": rid, "record": rec.__dict__, "risk_level": lvl, "risk_evidence": ev}


@app.get(f"{settings.api_prefix}/daily/{{user_id}}")
def daily_of(user_id: str, limit: int = 30):
    from storage import list_daily_records
    return [r.__dict__ for r in list_daily_records(user_id, limit)]


# ------------------------------------------------------------------ 量表
@app.get(f"{settings.api_prefix}/scales")
def list_scales():
    from perception import SCALES
    return {k: {"name": v["name"], "items": v["items"], "max": v["max"]} for k, v in SCALES.items()}


@app.post(f"{settings.api_prefix}/scales")
def submit_scale(req: ScaleSubmitRequest):
    import perception
    import storage

    try:
        sc = perception.score_scale(req.user_id, req.scale_name, req.item_scores, req.date)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    storage.add_scale_score(sc)

    # 量表通道风险硬触发
    from models import RiskFlag, now_iso
    lvl, ev = perception.scale_risk_signal(req.scale_name, req.item_scores)
    if lvl > 0:
        storage.add_risk_flag(RiskFlag(
            id=None, user_id=req.user_id, session_id="scale", ts=now_iso(),
            source="scale_item", evidence=ev,
            level=["none", "low", "moderate", "high", "imminent"][lvl],
        ))
    return {"score": sc.__dict__, "risk_level": lvl, "risk_evidence": ev}


@app.get(f"{settings.api_prefix}/scales/{{user_id}}")
def scales_of(user_id: str, scale_name: str | None = None):
    from storage import list_scale_scores
    return [s.__dict__ for s in list_scale_scores(user_id, scale_name)]


# ------------------------------------------------------------------ 画像 / 概览
@app.get(f"{settings.api_prefix}/profile/{{user_id}}")
def profile_of(user_id: str):
    from memory import render_profile
    from storage import get_profile
    p = get_profile(user_id)
    return {"profile": p.__dict__, "rendered": render_profile(p)}


@app.post(f"{settings.api_prefix}/profile")
def update_profile(req: ProfileUpdate):
    from storage import get_profile, upsert_profile
    p = get_profile(req.user_id)
    if req.nickname is not None:
        p.nickname = req.nickname
    for f in ("personality_traits", "coping_style", "social_support"):
        v = getattr(req, f)
        if v is not None:
            setattr(p, f, v)
    upsert_profile(p)
    return {"ok": True, "profile": p.__dict__}


@app.get(f"{settings.api_prefix}/overview/{{user_id}}")
def overview(user_id: str):
    """用户全景：情绪轨迹 + 量表趋势 + 风险事件 + 最近记录。给前端看板用。"""
    import memory
    import perception
    from storage import (
        get_profile,
        list_daily_records,
        list_risk_flags,
        list_scale_scores,
    )

    profile = get_profile(user_id)
    scales: dict[str, list[dict]] = {}
    for name in ("PHQ-9", "GAD-7", "PSS-4"):
        scales[name] = [s.__dict__ for s in list_scale_scores(user_id, name, limit=30)]

    trend = memory.build_risk_trend(user_id)

    return {
        "user_id": user_id,
        "profile": profile.__dict__,
        "profile_rendered": memory.render_profile(profile),
        "scale_trend": memory.build_scale_trend(user_id),
        "scales": scales,
        "risk_flags": [f.__dict__ for f in list_risk_flags(user_id=user_id, min_level=1, limit=20)],
        "risk_trend": trend,
        "risk_trend_text": memory.render_risk_trend(trend),
        "companion_history": memory.load_companion_context(user_id),
        "daily_records": [r.__dict__ for r in list_daily_records(user_id, limit=14)],
        "hotlines": perception.hotlines(),
    }


@app.get(f"{settings.api_prefix}/traces/{{session_id}}")
def traces(session_id: str):
    from storage import list_traces
    return list_traces(session_id)


@app.get(f"{settings.api_prefix}/health")
def health():
    return {"ok": True, "llm_provider": settings.llm_provider,
            "checkpointer": settings.checkpointer}


# ================================================================== 陪伴会话
@app.post(f"{settings.api_prefix}/companion/start")
def companion_start(req: CompanionStart):
    """创建一次 5 分钟陪伴会话。"""
    import companion.service as cs
    st = cs.create_session(req.user_id)
    return {"session_id": st.session_id, "phase": st.phase.value}


@app.post(f"{settings.api_prefix}/companion/{{session_id}}/consent")
def companion_consent(session_id: str, req: CompanionConsent):
    """PRD §8 知情同意。三项必填未确认则拒绝开始。"""
    import companion.service as cs
    if session_id not in cs._sessions:
        raise HTTPException(404, "会话不存在")
    if not (req.understood_ai and req.consent_media and req.adult_confirmed):
        raise HTTPException(400, "必须确认全部同意项后方可开始")
    st = cs.set_consent(session_id, req.model_dump())
    return {"ok": True, "phase": st.phase.value}


@app.post(f"{settings.api_prefix}/companion/{{session_id}}/quality")
def companion_quality(session_id: str, req: CompanionQuality):
    """PRD §6.3.2 质量前置检测，返回开场白。"""
    import companion.service as cs
    if session_id not in cs._sessions:
        raise HTTPException(404, "会话不存在")
    st = cs.set_quality(session_id, req.model_dump())
    eng = cs.engine_of(session_id)
    return {
        "phase": st.phase.value,
        "opening": eng.opening_text(),
        "degradation_notes": st.degradation_notes,
    }


@app.post(f"{settings.api_prefix}/companion/{{session_id}}/turn")
def companion_turn(session_id: str, req: CompanionTurn):
    """提交一轮用户回答（文本 + 语音 + 面部），返回 Agent 回复。"""
    import companion.service as cs
    eng = cs.engine_of(session_id)
    if not eng:
        raise HTTPException(404, "会话不存在")

    audio = req.audio.model_dump() if req.audio else None
    if audio:
        audio = audio.get("audio_features") or {}
    face = req.face.model_dump() if req.face else None

    out = eng.respond(req.text, audio=audio, face=face)
    out["elapsed"] = round(eng.s.elapsed, 1)
    out["should_wrap_up"] = eng.s.should_wrap_up()
    if eng.s.risk_level != "none":
        cs.persist_risk(eng.s)
    return out


@app.get(f"{settings.api_prefix}/companion/{{session_id}}/silence")
def companion_silence(session_id: str):
    """PRD §6.1 沉默检测：>8s 返回一句主动确认话术。"""
    import companion.service as cs
    eng = cs.engine_of(session_id)
    if not eng:
        raise HTTPException(404, "会话不存在")
    return {"prompt": eng.silence_check(), "elapsed": round(eng.s.elapsed, 1)}


@app.get(f"{settings.api_prefix}/companion/{{session_id}}/report")
def companion_report(session_id: str):
    """PRD §9.2/9.3 三层报告（只读，不落库）。"""
    import companion.service as cs
    eng = cs.engine_of(session_id)
    if not eng:
        raise HTTPException(404, "会话不存在")
    return eng.build_report()


@app.post(f"{settings.api_prefix}/companion/{{session_id}}/finish")
def companion_finish(session_id: str):
    """会话收尾：沉淀每日记录 + 更新画像 + 更新风险趋势。

    陪伴会话是唯一输入入口，结束时把这一轮的产出一次性写进所有下游：
    daily_record / risk_flag / user_profile / baseline_emotion。
    幂等，重复调用不会重复落库。
    """
    import companion.service as cs
    if session_id not in cs._sessions:
        raise HTTPException(404, "会话不存在")
    try:
        return cs.finalize_session(session_id)
    except KeyError:
        raise HTTPException(404, "会话不存在") from None


@app.post(f"{settings.api_prefix}/companion/{{session_id}}/abort")
def companion_abort(session_id: str):
    """PRD §6.4 用户中途退出：保存部分快照，友好告别。"""
    import companion.service as cs
    from companion.service import drop
    from companion.state import Phase
    st = cs.get(session_id)
    if not st:
        raise HTTPException(404, "会话不存在")
    st.phase = Phase.ABORTED
    rep = cs.engine_of(session_id).build_report()
    drop(session_id)
    return {"message": "好的，今天先到这里。谢谢你愿意聊这些。", "partial_report": rep}


# 生产构建产物（frontend/dist）存在时直接托管，方便单端口演示
_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
