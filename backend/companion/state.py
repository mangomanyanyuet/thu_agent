"""会话状态机（PRD §6.1）。

阶段流转：
    consent(知情同意) → quality(质检) → opening(开场) → main(主体 3-4 轮)
        → closing(结束语) → done(出报告)
    任意阶段可因风险进入 crisis，因异常进入 aborted。
"""
from __future__ import annotations

import time
from enum import Enum

# ---------------- 时长常量（秒，PRD §6.1） ----------------
TARGET_TOTAL = 300          # 目标总时长 5 分钟
WRAP_UP_AT = 240            # 4:00 触发收尾提醒
HARD_LIMIT = 330            # 5:30 硬上限，强制结束
SILENCE_PROMPT = 8          # 沉默 8s 主动确认
SILENCE_GIVE_UP = 20        # 单轮沉默累计 20s 视为放弃
MIN_TURNS = 3               # 主体最少轮次
MAX_TURNS = 4               # 主体最多轮次


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
        self._last_user_ts: float | None = None
        self._silence_prompted = 0        # 本轮已提示过几次

    # ---------------- 计时 ----------------
    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def mark_user_input(self) -> None:
        self._last_user_ts = time.time()
        self._silence_prompted = 0

    def seconds_since_user(self) -> float:
        if self._last_user_ts is None:
            return 0.0
        return time.time() - self._last_user_ts

    # ---------------- 阶段推进 ----------------
    def should_wrap_up(self) -> bool:
        """是否应进入收尾（PRD §6.1：第 4 分钟触发）。"""
        return (self.elapsed >= WRAP_UP_AT
                or self.turn_count >= MAX_TURNS
                or self.elapsed >= HARD_LIMIT)

    def is_hard_limit(self) -> bool:
        return self.elapsed >= HARD_LIMIT

    def next_phase(self) -> Phase:
        """根据当前状态决定下一阶段。"""
        if self.risk_level == "high":
            return Phase.CRISIS

        # 轮次够了 或 时间到了 -> 收尾
        if self.phase in (Phase.OPENING, Phase.MAIN):
            if self.turn_count >= MAX_TURNS or self.elapsed >= WRAP_UP_AT:
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
            "risk_level": self.risk_level,
            "modality": self.modality,
            "degradation_notes": self.degradation_notes,
        }
