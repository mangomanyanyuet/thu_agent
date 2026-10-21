"""陪伴会话编排：话术、时长控制、风险处理、报告生成。"""
from __future__ import annotations

import time

from config import get_settings
from llm import complete, sanitize_reply, strip_thinking, structured
from models import RiskAssessment
from perception import hotlines, rule_risk_level, voice_risk_signal

from .fusion import (
    ALL_EMOTIONS,
    acoustic_to_va,
    consistency,
    face_emotion,
    face_expressiveness,
    fuse,
    text_emotion,
)
from .state import (
    IDLE_CONFIRM_AT,
    IDLE_END_AT,
    SILENCE_PROMPT,
    Phase,
    SessionState,
)

# ------------------------------------------------------------------ 话术（PRD §6.2）
ROLE = """你是「小伴」，一款 AI 情绪陪伴助手。

【身份边界】
- 你是 AI 工具，不是心理医生、不是心理咨询师，绝不使用这类头衔自称。
- 只做情绪觉察与轻度支持，**不诊断、不开药、不给治疗建议**。

【语气】温和、缓慢、非评判，参考人本主义的「无条件积极关注」。

【核心原则】
1. 先共情后提问（Reflect → Ask）：先回应你听到的感受，再问下一个问题。
2. 多用开放式问题，少用封闭式是非题。
3. 一次只问一个问题，句子要短（1-3 句），像真人说话，不要书面语。
4. 避免连续追问造成压迫感；追问深度不超过两层。
5. 避免诊断性词汇（如"你这是焦虑症""你抑郁了"）。
6. 不要复述/总结用户的整段话，只回应其中最触动的那一点。
7. 不主动提及"情绪识别""分析结果""系统会判断"等技术实现。

【禁止】- 不道歉式开头（"很抱歉听到…"少用）
- 不说"我能理解你的感受"（未经证实）
- 不输出任何 JSON、标签、括号说明，只输出你要说的话本身

【输出格式（必须严格遵守）】
只输出一行以「回复：」开头的内容，后面紧接你要对用户说的话。
不要写任何分析、思考过程、目标说明、策略讨论，也不要解释你为什么这么说。
"""

OPENING = ("嗨，很高兴今天能和你聊聊。接下来的几分钟，我想单纯听听你最近的状态，"
           "没有对错，你想到什么都可以说。先随便聊聊——今天整体感觉怎么样？")

SILENCE_PROMPT_TEXT = "不着急，你可以慢慢说。"

# 静默 IDLE_CONFIRM_AT(60s) 后的确认话术；再过 IDLE_END_AT-IDLE_CONFIRM_AT(30s)
# 仍无回应，才说 CLOSING。收尾不该由轮数决定，该由"用户还想不想聊"决定。
IDLE_CONFIRM_TEXT = "看到你很久没回复了，这次先聊到这里吗"

WRAP_UP_HINT = "（本轮回应后，用一句话自然地把对话引向收尾，不要再开启新话题）"

CLOSING = ("谢谢你今天的分享，希望接下来的时间你能对自己好一点。"
           "今天的情绪小结整理好之后，你可以在界面上看到。")

WATCH_ADDENDUM = ("另外想跟你说一句：如果最近这种状态持续了一段时间，和信任的人聊聊、"
                  "或者了解一下专业的心理支持渠道，可能会有帮助。")

CRISIS_TEXT = ("听到你说的这些，我很在意你现在的安全。\n\n"
               "我不是专业的危机干预人员，但有一些人可以现在就帮到你——"
               "如果你有伤害自己的想法，请立刻联系下面这些渠道，或让身边的人陪着你。"
               "你现在不是一个人。")

MAIN_QUESTION_GUIDE = {
    1: "围绕用户刚提到的具体情境追问一层：当时是什么感觉、身体有什么反应。",
    2: "如果上一轮已触及情绪，追问这件事对ta的影响或ta是如何应对的；"
       "如果还停留在事件描述，引导到感受。",
    3: "问一个更轻的、面向当下的或支持性的问题（如：最近有什么让你稍微松口气的事吗）。",
    4: "收束：邀请用户说一句此刻最想对自己说的话，或最希望被理解的一点。",
}

