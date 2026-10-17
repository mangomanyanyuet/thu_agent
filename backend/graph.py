"""LangGraph 编排图 —— 系统核心。

图结构：

                ┌─► risk_detector(规则+量表+多模态) ─┐  ← 独立安全通道，对主流程有否决权
   entry ─┬─────┤                                    ├──► risk_gate ──┬─► crisis(高/危急)
          │     └─► context_builder ─► emotion ──────┘                │      └─► interrupt(等人审)
          │                                (模型通道)                  ├─► moderate(安全计划)
          │                                                           └─► cbt(低/无)
          └──────────────────────────────────────────────────────────────────┴─► memory_writer ─► END

要点：
1. 风险检测与对话生成【架构解耦】—— risk_detector 是并行独立分支，
   不依赖 LLM 的共情生成，避免"为了维持关系而漏报"。
2. 三通道取并集，只升不降（reducer keep_max + gate 再次取 max）。
3. high/imminent 走 crisis 节点并用 interrupt() 挂起等人工，绝不自动化闭环。
4. checkpointer 持久化每一步，事故可回放（医疗合规刚需）。
"""
from __future__ import annotations

import uuid
from typing import Any

from config import get_settings
from graph_state import CBT_NEXT, AgentState, assemble_system, context_budget_report
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from models import (
    CBTStage,
    EmotionAssessment,
    ExtractedFacts,
    MessageRow,
    RiskAssessment,
    RiskFlag,
    RiskLevel,
    SessionRow,
    TurnAssessment,
    now_iso,
)
from perception import (
    hotlines,
    multimodal_risk_signal,
    rule_risk_level,
    voice_risk_signal,
)
from storage import (
    add_message,
    add_risk_flag,
    add_trace,
    get_session,
    latest_scale_score,
    list_daily_records,
    list_messages,
    list_risk_flags,
    upsert_session,
)

# 风险等级数值 → 名称。与 models.RISK_LEVELS / tracer._RISK_NAMES 同序，
# 抽成常量避免各处重复写列表字面量时写错顺序。
_LEVEL_NAMES = ["none", "low", "moderate", "high", "imminent"]

# ------------------------------------------------------------------ LLM 调用
EMOTION_SYS = """你是情绪评估器。只输出 JSON，字段：
primary_emotion(joy/calm/neutral/sadness/anxiety/anger/fear/shame/numbness),
intensity(0-10), valence(-1~1), arousal(0~1), themes(数组), cognitive_distortions(数组),
rationale(字符串)。不要输出 JSON 以外的内容。"""

RISK_SYS = """你是独立的风险判定器，唯一职责是保守地识别自杀/自伤风险，宁可误报不可漏报。
只输出 JSON：level(none/low/moderate/high/imminent), has_ideation, has_plan,
has_preparation, has_intent, protective_factors(数组), evidence(触发判定的原话片段), confidence(0~1)。

判定参考（C-SSRS 维度）：
- low: 活着没意义、被动死亡愿望，无主动意念
- moderate: 有主动自杀意念，无计划
- high: 有计划 / 有准备行为 / 开始安排后事
- imminent: 正在实施 / 手段在手 / 明确即刻意图
注意识别隐喻与告别式表达（如"以后不用管我了""帮我把东西转交"），这类按 moderate 起判。
不要输出 JSON 以外的内容。"""

# 合并后的评估提示词：一次调用同时产出情绪 + 风险（省一轮推理，推理模型上尤其明显）
ASSESS_SYS = """你是一名心理评估助手，同时完成「情绪评估」与「风险判定」两项任务。

【任务一：情绪评估】识别用户当前主导情绪、强度、效价、唤醒度、主题与认知歪曲。

primary_emotion 只能取这 8 个中文标签之一：平静、愉悦、焦虑、低落、烦躁、疲惫、紧张、惊讶
- valence（效价）：-1 极度消极 ~ 0 中性 ~ 1 极度积极
- arousal（唤醒度）：-1 平静/低迷 ~ 0 适中 ~ 1 激动/亢奋
- emotion_confidence（把握）：0 ~ 1
- intensity（强度）：0 ~ 10

【任务二：风险判定】这是你的第一优先级，唯一标准是保守——宁可误报，绝不可漏报。
判定参考（C-SSRS 维度）：
- none: 无相关表达
- low: 活着没意义、被动死亡愿望、觉得自己是负担，但无主动意念
- moderate: 有主动自杀意念，无具体计划
- high: 有计划 / 有准备行为（如写遗书、安排后事、把东西送人）
- imminent: 正在实施 / 手段在手 / 明确即刻意图
务必留意隐喻与告别式表达（如"以后不用管我了""帮我把东西转交""最后一次说这些"），
这类按 moderate 起判。risk_evidence 必须是用户原话片段，原文引用，不要改写。

风险判定与情绪评估互不影响：即使整体情绪看起来平静，只要出现风险表达就必须升级。

【输出】只输出一个 JSON 对象，不要 Markdown 围栏、不要前后缀文字。
字段：primary_emotion, intensity, valence, arousal, emotion_confidence, themes,
cognitive_distortions, risk_level, has_ideation, has_plan, has_preparation,
has_intent, protective_factors, risk_evidence, risk_confidence
themes 与 cognitive_distortions 必须是数组，多个值用数组表示，没有就给空数组 []。"""


