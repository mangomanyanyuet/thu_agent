"""语音感知层：音频字节 -> (转写文本, 声学特征)。

设计原则同 perception.py：多模态理解放在感知层，只把结构化指标和一行摘要
送进 LLM 上下文。原始音频既不入库也不进上下文。

依赖说明：
- 解码用 faster-whisper 内置的 PyAV，因此**不需要系统 ffmpeg**（容器内通常没有）。
- 声学特征全部用 numpy 实现，避免引入 librosa/soundfile（它们依赖
  libsndfile 等系统库，在本环境装不上）。
"""
from __future__ import annotations

import io
import math
from typing import Any

import numpy as np

from models import AudioFeatures

# ------------------------------------------------------------------ 配置
SAMPLE_RATE = 16000

# 中文场景下 small 是精度/速度的平衡点；tiny/base 对中文识别率明显偏低。
ASR_MODEL_SIZE = "small"
ASR_DEVICE = "cpu"
ASR_COMPUTE = "int8"

_model = None


def get_model():
    """懒加载 Whisper 模型（首次会下载，之后走本地缓存）。"""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(ASR_MODEL_SIZE, device=ASR_DEVICE,
                              compute_type=ASR_COMPUTE)
    return _model


def asr_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 解码
def decode_audio(data: bytes, sr: int = SAMPLE_RATE) -> np.ndarray:
    """把任意浏览器/常见格式的音频字节解码成 16k 单声道 float32 数组。"""
    import av

    buf = io.BytesIO(data)
    with av.open(buf, mode="r") as container:
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(
            format="flt", layout="mono", rate=sr)
        chunks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # 冲刷缓冲
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32)