# 第 5 轮及以后（只在用户确实还想聊、且未到 4:00 收尾提醒时才会出现）
CONTINUE_GUIDE = ("继续围绕用户此刻的感受倾听并回应，不要开启与情绪无关的新话题，"
                  "也不要替用户宣布结束。")

SELF_CARE_TIPS = {
    "平静": ["看起来你今天状态比较稳，可以把这种节奏记下来，下次心乱时作为参照。"],
    "愉悦": ["可以把今天让你感觉不错的小事记下来，低落时翻一翻会很有用。"],
    "焦虑": ["如果脑子里的事太多，试着把它们写在纸上，把「担心」从脑子里移出来。",
             "试试缓慢的腹式呼吸：吸气 4 秒、呼气 6 秒，重复几次。"],
    "低落": ["今天不必勉强自己高效，允许自己慢一点。",
             "如果可以，做一件很小但确定的事，比如出门走十分钟。"],
    "烦躁": ["先给自己一点物理空间，离开当前环境几分钟，情绪会自然降一点。"],
    "疲惫": ["今天优先保证睡眠，把非必要的安排往后挪一挪。"],
    "紧张": ["试试把注意力放到脚踩地面的感觉上，做几次深长的呼气。"],
    "惊讶": ["如果这件事还在消化中，不用急着下结论，给自己一点时间。"],
}