FACT_SYS = """你是记忆抽取器。从对话中抽取长期有效的事实，只输出 JSON：
profile_facts(稳定的生活事实/重要他人/社会支持), triggers(触发源), events(近期事件),
preferences(沟通偏好), personality_traits(人格特质), coping_style(应对方式)。

两条硬约束（人格/应对方式比事件更容易抽错，抽错会直接误导后续共情）：
1. personality_traits 只抽用户**明确自述**的稳定特质或归因习惯，
   如「我对自己要求很高」「我总是把错揽到自己身上」「我很在意别人怎么看」。
   ❌ 严禁从单次情绪反推性格：用户今天难过 ≠ 「情绪不稳定」，
      今天焦虑 ≠ 「焦虑型人格」。这类推断一律不抽。
2. coping_style 只抽用户**明确说过**的应对做法，
   如「我会去跑跑步」「一般就自己待着」「找朋友聊聊」「写日记」。
   ❌ 不要从「想放松」推测出「喜欢听音乐」。

只抽取原文明确提到的内容，没有就给空数组。宁可少抽，不可错抽。"""

CBT_SYS_EXTRA = "\n当前 CBT 阶段目标：{guide}\n按此目标推进，一次只问一个问题。"


def _llm_messages(state: AgentState, n: int | None = None) -> list[dict[str, str]]:
    """取最近 n 轮对话给 LLM（滑动窗口）。"""
    s = get_settings()
    n = n or s.ctx_recent_turns
    msgs = [m for m in state.get("messages", []) if getattr(m, "type", None) in ("human", "ai")]
    tail = msgs[-n:]
    return [{"role": "user" if getattr(m, "type", None) == "human" else "assistant",
             "content": str(m.content)} for m in tail]


def _last_user_text(state: AgentState) -> str:
    for m in reversed(state.get("messages", [])):
        if getattr(m, "type", None) == "human":
            return str(m.content)
    return ""


# ------------------------------------------------------------------ 节点
def entry_node(state: AgentState) -> dict[str, Any]:
    """载入长期记忆：画像、量表趋势、多模态摘要、历史风险（强制区）。"""
    from memory import (
        build_risk_trend,
        build_scale_trend,
        load_companion_context,
        load_daily_context,
        load_profile,
        load_risk_context,
        render_profile,
        render_risk_trend,
    )

    uid = state["user_id"]
    profile = load_profile(uid)
    daily_ctx, inconsistent = load_daily_context(uid)
    trend = build_risk_trend(uid)

    return {
        "profile_snapshot": render_profile(profile),
        "scale_trend": build_scale_trend(uid),
        "daily_summary": daily_ctx,
        "modality_inconsistent": inconsistent,
        "risk_trend": render_risk_trend(trend),
        "companion_history": load_companion_context(uid),
        "risk_context": load_risk_context(uid),
        "cbt_stage": state.get("cbt_stage") or CBTStage.none.value,
        "risk_level": state.get("risk_level", 0),
    }


