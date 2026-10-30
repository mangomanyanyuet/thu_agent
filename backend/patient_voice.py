"""病人模拟 Agent 的「语音侧」：韵律状态机 + 语音产出。

为什么要单独一个模块：
  病人 Agent 原本只输出文本（`PatientTurn.text`），诊断侧的多模态通道因此
  永远是 0，`voice_risk_signal()` 的三条规则从来没被自动化覆盖过。本模块让
  病人「带上声音说话」，补齐文本联路之外的语音联路。

两档语音源共用同一套韵律状态机：
  - synth：按状态机直接生成声学特征（零新依赖、毫秒级、完全可复现）
  - tts  ：先合成音频，再交给 `asr.transcribe()` 真实测量（端到端，覆盖感知层）

与线上 UI 一致的三条硬约束（方案 §6.2）：
  C2 产出必须是 `VoiceMeta` 结构（audio_features/tone/tone_reason/summary）
  C3 tone 一律由 `asr.tone_label()` 产出，本模块不得另写一套语气判定
  C4 tts 档必须走 `asr.transcribe()`，与 `/api/asr` 同模型、同特征提取口径

⚠ 仅用于联调与评测，不得接入生产。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import asr
from models import AudioFeatures
from schemas import VoiceMeta

# ================================================================== 语气预设
# 取值由 `asr.tone_label()` 的打分阈值反推（见该文件），保证目标语气必然命中：
#   low      = -2(F0σ<15) -1(F0<110) -2(语速<3.0) -1(停顿>0.55) -1(能量<-38)
#   neutral  = 各项均在正常区间，得分 0
#   agitated = +1(语速>6.5) +1(F0σ>45)
TONE_PRESETS: dict[str, dict[str, float]] = {
    "low": {"f0_mean_hz": 98.0, "f0_std": 9.0, "syllable_rate": 2.4,
            "pause_ratio": 0.62, "energy_db": -40.0},
    "flat": {"f0_mean_hz": 115.0, "f0_std": 12.0, "syllable_rate": 3.4,
             "pause_ratio": 0.50, "energy_db": -36.0},
    "neutral": {"f0_mean_hz": 150.0, "f0_std": 25.0, "syllable_rate": 4.5,
                "pause_ratio": 0.35, "energy_db": -30.0},
    "agitated": {"f0_mean_hz": 210.0, "f0_std": 55.0, "syllable_rate": 7.2,
                 "pause_ratio": 0.25, "energy_db": -28.0},
}

# 内心严重程度 -> 语气基线；高严重程度走到后段抑制加深（flat -> low）
_SEVERITY_TONE: dict[int, str] = {1: "neutral", 2: "neutral", 3: "flat",
                                  4: "flat", 5: "low"}

# 语气档位只降不升（掩饰/回避型把声音压低一档）
_DOWN = {"agitated": "neutral", "neutral": "flat", "flat": "low", "low": "low"}

# 风格对韵律的影响：与严重程度正交
#   rate/pause/f0_std 是相对预设的比例/增量修正
STYLE_VOICE: dict[str, dict[str, Any]] = {
    "guarded": {"downgrade": True, "rate": -0.05, "pause": 0.05, "f0_std": -0.15},
    "avoidant": {"downgrade": True, "rate": -0.10, "pause": 0.12, "f0_std": -0.15},
    "verbose": {"downgrade": False, "rate": 0.15, "pause": -0.05, "f0_std": 0.0},
    "ruminative": {"downgrade": False, "rate": -0.05, "pause": 0.12, "f0_std": 0.0},
    # 躯体化型长期只讲身体：声音上「看不出来」，正合该风格设定
    "somatic": {"downgrade": False, "rate": 0.0, "pause": 0.0, "f0_std": -0.05,
                "force_neutral": True},
    "hostile": {"downgrade": False, "rate": 0.20, "pause": -0.05, "f0_std": 0.25,
                "agitate_from": 3},
}

# 校验 VoiceMeta 时必须存在的声学键（历史上踩过 syllable_rate / words_per_min
# 键名漂移的坑，这里直接卡死）
_REQUIRED_FEATURE_KEYS = ("syllable_rate", "pause_ratio", "f0_std")


@dataclass
class VoiceConfig:
    """病人 Agent 的语音开关。默认 off —— 不开启时行为与改造前完全一致。"""

    mode: str = "off"                 # off | synth | tts
    consistency: str = "auto"         # auto | mask | consistent
    backend: str = "auto"             # auto | edge | macos
    audio_dir: str | None = None      # 落 WAV 目录（仅 tts，供人工试听/回归样本）
    strict: bool = False              # ASR 空转写时照真实行为，不用原文兜底


@dataclass
class ProsodyPlan:
    """一轮的韵律方案：目标语气 + 声学指标（时长类指标在拿到文本后派生）。"""

    tone_target: str
    f0_mean_hz: float
    f0_std: float
    syllable_rate: float
    pause_ratio: float
    energy_db: float
    note: str = ""


# ================================================================== 工具
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _syllables(text: str) -> int:
    """粗估音节数：中文按字计，标点与空白不计。"""
    n = 0
    for ch in text or "":
        if ch.isspace() or ch in "，。！？、；：…—-.,!?;:'\"“”‘’（）()《》【】":
            continue
        n += 1
    return max(n, 1)


def _severity_tone(severity: int, turn: int, total: int) -> str:
    """内心严重程度决定的语气基线（不含风格修正）。"""
    base = _SEVERITY_TONE.get(severity, "neutral")
    if severity >= 4 and total > 0 and turn / total >= 0.5:
        return "low"          # 后段抑制加深
    return base


def _target_tone(style: str, severity: int, turn: int, total: int,
                 ceiling: int, consistency: str) -> str:
    """语音反映**内心状态**，与文本披露上限（ceiling）解耦 —— 这是掩饰样本的由来。"""
    if consistency == "consistent":
        # 与嘴上说的一致：轻描淡写时就该是平稳的声音（对照组）
        if ceiling <= 2:
            return "neutral"
        if ceiling == 3:
            return "flat"
        return _severity_tone(severity, turn, total)

    base = _severity_tone(severity, turn, total)
    if consistency == "mask":
        return base            # 忽略风格的"掩饰性衰减"，把内心状态直接挂到声音上

    sv = STYLE_VOICE.get(style, {})
    if sv.get("agitate_from") and severity >= sv["agitate_from"]:
        return "agitated"
    if sv.get("force_neutral"):
        return "neutral" if severity <= 3 else base
    # 掩饰/回避把声音压低一档 —— 但 severity=1 本来没什么可藏的，压低会误报
    # （方案 V4：severity=1 必须 neutral、mm=0）
    if sv.get("downgrade") and severity >= 2:
        return _DOWN[base]
    return base


# ================================================================== 状态机
def plan_for(*, style: str, severity: int, turn: int, total_turns: int,
             ceiling: int, openness: int = 5, cooperation: str = "",
             consistency: str = "auto", rng=None) -> ProsodyPlan:
    """生成一轮的韵律方案。

    `rng` 必须是病人 Agent 的**独立随机流**，绝不复用文本侧那一条 ——
    否则同 seed 的文本对白会整体漂移，现有矩阵基线全部作废（方案 §8）。
    """
    tone = _target_tone(style, severity, turn, total_turns, ceiling, consistency)
    p = dict(TONE_PRESETS[tone])
    sv = STYLE_VOICE.get(style, {})

    p["syllable_rate"] *= 1 + float(sv.get("rate", 0.0))
    p["pause_ratio"] = _clamp(p["pause_ratio"] + float(sv.get("pause", 0.0)),
                              0.05, 0.85)
    p["f0_std"] *= 1 + float(sv.get("f0_std", 0.0))

    # 压着不说 -> 说得更慢、停顿更多
    if openness <= 4:
        p["syllable_rate"] *= 0.9
        p["pause_ratio"] = _clamp(p["pause_ratio"] + 0.06, 0.05, 0.85)
    # 配合度低 -> 吞吞吐吐
    if cooperation.startswith(("低", "很低")):
        p["pause_ratio"] = _clamp(p["pause_ratio"] + 0.05, 0.05, 0.85)

    # ±5% 抖动：同一 seed 可复现，换 seed 有变化
    if rng is not None:
        for k in ("f0_mean_hz", "f0_std", "syllable_rate", "energy_db"):
            p[k] *= 1 + rng.uniform(-0.05, 0.05)

    note = f"{tone}"
    if consistency != "auto":
        note += f"/{consistency}"
    return ProsodyPlan(tone_target=tone, note=note, **p)


def _build(cur: dict[str, float], text: str) -> AudioFeatures:
    """把韵律参数拼成 `AudioFeatures`（时长类指标按文本长度派生）。"""
    rate = max(0.5, cur["syllable_rate"])
    pause = _clamp(cur["pause_ratio"], 0.05, 0.85)
    speech_dur = max(0.5, _syllables(text) / rate)
    duration = _clamp(speech_dur / (1 - pause), 1.0, 30.0)
    return AudioFeatures(
        duration_sec=round(duration, 2),
        speech_ratio=round(1 - pause, 3),
        pause_ratio=round(pause, 3),
        syllable_rate=round(rate, 2),
        f0_mean_hz=round(cur["f0_mean_hz"], 1),
        f0_std=round(cur["f0_std"], 1),
        energy_db=round(cur["energy_db"], 1),
        segments=int(_clamp(round(speech_dur / 2.0), 2, 6)),
    )


def finalize_audio_features(plan: ProsodyPlan,
                            text: str = "") -> tuple[AudioFeatures, str, bool, str]:
    """生成特征并**复核语气标签**：不符就向预设收敛，最多 8 次。

    复核是硬要求：风格修正（如反刍型 +0.12 停顿）可能把指标推出目标区间，
    若不校验，`low` 可能实际打成 `flat`，掩饰信号就从 2 级掉到 1 级。
    """
    cur = {"f0_mean_hz": plan.f0_mean_hz, "f0_std": plan.f0_std,
           "syllable_rate": plan.syllable_rate, "pause_ratio": plan.pause_ratio,
           "energy_db": plan.energy_db}
    preset = TONE_PRESETS[plan.tone_target]
    feats, tone, reason = None, "", ""
    for _ in range(8):
        feats = _build(cur, text)
        tone, reason = asr.tone_label(feats)
        if tone == plan.tone_target:
            return feats, tone, True, reason
        for k in cur:
            cur[k] = cur[k] + 0.4 * (preset[k] - cur[k])
    return feats, tone, False, reason


def _summary(f: AudioFeatures, tone: str) -> str:
    """与 `asr.transcribe()` 里 summary 的措辞保持一致 —— 进 LLM 上下文的
    是这一行，格式必须和线上完全一样。"""
    parts = [f"时长 {f.duration_sec}s"]
    if f.syllable_rate:
        parts.append(f"语速约 {f.syllable_rate} 音节/秒")
    if f.f0_mean_hz:
        parts.append(f"基频 {f.f0_mean_hz}Hz（标准差 {f.f0_std}）")
    if f.pause_ratio is not None:
        parts.append(f"停顿占比 {f.pause_ratio:.0%}")
    parts.append(asr.TONE_TEXT.get(tone, tone))
    return "；".join(parts)


# ================================================================== 契约校验
def validate_voice_meta(meta: dict) -> dict:
    """产出必须是线上 `/api/chat` 认的 `VoiceMeta`，且声学键齐全（C2 / V9）。"""
    vm = VoiceMeta.model_validate(meta)
    af = vm.audio_features or {}
    missing = [k for k in _REQUIRED_FEATURE_KEYS if k not in af]
    if missing:
        raise ValueError(
            f"audio_features 缺少键 {missing} —— 感知层口径漂移，"
            "诊断侧会读不到这些指标")
    return vm.model_dump()


def voice_from_asr(out: dict) -> dict:
    """把 `asr.transcribe()` 的返回组装成 VoiceMeta（C2：不另造字段）。"""
    return validate_voice_meta({
        "audio_features": out.get("audio_features") or {},
        "tone": out.get("tone", ""),
        "tone_reason": out.get("tone_reason", ""),
        "summary": out.get("summary", ""),
    })


def to_voice_meta(plan: ProsodyPlan, text: str = "") -> tuple[dict, bool]:
    """T1：直接产出 VoiceMeta。返回 (meta, tone 是否命中目标)。"""
    feats, tone, verified, reason = finalize_audio_features(plan, text)
    meta = validate_voice_meta({
        "audio_features": feats.model_dump(),
        "tone": tone,
        "tone_reason": reason,
        "summary": _summary(feats, tone),
    })
    return meta, verified


# ================================================================== TTS 档
def tts_params(plan: ProsodyPlan) -> dict:
    """韵律 -> TTS 参数。三者只能近似表达，靠闭环校准逼近目标。"""
    preset = TONE_PRESETS["neutral"]
    return {
        "rate_pct": int(_clamp((plan.syllable_rate / preset["syllable_rate"] - 1) * 100,
                               -60, 60)),
        "pitch_hz": int(_clamp((plan.f0_mean_hz - preset["f0_mean_hz"]) * 0.5, -50, 50)),
        "volume_pct": int(_clamp((plan.energy_db - preset["energy_db"]) * 2, -50, 20)),
        "syllable_rate": plan.syllable_rate,
    }


def _calibrate(params: dict, features: dict, preset: dict) -> dict:
    """按实测指标与目标预设的偏差调整 TTS 参数（闭环校准用）。"""
    out = dict(params)
    rate = features.get("syllable_rate")
    f0 = features.get("f0_mean_hz")
    if rate:
        out["rate_pct"] = _clamp(
            out["rate_pct"] + (preset["syllable_rate"] - rate) / max(rate, 0.1) * 100,
            -60, 60)
        out["syllable_rate"] = preset["syllable_rate"]
    if f0:
        out["pitch_hz"] = _clamp(out["pitch_hz"] + (preset["f0_mean_hz"] - f0) * 0.5,
                                 -50, 50)
    return out


def _dump(wav: bytes, audio_dir: str | None, tag: str) -> str:
    if not audio_dir or not wav:
        return ""
    try:
        os.makedirs(audio_dir, exist_ok=True)
        path = os.path.join(audio_dir, f"{tag}_{int(time.time() * 1000)}.wav")
        with open(path, "wb") as f:
            f.write(wav)
        return path
    except OSError as exc:
        print(f"[patient_voice] 音频落盘失败：{exc}")
        return ""


def make_voice(text: str, plan: ProsodyPlan, cfg: VoiceConfig,
               rng=None) -> tuple[dict | None, dict]:
    """按配置产出一轮的语音。返回 (VoiceMeta | None, 诊断信息)。

    `info` 里记录一切与线上不同的兜底行为（方案 C6）：voice_source /
    asr_text_miss / tone_verified，便于复盘时一眼看出这轮是不是"真声音"。
    """
    info: dict[str, Any] = {"voice_source": "off", "tone": "", "tone_verified": False,
                            "tts_backend": "", "tts_error": "", "asr_text": "",
                            "asr_text_miss": False, "audio_path": "",
                            "strict": bool(cfg.strict)}
    if cfg.mode == "off":
        return None, info

    # T1 结果同时是 T2 的兜底（TTS 不可用 / 合成失败时退回）
    meta, verified = to_voice_meta(plan, text)
    info.update(voice_source="synth", tone=meta["tone"], tone_verified=verified)
    if cfg.mode != "tts":
        return meta, info

    import tts

    backend, why = tts.available(cfg.backend)
    if not backend:
        info.update(voice_source="fallback", tts_error=why or "无可用 TTS 后端")
        return meta, info

    params = tts_params(plan)
    best: dict | None = None
    for attempt in range(3):
        wav = tts.synth(text, backend=backend, **params)
        if not wav:
            info["tts_error"] = "合成返回空音频"
            break
        info["audio_path"] = _dump(wav, cfg.audio_dir, f"{plan.tone_target}_{attempt}")
        out = asr.transcribe(wav, "zh")
        m2 = voice_from_asr(out)
        best = m2
        info.update(asr_text=out.get("text", ""), asr_error=out.get("asr_error", ""))

        if not out.get("text"):
            # 合成音被 whisper 判为无语音时，先"重录一次"（说慢一点）
            info["asr_text_miss"] = True
            if attempt == 0 and not cfg.strict:
                params = {**params,
                          "rate_pct": _clamp(params["rate_pct"] - 15, -60, 60)}
                continue
            break      # strict：照真实行为，空就是空

        if m2["tone"] == plan.tone_target:
            info.update(voice_source="tts", tts_backend=backend, tone=m2["tone"],
                        tone_verified=True)
            return m2, info
        params = _calibrate(params, m2["audio_features"],
                            TONE_PRESETS[plan.tone_target])

    if best is None:
        info.update(voice_source="fallback",
                    tts_error=info.get("tts_error") or "TTS 未产出可用音频")
        return meta, info
    hit = best["tone"] == plan.tone_target
    info.update(voice_source="tts", tts_backend=backend, tone=best["tone"],
                tone_verified=hit)
    return best, info