class CompanionEngine:
    def __init__(self, state: SessionState):
        self.s = state

    # ---------------------------------------------------------------- 开场
    def opening_text(self) -> str:
        return OPENING

    # ---------------------------------------------------------------- 主体
    def respond(self, user_text: str, audio: dict | None = None,
                face: dict | None = None) -> dict:
        """处理一轮用户回答，返回 {reply, phase, fused, risk_level, ...}。"""
        s = self.s
        s.mark_user_input()
        turn_id = s.turn_count + 1

        # ---- 1) 三通道情绪 ----
        t_label, t_conf, tv, ta = text_emotion(user_text)
        ac_v, ac_a, ac_conf = acoustic_to_va(audio)
        f_label, f_conf, fv, fa = face_emotion((face or {}).get("blendshapes"))
        audio_ok = bool(audio and audio.get("duration_sec", 0) > 0.3)
        face_ok = bool(face and face.get("blendshapes") and face.get("face_detected"))

        # 关键词结果只作兜底 & 生成时的"倾听参考"；LLM 判定出来后会覆盖
        fused = fuse(t_label, t_conf, ac_v, ac_a, ac_conf,
                     f_label, f_conf, audio_ok, face_ok)

        # ---- 可观测：本轮各模态判断 + 融合结果（LLM 情绪拿到后会再记一次）----
        import tracer

        def _log_emotion(fused_now: dict, text_src: str) -> None:
            tracer.emotion_channels(s.session_id, turn_id, {
                "文本": {"label": t_label, "confidence": t_conf},
                "声学": {"valence": ac_v, "arousal": ac_a},
                "面部": {"label": f_label, "confidence": f_conf},
            })
            tracer.emotion(
                s.session_id, turn_id,
                label=fused_now.get("label") or "未知",
                valence=float(fused_now.get("valence") or 0.0),
                arousal=float(fused_now.get("arousal") or 0.0),
                confidence=float(fused_now.get("confidence") or 0.0),
                weights=fused_now.get("weights"),
                audio_ok=audio_ok, face_ok=face_ok,
                text_source=text_src,
            )

        _log_emotion(fused, "keyword")

        # ---- 2) 风险（独立于生成，PRD §7.5）----
        risk_lvl, risk_ev, risk_ch = self._detect_risk(user_text, audio, face, fused)

        import tracer

        # prev_level 必须恒传实际值。原来只在等级变化时传、否则传 None，
        # 结果日志里绝大多数行 prev_level 都是 null，"只升不降"根本没法核对。
        prev_lvl = s.risk_level or "none"
        _LVL2NUM = {"none": 0, "watch": 1, "high": 3}
        tracer.risk_final(s.session_id, turn_id,
                          _LVL2NUM.get(risk_lvl, 0),
                          {name: {"level": lvl} for name, lvl in risk_ch.items()},
                          prev_level=_LVL2NUM.get(prev_lvl, 0))
        # 逐通道记录：合并后再复制一份给三个通道会掩盖真正的触发来源
        for name, lvl in risk_ch.items():
            tracer.risk_channel(s.session_id, turn_id, name, lvl, "")
        for ev in risk_ev:
            tracer.risk_channel(s.session_id, turn_id, "证据", risk_lvl, ev)

        # ---- 3) 降级记录（PRD §6.4）----
        if not audio_ok:
            s.note_degradation(f"第 {turn_id} 轮未采集到有效语音")
        if not face_ok:
            s.note_degradation(f"第 {turn_id} 轮面部检测失败（光线/遮挡/未授权）")
        s.bump_modality("voice", audio_ok)
        s.bump_modality("face", face_ok)

        # ---- 4) 记录本轮 ----
        s.turns.append({
            "turn_id": turn_id,
            "ts": round(time.time() - s.started_at, 1),
            "text": user_text,
            "text_emotion": {"label": t_label, "confidence": t_conf},
            "acoustic_emotion": {"valence": ac_v, "arousal": ac_a},
            "facial_emotion": {"label": f_label, "confidence": f_conf,
                               "expressiveness": face_expressiveness(
                                   (face or {}).get("blendshapes"))},
            "fused_emotion": fused,
            "risk_level": risk_lvl,
            # 原始感知输入（均为数值，不含原始音视频 —— PRD §8 数据最小化）。
            # 会话收尾时 daily_bridge 要按轮聚合这些数值写入 daily_record，
            # 只留 *emotion 的聚合结果会丢失声学/面部明细。
            "audio": audio or {},
            "face": face or {},
        })
        s.history.append({"role": "user", "content": user_text})

        if risk_ev:
            s.risk_evidence.extend([e for e in risk_ev if e not in s.risk_evidence])

        # ---- 5) 生成回复 ----
        if risk_lvl == "high":
            s.phase = Phase.CRISIS
            s.risk_level = "high"
            s.resource_suggested = True
            return self._crisis_resp()

        if risk_lvl == "watch" and s.risk_level == "none":
            s.risk_level = "watch"

        reply, llm_emotion = self._generate_reply(turn_id, fused)

        # ---- 5.5) 用 LLM 判定的文本情绪重新融合 ----
        # 关键词结果只用于生成时的"倾听参考"（必须同步可得）；
        # 真正记入报告与每日记录的，优先用 LLM 判定。
        if llm_emotion:
            t_label = llm_emotion["label"]
            t_conf = llm_emotion["confidence"]
            tv = llm_emotion["valence"]
            ta = llm_emotion["arousal"]
            fused = fuse(t_label, t_conf, ac_v, ac_a, ac_conf,
                         f_label, f_conf, audio_ok, face_ok,
                         text_valence=tv, text_arousal=ta)
            # 覆盖本轮已写入的记录
            s.turns[-1]["text_emotion"] = {"label": t_label, "confidence": t_conf}
            s.turns[-1]["fused_emotion"] = fused
            _log_emotion(fused, "llm")

        # ---- 6) 阶段推进（PRD §6.1）----
        nxt = s.next_phase()
        import tracer

        tracer.phase(s.session_id, s.phase.value, nxt.value,
                     reason=f"轮次 {s.turn_count} / 已用 {round(s.elapsed)}s")
        if nxt == Phase.CLOSING and s.phase != Phase.CLOSING:
            s.phase = Phase.CLOSING
            reply = self._closing(reply)
        else:
            s.phase = nxt

        s.history.append({"role": "assistant", "content": reply})
        return {
            "reply": reply,
            "phase": s.phase.value,
            "turn_id": turn_id,
            "fused": fused,
            "channels": {
                "text": {"label": t_label, "confidence": t_conf},
                "acoustic": {"valence": ac_v, "arousal": ac_a, "confidence": ac_conf},
                "face": {"label": f_label, "confidence": f_conf,
                         "expressiveness": face_expressiveness((face or {}).get("blendshapes"))},
            },
            "risk_level": s.risk_level,
            "should_finish": s.phase in (Phase.DONE, Phase.CRISIS),
        }

    # ---------------------------------------------------------------- 生成
    def _generate_reply(self, turn_id: int, fused: dict) -> tuple[str, dict | None]:
        """生成回复，同时让模型隐式输出本轮用户的文本情绪判定。

        返回 (reply, emotion)。emotion 为 None 表示解析失败，
        调用方应回落到关键词匹配 —— 绝不因为拿不到情绪就中断对话。

        一次调用两份产出，是为了不增加单轮延迟（单独再调一次实测约 +6s）。
        """
        from config import get_settings
        from .llm_emotion import OUTPUT_SPEC, parse_reply_and_emotion

        s = self.s
        # 关闭时退回关键词匹配（延迟优先场景）
        system = ROLE + (OUTPUT_SPEC if get_settings().companion_llm_emotion else "")

        # 轮次没有上限，超出引导表的部分用通用目标，避免第 5 轮起误发"收束"指令
        guide = MAIN_QUESTION_GUIDE.get(turn_id) or CONTINUE_GUIDE
        system += f"\n\n【本轮目标】{guide}"

        # 把感知到的情绪作为"倾听线索"给模型，但明确要求它**不要**复述或播报
        if fused.get("confidence", 0) > 0.3:
            system += (
                f"\n【倾听参考】从语气和表情看，用户此刻可能偏向「{fused['label']}」"
                f"（愉悦度 {fused['valence']}，唤醒度 {fused['arousal']}）。"
                "\n这只是参考，可能不准。你可以据此调整语气的轻重与节奏，"
                "但**绝对不要**在回复里提到这个判断、不要复述这些词、不要说"
                "「我注意到你…」这类分析性表述。你的任务只是倾听和回应。"
            )

        if s.should_wrap_up() and not s.wrap_up_hinted:
            system += "\n" + WRAP_UP_HINT
            s.wrap_up_hinted = True

        # 模态不一致 -> 更谨慎地探询（PRD §6.3.3 掩饰信号）
        if self._incongruent(fused):
            system += ("\n【注意】用户的言语内容与表情/语气可能存在不一致，"
                       "请温和地多给一点空间，不要直接指出矛盾。")

        # 重复守卫：上一轮说过一模一样的话，加一条约束重试一次
        prev_assistant = [m["content"] for m in s.history if m["role"] == "assistant"]

        messages = list(s.history[-6:])
        reply, emotion = "", None
        try:
            # 注意：必须先把 <reply>/<emotion> 标签留下再清洗，
            # sanitize_reply() 会按泄漏特征词砍行，可能把标签段删掉。
            raw = complete(system, messages)
            reply, emotion = parse_reply_and_emotion(raw)
            reply = _sanitize(reply)
            if reply and reply in prev_assistant:
                system += ("\n\n【重要】你上一轮已经说过几乎一模一样的话，"
                           "这次必须换一个角度、换一种说法。")
                raw = complete(system, messages)
                reply, emo2 = parse_reply_and_emotion(raw)
                reply = _sanitize(reply)
                emotion = emotion or emo2
        except Exception:  # noqa: BLE001
            reply, emotion = "", None
        return (reply or self._fallback_reply(turn_id)), emotion

    def _fallback_reply(self, turn_id: int) -> str:
        # 原来第 4 条兜底是"今天先聊到这里也挺好的" —— 那是用兜底话替用户宣布结束。
        # 轮次既然已不是终止条件，兜底就只能负责"把话接住"，不能负责收尾。
        base = ["嗯，我在听。能再多说一点吗？",
                "听起来这件事对你影响不小。",
                "谢谢你愿意告诉我这些。"]
        return base[min(turn_id - 1, len(base) - 1)]

    def _incongruent(self, fused: dict) -> bool:
        if not self.s.turns:
            return False
        last = self.s.turns[-1]
        tl = last["text_emotion"]["label"]
        fl = last["facial_emotion"]["label"]
        if not fl:
            return False
        pos = tl in ("愉悦", "平静")
        neg_face = fl in ("低落", "焦虑", "疲惫", "烦躁", "紧张")
        return pos and neg_face

    def _closing_tail(self) -> str:
        """结束语正文（不含上一轮的回应）。

        抽出来是为了让"静默超时收尾"和"正常收尾"共用同一套文案与资源转介逻辑。
        """
        text = CLOSING
        if self.s.risk_level == "watch":
            text += f"\n\n{WATCH_ADDENDUM}"
            self.s.resource_suggested = True
        return text

    def _closing(self, prev_reply: str) -> str:
        return f"{prev_reply}\n\n{self._closing_tail()}"

    def _crisis_resp(self) -> dict:
        return {
            "reply": CRISIS_TEXT,
            "phase": Phase.CRISIS.value,
            "hotlines": hotlines(),
            "risk_level": "high",
            "should_finish": True,
            "channels": None,
            "fused": None,
            "turn_id": self.s.turn_count,
        }

    # ---------------------------------------------------------------- 风险
    def _detect_risk(self, text: str, audio: dict | None, face: dict | None,
                     fused: dict) -> tuple[str, list[str], dict[str, str]]:
        """PRD §7.5.2 三通道并集升级。

        返回 (none|watch|high, 证据, 逐通道等级)。逐通道等级必须一并返回：
        之前只回传合并后的等级，日志把同一个值复制给三个通道，看起来像
        "三路都这么判"，实际掩盖了到底是哪一路触发的。
        """
        evidence: list[str] = []
        levels = []
        rule_lvl = model_lvl = mm_lvl = 0

        def _name(n: int) -> str:
            return "high" if n >= 2 else ("watch" if n >= 1 else "none")

        def _pack(evaluated: tuple[str, ...] = ("规则", "模型", "多模态")) -> dict[str, str]:
            """组装逐通道等级。

            规则/模型通道命中 high 时会短路返回，后面的通道其实**没被评估**。
            这时不能填 none —— 那会让审核员以为"模型看过、判为无风险"。
            """
            out = {"规则": _name(rule_lvl), "模型": _name(model_lvl),
                   "多模态": _name(mm_lvl)}
            for k in list(out):
                if k not in evaluated:
                    out[k] = "未评估"
            return out

        # 通道 1：文本规则（分级词表）
        rule_lvl, rule_ev = rule_risk_level(text)
        if rule_lvl:
            levels.append(rule_lvl)
            if rule_ev:
                evidence.append(f"[文本规则] {rule_ev}")
        # 规则引擎判为 moderate 及以上（有主动意念/计划/准备）即 high
        if rule_lvl >= 2:
            return "high", evidence, _pack(("规则",))

        # 通道 2：模型语义（隐晦表达、隐喻、反讽）
        model_lvl, model_ev = 0, ""
        try:
            ra: RiskAssessment = structured(
                RiskAssessment,
                "你是独立的风险判定器。唯一职责是保守地识别自伤/自杀风险，"
                "宁可误报，绝不可漏报。",
                f"用户说：{text}",
            )
            model_lvl = ["none", "low", "moderate", "high", "imminent"].index(ra.level.value)
            if ra.evidence:
                model_ev = ra.evidence
        except Exception:  # noqa: BLE001
            pass
        if model_lvl >= 2:
            levels.append(model_lvl)
            if model_ev:
                evidence.append(f"[模型语义] {model_ev}")
            return "high", evidence, _pack(("规则", "模型"))

        # 通道 3：多模态（声学 + 面部不一致）
        tone = (audio or {}).get("tone", "") if audio else ""
        v_lvl, v_ev = voice_risk_signal(tone, audio or {}, text)
        if v_lvl:
            levels.append(v_lvl)
            mm_lvl = max(mm_lvl, v_lvl)
            if v_ev:
                evidence.append(f"[语音声学] {v_ev}")
        # 面部持续低落 + 文本消极 -> watch
        f_label = (face or {}).get("blendshapes") and fused.get("label")
        if f_label in ("低落",) and fused.get("valence", 0) < -0.5:
            levels.append(1)
            mm_lvl = max(mm_lvl, 1)
            evidence.append("[面部表情] 持续低落且效价显著偏负")

        # 轻度消极关键词 -> watch（证据标的是 [文本规则]，归规则通道）
        watch_words = ["没意思", "没意义", "撑不住", "活不下去", "累死了",
                       "不想动", "没有人在乎", "我是负担"]
        if any(w in text for w in watch_words):
            levels.append(1)
            rule_lvl = max(rule_lvl, 1)
            evidence.append(f"[文本规则] 持续消极表达")

        if not levels:
            return "none", [], _pack()
        return ("high" if max(levels) >= 2 else "watch"), evidence, _pack()

    # ---------------------------------------------------------------- 静默
    def silence_check(self) -> dict:
        """静默阶梯（PRD §7.1）：由"用户还回不回复"决定收尾，而不是由轮次决定。

        | 静默时长 | action  | 话术                             |
        |----------|---------|----------------------------------|
        | ≥ 8s     | nudge   | 不着急，你可以慢慢说。            |
        | ≥ 60s    | confirm | 看到你很久没回复了，这次先聊到这里吗 |
        | ≥ 90s    | end     | CLOSING（+ watch 时的资源转介）    |

        返回 {"prompt": str|None, "action": none|nudge|confirm|end,
              "idle_sec": float, "phase": str}。

        每次调用最多推进一步；用户一开口 mark_user_input() 会把阶梯清零，
        所以"确认之后又聊起来了"不会在 30s 后被强行结束。
        调用方（前端轮询）必须能拿到 action == "end" 并据此收尾。
        """
        s = self.s
        gap = s.seconds_since_user()
        out = {"prompt": None, "action": "none",
               "idle_sec": round(gap, 1), "phase": s.phase.value}

        # 已经收尾/危机/退出的会话不再推进静默阶梯
        if s.phase in (Phase.CLOSING, Phase.DONE, Phase.CRISIS, Phase.ABORTED):
            return out

        if s.idle_stage >= 1 and gap >= IDLE_END_AT:
            s.idle_stage = 2
            out["action"] = "end"
            out["prompt"] = self.close_for_idle()
            out["phase"] = s.phase.value
            return out

        if s.idle_stage == 0 and gap >= IDLE_CONFIRM_AT:
            s.idle_stage = 1
            out["action"] = "confirm"
            out["prompt"] = IDLE_CONFIRM_TEXT
            return out

        if s._silence_prompted == 0 and gap >= SILENCE_PROMPT:
            s._silence_prompted = 1
            out["action"] = "nudge"
            out["prompt"] = SILENCE_PROMPT_TEXT
            return out
        return out

    def close_for_idle(self) -> str:
        """静默超时后的收尾：相位进 closing，返回结束话术。"""
        import tracer

        s = self.s
        prev = s.phase.value
        s.phase = Phase.CLOSING
        text = self._closing_tail()
        s.history.append({"role": "assistant", "content": text})
        tracer.phase(s.session_id, prev, s.phase.value,
                     reason=f"静默 {round(s.seconds_since_user())}s 无回复")
        return text

    # ---------------------------------------------------------------- 报告
    def build_report(self) -> dict:
        """PRD §9.2/§9.3 三层报告。"""
        s = self.s
        turns = s.turns

        if s.risk_level == "high" or s.phase == Phase.CRISIS:
            # §9.2 硬性规定 3：报告流程让位于关怀+转介
            return {
                "session_id": s.session_id,
                "risk_override": True,
                "risk_level": "high",
                "resource_suggested": True,
                "message": CRISIS_TEXT,
                "hotlines": hotlines(),
                "tier1_summary": None,
                "tier2_detail": None,
                "tier3_internal": {
                    "risk_triggered": True,
                    "risk_level": "high",
                    "resource_suggested": True,
                    "risk_evidence": s.risk_evidence,
                    "modality_availability": s.modality,
                    "degradation_notes": s.degradation_notes,
                },
            }

        if not turns:
            return {"session_id": s.session_id, "error": "no_turns",
                    "tier1_summary": None, "tier2_detail": None,
                    "tier3_internal": {"degradation_notes": ["用户中途退出"]}}

        # --- Tier 2：分布 / 轨迹 / 维度均值 ---
        dist: dict[str, float] = {e: 0.0 for e in ALL_EMOTIONS}
        for t in turns:
            dist[t["fused_emotion"]["label"]] = dist.get(
                t["fused_emotion"]["label"], 0.0) + 1
        total = sum(dist.values()) or 1
        dist = {k: round(v / total, 2) for k, v in dist.items() if v}

        trajectory = [
            {"turn_id": t["turn_id"],
             "valence": t["fused_emotion"]["valence"],
             "arousal": t["fused_emotion"]["arousal"]}
            for t in turns
        ]
        avg_v = round(sum(t["fused_emotion"]["valence"] for t in turns) / len(turns), 2)
        avg_a = round(sum(t["fused_emotion"]["arousal"] for t in turns) / len(turns), 2)

        # 显著变化点：相邻轮 valence 变化 > 0.4
        shifts = []
        for i in range(1, len(turns)):
            d = abs(turns[i]["fused_emotion"]["valence"]
                    - turns[i - 1]["fused_emotion"]["valence"])
            if d >= 0.4:
                shifts.append({"turn_id": turns[i]["turn_id"],
                               "topic_keyword": _topic(turns[i]["text"])})

        ranked = sorted(dist.items(), key=lambda kv: -kv[1])
        dominant = ranked[0][0]
        secondary = ranked[1][0] if len(ranked) > 1 and ranked[1][1] > 0.12 else None

        # --- Tier 3 ---
        vs = [t["fused_emotion"]["valence"] for t in turns]
        ars = [t["fused_emotion"]["arousal"] for t in turns]
        volatility = round(_stdev(vs) * 0.6 + _stdev(ars) * 0.4, 3)
        cons = round(sum(consistency(t["text_emotion"]["label"],
                                     t["facial_emotion"]["label"])
                         for t in turns) / len(turns), 2)
        incongruent = cons < 0.4 and any(t["facial_emotion"]["label"] for t in turns)

        return {
            "session_id": s.session_id,
            "risk_override": False,
            "tier1_summary": {
                "dominant_emotion": dominant,
                "mood_statement": _mood_statement(dominant, secondary, avg_v, avg_a),
                "secondary_emotion": secondary,
                "self_care_tips": SELF_CARE_TIPS.get(dominant, [])[:2],
                "duration_sec": int(s.elapsed),
                "turn_count": len(turns),
            },
            "tier2_detail": {
                "avg_valence": avg_v,
                "avg_arousal": avg_a,
                "emotion_distribution": dist,
                "emotion_trajectory": trajectory,
                "key_shift_points": shifts,
            },
            "tier3_internal": {
                "valence_range": [min(vs), max(vs)],
                "arousal_range": [min(ars), max(ars)],
                "emotional_volatility": volatility,
                "voice_face_consistency": cons,
                "incongruence_flag": incongruent,
                "incongruence_note": ("言语与表情存在明显背离，建议以更谨慎的方式理解"
                                      if incongruent else ""),
                "overall_confidence": round(
                    sum(t["fused_emotion"]["confidence"] for t in turns) / len(turns), 2),
                "modality_availability": s.modality,
                "degradation_notes": s.degradation_notes,
                "risk_triggered": s.risk_level != "none",
                "risk_level": s.risk_level,
                "resource_suggested": s.resource_suggested,
                "risk_evidence": s.risk_evidence,
                "raw_turns": turns,
            },
        }