def risk_detector_node(state: AgentState) -> dict[str, Any]:
    """独立安全通道：规则 + 量表 + 多模态。不依赖对话生成模型。"""
    text = _last_user_text(state)
    uid = state["user_id"]

    rule_lvl, rule_ev = rule_risk_level(text)

    # 量表通道
    # 必须取**最近一次**分数：list_scale_scores 按 date ASC 排序，limit=1 会
    # 拿到最早一条，量表通道将长期读陈旧分数（PHQ-9 第 9 题后来恶化会被漏掉）。
    # 证据里带上日期，让模型与审核员能感知这份分数有多旧。
    scale_lvl, scale_ev = 0, ""
    for name in ("PHQ-9", "GAD-7"):
        last = latest_scale_score(uid, name)
        if not last:
            continue
        from perception import scale_risk_signal
        lvl, ev = scale_risk_signal(name, last.item_scores)
        if lvl > scale_lvl:
            scale_lvl = lvl
            scale_ev = f"{ev}（{last.date}）" if ev else ""

    # 多模态通道
    # 每日记录有两种来源：种子/量表类带完整视频+音频特征，聊天收尾写的只有
    # {"source":"chat"}、video/audio 均为空。只取最新一条会撞上后者，
    # 多模态通道就永远读到空数据（日志里恒为 none）。这里取最近若干条中
    # 第一条**真正带多模态特征**的。
    mm_lvl, mm_ev = 0, ""
    for rec in list_daily_records(uid, limit=7):
        if (rec.video_features or {}).get("emotion_dist") or (rec.audio_features or {}):
            mm_lvl, mm_ev = multimodal_risk_signal(rec)
            break
    if state.get("modality_inconsistent"):
        mm_lvl = max(mm_lvl, 1)
        mm_ev = (mm_ev + "；" if mm_ev else "") + "模态不一致（掩饰倾向）"

    # 本轮语音：语气与文本内容不一致，或语气明显低落，都是独立的风险信号
    # （用 text 而非 emotion —— emotion 在并行分支上，这里读不到）
    v_lvl, v_ev = voice_risk_signal(state.get("voice_tone", ""),
                                    state.get("voice_features", {}), text)
    if v_lvl > mm_lvl:
        mm_lvl, mm_ev = v_lvl, ((mm_ev + "；" if mm_ev else "") + v_ev)

    # ---- 可观测：记录非模型通道（规则/量表/多模态）的判定 ----
    import tracer

    turn = state.get("turn", -1)
    tracer.risk_channel(state["session_id"], turn, "规则", rule_lvl, rule_ev)
    # 量表通道无条件记录：只在非零时打日志的话，日志里永远看不到第四通道，
    # 与「四通道并集」的设计对不上，也没法确认它到底参没参与判定。
    tracer.risk_channel(state["session_id"], turn, "量表", scale_lvl, scale_ev)
    tracer.risk_channel(state["session_id"], turn, "多模态", mm_lvl, mm_ev)

    return {
        "risk_rule": {"level": rule_lvl, "evidence": rule_ev, "source": "rule"},
        "risk_scale": {"level": scale_lvl, "evidence": scale_ev, "source": "scale_item"},
        "risk_multimodal": {"level": mm_lvl, "evidence": mm_ev, "source": "multimodal"},
        "risk_level": max(rule_lvl, scale_lvl, mm_lvl),
        "risk_evidence": [e for e in (rule_ev, scale_ev, mm_ev) if e],
    }


def context_builder_node(state: AgentState) -> dict[str, Any]:
    """并行分支：情绪评估 + 模型风险通道。

    注意：这里**不**装配 system prompt —— 因为 risk_detector 在另一条并行分支上，
    此刻 state 里还没有规则/量表/多模态通道的结果。装配必须放到汇聚节点，
    否则注入的"当前风险等级"会偏低（这正是并行化带来的陷阱）。
    """
    from llm import structured

    msgs = _llm_messages(state)
    convo = "对话：\n" + "\n".join(f"{m['role']}: {m['content']}" for m in msgs)

    # 一次调用同时做情绪评估 + 风险判定（省一轮推理；解耦的是"生成"而非"评估"）
    a: TurnAssessment = structured(
        TurnAssessment, ASSESS_SYS,
        f"{convo}\n\n用户最新一句话：{_last_user_text(state)}"
        f"\n\n近期上下文摘要：{state.get('rolling_summary', '')[:300]}",
    )
    emotion_dict = a.to_emotion()
    risk = a.to_risk()
    model_lvl = risk["level"]

    # ---- 可观测：情绪融合结果 + 模型通道风险判定 ----
    import tracer

    # primary_emotion 已是中文 8 类（EmotionLabel 统一后），无需再做英文映射
    tracer.emotion(
        state["session_id"], state.get("turn", -1),
        label=str(emotion_dict.get("primary_emotion") or "未知"),
        valence=float(emotion_dict.get("valence") or 0.0),
        arousal=float(emotion_dict.get("arousal") or 0.0),
        confidence=float(emotion_dict.get("confidence")
                         or risk.get("confidence") or 0.0),
        intensity=emotion_dict.get("intensity"),
        themes=emotion_dict.get("themes"),
        distortions=emotion_dict.get("cognitive_distortions"),
        text_source="llm",
    )
    tracer.risk_channel(state["session_id"], state.get("turn", -1), "模型", model_lvl,
                        risk.get("evidence") or "",
                        has_ideation=risk.get("has_ideation"),
                        has_plan=risk.get("has_plan"),
                        has_preparation=risk.get("has_preparation"),
                        has_intent=risk.get("has_intent"))

    return {
        "emotion": emotion_dict,
        "risk_model": risk,
        # 并集升级：reducer 会取 max，这里再显式取一次
        "risk_level": max(state.get("risk_level", 0), model_lvl),
        "risk_evidence": ([risk["evidence"]] if risk["evidence"] else []),
    }


def risk_gate(state: AgentState) -> str:
    """条件边：按最终等级分流。等级取所有通道最大值。"""
    import tracer

    level = final_level(state)
    if level >= 3:
        dst = "crisis"
    elif level == 2:
        dst = "moderate"
    else:
        dst = "cbt"

    tracer.transition(
        state["session_id"], state.get("turn", -1), "persist_flags", dst,
        condition=f"final_level={level}",
        reason={"crisis": "high 及以上，转危机协议并挂起等人工",
                "moderate": "中度，CBT + 安全计划 + 热线",
                "cbt": "低/无风险，常规 CBT 会谈"}[dst],
    )
    return dst


