"""多模态情绪融合（PRD §6.3.3 / §6.3.4）。

采用 PRD 建议的「特征级 + 决策级结合」：
1. 声学特征 → valence/arousal 的**连续维度**（决策级输入）
2. 文本语义 → 情绪类别 + 置信度（LLM 分类）
3. 面部 blendshapes → 情绪类别 + 置信度（规则映射）
4. 决策级加权融合：按各模态**置信度动态加权**，置信度低（如弱光、
   人脸丢失、ASR 失败）的通道自动降权甚至不参与。

情绪标签集（PRD §6.3.4，裁剪后）：平静/愉悦/焦虑/低落/烦躁/疲惫/紧张/惊讶
"""
from __future__ import annotations

# 标签 -> (valence, arousal) 原型坐标，用于类别与维度的互相映射
EMOTION_PROTOTYPE: dict[str, tuple[float, float]] = {
    "平静": (0.15, -0.35),
    "愉悦": (0.75, 0.25),
    "焦虑": (-0.55, 0.65),
    "低落": (-0.70, -0.30),
    "烦躁": (-0.50, 0.55),
    "疲惫": (-0.35, -0.55),
    "紧张": (-0.40, 0.70),
    "惊讶": (0.00, 0.75),
}

ALL_EMOTIONS = list(EMOTION_PROTOTYPE)

# 面部 blendshape -> 情绪贡献权重（MediaPipe FaceLandmarker 输出 52 个 blendshapes）
#
# 只用**正权重**：早期版本用负权重做"惩罚"（如 平静 的 mouthSmileLeft: -0.2），
# 结果中性脸时所有类别得分都是负数，max() 会随便挑一个（实测把一张平静的脸
# 判成了"愉悦"）。改为纯正权重 + 阈值：没有任何类别过阈值就是平静。
FACE_RULES: dict[str, dict[str, float]] = {
    "愉悦": {"mouthSmileLeft": 1.0, "mouthSmileRight": 1.0,
             "cheekSquintLeft": 0.5, "cheekSquintRight": 0.5},
    "低落": {"mouthFrownLeft": 1.0, "mouthFrownRight": 1.0,
             "browInnerUp": 0.6, "browDownLeft": 0.25, "browDownRight": 0.25},
    "焦虑": {"browInnerUp": 0.8, "browOuterUpLeft": 0.5, "browOuterUpRight": 0.5,
             "mouthPucker": 0.3, "eyeWideLeft": 0.3, "eyeWideRight": 0.3},
    "紧张": {"browDownLeft": 0.7, "browDownRight": 0.7,
             "mouthPressLeft": 0.6, "mouthPressRight": 0.6},
    "烦躁": {"browDownLeft": 1.0, "browDownRight": 1.0,
             "mouthPressLeft": 0.5, "noseSneerLeft": 0.5, "noseSneerRight": 0.5},
    "疲惫": {"eyeBlinkLeft": 0.8, "eyeBlinkRight": 0.8, "mouthFrownLeft": 0.3},
    "惊讶": {"jawOpen": 1.0, "browOuterUpLeft": 0.8, "browOuterUpRight": 0.8,
             "eyeWideLeft": 0.7, "eyeWideRight": 0.7},
}

# 低于该分数视为"无明显表情" -> 平静
FACE_PEAK_THRESHOLD = 0.25

# 文本情绪词 -> 标签
#
# ⚠「消极自我评价」是抑郁筛查里最关键的一类表达（PRD §6.3.1 明确要求提取），
# 且不依赖"难过/低落"这类显性情绪词 —— 用户常说的是"我什么都做不好""我很没用"。
# 实测漏了这组词会把"感觉自己什么都做不好，挺没用的"判成「平静」且 valence 偏正。
TEXT_KEYWORDS: dict[str, list[str]] = {
    "愉悦": ["开心", "高兴", "不错", "挺好", "喜欢", "顺利", "轻松", "愉快", "舒服"],
    "低落": [
        # 显性情绪词
        "难过", "低落", "沮丧", "失望", "孤独", "委屈", "没意思", "想哭", "空虚",
        "绝望", "无助", "心酸", "难受",
        # ⭐ 消极自我评价（无显性情绪词，但临床意义最强）
        "做不好", "做的不", "没用", "没用的人", "不够好", "一事无成", "失败者",
        "没价值", "没有价值", "废物", "拖累", "多余", "不配", "很差劲", "差劲",
        "搞不定", "什么都做不", "一无是处", "没出息", "不如别人", "让人失望",
    ],
    "焦虑": ["焦虑", "担心", "紧张", "害怕", "不安", "慌", "压力", "睡不着", "失眠"],
    "烦躁": ["烦", "生气", "愤怒", "恼", "受不了", "讨厌", "暴躁"],
    "疲惫": ["累", "疲惫", "疲倦", "没劲", "撑不住", "精疲力尽", "困",
             "筋疲力尽", "耗竭", "提不起劲"],
    "紧张": ["紧张", "紧绷", "慌张", "手足无措"],
    "惊讶": ["惊讶", "意外", "没想到", "震惊"],
    "平静": ["还行", "平静", "一般", "还好", "没什么", "还好吧"],
}

