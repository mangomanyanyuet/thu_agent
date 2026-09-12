"""微信小程序适配层。

小程序与浏览器有两个关键差异，这一层就是为它们兜底的：

1. **没有 EventSource / SSE。** 小程序只能用 `wx.request({enableChunked:true})`
   手工解析分块流，iOS 尚可、Android 限制多。所以小程序一律走**同步接口**，
   SSE（`/api/chat/stream`）只留给 Web 端。
2. **移动端网络差、会话易失。** 客户端本地存的 session_id 可能因为服务端重启
   或换机而失效。同步接口必须能**自愈**——session 不存在就静默重建，
   而不是像 Web 端那样抛 404 打断对话。

另外 `bootstrap` 把首屏需要的配置（热线 / 量表定义 / ASR 可用性）合并成一次请求，
移动端一次 RTT 常常几百毫秒，能省一次是一次。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import get_settings
from schemas import VoiceMeta

router = APIRouter(prefix="/api/mini", tags=["miniprogram"])

DISCLAIMER = (
    "本服务由 AI 提供情绪陪伴与自助练习，不是医疗诊断，也不能替代专业治疗。"
    "如遇紧急情况，请立即联系下方求助渠道。"
)


class MiniChatRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    text: str = Field(min_length=1, max_length=4000)
    voice: VoiceMeta | None = None


class MiniSessionRequest(BaseModel):
    user_id: str
    cbt_stage: str = "none"


class ScaleSubmitMini(BaseModel):
    user_id: str
    scale_name: str
    item_scores: list[int]


def _ensure_session(user_id: str, session_id: str | None) -> str:
    """会话自愈：不存在就重建，避免小程序端拿到 404 后对话中断。"""
    import graph
    import storage

    if session_id and storage.get_session(session_id):
        return session_id
    return graph.new_session(user_id)


# ------------------------------------------------------------------ 首屏配置
@router.get("/bootstrap")
def bootstrap(user_id: str = "demo_user") -> dict[str, Any]:
    """首屏一次性拉取：热线、量表定义、ASR 可用性、免责声明。"""
    import asr
    import perception
    import storage
    from memory import render_profile

    s = get_settings()
    profile = storage.get_profile(user_id)
    return {
        "user_id": user_id,
        "disclaimer": DISCLAIMER,
        "hotlines": perception.hotlines(),
        "asr_available": asr.asr_available(),
        "llm_provider": s.llm_provider,
        "scales": {
            k: {"name": v["name"], "items": v["items"], "max": v["max"]}
            for k, v in perception.SCALES.items()
        },
        "profile_rendered": render_profile(profile),
        "latest_risk": storage.latest_risk_level(user_id),
    }


# ------------------------------------------------------------------ 会话
@router.post("/session")
def create_session(req: MiniSessionRequest) -> dict[str, Any]:
    import graph

    return {"session_id": graph.new_session(req.user_id, req.cbt_stage)}


@router.post("/chat")
def chat(req: MiniChatRequest) -> dict[str, Any]:
    """同步对话（小程序专用）。

    与 Web 端 `/api/chat` 的区别：
    - session 不存在时自愈重建，不抛 404；
    - 返回 `latency_ms`，便于在小程序端展示「思考中」的真实耗时；
    - 返回 `degraded`，模型不可用时前端要显式提示「当前为降级回复」。
    """
    import graph

    sid = _ensure_session(req.user_id, req.session_id)
    t0 = time.monotonic()
    try:
        result = graph.run_turn(
            req.user_id, sid, req.text,
            voice=req.voice.model_dump() if req.voice else None,
        )
    except Exception as exc:  # noqa: BLE001
        # 绝不让小程序端拿到一个裸 500：至少给兜底话术与热线（红线 4）
        import perception
        from graph import safe_fallback_reply
        from config import RISK_PATTERNS_HIGH, RISK_PATTERNS_IMMINENT, RISK_PATTERNS_MODERATE

        text = req.text
        level = 3 if any(p in text for p in RISK_PATTERNS_IMMINENT + RISK_PATTERNS_HIGH) \
            else 2 if any(p in text for p in RISK_PATTERNS_MODERATE) else 0
        return {
            "session_id": sid,
            "reply": safe_fallback_reply(level),
            "risk_level": ["none", "low", "moderate", "high", "imminent"][level],
            "hotlines": perception.hotlines(),
            "crisis_mode": level >= 3,
            "degraded": True,
            "error": str(exc),
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }

    return {
        "session_id": sid,
        **result,
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


# ------------------------------------------------------------------ 量表 / 概览
@router.post("/scales")
def submit_scale(req: ScaleSubmitMini) -> dict[str, Any]:
    import perception
    import storage
    from models import RiskFlag, now_iso

    try:
        sc = perception.score_scale(req.user_id, req.scale_name, req.item_scores)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    storage.add_scale_score(sc)

    lvl, ev = perception.scale_risk_signal(req.scale_name, req.item_scores)
    if lvl > 0:
        storage.add_risk_flag(RiskFlag(
            id=None, user_id=req.user_id, session_id="scale", ts=now_iso(),
            source="scale_item", evidence=ev,
            level=["none", "low", "moderate", "high", "imminent"][lvl],
        ))
    return {"score": sc.__dict__, "risk_level": lvl, "risk_evidence": ev}


@router.get("/overview")
def overview(user_id: str = "demo_user") -> dict[str, Any]:
    """看板：画像 + 量表趋势 + 风险事件 + 最近记录。"""
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


# ------------------------------------------------------------------ 语音（录音上传）
@router.get("/audio/formats")
def audio_formats() -> dict[str, Any]:
    """小程序端录音参数建议 + 服务端解码能力说明。

    `wx.getRecorderManager()` 只输出 mp3 / aac / pcm，不产出 Web 端的 webm/opus。
    pcm 体积过大（16k/16bit/单声道 ≈ 32KB/s），5 分钟约 9MB，不推荐走公网上传。
    """
    return {
        "recommended": {
            "format": "mp3",
            "sampleRate": 16000,
            "numberOfChannels": 1,
            "encodeBitRate": 48000,
            "frameSize": 1024,
            "maxDurationSec": 120,
        },
        "supported_decode": ["mp3", "aac", "wav", "webm", "ogg", "m4a", "flac"],
        "asr_model": "faster-whisper small",
        "note": "解码走 PyAV，不依赖系统 ffmpeg；ASR 不可用时仍返回声学特征与语气。",
    }


# ------------------------------------------------------------------ 人工审核（HITL）
@router.get("/review/pending")
def pending_reviews(min_level: int = 2) -> list[dict[str, Any]]:
    from storage import list_risk_flags

    return [f.__dict__ for f in list_risk_flags(unhandled_only=True, min_level=min_level, limit=100)]


@router.post("/review")
def submit_review(req: dict[str, Any]) -> dict[str, Any]:
    import graph
    from storage import list_risk_flags, mark_risk_handled

    sid = req.get("session_id", "")
    action = req.get("action", "approve")
    payload = {"action": action}
    if action == "override":
        payload["reply"] = req.get("reply", "")

    result = graph.run_turn("", sid, "", resume_payload=payload)

    reviewer = req.get("reviewer", "human")
    for f in list_risk_flags(unhandled_only=True, min_level=2, limit=50):
        if f.session_id == sid:
            mark_risk_handled(f.id, reviewer, f"{action}: {str(req.get('reply', ''))[:100]}")
    return result