def final_level(state: AgentState) -> int:
    """三通道并集后的最终等级：取最大值，只升不降。"""
    return max(
        state.get("risk_level", 0),
        state.get("risk_rule", {}).get("level", 0),
        state.get("risk_model", {}).get("level", 0),
        state.get("risk_multimodal", {}).get("level", 0),
        state.get("risk_scale", {}).get("level", 0),
    )


def persist_risk_flags(state: AgentState) -> dict[str, Any]:
    """汇聚节点：装配 system prompt（此时所有通道已就绪）+ 落库用户消息与风险事实。

    ⚠ 两个关键设计：
    1. system prompt 在这里装配（而非并行分支内），保证注入的"当前风险等级"
       已包含规则/量表/多模态通道的结果。
    2. 风险 flag 按「证据去重」写入，而不是用一个布尔开关 —— 布尔开关会被
       checkpoint 带到下一轮，导致后续轮次的风险事件被吞掉。
    3. 用户消息在这里就落库，即使后面被 interrupt 挂起，审计记录也不丢。
    """
    system = assemble_system({**state, "risk_level": final_level(state)})
    stats = context_budget_report(system)
    uid, sid = state["user_id"], state["session_id"]
    level = final_level(state)

    # ---- 可观测：最终分级（并集后）+ 上下文预算 ----
    # 注意：crisis / moderate 节点会二次调用本函数（见各节点开头的
    # persist_risk_flags(state)），若不判重，风险分级、上下文装配与
    # audit_trace 会各记两遍（tracer 事件与 DB 轨迹都要挂在这个幂等键下）。
    import tracer

    _trace_key = f"{sid}:{len(state.get('messages', []))}:trace"
    traced = state.get("_traced_turn") == _trace_key
    if not traced:
        # 上一轮等级只能取**持久化**值：state["risk_level"] 带 keep_max
        # reducer，而 risk_detector / context_builder 都会写它，走进本节点时
        # 它已经是"本轮"值了。拿它当 prev 会让 level > prev_level 永不成立，
        # 「⚠ 升级」告警变成死代码（实测 none→high 仍只打印普通文案）。
        prev_sess = get_session(sid)
        prev_name = (prev_sess.last_risk if prev_sess else "") or ""
        prev_level = (_LEVEL_NAMES.index(prev_name)
                      if prev_name in _LEVEL_NAMES else None)
        tracer.risk_final(
            sid, state.get("turn", -1), level,
            {"rule": state.get("risk_rule", {}),
             "model": state.get("risk_model", {}),
             "multimodal": state.get("risk_multimodal", {}),
             "scale": state.get("risk_scale", {})},
            prev_level=prev_level,
        )
        tracer.context(sid, state.get("turn", -1), stats,
                       risk_level=_LEVEL_NAMES[min(level, 4)])
        # 把本轮等级固化到会话记录，供**下一轮**取 prev_level。
        # 原来只在收尾节点写 last_risk，而危机轮会被 interrupt 挂起、
        # 收尾节点根本不执行 —— 于是它永远停在 none，之后每轮都误报"升级"。
        sess_now = get_session(sid)
        if sess_now is not None:
            sess_now.last_risk = _LEVEL_NAMES[min(level, 4)]
            upsert_session(sess_now)
        # 上下文快照落 audit_trace（/api/traces/{sid} 与回放用）。
        # 与上面两个 tracer 事件同属本轮一次性记录，必须在幂等键内，
        # 否则 crisis / moderate 的二次调用会写出两条完全一样的记录。
        add_trace(sid, "context", system[:1500], f"risk_level={level}")

    # 用户消息落库（幂等：同一轮只写一次，靠 msg_persisted_turn 判重）
    turn_key = f"{sid}:{len(state.get('messages', []))}"
    if state.get("_persisted_turn") != turn_key:
        add_message(MessageRow(
            id=None, session_id=sid, role="user", content=_last_user_text(state),
            emotion=str(state.get("emotion", {}).get("primary_emotion", "")),
            risk_level=["none", "low", "moderate", "high", "imminent"][min(level, 4)],
            meta={"intensity": state.get("emotion", {}).get("intensity"),
                  "themes": state.get("emotion", {}).get("themes", [])},
        ))

    if level <= 0:
        return {"assembled_system": system, "context_stats": stats,
                "_persisted_turn": turn_key, "_traced_turn": _trace_key}

    level_name = ["none", "low", "moderate", "high", "imminent"][level]
    dims = []
    detail = state.get("risk_model", {})
    if level >= 2:
        dims.append("自杀意念")
    if detail.get("has_plan"):
        dims.append("有计划")
    if detail.get("has_preparation"):
        dims.append("准备行为")
    if detail.get("has_intent"):
        dims.append("有意图")

    # 已存在的证据不再重复写。
    #
    # ⚠ 去重范围必须按证据性质分开，否则不是漏报就是刷屏：
    #   · rule / model / multimodal = 用户**本轮说的话**，属急性事件。
    #     原先按 user 级永久去重、且把已处理（handled）的记录也算作"已存在"，
    #     后果是同一用户第二次说出同样的危机话术**永远不会再进审核队列**
    #     （实测：新会话连跑 3 个危机轮，审核队列 0 条 —— 直接漏报）。
    #     改为只在同一会话内、且该条仍未处理时去重：换会话就是新事件，
    #     已处理的事件再次发生也算新事件。
    #   · scale_item = 量表分数触发，属持续状态。同一份分数会每轮重复命中，
    #     必须按 user 级去重，否则审核队列被同一条分数刷屏。
    flags = list_risk_flags(user_id=uid, limit=500)
    dup_session = {(f.source, f.evidence)
                   for f in flags if f.session_id == sid and not f.handled}
    dup_user = {(f.source, f.evidence) for f in flags}
    sources = [
        ("rule", state.get("risk_rule", {}).get("evidence", "")),
        ("model", state.get("risk_model", {}).get("evidence", "")),
        ("multimodal", state.get("risk_multimodal", {}).get("evidence", "")),
        ("scale_item", state.get("risk_scale", {}).get("evidence", "")),
    ]
    for src, ev in sources:
        dup = dup_user if src == "scale_item" else dup_session
        if ev and (src, ev) not in dup:
            add_risk_flag(RiskFlag(
                id=None, user_id=uid, session_id=sid, ts=now_iso(), source=src,
                evidence=ev, level=level_name, cssrs_dimension="/".join(dims),
            ))

    return {"risk_detail": {"level_name": level_name, "dims": dims},
            "assembled_system": system, "context_stats": stats,
            "_persisted_turn": turn_key, "_traced_turn": _trace_key}


