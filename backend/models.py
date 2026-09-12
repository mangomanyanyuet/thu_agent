"""数据模型。

- Pydantic 模型用于 LLM 结构化输出与 API 交互（强校验，杜绝解析失败）
- dataclass 用于 DB 行映射
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 枚举
RISK_ORDER = {"none": 0, "low": 1, "moderate": 2, "high": 3, "imminent": 4}
RISK_LABEL_CN = {
    "none": "无风险",
    "low": "轻度",
    "moderate": "中度",
    "high": "重度",
    "imminent": "危急",
}


class RiskLevel(str, Enum):
    none = "none"
    low = "low"
    moderate = "moderate"
    high = "high"
    imminent = "imminent"


class EmotionLabel(str, Enum):
    joy = "joy"
    calm = "calm"
    neutral = "neutral"
    sadness = "sadness"
    anxiety = "anxiety"
    anger = "anger"
    fear = "fear"
    shame = "shame"
    numbness = "numbness"


class CBTStage(str, Enum):
    none = "none"
    affect_labeling = "affect_labeling"
    situation = "situation"
    automatic_thought = "automatic_thought"
    distortion = "distortion"
    socratic = "socratic"
    alternative = "alternative"
    behavior = "behavior"
    wrapup = "wrapup"


# ---------------------------------------------------------------- LLM 结构化输出
class EmotionAssessment(BaseModel):
    """情绪评估节点的结构化输出。"""

    primary_emotion: EmotionLabel
    intensity: int = Field(ge=0, le=10, description="情绪强度 0-10")
    valence: float = Field(ge=-1, le=1, description="效价 -1 极度负面 ~ 1 极度正面")
    arousal: float = Field(ge=0, le=1, description="唤醒度")
    themes: list[str] = Field(default_factory=list, description="提及的主题，如 工作/亲密关系/睡眠")
    cognitive_distortions: list[str] = Field(
        default_factory=list, description="认知歪曲：灾难化/全或无/读心/过度概括/情绪化推理 等"
    )
    rationale: str = Field(default="", description="简要判断依据")


class RiskAssessment(BaseModel):
    """风险检测通道的结构化输出（独立于对话生成）。"""

    level: RiskLevel
    has_ideation: bool = False
    has_plan: bool = False
    has_preparation: bool = False
    has_intent: bool = False
    protective_factors: list[str] = Field(default_factory=list)
    evidence: str = Field(default="", description="触发判定的原话片段，留证据")
    confidence: float = Field(ge=0, le=1, default=0.5)


class TurnAssessment(BaseModel):
    """一次调用同时产出「情绪评估」与「风险判定」。

    为什么合并：两者读的是同一份上下文，分开调用会让每次对话多一轮完整推理。
    在 hy3 这类推理模型上，推理 token 占总量的 90%+，合并后每轮可省约 12s。

    注意：这里合的是「两个评估任务」，**不是**把风险判定并入对话生成 ——
    风险检测与对话生成仍然分属不同节点（见 graph.py），解耦原则不变。
    """

    # --- 情绪 ---
    primary_emotion: EmotionLabel
    intensity: int = Field(ge=0, le=10, description="情绪强度 0-10")
    valence: float = Field(ge=-1, le=1, description="效价 -1 极度负面 ~ 1 极度正面")
    arousal: float = Field(ge=0, le=1, default=0.5, description="唤醒度")
    themes: list[str] = Field(default_factory=list, description="提及的主题，如 工作/亲密关系/睡眠")
    cognitive_distortions: list[str] = Field(
        default_factory=list, description="认知歪曲：灾难化/全或无/读心/过度概括/情绪化推理 等"
    )
    emotion_rationale: str = Field(default="", description="情绪判断依据")

    # --- 风险（保守判定，宁可高估不可低估）---
    risk_level: RiskLevel = Field(default=RiskLevel.none, description="自杀/自伤风险等级")
    has_ideation: bool = Field(default=False, description="是否有自杀意念")
    has_plan: bool = Field(default=False, description="是否有具体计划")
    has_preparation: bool = Field(default=False, description="是否有准备行为")
    has_intent: bool = Field(default=False, description="是否有实施意图")
    protective_factors: list[str] = Field(default_factory=list, description="保护性因素")
    risk_evidence: str = Field(default="", description="触发判定的原话片段，务必原文引用")
    risk_confidence: float = Field(ge=0, le=1, default=0.5, description="判定置信度")
    risk_rationale: str = Field(default="", description="风险判断依据")

    def to_emotion(self) -> dict:
        return {
            "primary_emotion": self.primary_emotion.value,
            "intensity": self.intensity,
            "valence": self.valence,
            "arousal": self.arousal,
            "themes": self.themes,
            "cognitive_distortions": self.cognitive_distortions,
            "rationale": self.emotion_rationale,
        }

    def to_risk(self) -> dict:
        return {
            "level": ["none", "low", "moderate", "high", "imminent"].index(self.risk_level.value),
            "evidence": self.risk_evidence,
            "source": "model",
            "has_plan": self.has_plan,
            "has_preparation": self.has_preparation,
            "has_intent": self.has_intent,
            "confidence": self.risk_confidence,
        }


class ExtractedFacts(BaseModel):
    """记忆写回节点：从本轮对话抽取的长期事实。"""

    profile_facts: list[str] = Field(
        default_factory=list, description="稳定的用户事实，如 '有一只叫豆豆的猫'"
    )
    triggers: list[str] = Field(default_factory=list, description="触发源，如 '被领导当众批评'")
    events: list[str] = Field(default_factory=list, description="近期事件")
    preferences: list[str] = Field(default_factory=list, description="沟通偏好")


class AudioFeatures(BaseModel):
    """语音感知层输出的声学特征（结构化，只把摘要送进上下文）。"""

    duration_sec: float = 0.0
    speech_ratio: float | None = Field(None, description="语音段占总时长比例")
    pause_ratio: float | None = Field(None, description="静音/停顿占比")
    syllable_rate: float | None = Field(None, description="近似音节速率（音节/秒）")
    f0_mean_hz: float | None = Field(None, description="基频均值")
    f0_std: float | None = Field(None, description="基频标准差，越小语调越平")
    energy_db: float | None = Field(None, description="平均能量（dBFS）")
    segments: int = Field(0, description="检出的语音段数")


class DailySummary(BaseModel):
    """感知层输出：每日多模态记录的结构化摘要。"""

    transcript: str = ""
    narrative: str = ""
    video_features: dict[str, Any] = Field(default_factory=dict)
    audio_features: dict[str, Any] = Field(default_factory=dict)
    narrative_analysis: dict[str, Any] = Field(default_factory=dict)
    modality_summary: str = ""
    inconsistency: bool = False


# ---------------------------------------------------------------- DB 行
@dataclass
class UserProfile:
    user_id: str
    nickname: str = ""
    personality_traits: list[str] = field(default_factory=list)
    coping_style: list[str] = field(default_factory=list)
    social_support: list[str] = field(default_factory=list)
    known_triggers: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    baseline_emotion: dict[str, float] = field(default_factory=dict)
    updated_at: str = ""


@dataclass
class DailyRecord:
    id: int | None
    user_id: str
    date: str
    video_uri: str = ""
    transcript: str = ""
    narrative: str = ""
    video_features: dict[str, Any] = field(default_factory=dict)
    audio_features: dict[str, Any] = field(default_factory=dict)
    narrative_analysis: dict[str, Any] = field(default_factory=dict)
    modality_summary: str = ""
    inconsistency: bool = False
    created_at: str = ""


@dataclass
class ScaleScore:
    id: int | None
    user_id: str
    date: str
    scale_name: str
    item_scores: list[int]
    total_score: int
    delta_vs_last: int
    delta_vs_baseline: int
    severity: str
    created_at: str = ""


@dataclass
class RiskFlag:
    id: int | None
    user_id: str
    session_id: str
    ts: str
    source: str  # rule | model | scale_item | multimodal
    evidence: str
    level: str
    cssrs_dimension: str = ""
    handled: bool = False
    handled_by: str = ""
    handled_note: str = ""


@dataclass
class SessionRow:
    id: str
    user_id: str
    started_at: str
    ended_at: str = ""
    summary: str = ""
    last_risk: str = "none"
    cbt_stage: str = "none"
    interrupted: bool = False


@dataclass
class MessageRow:
    id: int | None
    session_id: str
    role: str  # user | assistant | system
    content: str
    emotion: str = ""
    risk_level: str = "none"
    meta: dict[str, Any] = field(default_factory=dict)
    ts: str = ""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
