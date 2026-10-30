"""文本 -> 语音（**仅供病人模拟联路的 T2 档使用**，不参与线上链路）。

线上是真人说话 -> `/api/asr`；这里反过来：病人 Agent 的文本 -> 合成音频 ->
再交给 `asr.transcribe()` 真实测量，补齐感知层那一半的覆盖。

后端优先级：
  macos —— `say` 系统合成器，零依赖、**离线**（心理危机语料不该随意外发）
  edge  —— `edge-tts`，韵律可控（rate/pitch/volume），但**需外网**，
           且合成文本会发到云端，默认不优先

⚠ 合成出来的仍是**合成人声**，与真人声学分布有差异，只能用于链路验证，
   不能当作真人语料使用。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

# `say` 的语速单位是 wpm（词/分钟），与音节/秒不是 1:1。
# 实测标定（macOS 中文音色 Tingting，用 asr 回测量 syllable_rate）：
#   wpm = syllable_rate * 60 * SAY_WPM_SCALE
#   系数 1.0 时目标 2.4 实测 3.97、目标 4.5 实测 4.52（整体偏快）；
#   系数 0.625 时实测 3.39 / 4.36，语速基本对得上。
# ⚠ 只标定到「语速」。音高无法标定 —— `say` 没有音高/停顿参数，
#   实测 F0 恒为 ~228Hz、标准差 ~57Hz，`tone_label()` 一律判 agitated。
#   即：macOS 后端只能验证「合成->转写->特征提取」链路通不通，
#   命中目标语气需要可控音高的后端（edge-tts）。
SAY_WPM_SCALE = 0.625
SAY_DEFAULT_WPM = 175

DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
PREFERRED_MACOS_VOICES = ("Tingting", "Ting-Ting", "Meijia")

_zh_voice_cache: dict[str, str] = {}


# ================================================================== 可用性
def _macos_zh_voice() -> str:
    """挑一个中文音色；没有就返回空串。"""
    if "voice" in _zh_voice_cache:
        return _zh_voice_cache["voice"]
    voice = ""
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True,
                             text=True, timeout=15)
        zh: list[str] = []
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("zh"):
                zh.append(parts[0])
        for name in PREFERRED_MACOS_VOICES:
            if name in zh:
                voice = name
                break
        voice = voice or (zh[0] if zh else "")
    except Exception:  # noqa: BLE001
        voice = ""
    _zh_voice_cache["voice"] = voice
    return voice


def _check(backend: str) -> tuple[bool, str]:
    if backend == "macos":
        if sys.platform != "darwin":
            return False, "当前不是 macOS"
        if not shutil.which("say"):
            return False, "未找到 say"
        v = _macos_zh_voice()
        if not v:
            return False, "系统未安装中文音色（系统设置 -> 辅助功能 -> 朗读内容）"
        return True, ""
    if backend == "edge":
        if shutil.which("edge-tts"):
            return True, ""
        try:
            import importlib.util
            if importlib.util.find_spec("edge_tts") is not None:
                return True, ""
        except Exception:  # noqa: BLE001
            pass
        return False, "未安装 edge-tts（pip install edge-tts）；注意它需联网且会上传文本"
    return False, f"未知后端 {backend!r}"


def available(backend: str = "auto") -> tuple[str | None, str]:
    """返回 (可用后端名, 不可用原因)。auto 时优先离线后端。"""
    order = ("macos", "edge") if backend == "auto" else (backend,)
    why = ""
    for b in order:
        ok, reason = _check(b)
        if ok:
            return b, ""
        why = reason
    return None, why


# ================================================================== 合成
def synth(text: str, *, backend: str = "auto", rate_pct: int = 0,
          pitch_hz: int = 0, volume_pct: int = 0, voice: str = "",
          syllable_rate: float | None = None, timeout: int = 90) -> bytes | None:
    """合成音频，返回字节（格式由后端决定，`asr.decode_audio` 都能解）。

    失败返回 None —— 调用方会回落到 T1（合成特征），不让对练中断。
    """
    if not (text or "").strip():
        return None
    resolved, _ = available(backend)
    if not resolved:
        return None
    return (_synth_macos if resolved == "macos" else _synth_edge)(
        text, voice=voice, rate_pct=rate_pct, pitch_hz=pitch_hz,
        volume_pct=volume_pct, syllable_rate=syllable_rate, timeout=timeout)


def _synth_macos(text: str, *, voice: str, rate_pct: int, pitch_hz: int,
                 volume_pct: int, syllable_rate: float | None,
                 timeout: int) -> bytes | None:
    voice = voice or _macos_zh_voice()
    if syllable_rate:
        wpm = int(max(60, min(400, syllable_rate * 60 * SAY_WPM_SCALE)))
    else:
        wpm = int(SAY_DEFAULT_WPM * (1 + rate_pct / 100))
    with tempfile.TemporaryDirectory() as d:
        txt_path, wav_path = os.path.join(d, "t.txt"), os.path.join(d, "o.wav")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        cmd = ["say", "-v", voice, "-r", str(wpm), "-o", wav_path,
               "--file-format=WAVE", "--data-format=LEI16@16000", "-f", txt_path]
        try:
            subprocess.run(cmd, check=True, timeout=timeout,
                           capture_output=True, text=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[tts] say 合成失败：{exc}")
            return None
        if not os.path.exists(wav_path):
            return None
        with open(wav_path, "rb") as f:
            return f.read()


def _synth_edge(text: str, *, voice: str, rate_pct: int, pitch_hz: int,
                volume_pct: int, syllable_rate: float | None,
                timeout: int) -> bytes | None:
    voice = voice or DEFAULT_EDGE_VOICE
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "o.mp3")
        args = ["--voice", voice, "--rate", f"{rate_pct:+d}%",
                "--pitch", f"{pitch_hz:+d}Hz", "--volume", f"{volume_pct:+d}%",
                "--text", text, "--write-media", out]
        cmds = [["edge-tts", *args]]
        if not shutil.which("edge-tts"):
            cmds = [[sys.executable, "-m", "edge_tts", *args], cmds[0]]
        last = ""
        for cmd in cmds:
            try:
                subprocess.run(cmd, check=True, timeout=timeout,
                               capture_output=True, text=True)
                if os.path.exists(out):
                    with open(out, "rb") as f:
                        return f.read()
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
        print(f"[tts] edge-tts 合成失败：{last or '未产出音频'}")
        return None


def describe(backend: str = "auto") -> dict[str, Any]:
    """给 `/api/sim/voice/status` 用的可用性描述。"""
    info: dict[str, Any] = {"macos": False, "edge": False, "hint": ""}
    for b in ("macos", "edge"):
        ok, why = _check(b)
        info[b] = ok
        if not ok and not info["hint"]:
            info["hint"] = why
    resolved, why = available(backend)
    info["resolved"] = resolved or ""
    if not resolved:
        info["hint"] = why
    return info