def _hotline_block() -> str:
    return "\n".join(f"- {h['name']} {h['number']}" for h in hotlines())


def safe_fallback_reply(level: int) -> str:
    """红线 4：模型超时/异常/被拦截时的兜底话术 + 热线，绝不静默失败。

    注意 crisis/moderate 分支绝对不能用 MockLLM 的 CBT 台词兜底 ——
    那是一句「能再多说一点吗」，在危机场景下等于放弃干预。
    """
    s = get_settings()
    lines = _hotline_block()
    if level >= 3:
        return (
            "我现在很担心你的安全。请先不要一个人待着。\n\n"
            "下面是现在就能打通的求助渠道，请尽快联系其中一个：\n"
            f"{lines}\n\n"
            f"如果此刻有即时危险，请立刻拨打 {s.hotline_emergency_medical} 或 "
            f"{s.hotline_emergency_police}。\n\n"
            "我已经通知了值班人员，很快会有人来跟进。你愿意现在告诉我，"
            "身边有没有可以联系的人吗？"
        )
    if level == 2:
        return (
            "谢谢你愿意告诉我这些。听起来你最近真的很难熬。\n\n"
            "如果你愿意，可以先和身边信任的人说一说；也可以随时拨打这些渠道：\n"
            f"{lines}\n\n"
            "我想先确认一下：你现在是安全的吗？"
        )
    return (
        "抱歉，我这边刚才有点卡住了，没能好好接住你的话。\n"
        "你愿意再说一次吗？我会认真听。\n\n"
        f"如果现在很难受，也可以先联系：{s.hotline_national}"
    )


def crisis_node(state: AgentState) -> dict[str, Any]:
    """危机协议：停止探索式提问，给热线，挂起等人工。"""
    from llm import complete, using_mock, was_degraded
    persist_risk_flags(state)

    s = get_settings()
    lines = "\n".join(f"- {h['name']} {h['number']}" for h in hotlines())
    system = (
        state["assembled_system"]
        + "\n\n## 危机干预指令（最高优先级）\n"
        + "1. 用一句话表达你听到了他的痛苦并在乎他的安全，不要说教。\n"
        + "2. 明确给出以下求助渠道：\n" + lines + "\n"
        + "3. 询问身边此刻是否有人能陪他，鼓励现在就联系一个人。\n"
        + "4. 不要再追问计划细节、不要再探索情绪成因。\n"
        + "5. 告知已通知值班人员。\n"
        + "6. 全程语气平稳、温暖、简短。"
    )
    reply = complete(system, _llm_messages(state))
    # 红线 4：只要不是真实模型生成的，就必须换成热线兜底话术。
    #
    # 只判 was_degraded() 不够 —— mock 模式下它恒为 False（没配真实模型，
    # 压根不会抛异常），于是会把 MockLLM 的 CBT 台词当成危机干预发出去。
    # 而 MockLLM.complete 只看**最后一句话**判风险：会话里累积了历史危机句
    # 之后，末句哪怕是"我今天吃了苹果"，本轮 final_level 仍是 high，
    # 却会拿到一句"谢谢你愿意跟我说这些"（实测复现）。
    if using_mock() or not reply or was_degraded():
        reply = safe_fallback_reply(final_level(state))

    return {
        "reply": reply,
        "crisis_mode": True,
        "hotlines": hotlines(),
        "interrupt_reason": f"检测到{['','轻度','中度','重度','危急'][min(final_level(state), 4)]}风险，已挂起等待人工审核",
    }


