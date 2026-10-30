"""FastAPI 服务入口。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, File, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

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
    SimRunRequest,
    SimTurnRequest,
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


# ------------------------------------------------------------------ 鉴权
# 白名单：这些路径不需要登录
AUTH_WHITELIST = (
    "/api/auth/",
    "/api/health",
    "/api/asr/status",
    "/docs", "/redoc", "/openapi.json",
)


def _current_session(request) -> dict | None:
    token = request.headers.get("x-auth-token") or request.query_params.get("token")
    if not token:
        return None
    import auth as auth_mod

    return auth_mod.verify_token(token)


def _json_body_user_id(raw: bytes) -> str | None:
    """从 JSON 请求体里取 user_id（POST 接口普遍把身份放在 body 里）。"""
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(obj, dict):
        v = obj.get("user_id")
        return str(v) if v else None
    return None


@app.middleware("http")
async def auth_middleware(request, call_next):
    """统一鉴权 + 越权拦截。

    两件事：
    1. 除白名单外，/api/* 必须带有效 token（401）。
    2. 请求里指定的 user_id 必须与登录身份一致（403）。

    第 2 条是必须的：只校验 token 不校验身份，登录 A 账号就能读 B 的数据
    （实测过：`/api/overview/u_demo` 用 alice 的 token 能拿到 demo 的全部
    概览 —— 这就是典型的越权/IDOR）。

    放在中间件而不是每个路由加依赖，是为了避免"新加接口忘记鉴权"。
    """
    from fastapi.responses import JSONResponse

    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if any(path.startswith(p) for p in AUTH_WHITELIST):
        return await call_next(request)
    if not get_settings().require_auth:
        return await call_next(request)

    session = _current_session(request)
    if session is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "未登录或登录已过期，请重新登录"},
        )
    request.state.user = session

    # ---- 越权检查：收集请求里出现的所有 user_id ----
    requested: list[str] = []
    q = request.query_params.get("user_id")
    if q:
        requested.append(q)
    # 路径里的 u_xxx（如 /api/overview/u_demo）
    requested += [s for s in path.split("/") if s.startswith("u_")]
    # 请求体里的 user_id
    raw = b""
    if request.method in ("POST", "PUT", "PATCH"):
        raw = await request.body()
        b = _json_body_user_id(raw)
        if b:
            requested.append(b)

    bad = [u for u in requested if u and u != session["user_id"]]
    if bad:
        return JSONResponse(
            status_code=403,
            content={"detail": f"无权访问用户 {bad[0]} 的数据"},
        )

    # body 被读过后必须重新灌回去，否则下游路由拿到空 body
    if raw:
        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        request = Request(request.scope, receive)

    return await call_next(request)


def require_user(request) -> dict:
    """取当前登录身份；未开启鉴权时回退为匿名（仅 demo 模式）。"""
    s = getattr(request.state, "user", None)
    if s:
        return s
    if get_settings().require_auth:
        raise HTTPException(401, "未登录")
    return {"user_id": "", "username": "anonymous"}


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post(f"{settings.api_prefix}/auth/register")
def auth_register(req: RegisterRequest):
    import auth as auth_mod

    try:
        return auth_mod.register(req.username, req.password, req.display_name)
    except auth_mod.AuthError as e:
        raise HTTPException(400, str(e)) from e


@app.post(f"{settings.api_prefix}/auth/login")
def auth_login(req: LoginRequest):
    import auth as auth_mod

    try:
        return auth_mod.login(req.username, req.password)
    except auth_mod.AuthError as e:
        # 统一文案，不区分"用户不存在"与"密码错误"，避免账号枚举
        raise HTTPException(401, str(e)) from e


@app.post(f"{settings.api_prefix}/auth/logout")
def auth_logout(request: Request):
    import auth as auth_mod

    auth_mod.revoke_token(request.headers.get("x-auth-token", ""))
    return {"ok": True}


@app.get(f"{settings.api_prefix}/auth/me")
def auth_me(request: Request):
    """注意：本路径在鉴权白名单里，中间件不会注入 request.state.user，
    所以这里必须自己校验 token，不能直接读 state。"""
    import auth as auth_mod

    if not get_settings().require_auth:
        return {"user_id": "", "username": "anonymous"}
    token = request.headers.get("x-auth-token") or request.query_params.get("token")
    s = auth_mod.verify_token(token) if token else None
    if not s:
        raise HTTPException(401, "未登录或登录已过期")
    return s


@app.get(f"{settings.api_prefix}/auth/hint")
def auth_hint():
    """登录页用来提示演示账号。

    只在「自动创建的演示账号还没被用过」时显示密码 —— 一旦有人登录过或
    改过密码，就不再提示，避免把一个已知密码长期暴露在登录页上。
    """
    import auth as auth_mod

    s = get_settings()
    if not s.require_auth:
        return {"require_auth": False, "demo": None}
    demo = auth_mod.default_account_untouched()
    return {"require_auth": True, "demo": demo}


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


# ================================================================== 病人模拟（测试用）
def _sim_enabled() -> None:
    """可选项的总开关。关掉后 /api/sim/* 一律 403。

    模拟数据会写进会话/消息/风险事件/画像，演示与生产环境不该开放。
    """
    if not get_settings().enable_patient_sim:
        raise HTTPException(403, "病人模拟未开启（ENABLE_PATIENT_SIM=0）")


@app.get(f"{settings.api_prefix}/sim/voice/status")
def sim_voice_status():
    """语音联路的可用性（前端据此决定 tts 档是否可选）。

    与 `/api/asr/status` 同理：把「没装」和「装了但用不了」分开说，
    否则开关亮着却永远跑不出声音。
    """
    _sim_enabled()
    import asr
    import tts

    info = tts.describe()
    return {**info, "asr_available": asr.asr_available(),
            "asr_model_ready": asr.model_ready()}


@app.get(f"{settings.api_prefix}/sim/profiles")
def sim_profiles():
    """可选的风格 / 严重程度清单，供前端下拉框使用。"""
    _sim_enabled()
    from patient_agent import list_profiles
    return {"enabled": True, **list_profiles()}


@app.post(f"{settings.api_prefix}/sim/turn")
def sim_turn(req: SimTurnRequest):
    """跑**一轮**模拟对话：病人 Agent 出话 -> 陪伴 Agent 回复。

    与 /api/chat 完全隔离：不传 style/severity 的普通对话永远走真人输入。
    """
    _sim_enabled()
    import graph
    import simulate
    from patient_agent import STYLES, SEVERITIES

    if req.style not in STYLES:
        raise HTTPException(400, f"未知风格：{req.style}")
    if req.severity not in SEVERITIES:
        raise HTTPException(400, "严重程度须为 1~5")

    sid = req.session_id or graph.new_session(req.user_id)
    import storage
    if not storage.get_session(sid):
        raise HTTPException(404, "session 不存在")

    # 按会话复用：否则每点一次「跑一轮」披露曲线都从第 1 级重开
    from patient_voice import VoiceConfig

    patient = simulate.patient_of(
        sid, req.style, req.severity, req.seed, req.turns,
        voice=VoiceConfig(mode=req.voice, consistency=req.voice_consistency,
                          backend=req.tts_backend, strict=req.strict_asr))
    rec = simulate.run_one_turn(req.user_id, sid, patient,
                                doctor_reply=simulate.last_assistant_text(sid),
                                patient_text=req.patient_text,
                                auto_review=req.auto_review)
    return {"session_id": sid, "profile": patient.describe(), **rec}


@app.post(f"{settings.api_prefix}/sim/run")
def sim_run(req: SimRunRequest):
    """连续跑多轮，返回完整对练记录（含风险轨迹与首次挂起的轮次）。

    同步执行，轮数较多时会比较久；前端请限制 turns 并给出等待提示。
    """
    _sim_enabled()
    import simulate
    from patient_agent import STYLES, SEVERITIES

    if req.style not in STYLES:
        raise HTTPException(400, f"未知风格：{req.style}")
    if req.severity not in SEVERITIES:
        raise HTTPException(400, "严重程度须为 1~5")

    from patient_voice import VoiceConfig

    try:
        out = simulate.run_session(
            req.user_id, req.style, req.severity, req.turns,
            seed=req.seed, auto_review=req.auto_review,
            stop_on_crisis=req.stop_on_crisis, session_id=req.session_id,
            voice=VoiceConfig(mode=req.voice, consistency=req.voice_consistency,
                              backend=req.tts_backend, strict=req.strict_asr),
        )
    except KeyError:
        raise HTTPException(404, "session 不存在") from None
    return out


@app.get(f"{settings.api_prefix}/asr/status")
def asr_status():
    """ASR 可用性（前端据此决定麦克风按钮是否可用）。

    `available` 只代表包装了；`model_ready` 才代表权重已在本地、转写不会
    静默返回空文本。两者不一致时必须给出可操作的提示，否则按钮亮着却
    永远识别不出内容。
    """
    import asr
    ready = asr.model_ready()
    hint = ""
    if not asr.asr_available():
        hint = "未安装 faster-whisper：pip install faster-whisper"
    elif not ready:
        hint = ("模型未就绪，首次转写需联网下载（约 460MB）。若无法访问 "
                "huggingface.co，可设 HF_ENDPOINT=https://hf-mirror.com 与 "
                "HF_HUB_DISABLE_XET=1 后重启后端")
    return {"available": asr.asr_available(), "model_ready": ready,
            "model": asr.ASR_MODEL_SIZE, "hint": hint}


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


@app.get(f"{settings.api_prefix}/trace/events")
def trace_events(request: Request, limit: int = 200, kind: str = ""):
    """结构化事件流（可观测层）。

    返回内存环形缓冲里的最近事件，供界面实时查看中间决策过程：
    情绪融合、三通道判定、最终分级、状态转移、CBT 阶段、延迟。
    """
    import tracer

    s = getattr(request.state, "user", None)
    items = tracer.recent(limit=limit, kind=kind)
    # 只返回当前登录用户相关事件（session_id 无法直接关联用户时退化为全量）
    if s:
        uid = s["user_id"]
        items = [e for e in items
                 if e.get("user_id") in (None, "", uid)
                 or str(e.get("session_id", "")).startswith("cmp_")
                 or str(e.get("session_id", "")).startswith("ses_")]
    return {"events": items, "log_file": tracer.log_file_today()}


@app.get(f"{settings.api_prefix}/health")
def health():
    return {"ok": True, "llm_provider": settings.llm_provider,
            "checkpointer": settings.checkpointer,
            "require_auth": settings.require_auth}


@app.on_event("startup")
def on_startup():
    """建演示账号 + 清理过期 token。失败不影响服务启动。"""
    try:
        import auth as auth_mod

        created = auth_mod.ensure_default_account()
        if created:
            print(f"[auth] 已创建演示账号：{created}（仅首次启动创建）", flush=True)
        expired = auth_mod.purge_expired_tokens()
        if expired:
            print(f"[auth] 清理过期 token {expired} 条", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] 初始化失败（服务继续运行）：{exc}", flush=True)


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
    """PRD §7.1 静默阶梯：8s 轻提示 / 60s 确认是否结束 / 90s 收尾。

    返回 {prompt, action(none|nudge|confirm|end), idle_sec, phase, elapsed}。
    前端需轮询本接口：action == "end" 时展示结束话术并调用 /finish。
    """
    import companion.service as cs
    eng = cs.engine_of(session_id)
    if not eng:
        raise HTTPException(404, "会话不存在")
    out = eng.silence_check()
    out["elapsed"] = round(eng.s.elapsed, 1)
    return out


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
