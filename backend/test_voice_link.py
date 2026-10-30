#!/usr/bin/env python3
"""语音联路自检（对应方案 §7 的 V1–V4 / V6 / V9）。

默认**不依赖 LLM**：韵律状态机与 `voice_risk_signal()` 都是纯函数，
几秒内跑完，能挡住绝大多数改歪的情况。

    python3 test_voice_link.py                 # 离线自检（默认）
    python3 test_voice_link.py --with-llm      # 附加 V6 文本回归（需 vLLM）
    python3 test_voice_link.py --with-tts      # 附加 T2 端到端（需 TTS + ASR）
"""
from __future__ import annotations

import argparse
import random
import sys

import patient_voice
from patient_voice import VoiceConfig, plan_for, to_voice_meta, validate_voice_meta
from perception import voice_risk_signal

FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    mark = "OK  " if cond else "FAIL"
    if not cond:
        FAILS.append(f"{label}{('：' + detail) if detail else ''}")
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail and not cond else ""))
    return cond


# ------------------------------------------------------------------ V9 契约
def t_contract() -> None:
    print("\n[V9] VoiceMeta 契约：缺键必须直接报错（感知层口径漂移）")
    ok_raise = False
    try:
        validate_voice_meta({"audio_features": {"words_per_min": 90},
                             "tone": "low", "tone_reason": "", "summary": ""})
    except ValueError as exc:
        ok_raise = "syllable_rate" in str(exc)
    check(ok_raise, "缺 syllable_rate 时抛 ValueError")

    meta, verified = to_voice_meta(
        plan_for(style="guarded", severity=4, turn=4, total_turns=8, ceiling=3,
                 rng=random.Random(1)),
        "还行吧，没什么事。")
    check(set(meta) == {"audio_features", "tone", "tone_reason", "summary"},
          "字段集合与线上 VoiceMeta 一致", str(sorted(meta)))
    check("；" in meta["summary"] and "时长" in meta["summary"],
          "summary 措辞与 asr.transcribe() 同格式", meta["summary"])
    check(verified, "语气标签经 asr.tone_label 复核命中")


# ------------------------------------------------------------------ 状态机全覆盖
def t_state_machine() -> None:
    print("\n[状态机] 6 风格 × 5 严重程度 × 8 轮：语气必须命中目标且契约合法")
    bad, rows = 0, []
    for style in ("guarded", "avoidant", "verbose", "ruminative", "somatic", "hostile"):
        for sev in range(1, 6):
            rng = random.Random(7)
            tones = set()
            for turn in range(1, 9):
                plan = plan_for(style=style, severity=sev, turn=turn,
                                total_turns=8, ceiling=min(sev, turn),
                                rng=rng)
                meta, verified = to_voice_meta(plan, "今天也还是那样，没什么特别的。")
                if (not verified) or meta["tone"] != plan.tone_target:
                    bad += 1
                    rows.append(f"{style}/sev{sev}/t{turn} 目标{plan.tone_target}"
                                f" 实测{meta['tone']}")
                tones.add(meta["tone"])
            print(f"    {style:<11} sev{sev} -> {sorted(tones)}")
    check(bad == 0, "全部命中目标语气", f"{bad} 轮不符：" + "；".join(rows[:5]))


# ------------------------------------------------------------------ V1-V4
def _plan(style, sev, turn=4, total=8, ceiling=3, consistency="auto", seed=3):
    return plan_for(style=style, severity=sev, turn=turn, total_turns=total,
                    ceiling=ceiling, consistency=consistency,
                    rng=random.Random(seed))