def _sanitize(raw: str) -> str:
    """把模型输出清洗成「对用户说的话」。

    实测 Qwen3.8-27B-FP8 会把推理过程直接吐出来，例如：
        "我们需要回答用户中文。用户说…目标：围绕具体情境追问一层…"
    处理顺序：去思考块 -> 取「回复：」标记 -> 通用清洗（llm.sanitize_reply）。
    """
    if not raw:
        return ""
    # 先去思考块，否则「回复：」可能命中思考块里引用的标记
    text = strip_thinking(raw)
    for marker in ("回复：", "回复:", "小伴：", "小伴:"):
        idx = text.rfind(marker)
        if idx != -1:
            text = text[idx + len(marker):]
            break
    return sanitize_reply(text)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


TOPIC_WORDS = ["工作", "学习", "考试", "睡眠", "失眠", "家人", "父母", "孩子",
               "朋友", "同事", "领导", "恋人", "分手", "吵架", "身体", "钱",
               "经济", "搬家", "生病", "未来"]


def _topic(text: str) -> str:
    for w in TOPIC_WORDS:
        if w in text:
            return w
    return "其他"


def _mood_statement(dominant: str, secondary: str | None, v: float, a: float) -> str:
    """按 PRD §9.2 示例风格生成自然语句（模板，非诊断）。"""
    v_word = "偏积极" if v > 0.25 else ("偏消极" if v < -0.25 else "比较平稳")
    a_word = "带着一些起伏" if a > 0.3 else ("比较松弛" if a < -0.2 else "不急不缓")
    base = f"今天的你整体{v_word}，情绪{a_word}"
    if secondary and secondary != dominant:
        return f"{base}，主要是「{dominant}」，也带着一点「{secondary}」。"
    return f"{base}，最明显的感觉是「{dominant}」。"
