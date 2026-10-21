"""会话状态机（PRD §6.1 / §7.1）。

阶段流转：
    consent(知情同意) → quality(质检) → opening(开场) → main(主体)
        → closing(结束语) → done(出报告)
    任意阶段可因风险进入 crisis，因异常进入 aborted。

收束条件（§7.1）：
    * 累计时长 ≥ WRAP_UP_AT：当前轮回应后追加一句自然过渡，不再开启新话题；
    * 用户静默 ≥ IDLE_CONFIRM_AT：询问「这次先聊到这里吗」；
    * 上一步之后再静默 ≥ IDLE_END_AT - IDLE_CONFIRM_AT：进入结束话术。
    **轮次不再作为终止条件** —— 用户愿意继续聊就继续，
    「满 4 轮就掐断」会在用户正说得起劲时突兀收尾。
"""
from __future__ import annotations

import time
from enum import Enum

# ---------------- 时长常量（秒，PRD §6.1 / §7.1） ----------------
TARGET_TOTAL = 300          # 目标总时长 5 分钟
WRAP_UP_AT = 240            # 4:00 触发收尾提醒
HARD_LIMIT = 330            # 5:30 硬上限，强制结束
SILENCE_PROMPT = 8          # 沉默 8s 主动确认
IDLE_CONFIRM_AT = 60        # 静默 60s：确认是否就此结束
IDLE_END_AT = 90            # 上一步后再等 30s 仍无回应：进入结束话术
MIN_TURNS = 3               # 主体建议轮次（仅用于报告完整性提示，非终止条件）


class Phase(str, Enum):
    CONSENT = "consent"
    QUALITY = "quality"
    OPENING = "opening"
    MAIN = "main"
    CLOSING = "closing"
    CRISIS = "crisis"
    DONE = "done"
    ABORTED = "aborted"


class SessionState:
    """一次陪伴会话的运行时状态。"""

    def __init__(self, session_id: str, user_id: str, now: float | None = None):
        self.session_id = session_id
        self.user_id = user_id
        self.phase = Phase.CONSENT
        self.started_at = now if now is not None else time.time()
        self.consent: dict = {}
        self.quality: dict = {}
        self.turns: list[dict] = []       # 每轮：{turn_id, text, audio, face, fused, ts}
        self.history: list[dict] = []     # 供 LLM 的对话历史
        self.degradation_notes: list[str] = []
        self.modality = {"voice": 0.0, "face": 0.0}
        self.risk_level = "none"          # none | watch | high
        self.risk_evidence: list[str] = []
        self.resource_suggested = False
        self.wrap_up_hinted = False
        self.idle_stage = 0               # 静默阶梯：0=正常 1=已确认是否结束 2=已给出结束话术
        # 静默计时基准：用户最后一次开口 / 开场白送达时刻。
        # 用户一整轮都没开口时也要能计时，所以初值为会话创建时刻而非 None。
        self._last_activity_ts: float = self.started_at
        self._silence_prompted = 0        # 本轮已提示过几次

    # ---------------- 计时 ----------------
    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def mark_user_input(self) -> None:
        """用户开口：静默阶梯整体清零，确认过结束也要重新来过。"""
        self._last_activity_ts = time.time()
        self._silence_prompted = 0
        self.idle_stage = 0

    def mark_activity(self) -> None:
        """非用户输入的活跃点（开场白送达）。只重置计时，不清提示计数。"""
        self._last_activity_ts = time.time()

    def seconds_since_user(self) -> float:
        return time.time() - self._last_activity_ts

    # ---------------- 阶段推进 ----------------
    def should_wrap_up(self) -> bool:
        """是否应给模型追加收尾提示（PRD §7.1：第 4 分钟起引向收束）。

        轮次**不再**参与判断：只由时长决定，避免"聊到第 4 轮就被掐断"。
        """
        return self.elapsed >= WRAP_UP_AT

    def is_hard_limit(self) -> bool:
        return self.elapsed >= HARD_LIMIT

    def next_phase(self) -> Phase:
        """根据当前状态决定下一阶段。"""
        if self.risk_level == "high":
            return Phase.CRISIS

        # 时间到了 -> 收尾。轮次不再触发收尾（见 should_wrap_up 说明）。
        if self.phase in (Phase.OPENING, Phase.MAIN):
            if self.elapsed >= WRAP_UP_AT:
                return Phase.CLOSING
            return Phase.MAIN
        if self.phase == Phase.CLOSING:
            return Phase.DONE
        return self.phase

    def note_degradation(self, msg: str) -> None:
        if msg not in self.degradation_notes:
            self.degradation_notes.append(msg)

    def bump_modality(self, kind: str, ok: bool) -> None:
        """累计某模态的有效采集占比（用于 Tier3 modality_availability）。"""
        key = "voice" if kind == "voice" else "face"
        n = max(len(self.turns), 1)
        cur = self.modality.get(key, 0.0) * (n - 1) / n
        self.modality[key] = round(cur + (1.0 / n if ok else 0.0), 3)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "phase": self.phase.value,
            "elapsed": round(self.elapsed, 1),
            "turn_count": self.turn_count,
            "idle_stage": self.idle_stage,
            "idle_sec": round(self.seconds_since_user(), 1),
            "risk_level": self.risk_level,
            "modality": self.modality,
            "degradation_notes": self.degradation_notes,
        }