def t_risk_rules() -> None:
    print("\n[V1] 掩饰：嘴上正面 + 声音低落 -> 2 级")
    meta, _ = to_voice_meta(_plan("guarded", 4, consistency="mask"), "还行吧，挺好的。")
    lvl, ev = voice_risk_signal(meta["tone"], meta["audio_features"], "还行吧，挺好的。")
    check(meta["tone"] in ("low", "flat"), "掩饰档声音为低落/平淡", meta["tone"])
    check(lvl == 2, "mm 通道=2（掩饰倾向）", f"实际 {lvl}｜{ev}")
    check("掩饰" in ev, "证据串含掩饰倾向", ev)

    print("\n[V2] 一致印证：声音低落 + 自述负面 -> 1 级")
    meta, _ = to_voice_meta(_plan("guarded", 3), "真的不想活了，太累了。")
    lvl, ev = voice_risk_signal(meta["tone"], meta["audio_features"],
                                "真的不想活了，太累了。")
    check(lvl == 1 and "一致" in ev, "mm=1 且证据含一致", f"{lvl}｜{ev}")

    print("\n[V3] 激动：抵触型高严重程度 -> 1 级")
    meta, _ = to_voice_meta(_plan("hostile", 5), "你问这些有什么用。")
    lvl, ev = voice_risk_signal(meta["tone"], meta["audio_features"], "你问这些有什么用。")
    check(meta["tone"] == "agitated", "语气=agitated", meta["tone"])
    check(lvl == 1, "mm=1", f"{lvl}｜{ev}")

    print("\n[V4] 不误报：severity=1 -> neutral，mm=0")
    meta, _ = to_voice_meta(_plan("guarded", 1), "最近加班有点多，睡不太好。")
    lvl, ev = voice_risk_signal(meta["tone"], meta["audio_features"],
                                "最近加班有点多，睡不太好。")
    check(meta["tone"] == "neutral", "语气=neutral", meta["tone"])
    check(lvl == 0 and ev == "", "mm=0 且无证据", f"{lvl}｜{ev}")

    print("\n[对照] consistent 档：轻描淡写时声音也应平稳（mm=0）")
    meta, _ = to_voice_meta(_plan("guarded", 4, ceiling=1, consistency="consistent"),
                            "还行吧，挺好的。")
    lvl, _ = voice_risk_signal(meta["tone"], meta["audio_features"], "还行吧，挺好的。")
    check(lvl == 0, "mm=0（对照组不产生掩饰信号）", f"{lvl}")


# ------------------------------------------------------------------ V6 文本回归
def t_text_regression(turns: int = 6) -> None:
    print(f"\n[V6] 文本回归：同一 seed 下 开语音 vs 关语音 文本必须逐字相同")
    from patient_agent import PatientAgent

    def texts(cfg: VoiceConfig) -> list[str]:
        p = PatientAgent(style="guarded", severity=4, seed=42,
                         total_turns=turns, voice=cfg)
        return [p.next("嗯，我在听。").text for _ in range(turns)]

    off = texts(VoiceConfig())
    on = texts(VoiceConfig(mode="synth", consistency="mask"))
    same = off == on
    check(same, "开/关语音文本一致（独立 RNG 流生效）",
          f"off[0]={off[0]!r} on[0]={on[0]!r}" if not same else "")
    print(f"      样例：{off[0]}")


# ------------------------------------------------------------------ T2 端到端
def t_tts_e2e() -> None:
    print("\n[T2] 端到端：文本 -> TTS -> asr.transcribe() -> 真实特征")
    import tts

    backend, why = tts.available("auto")
    if not backend:
        print(f"    跳过：无可用 TTS 后端（{why}）")
        return
    print(f"    后端：{backend}")
    cfg = VoiceConfig(mode="tts", backend=backend)
    hits = 0
    for sev, want in ((1, "neutral"), (3, "flat"), (5, "low")):
        plan = plan_for(style="guarded", severity=sev, turn=4, total_turns=8,
                        ceiling=min(sev, 3), rng=random.Random(5))
        meta, info = patient_voice.make_voice("最近一直睡不好，也没什么胃口。", plan, cfg)
        print(f"    sev{sev} 目标={plan.tone_target} 实测={info['tone']} "
              f"来源={info['voice_source']} 转写={info['asr_text']!r}")
        hits += int(info["tone"] == plan.tone_target)
        check(bool(meta), f"sev{sev} 产出 VoiceMeta")
    print(f"    语气命中 {hits}/3（不作断言：合成人声与真人有差异）")
    if backend == "macos":
        print("    注：macOS say 无音高/停顿参数，实测 F0≈228Hz、σ≈57、"
              "停顿≈0.10，tone 恒为 agitated —— 只能验证链路通断，"
              "断言语气请用 --voice synth 或装 edge-tts")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-llm", action="store_true", help="附加 V6（需 vLLM）")
    ap.add_argument("--with-tts", action="store_true", help="附加 T2 端到端")
    args = ap.parse_args()

    print("=" * 70)
    print("语音联路自检")
    print("=" * 70)
    t_contract()
    t_state_machine()
    t_risk_rules()
    if args.with_llm:
        t_text_regression()
    if args.with_tts:
        t_tts_e2e()

    print("\n" + "=" * 70)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
