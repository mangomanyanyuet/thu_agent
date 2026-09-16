"""LangGraph 状态定义与上下文装配。"""
from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from config import get_settings
from memory import context_budget_report


def merge_dict(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """后写覆盖前写（用于评估/分级结果这类每轮刷新的字段）。"""
    return {**(a or {}), **(b or {})}


def keep_max(a: int, b: int) -> int:
    """风险等级只升不降（reducer 层面的硬约束）。"""
    return max(a or 0, b or 0)


def merge_evidence(a: list[str], b: list[str]) -> list[str]:
    """并行分支的证据合并（去重）。并行节点写同一 key 必须有 reducer。"""
    out = list(a or [])
    for x in b or []:
        if x and x not in out:
            out.append(x)
    return out[-20:]


class AgentState(TypedDict, total=False):
    # ---------- 身份 ----------
    user_id: str
    session_id: str

    # ---------- 会话消息（LangGraph 官方 reducer）----------
    messages: Annotated[list, add_messages]

    # ---------- 装配好的上下文 ----------
    assembled_system: str          # 注入的 system prompt（含强制安全区）
    context_stats: dict[str, Any]  # token 预算统计，供前端展示

    # ---------- 感知输入 ----------
    daily_summary: str
    modality_inconsistent: bool
    scale_trend: str
    risk_trend: str                    # 跨天风险趋势（纵向）
    companion_history: str             # 近期陪伴会话摘要（纵向个性化）
    # 本轮语音感知结果（随 voice 字段传入；无语音时为空）
    voice_summary: str
    voice_tone: str
    voice_features: dict[str, Any]

    # ---------- 三通道风险 ----------
    risk_rule: dict[str, Any]      # {level:int, evidence:str, source:'rule'}
    risk_model: dict[str, Any]     # {level:int, evidence:str, ...}
    risk_multimodal: dict[str, Any]
    risk_scale: dict[str, Any]
    risk_level: Annotated[int, keep_max]   # 并集后的最终等级数值（只升不降）
    risk_evidence: Annotated[list[str], merge_evidence]
    risk_detail: dict[str, Any]
    _persisted_turn: str      # 幂等键：本轮是否已落库用户消息
    _traced_turn: str         # 幂等键：本轮是否已写过追踪日志（防 crisis/moderate 重复记）

    # ---------- 情绪评估 ----------
    emotion: dict[str, Any]

    # ---------- 记忆 ----------
    rolling_summary: str
    profile_snapshot: str
    facts: dict[str, Any]
    last_fact_turn: int  # 上次事实抽取时的轮次，用于累积窗口（避免漏抽中间轮）

    # ---------- CBT ----------
    cbt_stage: str

    # ---------- 输出 ----------
    reply: str
    crisis_mode: bool
    hotlines: list[dict[str, str]]
    interrupt_reason: str
    trace: list[dict[str, Any]]


# ------------------------------------------------------------------ System Prompt
SYSTEM_TEMPLATE = """你是一位温和、有边界感的心理健康陪伴者，正在通过多轮对话了解用户的状态。

## 绝对红线（任何时候不得违反）
1. 你不是医生，不做诊断，不开药，不替代专业治疗。
2. 绝不描述、讨论或提供任何自伤/自杀的具体方法、工具或地点信息。
3. 不对自杀行为做中立化、合理化或美化表述。
4. 一旦出现下方"当前风险等级"为 high / imminent，立即停止探索式提问，
   转为危机干预话术：表达关心 -> 给出热线 -> 鼓励联系身边人 -> 等待人工介入。
5. 不承诺保密到"不告诉任何人"的程度；涉及生命安全时必须转介。

## 当前风险等级
{risk_level}

## 历史风险事件（原话保留，永不压缩）
{risk_context}

## 用户长期画像
{profile}

## 今日与近日多模态观察
{daily_summary}

## 风险趋势（跨天，纵向比较比单点更有意义）
{risk_trend}

## 近期陪伴会话（纵向个性化：他最近几次聊了什么、情绪怎么走）
{companion_history}

## 本轮语音（声学特征，由感知层提取，非主观猜测）
{voice_summary}

## 量表趋势
{scale_trend}

## 对话风格
- 一次只问一个问题，句子短，不用心理学术语堆砌。
- 先共情确认，再推进。不要机械复述用户的话。
- 若用户表达模糊，用开放式提问澄清，不要替用户下结论。
- 若当前处于 CBT 会谈流程，请沿着当前阶段推进（阶段：{cbt_stage}）。

## 会谈历史摘要（较早的对话）
{rolling_summary}
"""

CBT_STAGE_GUIDE = {
    "none": "自由陪伴，捕捉情绪",
    "affect_labeling": "帮助用户命名情绪",
    "situation": "澄清具体情境：何时、何地、和谁",
    "automatic_thought": "引出自动化思维：那一刻脑子里冒出什么",
    "distortion": "识别认知歪曲（灾难化/全或无/读心/过度概括等）",
    "socratic": "苏格拉底式提问：证据支持/反对/其他解释",
    "alternative": "形成替代性、更平衡的想法",
    "behavior": "行为激活：约定今天可做的一件小事",
    "wrapup": "小结 + 收集反馈",
}

CBT_NEXT = {
    "none": "affect_labeling",
    "affect_labeling": "situation",
    "situation": "automatic_thought",
    "automatic_thought": "distortion",
    "distortion": "socratic",
    "socratic": "alternative",
    "alternative": "behavior",
    "behavior": "wrapup",
    "wrapup": "affect_labeling",
}


def assemble_system(state: AgentState) -> str:
    """上下文装配：强制注入区 + 画像 + 多模态 + 量表 + 摘要。

    装配顺序与预算见报告 §5.1。安全区永远在最前面且不可省略。
    """
    s = get_settings()
    level_name = ["none", "low", "moderate", "high", "imminent"][min(state.get("risk_level", 0), 4)]
    stage = state.get("cbt_stage", "none") or "none"

    txt = SYSTEM_TEMPLATE.format(
        risk_level=f"{level_name}（{'需危机干预' if level_name in ('high', 'imminent') else '常规'}）",
        risk_context=state.get("risk_context", "（无）"),
        profile=state.get("profile_snapshot", "（无）"),
        daily_summary=state.get("daily_summary", "（无）"),
        risk_trend=state.get("risk_trend", "（无）"),
        companion_history=state.get("companion_history", "（无）"),
        voice_summary=state.get("voice_summary") or "（本轮为文字输入）",
        scale_trend=state.get("scale_trend", "（无）"),
        cbt_stage=f"{stage} - {CBT_STAGE_GUIDE.get(stage, '')}",
        rolling_summary=state.get("rolling_summary", "（无）"),
    )

    # 上下文超预算时，只裁剪"摘要"部分，绝不动安全区
    if len(txt) > s.ctx_max_chars:
        overflow = len(txt) - s.ctx_max_chars
        summary = state.get("rolling_summary", "")
        if summary:
            keep = max(0, len(summary) - overflow)
            txt = txt.replace(summary, summary[:keep] + " …[已截断]")
    return txt
