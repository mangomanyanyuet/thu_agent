"""感知层：把多模态原始数据 -> 结构化 JSON。

设计原则（对应报告 §6.1）：
多模态理解放在感知层，不塞进 LLM 上下文。
视频/语音先由专用模型出结构化指标，再以一段简短摘要进入上下文，
好处：可解释、可缓存、token 便宜、可单独评估与迭代。
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any

from config import (
    NEGATIVE_WORDS,
    POSITIVE_WORDS,
    SLOW_SPEECH_SYLLABLE_RATE,
    SLOW_SPEECH_WORDS_PER_MIN,
    get_settings,
)
from models import DailyRecord, DailySummary, ScaleScore
from storage import list_scale_scores

# ------------------------------------------------------------------ 量表定义
SCALES: dict[str, dict[str, Any]] = {
    "PHQ-9": {
        "name": "PHQ-9 抑郁筛查",
        "items": [
            "做事时提不起劲或没有兴趣", "感到心情低落、沮丧或绝望",
            "入睡困难、睡不安稳或睡眠过多", "感觉疲倦或没有活力",
            "食欲不振或吃太多", "觉得自己很糟，或觉得自己是失败者",
            "对事物专注有困难", "动作或说话速度缓慢/烦躁坐立不安",
            "有不如死掉或用某种方式伤害自己的念头",
        ],
        "max": 3,
        "thresholds": [(0, 4, "无"), (5, 9, "轻度"), (10, 14, "中度"),
                       (15, 19, "中重度"), (20, 27, "重度")],
    },
    "GAD-7": {
        "name": "GAD-7 焦虑筛查",
        "items": [
            "感觉紧张、焦虑或急切", "不能停止或控制担忧",
            "对各种各样的事情担忧过多", "很难放松下来",
            "由于不安而无法静坐", "变得容易烦恼或急躁",
            "感到似乎将有可怕的事情发生而害怕",
        ],
        "max": 3,
        "thresholds": [(0, 4, "无"), (5, 9, "轻度"), (10, 14, "中度"), (15, 21, "重度")],
    },
    "PSS-4": {
        "name": "PSS-4 压力知觉",
        "items": ["觉得无法控制生活中重要的事", "对处理个人问题感到没有信心",
                  "觉得事情顺心如意", "觉得困难堆积如山无法克服"],
        "max": 4,
        "thresholds": [(0, 5, "低"), (6, 10, "中等"), (11, 16, "高")],
    },
}

# PHQ-9 第 9 题是自杀相关条目，>0 必须触发风险通道
PHQ9_SUICIDE_ITEM_INDEX = 8


def severity_of(scale_name: str, total: int) -> str:
    for lo, hi, label in SCALES[scale_name]["thresholds"]:
        if lo <= total <= hi:
            return label
    return "未知"


def score_scale(user_id: str, scale_name: str, item_scores: list[int],
                day: str | None = None) -> ScaleScore:
    """计算量表得分 + 与上次/基线的差值（趋势比绝对值更有临床意义）。"""
    if scale_name not in SCALES:
        raise ValueError(f"未知量表: {scale_name}")
    cfg = SCALES[scale_name]
    n = len(cfg["items"])
    if len(item_scores) != n:
        raise ValueError(f"{scale_name} 需要 {n} 个条目分，收到 {len(item_scores)}")
    if any(s < 0 or s > cfg["max"] for s in item_scores):
        raise ValueError(f"{scale_name} 条目分需在 0-{cfg['max']} 之间")

    day = day or _date.today().isoformat()
    total = int(sum(item_scores))

    hist = [s for s in list_scale_scores(user_id, scale_name) if s.date != day]
    delta_last = total - hist[-1].total_score if hist else 0
    baseline = sum(s.total_score for s in hist[:3]) / len(hist[:3]) if hist[:3] else total
    delta_baseline = int(round(total - baseline))

    return ScaleScore(
        id=None, user_id=user_id, date=day, scale_name=scale_name,
        item_scores=item_scores, total_score=total,
        delta_vs_last=delta_last, delta_vs_baseline=delta_baseline,
        severity=severity_of(scale_name, total),
    )


def scale_risk_signal(scale_name: str, item_scores: list[int]) -> tuple[int, str]:
    """量表通道的风险信号：返回 (等级数值, 证据)。

    这是"三通道并集召回"里的量表通道，规则硬编码、零漏报。
    """
    if scale_name == "PHQ-9" and len(item_scores) > PHQ9_SUICIDE_ITEM_INDEX:
        v = item_scores[PHQ9_SUICIDE_ITEM_INDEX]
        if v >= 3:
            return 3, f"PHQ-9 第9题得分 {v}（几乎每天有自伤/自杀念头）"
        if v == 2:
            return 2, f"PHQ-9 第9题得分 {v}（超过一半天数有相关念头）"
        if v == 1:
            return 1, f"PHQ-9 第9题得分 {v}（有几天出现相关念头）"
    return 0, ""


# ------------------------------------------------------------------ 多模态特征
def analyze_video_stub(video_uri: str, transcript: str = "") -> dict[str, Any]:
    """视频/音频特征提取。

    骨架实现为确定性桩函数；生产替换点：
      抽帧 -> 表情/AU 模型（如 OpenFace / py-feat）
      音频 -> 语速、停顿比、基频(F0)、能量、抖动
    """
    seed = sum(ord(c) for c in (video_uri or "x")) % 100
    return {
        "duration_sec": 30 + seed % 60,
        "face_detected_ratio": round(0.6 + (seed % 30) / 100, 2),
        "emotion_dist": {
            "neutral": round(0.4 + (seed % 20) / 100, 2),
            "sad": round((seed % 45) / 100, 2),
            "happy": round((seed % 15) / 100, 2),
            "fear": round((seed % 10) / 100, 2),
        },
        "au": {"AU4_browLowerer": round((seed % 30) / 100, 2), "AU12_lipCorner": round((seed % 20) / 100, 2)},
        "gaze_down_ratio": round((seed % 40) / 100, 2),
        "head_pose_stability": round(0.5 + (seed % 40) / 100, 2),
        "speech": {
            "words_per_min": 90 + seed % 70,
            "pause_ratio": round((seed % 50) / 100, 2),
            "f0_mean_hz": 110 + seed % 60,
            "f0_std": round((seed % 25) / 10, 2),
            "energy_db": -30 + (seed % 15),
        },
        "asr_transcript": transcript,
    }


def analyze_narrative_stub(narrative: str) -> dict[str, Any]:
    """自述文本分析。生产替换为情绪分类/主题抽取模型。"""
    # 词表统一走 config（此前 4 处各维护一份，口径不一致，见 config 注释）
    pos = sum(narrative.count(w) for w in POSITIVE_WORDS)
    neg = sum(narrative.count(w) for w in NEGATIVE_WORDS)
    tone = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
    themes = [t for t in ["工作", "学习", "睡眠", "亲密关系", "家庭", "身体", "经济"] if t in narrative]
    return {
        "tone": tone,
        "pos_hits": pos,
        "neg_hits": neg,
        "themes": themes,
        "insomnia": "失眠" in narrative or "睡不着" in narrative,
        "appetite": "吃不下" in narrative or "没胃口" in narrative,
        "length": len(narrative),
    }


def detect_inconsistency(video_features: dict[str, Any], narrative_analysis: dict[str, Any]) -> bool:
    """模态不一致检测：嘴上说好、身体说不好。

    报告 §6.3：不一致本身是强信号，应触发更谨慎的探询路径。
    """
    if not video_features or not narrative_analysis:
        return False
    if narrative_analysis.get("tone") != "positive":
        return False
    emo = video_features.get("emotion_dist", {})
    speech = video_features.get("speech", {})
    sad_fear = emo.get("sad", 0) + emo.get("fear", 0)
    flat_voice = speech.get("f0_std", 9) < 8
    slow_speech = speech.get("words_per_min", 200) < 110
    return bool(sad_fear > 0.5 or (flat_voice and slow_speech))


def _tone_text(tone: str) -> str:
    """取语气标签的中文说明。

    软依赖 asr：没装 faster-whisper 时 perception 仍要能正常工作。
    """
    try:
        from asr import TONE_TEXT
        return TONE_TEXT.get(tone, tone)
    except Exception:  # noqa: BLE001
        return {"low": "语气低落", "flat": "语气平淡", "neutral": "语气平稳",
                "agitated": "语气激动", "unknown": "语气未知"}.get(tone, tone)


def summarize_daily(video_uri: str, transcript: str, narrative: str,
                    voice: dict | None = None) -> DailySummary:
    """汇总当日多模态观察。

    voice 为 /api/asr 产出的真实声学特征；给了就用真实的，
    没给才回落到桩函数（保证无语音时流程仍可跑通）。
    """
    vf = analyze_video_stub(video_uri, transcript)
    na = analyze_narrative_stub(narrative)
    emo = vf.get("emotion_dist", {})

    if voice and voice.get("audio_features"):
        sp = dict(voice["audio_features"])
        sp["tone"] = voice.get("tone", "")
        real_audio = True
    else:
        sp = dict(vf.get("speech", {}))
        real_audio = False

    parts = [
        f"自述语调：{na['tone']}（正面词 {na['pos_hits']} / 负面词 {na['neg_hits']}）",
        f"面部情绪分布：平静 {emo.get('neutral', 0)}、悲伤 {emo.get('sad', 0)}、愉悦 {emo.get('happy', 0)}",
    ]
    if real_audio:
        parts.append(
            f"语音（实测）：语速约 {sp.get('syllable_rate')} 音节/秒、"
            f"停顿占比 {sp.get('pause_ratio')}、基频均值 {sp.get('f0_mean_hz')}Hz"
            f"（标准差 {sp.get('f0_std')}）"
        )
        if voice.get("tone"):
            parts.append(f"语气判定：{_tone_text(voice['tone'])}"
                         f"（{voice.get('tone_reason', '')}）")
    else:
        parts.append(
            f"语音（桩）：语速 {sp.get('words_per_min')} 词/分、"
            f"停顿比 {sp.get('pause_ratio')}、基频均值 {sp.get('f0_mean_hz')}Hz"
        )
        parts.append("（本条未附真实语音，声学指标为占位值）")
    parts.append(f"注视下方比例 {vf.get('gaze_down_ratio')}")

    if na.get("insomnia"):
        parts.append("自述提到失眠")
    if na.get("themes"):
        parts.append("主题：" + "、".join(na["themes"]))

    return DailySummary(
        transcript=transcript, narrative=narrative,
        video_features=vf, audio_features=sp, narrative_analysis=na,
        modality_summary="；".join(parts),
        inconsistency=detect_inconsistency(vf, na),
    )


def build_daily_record(user_id: str, day: str, video_uri: str,
                       transcript: str, narrative: str,
                       voice: dict | None = None) -> DailyRecord:
    s = summarize_daily(video_uri, transcript, narrative, voice=voice)
    return DailyRecord(
        id=None, user_id=user_id, date=day, video_uri=video_uri,
        transcript=s.transcript, narrative=s.narrative,
        video_features=s.video_features, audio_features=s.audio_features,
        narrative_analysis=s.narrative_analysis,
        modality_summary=s.modality_summary, inconsistency=s.inconsistency,
    )


# ------------------------------------------------------------------ 规则风险引擎
def rule_risk_level(text: str) -> tuple[int, str]:
    """规则通道：返回 (等级数值 0-4, 命中证据)。零漏报底线。

    ⚠ 曾经漏掉 `RISK_PATTERNS_IMPLICIT`（告别式/托付式表达），导致
    "以后不用管我了""帮我把东西转交"这类**只能靠模型通道兜底**——
    而模型通道在关闭思考模式时会判 none，形成双层漏报。
    这类表达是 PRD 补齐 §7.5.1 明确的 high 判定依据，规则通道必须兜住。
    """
    from config import (
        RISK_PATTERNS_HIGH,
        RISK_PATTERNS_IMMINENT,
        RISK_PATTERNS_IMPLICIT,
        RISK_PATTERNS_LOW,
        RISK_PATTERNS_MODERATE,
    )

    for pats, lvl in ((RISK_PATTERNS_IMMINENT, 4), (RISK_PATTERNS_HIGH, 3),
                      (RISK_PATTERNS_MODERATE, 2), (RISK_PATTERNS_LOW, 1),
                      # 告别式/隐喻：弱信号，按 moderate 起判（与选型报告 §8.3 一致），
                      # 由模型通道决定是否升级到 high
                      (RISK_PATTERNS_IMPLICIT, 2)):
        for p in pats:
            if p in text:
                return lvl, p
    return 0, ""


def _slow_speech(audio_features: dict[str, Any]) -> bool:
    """语速是否显著下降（精神运动性抑制）。

    三种来源的键名不一致，必须都认：
      - 真实语音（asr.AudioFeatures）        ：只有 syllable_rate（音节/秒）
      - 陪伴会话聚合（companion.daily_bridge）：syllable_rate + words_per_min
      - 视频桩函数（analyze_video_stub）      ：只有 words_per_min（词/分）

    ⚠ 原实现只读 `words_per_min`（缺失时默认 200），而 /api/daily 上报的
    真实语音里根本没有这个键 —— 该链路上这条规则从未生效过。
    """
    sp = audio_features or {}
    rate = sp.get("syllable_rate")
    if isinstance(rate, (int, float)):
        return rate < SLOW_SPEECH_SYLLABLE_RATE
    wpm = sp.get("words_per_min")
    if isinstance(wpm, (int, float)):
        return wpm < SLOW_SPEECH_WORDS_PER_MIN
    return False


def multimodal_risk_signal(record: DailyRecord | None) -> tuple[int, str]:
    """多模态通道：从当日记录里找风险信号。"""
    if not record:
        return 0, ""
    na = record.narrative_analysis or {}
    emo = (record.video_features or {}).get("emotion_dist", {})
    sp = record.audio_features or {}
    signals = []
    level = 0
    if na.get("tone") == "negative" and na.get("neg_hits", 0) >= 4:
        level = max(level, 1)
        signals.append(f"自述负面词密集({na['neg_hits']})")
    if na.get("insomnia") and _slow_speech(sp):
        level = max(level, 1)
        signals.append("失眠 + 语速显著下降")
    if emo.get("sad", 0) > 0.6:
        level = max(level, 2)
        signals.append(f"面部悲伤占比 {emo['sad']}")
    if record.inconsistency:
        level = max(level, 1)
        signals.append("自述与表情/语音不一致（可能存在掩饰）")
    return level, "；".join(signals)


def text_tone_of(text: str) -> str:
    """同步判断文本情绪倾向。

    注意：risk_detector 与 context_builder 是并行分支，读不到后者产出的
    emotion，所以这里不能用情绪评估结果，只能用文本自身做同步判断。
    """
    if not text:
        return "neutral"
    pos = sum(text.count(w) for w in POSITIVE_WORDS)
    neg = sum(text.count(w) for w in NEGATIVE_WORDS)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def voice_risk_signal(tone: str, audio_features: dict[str, Any],
                      text: str = "") -> tuple[int, str]:
    """本轮语音的声学风险信号（多模态通道的一部分）。

    三条规则（对应报告 §6.3「模态不一致本身是强信号」）：
    1. 语气低落 + 文本负面 → 互相印证，1 级。
    2. 语气低落 + 文本却说"挺好的" → **掩饰信号**，临床更需警惕，2 级。
    3. 语气低落 + 文本中性 → 1 级。
    """
    if not tone or tone == "unknown":
        return 0, ""

    feats = audio_features or {}
    f0_std = feats.get("f0_std")
    rate = feats.get("syllable_rate")
    pause = feats.get("pause_ratio")

    detail: list[str] = []
    if tone in ("low", "flat"):
        if f0_std is not None and f0_std < 15:
            detail.append(f"语调平板(F0标准差{f0_std}Hz)")
        if rate is not None and rate < 3.0:
            detail.append(f"语速偏慢({rate}音节/秒)")
        if pause is not None and pause > 0.55:
            detail.append(f"停顿占比高({pause:.0%})")
        head = "语音呈低落抑制特征" if tone == "low" else "语音偏平淡"
        signal = head + "：" + "、".join(detail or ["多指标偏低"])
    elif tone == "agitated":
        if rate is not None and rate > 6.5:
            detail.append(f"语速偏快({rate}音节/秒)")
        if f0_std is not None and f0_std > 45:
            detail.append(f"基频起伏大(F0标准差{f0_std}Hz)")
        signal = "语音呈激动/焦虑特征：" + "、".join(detail or ["唤醒度偏高"])
    else:
        return 0, ""

    text_tone = text_tone_of(text)
    if tone in ("low", "flat") and text_tone == "positive":
        # 嘴上说好、声音低落 —— 掩饰信号，比单纯负面更需要关注
        return 2, signal + "；但自述偏正面，存在掩饰倾向"
    if text_tone == "negative":
        return 1, signal + "；与自述负面情绪一致"
    return 1, signal


def hotlines() -> list[dict[str, str]]:
    s = get_settings()
    out = [
        {"name": "全国心理援助热线", "number": s.hotline_national},
        {"name": "医疗急救", "number": s.hotline_emergency_medical},
        {"name": "报警求助", "number": s.hotline_emergency_police},
    ]
    for item in filter(None, s.hotlines_extra.split("|")):
        if " " in item:
            name, number = item.rsplit(" ", 1)
            out.append({"name": name.strip(), "number": number.strip()})
    return out