# ------------------------------------------------------------------ 声学特征
def _frame(x: np.ndarray, win: int, hop: int) -> np.ndarray:
    """分帧，返回 (n_frames, win)。长度不足时补零。"""
    if x.size < win:
        x = np.pad(x, (0, win - x.size))
    n = 1 + (x.size - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _rms(x: np.ndarray, win: int = 512, hop: int = 256) -> np.ndarray:
    f = _frame(x, win, hop)
    return np.sqrt(np.mean(f * f, axis=1) + 1e-12)


def _energy_db(x: np.ndarray) -> float:
    r = float(np.sqrt(np.mean(x * x) + 1e-12))
    return round(20 * math.log10(r + 1e-12), 1)


def _estimate_f0(x: np.ndarray, sr: int = SAMPLE_RATE,
                 fmin: float = 70.0, fmax: float = 400.0) -> tuple[float, float]:
    """自相关法估基频。比 YIN 粗糙但够用，且零依赖。

    返回 (f0_mean, f0_std)，未检测到清音/静音帧时不计入。
    """
    win, hop = 1024, 512
    frames = _frame(x, win, hop)
    # 先剔除静音帧
    energy = np.sqrt(np.mean(frames * frames, axis=1))
    thr = max(energy.max() * 0.05, 1e-4)
    voiced = frames[energy > thr]
    if voiced.shape[0] < 3:
        return 0.0, 0.0

    voiced = voiced - voiced.mean(axis=1, keepdims=True)
    # 加汉宁窗，减少边界效应
    window = np.hanning(win).astype(np.float32)
    voiced = voiced * window

    n_fft = 2048
    spec = np.fft.rfft(voiced, n=n_fft, axis=1)
    acf = np.fft.irfft(spec * np.conjugate(spec), n=n_fft, axis=1)[:, : n_fft // 2]
    acf0 = acf[:, 0:1] + 1e-12
    acf = acf / acf0

    lo = max(int(sr / fmax), 2)
    hi = min(int(sr / fmin), n_fft // 2 - 1)
    if hi <= lo:
        return 0.0, 0.0
    band = acf[:, lo:hi]
    # 逐帧取相关峰（axis=1 -> 每帧一个 lag），再换算成基频
    peak_rel = np.argmax(band, axis=1) + lo
    peak_val = band.max(axis=1)
    # 相关峰值太低说明不是周期性浊音（清音/噪声）
    mask = peak_val > 0.35
    if int(mask.sum()) < 3:
        return 0.0, 0.0
    f0 = sr / peak_rel[mask].astype(np.float64)
    return round(float(np.mean(f0)), 1), round(float(np.std(f0)), 1)


def _vad_segments(x: np.ndarray, sr: int = SAMPLE_RATE) -> tuple[list[tuple[float, float]], float]:
    """基于能量的简易 VAD：返回 (语音段[(start_s,end_s)], 静音占比)。"""
    hop = 256
    r = _rms(x, 512, hop)
    if r.size == 0:
        return [], 1.0
    thr = max(float(r.max()) * 0.08, 1e-4)
    active = r > thr

    # 形态学平滑：填掉短间隙、去掉过短的爆发
    def _smooth(a: np.ndarray, min_len: int) -> np.ndarray:
        out = a.copy()
        i = 0
        while i < out.size:
            j = i
            while j < out.size and out[j] == out[i]:
                j += 1
            if (j - i) < min_len:
                out[i:j] = not out[i]
            i = j
        return out

    active = _smooth(active, 3)          # 去毛刺
    active = _smooth(~active, 10)        # 填掉 <~160ms 的间隙
    active = ~_smooth(~(~active), 10)
    active = ~_smooth(active, 10)
    active = ~active

    segs, start = [], None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            segs.append((start * hop / sr, i * hop / sr))
            start = None
    if start is not None:
        segs.append((start * hop / sr, active.size * hop / sr))
    pause_ratio = 1.0 - float(active.mean()) if active.size else 1.0
    return segs, round(pause_ratio, 3)


def _syllable_rate(x: np.ndarray, sr: int, speech_dur: float) -> float | None:
    """用**平滑后**的能量包络估计音节速率。

    关键点：原始 RMS 包络里混着基频周期的波动（70~400Hz），直接数峰会把
    声带振动当成音节 —— 实测对合成音算出 29 音节/秒（正常人约 4~7）。
    所以先做 ~90ms 滑动平均滤掉 F0 ripple，再数峰。
    """
    hop = 256  # 16ms
    r = _rms(x, 512, hop)
    if r.size < 8:
        return None

    # 1) 滑动平均低通（约 6 帧 ≈ 96ms，低于音节尺度 ~100-200ms）
    k = 6
    kernel = np.ones(k, dtype=np.float32) / k
    smooth = np.convolve(r, kernel, mode="same")

    # 2) 只在语音段内统计（静音段的噪声不算）
    thr = max(float(r.max()) * 0.08, 1e-4)
    voiced = r > thr
    if voiced.sum() < 8:
        return None

    # 3) 数局部极大值，并要求两峰间隔 >= 120ms（人发音的生理下限）
    min_gap_frames = max(int(0.12 * sr / hop), 1)
    idx = np.arange(1, smooth.size - 1)
    peaks = idx[(smooth[1:-1] > smooth[:-2]) & (smooth[1:-1] >= smooth[2:]) & voiced[1:-1]]
    if peaks.size == 0:
        return None
    kept = [peaks[0]]
    for p in peaks[1:]:
        if p - kept[-1] >= min_gap_frames:
            kept.append(p)
    # 首尾各补一个峰：包络两端往往没有严格局部极大
    count = len(kept)
    if count < 2:
        return None
    return round(count / speech_dur, 2)


def extract_features(x: np.ndarray, sr: int = SAMPLE_RATE) -> AudioFeatures:
    """从波形提取结构化声学特征。"""
    if x.size == 0:
        return AudioFeatures(duration_sec=0.0)

    dur = round(float(x.size / sr), 2)
    segs, pause_ratio = _vad_segments(x, sr)
    speech_dur = sum(e - s for s, e in segs)
    f0_mean, f0_std = _estimate_f0(x, sr)

    syllable_rate = None
    if speech_dur > 0.3:
        syllable_rate = _syllable_rate(x, sr, speech_dur)

    return AudioFeatures(
        duration_sec=dur,
        speech_ratio=round(speech_dur / dur, 3) if dur else 0.0,
        pause_ratio=pause_ratio,
        syllable_rate=syllable_rate,
        f0_mean_hz=f0_mean,
        f0_std=f0_std,
        energy_db=_energy_db(x),
        segments=len(segs),
    )


# ------------------------------------------------------------------ 语气判定
def tone_label(f: AudioFeatures) -> tuple[str, str]:
    """由声学特征推断语气倾向，返回 (标签, 依据说明)。

    这是"多模态通道"的一环：语气与自述内容不一致时是重要的掩饰信号。
    注意：这是启发式规则，不是临床诊断工具。
    """
    if not f.duration_sec or f.duration_sec < 0.3:
        return "unknown", "音频过短，无法判断"

    cues: list[str] = []
    score = 0  # 正=积极唤醒，负=低落/抑制

    # 基频：低而平 = 情绪平淡/低落；高且起伏大 = 激动/焦虑
    if f.f0_std is not None:
        if f.f0_std < 15:
            score -= 2
            cues.append(f"基频变化很小(F0标准差{f.f0_std}Hz)，语调平板")
        elif f.f0_std > 45:
            score += 1
            cues.append(f"基频起伏大(F0标准差{f.f0_std}Hz)，情绪激动")
    if f.f0_mean_hz:
        if f.f0_mean_hz < 110:
            score -= 1
            cues.append(f"基频偏低({f.f0_mean_hz}Hz)")
        elif f.f0_mean_hz > 220:
            score += 1
            cues.append(f"基频偏高({f.f0_mean_hz}Hz)")

    # 语速：慢 = 精神运动性抑制（抑郁常见体征）
    if f.syllable_rate is not None:
        if f.syllable_rate < 3.0:
            score -= 2
            cues.append(f"语速偏慢(约{f.syllable_rate}音节/秒)")
        elif f.syllable_rate > 6.5:
            score += 1
            cues.append(f"语速偏快(约{f.syllable_rate}音节/秒)")

    # 停顿：长停顿多 = 思维迟缓/犹豫
    if f.pause_ratio is not None and f.pause_ratio > 0.55:
        score -= 1
        cues.append(f"停顿占比高({f.pause_ratio:.0%})")

    # 能量：过低 = 无力/萎靡
    if f.energy_db is not None and f.energy_db < -38:
        score -= 1
        cues.append(f"音量偏低({f.energy_db}dB)")

    if score <= -3:
        label = "low"        # 低落 / 抑制
    elif score <= -1:
        label = "flat"       # 平淡 / 略低
    elif score >= 2:
        label = "agitated"   # 激动 / 焦虑
    else:
        label = "neutral"

    return label, ("；".join(cues) if cues else "各项指标在正常范围")


TONE_TEXT = {
    "low": "语气低落（语调平板、语速慢、能量低）",
    "flat": "语气平淡偏低",
    "neutral": "语气平稳",
    "agitated": "语气激动/焦虑（语速快、基频起伏大）",
    "unknown": "语气未知",
}


# ------------------------------------------------------------------ 对外主入口
def transcribe(data: bytes, language: str = "zh") -> dict[str, Any]:
    """音频字节 -> {text, audio_features, tone, tone_reason, summary}。

    ASR 失败时 text 为空，但声学特征仍会尽量返回 —— 特征不依赖 ASR，
    即使转写失败，语气信息也能进入风险判定的多模态通道。
    """
    x = decode_audio(data, SAMPLE_RATE)
    feats = extract_features(x, SAMPLE_RATE)
    tone, reason = tone_label(feats)

    text = ""
    if x.size > 0 and asr_available():
        try:
            model = get_model()
            segments, info = model.transcribe(
                x, language=language, beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400},
            )
            segs = list(segments)
            # Whisper 对非语音/纯噪声会产生幻觉文本（实测对合成音输出
            # "字幕by索兰娅"）。用 no_speech_prob 过滤，避免脏文本污染上下文。
            NO_SPEECH_MAX = 0.6
            kept = [s for s in segs if getattr(s, "no_speech_prob", 0.0) <= NO_SPEECH_MAX]
            if kept:
                text = "".join(s.text for s in kept).strip()
            elif segs:
                print(f"[asr] 疑似无语音（no_speech_prob={segs[0].no_speech_prob:.2f}），丢弃转写")
        except Exception as exc:  # noqa: BLE001
            # 转写失败不能拖垮对话：文本为空，特征照常返回
            print(f"[asr] 转写失败: {exc}")

    summary_parts = [f"时长 {feats.duration_sec}s"]
    if feats.syllable_rate:
        summary_parts.append(f"语速约 {feats.syllable_rate} 音节/秒")
    if feats.f0_mean_hz:
        summary_parts.append(f"基频 {feats.f0_mean_hz}Hz（标准差 {feats.f0_std}）")
    if feats.pause_ratio is not None:
        summary_parts.append(f"停顿占比 {feats.pause_ratio:.0%}")
    summary_parts.append(TONE_TEXT.get(tone, tone))

    return {
        "text": text,
        "audio_features": feats.model_dump(),
        "tone": tone,
        "tone_reason": reason,
        "summary": "；".join(summary_parts),
    }