NEGATIONS = ["不", "没", "无", "别", "未"]


# ------------------------------------------------------------------ 文本通道
def text_emotion(text: str) -> tuple[str, float, float, float]:
    """文本 -> (标签, 置信度, valence, arousal)。纯规则，作为 LLM 不可用时的兜底。"""
    if not text:
        return "平静", 0.0, 0.0, 0.0
    scores: dict[str, int] = {}
    for label, words in TEXT_KEYWORDS.items():
        hits = sum(text.count(w) for w in words)
        if hits:
            scores[label] = hits
    if not scores:
        # 无命中时给**中性 valence 0.0**，不要用「平静」原型的 +0.15。
        # 理由：未识别出情绪 ≠ 状态偏正向。在心理场景下默认偏正是有偏的，
        # 会让"明显消极但没命中词表"的句子被判成还不错的样子。
        # 置信度给低值，让融合时该通道自动降权（PRD §6.3.3 权重随置信度调整）。
        return "平静", 0.12, 0.0, 0.0
    label = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    conf = min(0.9, 0.35 + 0.2 * scores[label] / max(total, 1) * 2)
    return label, round(conf, 2), *EMOTION_PROTOTYPE[label]


# ------------------------------------------------------------------ 声学通道
def acoustic_to_va(audio: dict | None) -> tuple[float, float, float]:
    """声学特征 -> (valence, arousal, 置信度)。

    PRD §6.3.1：声学特征更能反映**情绪强度和真实性**（arousal 的主要来源），
    而 valence 主要交给文本与面部 —— 所以这里对 valence 只给弱信号。
    """
    if not audio:
        return 0.0, 0.0, 0.0

    f0_mean = audio.get("f0_mean_hz") or 0.0
    f0_std = audio.get("f0_std") or 0.0
    rate = audio.get("syllable_rate")
    pause = audio.get("pause_ratio") or 0.0
    energy = audio.get("energy_db") or -60.0
    duration = audio.get("duration_sec") or 0.0

    if duration < 0.3:
        return 0.0, 0.0, 0.0

    arousal = 0.0
    # 基频越高越起伏 -> 唤醒度越高
    if f0_std > 45:
        arousal += 0.45
    elif f0_std > 25:
        arousal += 0.2
    elif f0_std < 12:
        arousal -= 0.35
    # 语速
    if rate is not None:
        if rate > 6.5:
            arousal += 0.3
        elif rate < 3.0:
            arousal -= 0.3
    # 能量
    if energy > -15:
        arousal += 0.2
    elif energy < -35:
        arousal -= 0.25
    # 停顿多 -> 思维迟缓，唤醒度低
    if pause > 0.55:
        arousal -= 0.2

    # valence：声学只给弱信号（低沉+慢+低能量 = 略偏负）
    valence = 0.0
    if f0_mean and f0_mean < 120:
        valence -= 0.2
    if energy < -35:
        valence -= 0.15
    if f0_std < 12 and (rate or 9) < 3.5:
        valence -= 0.2

    conf = 0.55 if duration > 1.0 else 0.3
    return round(max(-1, min(1, valence)), 2), round(max(-1, min(1, arousal)), 2), conf


