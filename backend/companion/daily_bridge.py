"""把一次「视频语音陪伴」会话沉淀成当日记录（daily_record）。

设计要点
--------
1. **陪伴会话是唯一输入入口。** 用户不再单独填每日自述 —— 会话里说的
   每一轮就是当天的自述。这样避免了"两套输入、两套口径"的数据不一致。
2. **落库的是结构化聚合，不是原始对话。** 情绪分布、声学均值、风险等级
   都是按轮聚合后的数值；原始多模态数据不入库（PRD §8 数据最小化）。
3. **字段口径与 perception.build_daily_record 保持一致**，这样既有的
   多模态风险通道（`perception.multimodal_risk_signal`）无需改动即可复用。

融合输出的情绪标签是中文（平静/愉悦/低落/…），而风险通道读的是英文 key
（`emotion_dist.sad`），这里做一次映射。
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from config import NEGATIVE_WORDS, POSITIVE_WORDS
from models import DailyRecord, now_iso

from .state import SessionState

# 融合情绪标签 -> video_features.emotion_dist 的 key
# 与 perception.multimodal_risk_signal / detect_inconsistency 读取的 key 对齐
LABEL_TO_KEY = {
    "平静": "neutral",
    "愉悦": "happy",
    "低落": "sad",
    "焦虑": "fear",
    "烦躁": "angry",
    "疲惫": "tired",
    "紧张": "tense",
    "惊讶": "surprise",
}

# 反查：展示层用中文，存储层用英文 key 兼容风险通道
KEY_TO_LABEL = {v: k for k, v in LABEL_TO_KEY.items()}

# 自述文本分析用的正负向词表统一走 config.POSITIVE_WORDS / NEGATIVE_WORDS。
# 此前这里单独维护一份（比 perception 的多"没用/做不好"），导致同一段自述
# 在陪伴链路与每日上报链路判出不同 tone，进而影响掩饰检测与负面词判定。
_TOPIC_WORDS = ["工作", "学习", "考试", "睡眠", "失眠", "家人", "父母", "孩子",
                "朋友", "同事", "领导", "恋人", "分手", "吵架", "身体", "钱",
                "经济", "搬家", "生病", "未来"]


def _mean(xs: list[Any]) -> float | None:
    vals = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(vals) / len(vals), 3) if vals else None


def _narrative_of(st: SessionState) -> str:
    """用户各轮发言拼成当日自述。"""
    parts = [str(t.get("text") or "").strip() for t in st.turns]
    parts = [p for p in parts if p]
    # 用换行分隔，保留轮次边界，便于人工复核
    return "\n".join(parts)


def _emotion_counts(turns: list[dict]) -> dict[str, int]:
    """各轮融合情绪的**计数**（可跨会话相加；占比不可直接相加）。"""
    labels = [str((t.get("fused_emotion") or {}).get("label") or "")
              for t in turns]
    labels = [l for l in labels if l]
    if not labels:
        return {}
    c = Counter(labels)
    out: dict[str, int] = {}
    for label, n in c.items():
        key = LABEL_TO_KEY.get(label, label)
        out[key] = out.get(key, 0) + n
    return out


def _emotion_dist(turns: list[dict]) -> dict[str, float]:
    """各轮融合情绪的占比分布。"""
    counts = _emotion_counts(turns)
    total = sum(counts.values())
    if not total:
        return {}
    return {k: round(v / total, 3) for k, v in counts.items()}


def _audio_features(turns: list[dict]) -> dict[str, Any]:
    """均值聚合的声学特征。

    注意：perception.multimodal_risk_signal 读 `words_per_min`，
    而 ASR 产出的是 `syllable_rate`（音节/秒）。这里两个都给，
    词义转换只是量纲不同，阈值判断用 words_per_min 分支。
    """
    feats = [t.get("audio") or {} for t in turns]
    feats = [f for f in feats if f]
    if not feats:
        return {}
    out: dict[str, Any] = {}
    for key in ("duration_sec", "pause_ratio", "syllable_rate",
                "f0_mean_hz", "f0_std", "energy_db", "speech_ratio"):
        m = _mean([f.get(key) for f in feats])
        if m is not None:
            out[key] = m
    # 音节/秒 -> 近似词/分（中文 1 字≈1 音节，按 60s 折算）
    if out.get("syllable_rate"):
        out["words_per_min"] = round(out["syllable_rate"] * 60, 1)
    tones = [f.get("tone") for f in feats if f.get("tone")]
    out["tone"] = tones[-1] if tones else ""
    return out


def _video_features(turns: list[dict]) -> dict[str, Any]:
    """面部特征聚合：情绪分布 + 平均活跃度。"""
    faces = [t.get("face") or {} for t in turns]
    faces = [f for f in faces if f and f.get("blendshapes")]
    if not faces:
        return {}
    expr = _mean([f.get("expressiveness") for f in faces])
    detected = [f for f in faces if f.get("face_detected")]
    return {
        "emotion_dist": _emotion_dist(turns),
        "expressiveness": expr,
        "face_detected_ratio": round(len(detected) / len(faces), 3) if faces else 0.0,
        "frames": len(faces),
    }


def _narrative_analysis(st: SessionState, narrative: str,
                        report: dict) -> dict[str, Any]:
    pos = sum(narrative.count(w) for w in POSITIVE_WORDS)
    neg = sum(narrative.count(w) for w in NEGATIVE_WORDS)
    tone = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
    themes = [w for w in _TOPIC_WORDS if w in narrative]

    t2 = report.get("tier2_detail") or {}
    t3 = report.get("tier3_internal") or {}
    t1 = report.get("tier1_summary") or {}

    return {
        "tone": tone,
        "pos_hits": pos,
        "neg_hits": neg,
        "themes": themes,
        "insomnia": "失眠" in narrative or "睡不着" in narrative,
        "appetite": "吃不下" in narrative or "没胃口" in narrative,
        "length": len(narrative),
        # ---- 陪伴会话特有：直接带三层报告的结论，供上下文消费 ----
        "source": "companion",
        "session_id": st.session_id,
        "dominant_emotion": t1.get("dominant_emotion") or "",
        "secondary_emotion": t1.get("secondary_emotion") or "",
        "avg_valence": t2.get("avg_valence"),
        "avg_arousal": t2.get("avg_arousal"),
        "emotion_distribution": t2.get("emotion_distribution") or {},
        "emotional_volatility": t3.get("emotional_volatility"),
        "voice_face_consistency": t3.get("voice_face_consistency"),
        "risk_level": st.risk_level,
        "risk_evidence": list(st.risk_evidence),
        "turn_count": st.turn_count,
        "duration_sec": round(st.elapsed, 1),
    }


def _modality_summary(st: SessionState, report: dict,
                      dist: dict[str, float], audio: dict[str, Any]) -> str:
    t1 = report.get("tier1_summary") or {}
    parts: list[str] = []

    if t1.get("dominant_emotion"):
        parts.append(f"本次陪伴主导情绪：{t1['dominant_emotion']}")
    if t1.get("mood_statement"):
        parts.append(str(t1["mood_statement"]))

    if dist:
        # 取占比最高的前 3 个。展示用中文标签（dist 的 key 是英文，
        # 为的是兼容 perception 的风险通道，不能直接给用户看）
        top = sorted(dist.items(), key=lambda kv: -kv[1])[:3]
        parts.append("情绪分布："
                     + "、".join(f"{KEY_TO_LABEL.get(k, k)} {v:.0%}" for k, v in top))

    if audio:
        bits = []
        if audio.get("syllable_rate"):
            bits.append(f"语速约 {audio['syllable_rate']} 音节/秒")
        if audio.get("f0_mean_hz"):
            bits.append(f"基频均值 {audio['f0_mean_hz']}Hz（标准差 {audio.get('f0_std')}）")
        if audio.get("pause_ratio") is not None:
            bits.append(f"停顿占比 {audio['pause_ratio']:.0%}")
        if audio.get("tone"):
            bits.append(f"语气 {audio['tone']}")
        if bits:
            parts.append("语音：" + "；".join(bits))
    else:
        parts.append("语音：本次未采集到有效语音")

    if st.modality.get("face", 0) > 0:
        parts.append(f"面部有效采集占比 {st.modality['face']:.0%}")
    else:
        parts.append("面部：未采集")

    if st.risk_level != "none":
        parts.append(f"⚠ 风险等级 {st.risk_level}")

    if st.degradation_notes:
        parts.append("降级记录：" + "；".join(st.degradation_notes[-3:]))

    return "；".join(parts)


def session_to_daily_record(st: SessionState, report: dict,
                            day: str | None = None) -> DailyRecord:
    """把结束的陪伴会话转成一条当日记录。

    report 为 `CompanionEngine.build_report()` 的返回值（三层报告）。
    """
    narrative = _narrative_of(st)
    turns = st.turns
    dist = _emotion_dist(turns)
    audio = _audio_features(turns)
    video = _video_features(turns)
    # 计数一并存下来：同一天多次陪伴要合并，占比不能直接相加
    video["emotion_counts"] = _emotion_counts(turns)
    na = _narrative_analysis(st, narrative, report)

    # 模态不一致：自述偏正面，但面部/语音持续偏负（掩饰倾向）
    t3 = report.get("tier3_internal") or {}
    inconsistency = bool(t3.get("incongruence_flag"))

    return DailyRecord(
        id=None,
        user_id=st.user_id,
        date=day or date.today().isoformat(),
        video_uri=f"companion://{st.session_id}",
        transcript=narrative,          # 陪伴会话的用户发言即转写来源
        narrative=narrative,
        video_features=video,
        audio_features=audio,
        narrative_analysis=na,
        modality_summary=_modality_summary(st, report, dist, audio),
        inconsistency=inconsistency,
        created_at=now_iso(),
    )


# ------------------------------------------------------------------ 同日合并
_LEVEL_ORDER = {"none": 0, "watch": 1, "low": 1, "moderate": 2, "high": 3, "imminent": 4}


def _wavg(pairs: list[tuple[float | None, float]]) -> float | None:
    """按权重平均；权重为 0 的样本不计入。"""
    num = den = 0.0
    for v, w in pairs:
        if v is None or not w:
            continue
        num += v * w
        den += w
    return round(num / den, 3) if den else None


def merge_daily_record(old: DailyRecord, new: DailyRecord) -> DailyRecord:
    """同一天的多次陪伴合并成一条记录。

    一天多次会话（比如中途退出后重来）不应堆成多条同日记录 ——
    那会让「每日记录」看起来重复，也会稀释按天统计的风险趋势。
    """
    oa, na = old.narrative_analysis or {}, new.narrative_analysis or {}
    ov, nv = old.video_features or {}, new.video_features or {}
    oaud, naud = old.audio_features or {}, new.audio_features or {}

    # 1) 自述拼接（保留轮次边界）
    narrative = "\n".join(x for x in (old.narrative, new.narrative) if x)

    # 2) 情绪：计数相加后重新归一
    counts: dict[str, int] = {}
    for c in (ov.get("emotion_counts") or {}, nv.get("emotion_counts") or {}):
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
    total = sum(counts.values()) or 1
    dist = {k: round(v / total, 3) for k, v in counts.items()}

    video = {
        "emotion_dist": dist,
        "emotion_counts": counts,
        "expressiveness": _wavg([
            (ov.get("expressiveness"), ov.get("frames") or 0),
            (nv.get("expressiveness"), nv.get("frames") or 0),
        ]),
        "face_detected_ratio": _wavg([
            (ov.get("face_detected_ratio"), ov.get("frames") or 0),
            (nv.get("face_detected_ratio"), nv.get("frames") or 0),
        ]),
        "frames": (ov.get("frames") or 0) + (nv.get("frames") or 0),
    }
    video = {k: v for k, v in video.items() if v is not None}

    # 3) 声学：按轮数加权
    ow = oa.get("turn_count") or 1
    nw = na.get("turn_count") or 1
    audio: dict[str, Any] = {}
    for key in ("duration_sec", "pause_ratio", "syllable_rate",
                "f0_mean_hz", "f0_std", "energy_db", "speech_ratio"):
        m = _wavg([(oaud.get(key), ow), (naud.get(key), nw)])
        if m is not None:
            audio[key] = m
    if audio.get("syllable_rate"):
        audio["words_per_min"] = round(audio["syllable_rate"] * 60, 1)
    if naud.get("tone"):
        audio["tone"] = naud["tone"]

    # 4) 分析结论：主题取并集、风险取最高、证据去重拼接
    themes = sorted(set(oa.get("themes") or []) | set(na.get("themes") or []))
    o_lvl, n_lvl = oa.get("risk_level") or "none", na.get("risk_level") or "none"
    risk = o_lvl if _LEVEL_ORDER.get(o_lvl, 0) >= _LEVEL_ORDER.get(n_lvl, 0) else n_lvl
    evidence = list(dict.fromkeys(
        (oa.get("risk_evidence") or []) + (na.get("risk_evidence") or [])))

    merged_na = dict(na)          # 新会话的结论为准
    merged_na.update({
        "themes": themes,
        "risk_level": risk,
        "risk_evidence": evidence,
        "turn_count": (oa.get("turn_count") or 0) + (na.get("turn_count") or 0),
        "duration_sec": round((oa.get("duration_sec") or 0)
                              + (na.get("duration_sec") or 0), 1),
        "sessions": (oa.get("sessions") or 1) + 1,
        # 文本统计基于合并后的全文
        "pos_hits": sum(narrative.count(w) for w in POSITIVE_WORDS),
        "neg_hits": sum(narrative.count(w) for w in NEGATIVE_WORDS),
    })
    pos, neg = merged_na["pos_hits"], merged_na["neg_hits"]
    merged_na["tone"] = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
    merged_na["insomnia"] = "失眠" in narrative or "睡不着" in narrative
    merged_na["length"] = len(narrative)

    # 5) 摘要：基于合并后的数据重写
    top = sorted(dist.items(), key=lambda kv: -kv[1])[:3]
    parts = [f"当日 {merged_na['sessions']} 次陪伴，共 {merged_na['turn_count']} 轮"]
    if na.get("dominant_emotion"):
        parts.append(f"主导情绪：{na['dominant_emotion']}")
    if top:
        parts.append("情绪分布："
                     + "、".join(f"{KEY_TO_LABEL.get(k, k)} {v:.0%}" for k, v in top))
    if audio.get("syllable_rate"):
        parts.append(f"语速约 {audio['syllable_rate']} 音节/秒、"
                     f"基频 {audio.get('f0_mean_hz')}Hz")
    if risk != "none":
        parts.append(f"⚠ 风险等级 {risk}")
    if themes:
        parts.append("主题：" + "、".join(themes[:4]))

    return DailyRecord(
        id=old.id,
        user_id=old.user_id,
        date=old.date,
        video_uri=new.video_uri,
        transcript=narrative,
        narrative=narrative,
        video_features=video,
        audio_features=audio,
        narrative_analysis=merged_na,
        modality_summary="；".join(parts),
        inconsistency=bool(old.inconsistency or new.inconsistency),
        created_at=old.created_at,
    )