def human_review_node(state: AgentState) -> dict[str, Any]:
    """人在回路：high 及以上必须挂起等人工，不做自动化闭环。"""
    payload = {
        "session_id": state["session_id"],
        "user_id": state["user_id"],
        "risk_level": ["none", "low", "moderate", "high", "imminent"][min(state.get("risk_level", 4), 4)],
        "evidence": state.get("risk_evidence", []),
        "emotion": state.get("emotion", {}),
        "reply_draft": state.get("reply", ""),
        "message": "高风险会话，请人工审核后放行或改写回复",
    }
    # interrupt 抛出后图暂停；前端调用 resume 接口带上人工决定继续执行
    decision = interrupt(payload)
    if isinstance(decision, dict) and decision.get("action") == "override":
        return {"reply": decision.get("reply", state.get("reply", "")),
                "interrupt_reason": "已由人工改写"}
    if isinstance(decision, dict) and decision.get("action") == "approve":
        return {"interrupt_reason": "已由人工确认放行"}
    return {"interrupt_reason": "人工已处理"}


def moderate_node(state: AgentState) -> dict[str, Any]:
    """中度：CBT + 共同制定安全计划 + 提供热线。"""
    from llm import complete, using_mock, was_degraded
    persist_risk_flags(state)

    lines = "\n".join(f"- {h['name']} {h['number']}" for h in hotlines())
    system = (
        state["assembled_system"]
        + "\n\n## 中度风险干预指令\n"
        + "1. 先共情确认他的痛苦，不要急于解决问题。\n"
        + "2. 温和地确认安全：问他现在是否安全、身边有没有人。\n"
        + "3. 和他一起制定一份简单的安全计划：\n"
        + "   a) 识别预警信号  b) 自己能做的应对方式  c) 能联系的人  d) 求助渠道\n"
        + "4. 在合适时机告知求助渠道：\n" + lines + "\n"
        + "5. 不要追问具体方法细节。\n"
        + "6. 语气温和、不评判、一次只问一个问题。"
    )
    reply = complete(system, _llm_messages(state))
    # 同 crisis_node：mock 下 was_degraded() 恒为 False，必须用 using_mock()
    # 兜住，否则中度分支也会发出 mock 的通用 CBT 台词。
    if using_mock() or not reply or was_degraded():
        reply = safe_fallback_reply(2)
    return {"reply": reply, "crisis_mode": False, "hotlines": hotlines()}


def cbt_node(state: AgentState) -> dict[str, Any]:
    """轻/无风险：CBT 会谈流程，按阶段推进。"""
    from llm import complete
    from graph_state import CBT_STAGE_GUIDE

    stage = state.get("cbt_stage") or CBTStage.none.value
    guide = CBT_STAGE_GUIDE.get(stage, "")
    extra = CBT_SYS_EXTRA.format(guide=guide)
    if state.get("modality_inconsistent"):
        extra += "\n注意：观察到用户自述偏正面但表情/语音偏负面，请更温和谨慎地探询，不要直接指出矛盾。"

    reply = complete(state["assembled_system"] + extra, _llm_messages(state))
    if not reply:
        # 低风险分支没有热线索要，但仍不能静默失败
        reply = safe_fallback_reply(0)

    import tracer

    tracer.cbt_stage(state["session_id"], stage,
                     CBT_NEXT.get(stage, CBTStage.affect_labeling.value),
                     turn=state.get("turn", -1))
    return {
        "reply": reply,
        "crisis_mode": False,
        "cbt_stage": CBT_NEXT.get(stage, CBTStage.affect_labeling.value),
    }


def _user_texts(msgs: list) -> list[str]:
    """从 messages 里取出用户说的话。

    兼容两种形态：LangGraph 的 add_messages 会把 dict 转成 Message 对象，
    但首轮 invoke 直接传入的仍是 dict，两种都要能吃下。
    """
    out: list[str] = []
    for m in msgs:
        if isinstance(m, dict):
            role, content = m.get("role"), m.get("content")
        else:
            role, content = getattr(m, "type", None), getattr(m, "content", None)
        if role in ("user", "human") and isinstance(content, str) and content.strip():
            out.append(content.strip())
    return out