# ------------------------------------------------------------------ 面部通道
def face_emotion(blendshapes: dict[str, float] | None) -> tuple[str, float, float, float]:
    """MediaPipe blendshapes -> (标签, 置信度, valence, arousal)。

    PRD §6.3.2 要求输出基本类别概率分布，这里用规则加权近似；
    生产环境应换成在标注数据上训练的分类器（见 README 待办）。
    """
    if not blendshapes:
        return "平静", 0.0, 0.0, 0.0

    scores: dict[str, float] = {}
    for label, rules in FACE_RULES.items():
        s = 0.0
        touched = 0
        for bs, w in rules.items():
            v = blendshapes.get(bs)
            if v is None:
                continue
            touched += 1
            s += w * v
        # 只统计确实提供了相关 blendshape 的类别，避免缺失项被当成 0 而"躺赢"
        #
        # ⚠ 归一化必须除以【规则权重总和】，不能除以 touched（命中个数）。
        # 反例：只给 browInnerUp=0.7 时，
        #   低落 5 条规则全命中 -> s=2.235, /5*2 = 0.89
        #   焦虑 6 条只命中 1 条 -> s=0.56,  /1*2 = 1.12  <- 命中更少反而赢
        # 除以 touched 会把「求和」变成「求平均」，惩罚规则多、命中全的类别。
        # 按权重总和归一后各标签分值都落在 [0,1]，可直接横向比较。
        if touched:
            total_w = sum(rules.values())
            scores[label] = s / total_w if total_w else 0.0

    if not scores:
        return "平静", 0.2, *EMOTION_PROTOTYPE["平静"]

    label = max(scores, key=lambda k: scores[k])
    peak = scores[label]
    if peak < FACE_PEAK_THRESHOLD:
        # 无明显表情特征 -> 平静，置信度随峰值递减
        return "平静", round(0.2 + peak, 2), *EMOTION_PROTOTYPE["平静"]

    # peak 已归一到 [0,1]（见上方归一化说明），故除以 2 而非 2.5
    conf = round(min(0.95, 0.35 + peak / 2), 2)
    return label, conf, *EMOTION_PROTOTYPE[label]


def face_expressiveness(blendshapes: dict[str, float] | None) -> float:
    """面部整体活跃度（PRD §6.3.2 微表情/持续状态特征）。"""
    if not blendshapes:
        return 0.0
    vals = list(blendshapes.values())
    if not vals:
        return 0.0
    return round(min(1.0, sum(vals) / len(vals) * 5), 3)


# ------------------------------------------------------------------ 融合
def fuse(
    text_label: str, text_conf: float,
    ac_valence: float, ac_arousal: float, ac_conf: float,
    face_label: str, face_conf: float,
    audio_ok: bool, face_ok: bool,
) -> dict:
    """决策级加权融合（PRD §6.3.3）。

    权重随置信度动态调整：某通道不可用（ASR 失败 / 人脸丢失 / 弱光）时
    该通道权重归零，其余通道重新归一化。
    """
    channels = {}

    # 文本：给类别 + valence（语义最能反映 valence）
    if text_conf > 0:
        tv, ta = EMOTION_PROTOTYPE.get(text_label, (0.0, 0.0))
        channels["text"] = {"w": text_conf, "conf": text_conf,
                            "v": tv, "a": ta * 0.5, "label": text_label}
    # 声学：主要给 arousal
    if audio_ok and ac_conf > 0:
        channels["acoustic"] = {"w": ac_conf, "conf": ac_conf,
                                "v": ac_valence * 0.6, "a": ac_arousal, "label": None}
    # 面部：给类别 + 维度
    if face_ok and face_conf > 0:
        fv, fa = EMOTION_PROTOTYPE.get(face_label, (0.0, 0.0))
        channels["face"] = {"w": face_conf, "conf": face_conf,
                            "v": fv, "a": fa, "label": face_label}

    if not channels:
        return {"label": "平静", "valence": 0.0, "arousal": 0.0,
                "confidence": 0.0, "weights": {}}

    tot_w = sum(c["w"] for c in channels.values()) or 1.0
    valence = sum(c["w"] * c["v"] for c in channels.values()) / tot_w
    arousal = sum(c["w"] * c["a"] for c in channels.values()) / tot_w

    # 类别：在有标签的通道里按权重投票
    label_scores: dict[str, float] = {}
    for c in channels.values():
        if c["label"]:
            label_scores[c["label"]] = label_scores.get(c["label"], 0.0) + c["w"]
    label = max(label_scores, key=lambda k: label_scores[k]) if label_scores else "平静"

    # 整体置信度 = 各通道置信度的加权平均，再按通道数惩罚（通道越少越不确定）
    conf = (sum(c["w"] * c["conf"] for c in channels.values()) / tot_w
            * (0.6 + 0.2 * len(channels)))

    return {
        "label": label,
        "valence": round(max(-1, min(1, valence)), 2),
        "arousal": round(max(-1, min(1, arousal)), 2),
        "confidence": round(min(1.0, conf), 2),
        "weights": {k: round(v["w"] / tot_w, 2) for k, v in channels.items()},
    }


def consistency(text_label: str, face_label: str) -> float:
    """语音/文本通道与面部通道的吻合度（Tier3 voice_face_consistency）。"""
    if not text_label or not face_label:
        return 0.0
    if text_label == face_label:
        return 1.0
    tv, ta = EMOTION_PROTOTYPE.get(text_label, (0, 0))
    fv, fa = EMOTION_PROTOTYPE.get(face_label, (0, 0))
    dist = ((tv - fv) ** 2 + (ta - fa) ** 2) ** 0.5
    return round(max(0.0, 1 - dist / 2), 2)
