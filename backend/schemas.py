"""API 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VoiceMeta(BaseModel):
    """本轮语音的感知结果（由 /api/asr 产出，随对话一起提交）。"""

    audio_features: dict[str, Any] = Field(default_factory=dict)
    tone: str = ""            # low | flat | neutral | agitated | unknown
    tone_reason: str = ""
    summary: str = ""


class ChatRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    text: str = Field(min_length=1, max_length=4000)
    voice: VoiceMeta | None = None


# ---------------------------------------------------------------- 陪伴会话
class CompanionStart(BaseModel):
    user_id: str


class CompanionConsent(BaseModel):
    """PRD §8 知情同意：必须三项都确认。"""
    understood_ai: bool        # 已知悉是 AI 工具、非医疗/咨询
    consent_media: bool        # 同意采集音视频
    adult_confirmed: bool      # 已确认成年（或监护人同意）
    audio: bool = True
    video: bool = True
    data_retention_ok: bool = False


class CompanionQuality(BaseModel):
    """PRD §6.3.2 质量前置检测。"""
    face_visible: bool = False
    single_person: bool = True
    low_light: bool = False
    multi_person: bool = False
    brightness: float | None = None


class FaceFeatures(BaseModel):
    """前端 MediaPipe 提取的面部特征（PRD §6.3.2）。"""
    face_detected: bool = False
    blendshapes: dict[str, float] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)


class CompanionTurn(BaseModel):
    """一轮用户回答。

    `session_id` 可选：路由上已有 `/companion/{session_id}/turn`，handler 取路径参数。
    保留该字段仅为兼容早期调用方，必填会导致前端（只传 text/audio/face）拿到 422。
    """
    session_id: str = ""
    text: str = ""
    audio: VoiceMeta | None = None
    face: FaceFeatures | None = None


class ReviewRequest(BaseModel):
    session_id: str
    action: str = Field(default="approve", pattern="^(approve|override|reject)$")
    reply: str = ""
    reviewer: str = "human"


class DailyReportRequest(BaseModel):
    user_id: str
    date: str | None = None
    video_uri: str = ""
    transcript: str = ""
    narrative: str = ""
    # 真实声学特征（来自 /api/asr）。给了就用真实的，没给才回落到桩函数。
    voice: VoiceMeta | None = None


class ScaleSubmitRequest(BaseModel):
    user_id: str
    scale_name: str
    item_scores: list[int]
    date: str | None = None


class ProfileUpdate(BaseModel):
    user_id: str
    nickname: str | None = None
    personality_traits: list[str] | None = None
    coping_style: list[str] | None = None
    social_support: list[str] | None = None