def memory_writer_node(state: AgentState) -> dict[str, Any]:
    """记忆写回：事实抽取 + 滚动摘要 + 消息落库 + 会话状态更新。"""
    from llm import structured
    import memory
    from memory import summarize_history, write_facts

    uid, sid = state["user_id"], state["session_id"]
    text = _last_user_text(state)

    # 1) 事实抽取（长期记忆写入）
    s = get_settings()
    msgs = state.get("messages", []) or []
    turn_no = len(msgs)
    last = int(state.get("last_fact_turn") or 0)
    facts_dict: dict[str, list[str]] = {}
    new_last = last

    # 首轮必抽：冷启动用户最需要画像，等满 N 轮再抽会让前几轮完全「裸奔」
    # （且实测新用户常只聊 1~2 轮就走，那时画像恒为空）。
    # 代价可接受 —— 实测 thinking=False 的抽取约 1.0~1.4s，不是原先估的 12s。
    if turn_no == 1 or turn_no - last >= s.fact_extract_every:
        # ⚠ 抽取窗口必须是「自上次抽取以来的全部用户文本」，不能只看最后一句。
        #    原实现传 _last_user_text()，中间几轮说过的事实会永久丢失：
        #    实测聊 4 轮只落 2 条，第 1 轮的「养了只猫叫豆豆」永远进不了画像。
        window = "\n".join(_user_texts(msgs[last:])) or text
        # thinking=False：事实抽取是信息搬运（抽取原文明确提到的内容），
        # 不需要推理。安全语义不受影响 —— 这条路径不参与任何风险判定。
        facts: ExtractedFacts = structured(ExtractedFacts, FACT_SYS,
                                           f"用户说：{window}", thinking=False)
        write_facts(uid, facts)
        facts_dict = facts.model_dump()
        new_last = turn_no

    # 1.5) 沉淀为当日记录（纯文字对话此前完全不写，导致「近日观察」恒为空）
    try:
        memory.persist_daily_from_chat(
            uid, text,
            emotion=state.get("emotion", {}),
            risk_level=["none", "low", "moderate", "high", "imminent"][
                min(final_level(state), 4)],
        )
    except Exception:  # noqa: BLE001  记录失败不能影响本轮回复
        pass

    # 2) 消息落库（用户消息已在 persist_flags 落过，这里只落回复）
    level_name = ["none", "low", "moderate", "high", "imminent"][min(final_level(state), 4)]
    emo = state.get("emotion", {})
    add_message(MessageRow(
        id=None, session_id=sid, role="assistant", content=state.get("reply", ""),
        risk_level=level_name,
        meta={"crisis": state.get("crisis_mode", False),
              "stage": state.get("cbt_stage", "")},
    ))

    # 3) 滚动摘要：每 6 条消息压缩一次（风险原话已在 risk_flag 独立留存）
    summary = state.get("rolling_summary", "")
    stored = list_messages(sid, limit=200)
    if len(stored) % 6 == 0 and len(stored) >= 6:
        pending = [{"role": m.role, "content": m.content} for m in stored[-6:-2]]
        summary = summarize_history(summary, pending)

    # 4) 会话状态
    sess = get_session(sid) or SessionRow(id=sid, user_id=uid, started_at=now_iso())
    sess.last_risk = level_name
    sess.cbt_stage = state.get("cbt_stage", sess.cbt_stage)
    sess.summary = summary
    sess.interrupted = bool(state.get("crisis_mode"))
    upsert_session(sess)

    # 5) 审计 trace
    add_trace(sid, "turn", str(state.get("context_stats", {}))[:500],
              (state.get("reply", ""))[:500])

    # 6) 可观测：本轮收尾
    import tracer

    tracer.memory_write(sid, uid, facts_dict, None, turn=state.get("turn", -1))

    return {
        "rolling_summary": summary,
        "facts": facts_dict,
        "last_fact_turn": new_last,
        "trace": [*state.get("trace", []), {
            "ts": now_iso(),
            "risk": level_name,
            "emotion": emo.get("primary_emotion"),
            "stage": state.get("cbt_stage"),
            "ctx": state.get("context_stats", {}),
        }],
    }


# ------------------------------------------------------------------ 构图
def build_graph():
    g = StateGraph(AgentState)

    g.add_node("entry", entry_node)
    g.add_node("risk_detector", risk_detector_node)
    g.add_node("context_builder", context_builder_node)
    g.add_node("persist_flags", persist_risk_flags)
    g.add_node("crisis", crisis_node)
    g.add_node("human_review", human_review_node)
    g.add_node("moderate", moderate_node)
    g.add_node("cbt", cbt_node)
    g.add_node("memory_writer", memory_writer_node)

    g.set_entry_point("entry")
    # 并行：安全通道 与 上下文装配+情绪/模型风险 同时开始
    g.add_edge("entry", "risk_detector")
    g.add_edge("entry", "context_builder")
    g.add_edge("risk_detector", "persist_flags")
    g.add_edge("context_builder", "persist_flags")

    g.add_conditional_edges(
        "persist_flags",
        risk_gate,
        {"crisis": "crisis", "moderate": "moderate", "cbt": "cbt"},
    )
    # 危机路径：生成危机话术后必须过人工
    g.add_edge("crisis", "human_review")
    g.add_edge("human_review", "memory_writer")
    g.add_edge("moderate", "memory_writer")
    g.add_edge("cbt", "memory_writer")
    g.add_edge("memory_writer", END)
    return g


_checkpointer = None
_app = None


def get_checkpointer():
    """checkpointer：memory（演示）或 sqlite（持久化、可回放）。"""
    global _checkpointer
    if _checkpointer is None:
        s = get_settings()
        if s.checkpointer == "sqlite":
            from langgraph.checkpoint.sqlite import SqliteSaver
            import sqlite3
            conn = sqlite3.connect(s.checkpoint_conn, check_same_thread=False)
            _checkpointer = SqliteSaver(conn)
        else:
            _checkpointer = MemorySaver()
    return _checkpointer


def get_app():
    global _app
    if _app is None:
        _app = build_graph().compile(checkpointer=get_checkpointer())
    return _app


def new_session(user_id: str, cbt_stage: str = "none") -> str:
    sid = f"ses_{uuid.uuid4().hex[:12]}"
    upsert_session(SessionRow(id=sid, user_id=user_id, started_at=now_iso(),
                              cbt_stage=cbt_stage))
    return sid


def run_turn(user_id: str, session_id: str, user_text: str,
             resume_payload: dict | None = None,
             voice: dict | None = None) -> dict[str, Any]:
    """执行一轮对话。

    resume_payload 用于在 interrupt 后恢复（人工审核决定）。
    voice 为本轮语音的感知结果（{audio_features, tone, tone_reason, summary}），
    来自 /api/asr；无语音时传 None。
    """
    app = get_app()
    cfg = {"configurable": {"thread_id": session_id}}

    import time as _time

    _t0 = _time.monotonic()

    import tracer

    prev_sess = get_session(session_id)
    # 轮次号按**用户消息数**算，不能用 len(messages)//2：
    # 危机轮会被 interrupt 挂起、助手消息根本不落库，除法会算重
    # （实测第 4 轮仍被标成第 3 轮）。
    turn_no = sum(1 for m in list_messages(session_id, limit=200)
                  if getattr(m, "role", "") == "user") + 1
    tracer.turn_start(
        session_id, user_id,
        turn=turn_no,
        text=user_text or "(resume)",
    )

    if resume_payload is not None:
        from langgraph.types import Command as _Cmd
        out = app.invoke(_Cmd(resume=resume_payload), config=cfg)
    else:
        voice = voice or {}
        out = app.invoke(
            {
                "user_id": user_id,
                "session_id": session_id,
                "turn": turn_no,
                "messages": [{"role": "user", "content": user_text}],
                "rolling_summary": (get_session(session_id).summary
                                    if get_session(session_id) else ""),
                "voice_summary": voice.get("summary", ""),
                "voice_tone": voice.get("tone", ""),
                "voice_features": voice.get("audio_features", {}),
            },
            config=cfg,
        )

    state = app.get_state(cfg)
    interrupted = bool(getattr(state, "next", None))
    from llm import was_degraded

    latency_ms = int((_time.monotonic() - _t0) * 1000)
    tracer.turn_end(session_id, turn_no, latency_ms,
                    len(out.get("reply", "")),
                    crisis=bool(out.get("crisis_mode")),
                    degraded=was_degraded())

    return {
        "latency_ms": latency_ms,
        "reply": out.get("reply", ""),
        "degraded": was_degraded(),
        "risk_level": ["none", "low", "moderate", "high", "imminent"][
            min(out.get("risk_level", 0), 4)],
        "risk_detail": out.get("risk_detail", {}),
        "risk_evidence": out.get("risk_evidence", []),
        "channels": {
            "rule": out.get("risk_rule", {}),
            "model": out.get("risk_model", {}),
            "multimodal": out.get("risk_multimodal", {}),
            "scale": out.get("risk_scale", {}),
        },
        "emotion": out.get("emotion", {}),
        "cbt_stage": out.get("cbt_stage", ""),
        "crisis_mode": out.get("crisis_mode", False),
        "hotlines": out.get("hotlines", []),
        "context_stats": out.get("context_stats", {}),
        "facts": out.get("facts", {}),
        "interrupted": interrupted,
        "interrupt_reason": out.get("interrupt_reason", ""),
        "pending_review": (state.tasks[0].interrupts[0].value
                           if interrupted and state.tasks and state.tasks[0].interrupts else None),
    }
