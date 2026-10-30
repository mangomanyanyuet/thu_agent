#!/usr/bin/env python3
"""病人模拟对练：让「病人 Agent」自动与陪伴 Agent 对话。

用途：验证风险识别 / 危机干预 / 人工审核中断（interrupt）链路，
不必靠人反复手打「不想活了」这类句子。

命令行：

    python3 simulate.py --list
    python3 simulate.py --style guarded --severity 4 --turns 8
    python3 simulate.py --style somatic --severity 5 --turns 6 --auto-review approve
    python3 simulate.py --style hostile --severity 3 --seed 42 --json out.json

服务端的 /api/sim/* 直接复用本文件的 `run_one_turn()` / `run_session()`，
保证脚本与接口走的是同一套逻辑，不会两边跑出不同结果。

⚠ 模拟数据会**真实写入数据库**（会话 / 消息 / 风险事件 / 画像），默认挂在
   `u_sim` 这个用户下，便于事后整体清理。不要把它的产出当真实用户数据看。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import patient_agent
from patient_agent import PatientAgent, PatientTurn, list_profiles
from patient_voice import VoiceConfig


# ------------------------------------------------------------------ 打印
def _short(text: str, n: int = 90) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def _fmt_channels(ch: dict) -> str:
    def lv(key: str) -> int:
        return int((ch.get(key) or {}).get("level") or 0)

    return f"{lv('rule')}/{lv('model')}/{lv('multimodal')}/{lv('scale')}"


def _mm_level(ch: dict) -> int:
    """多模态通道等级 —— 语音贡献是否生效，全看这一项。"""
    return int((ch.get("multimodal") or {}).get("level") or 0)


def _fmt_voice(v: dict) -> str:
    """一行说清本轮声音：语气、关键指标、来源、是否可信。"""
    af = v.get("audio_features") or {}
    marks = []
    if v.get("voice_source") == "fallback":
        marks.append(f"回落synth:{v.get('tts_error', '')}")
    if v.get("voice_source") == "tts":
        marks.append(f"tts={v.get('tts_backend', '')}")
    if not v.get("tone_verified", True):
        marks.append("未命中目标语气")
    if v.get("asr_text_miss"):
        marks.append("转写为空")
    return (f"tone={v.get('tone', '-')}（F0σ{af.get('f0_std', '-')}/"
            f"语速{af.get('syllable_rate', '-')}/"
            f"停顿{af.get('pause_ratio', '-')}）"
            + (f" [{'；'.join(marks)}]" if marks else ""))


def print_profiles() -> None:
    p = list_profiles()
    print("风格（--style）：")
    for s in p["styles"]:
        print(f"  {s['key']:<12} {s['name']:<6} {_short(s['desc'], 58)}")
    print("\n严重程度（--severity 1~5）：")
    for s in p["severities"]:
        print(f"  {s['level']}  {s['name']:<4} 目标风险 {s['target']}")
        print(f"      {_short(s['desc'], 68)}")
    print("\n默认：--style guarded --severity 3 --turns 6")


# ------------------------------------------------------------------ 会话
def new_sim_session(user_id: str, style: str, severity: int,
                    seed: int | None, turns: int,
                    voice: VoiceConfig | None = None) -> tuple[str, PatientAgent]:
    """建会话 + 建病人 Agent，并给会话打上 [SIM] 标记。"""
    import graph
    import storage

    patient = PatientAgent(style=style, severity=severity, seed=seed,
                           total_turns=turns, voice=voice or VoiceConfig())
    sid = graph.new_session(user_id)

    # 模拟数据混在真实会话里很难分辨，事后清理全靠这个标记
    sess = storage.get_session(sid)
    if sess is not None:
        sess.summary = f"[SIM] style={style} severity={severity} seed={patient.seed}"
        storage.upsert_session(sess)
    return sid, patient


# 病人 Agent 按会话复用。
#
# 必须复用：`/api/sim/turn` 每次调用都是独立请求，不复用就会每轮都从
# turn=1 重开，披露曲线永远停在第 1 级 —— 表现为「连点十次『跑一轮』，
# 风险一直是 none」，根本走不到危机分支。
_PATIENTS: dict[str, PatientAgent] = {}
_PATIENT_CAP = 200


def patient_of(sid: str, style: str, severity: int, seed: int | None,
               turns: int, voice: VoiceConfig | None = None) -> PatientAgent:
    """取（或建）该会话的病人 Agent。风格/严重程度变了就重建。

    语音配置不参与重建判断 —— 它不影响文本侧状态，改开关直接覆盖即可。
    """
    p = _PATIENTS.get(sid)
    if p is None or p.style != style or p.severity != severity:
        p = PatientAgent(style=style, severity=severity, seed=seed,
                         total_turns=turns, voice=voice or VoiceConfig())
        _PATIENTS[sid] = p
        if len(_PATIENTS) > _PATIENT_CAP:
            for k in list(_PATIENTS)[:len(_PATIENTS) - _PATIENT_CAP]:
                _PATIENTS.pop(k, None)
    else:
        p.total_turns = max(1, turns)   # 轮数可能被前端改过
        if voice is not None:
            p.voice = voice
    return p


def drop_patient(sid: str) -> None:
    _PATIENTS.pop(sid, None)


def last_assistant_text(sid: str) -> str:
    """取会话里最近一句陪伴者的话，作为病人本轮要回应的对象。"""
    from storage import list_messages

    for m in reversed(list_messages(sid, limit=20)):
        if getattr(m, "role", "") == "assistant":
            return m.content or ""
    return ""


def run_one_turn(user_id: str, sid: str, patient: PatientAgent,
                 doctor_reply: str = "", patient_text: str | None = None,
                 auto_review: str = "approve") -> dict[str, Any]:
    """跑一轮：病人出话 -> 陪伴 Agent 回复 ->（必要时）自动放行挂起。

    `patient_text` 给了就直接用，不再生成（用于复现特定句子）。
    `auto_review == "none"` 时不放行，把挂起状态原样返回给调用方。

    语音：`pt.voice` 由病人 Agent 产出，结构与线上 `/api/asr` 的返回一致
    （`VoiceMeta`），这里**原样传给同一个 `graph.run_turn()`** —— 诊断侧
    逻辑与真实用户在 UI 上说话时完全同一份代码（方案 C1）。
    """
    import graph

    pt: PatientTurn = (patient.say(patient_text, doctor_reply)
                       if patient_text else patient.next(doctor_reply))

    # tts 档送进诊断侧的是**转写文本**（真实链路如此）；转写为空时，
    # 非 strict 用原话兜底，strict 照真实行为送空文本
    vi = pt.voice_info or {}
    sent = pt.text
    if vi.get("voice_source") == "tts":
        asr_text = vi.get("asr_text", "")
        if asr_text or vi.get("strict"):
            sent = asr_text

    t0 = time.monotonic()
    res = graph.run_turn(user_id, sid, sent, voice=pt.voice)
    dt_ms = int((time.monotonic() - t0) * 1000)

    record: dict[str, Any] = {
        "turn": pt.turn,
        "patient": pt.text,
        "sent_text": sent,
        "patient_meta": {"ceiling": pt.ceiling, "situation": pt.situation,
                         "beat": pt.beat, "source": pt.source,
                         "redacted": pt.redacted, "note": pt.note},
        "voice": {**vi, "tone": (pt.voice or {}).get("tone", ""),
                  "audio_features": (pt.voice or {}).get("audio_features", {})},
        "doctor": res.get("reply", ""),
        "risk_level": res.get("risk_level", "none"),
        "channels": res.get("channels", {}),
        "emotion": (res.get("emotion") or {}).get("primary_emotion", ""),
        "cbt_stage": res.get("cbt_stage", ""),
        "crisis_mode": bool(res.get("crisis_mode")),
        "interrupted": bool(res.get("interrupted")),
        "interrupt_reason": res.get("interrupt_reason", ""),
        "latency_ms": dt_ms,
        "pending_review": res.get("pending_review"),
    }

    if record["interrupted"] and auto_review != "none":
        resumed = graph.run_turn("", sid, "", resume_payload={"action": auto_review})
        record["resumed_with"] = auto_review
        record["resumed_reply"] = resumed.get("reply", "")
    return record


def run_session(user_id: str, style: str, severity: int, turns: int,
                seed: int | None = None, auto_review: str = "approve",
                stop_on_crisis: bool = False,
                session_id: str | None = None,
                patient: PatientAgent | None = None,
                on_turn=None, voice: VoiceConfig | None = None) -> dict[str, Any]:
    """连续跑多轮，返回完整对练记录。`on_turn(record)` 用于 CLI 边跑边打。

    `patient` 可由调用方预先建好（CLI 需要先打印它的元信息再开跑），
    同一个 seed 建出来的 Agent 行为完全一致，不会造成两次结果不一致。
    """
    if patient is None:
        if session_id:
            import storage
            if not storage.get_session(session_id):
                raise KeyError(session_id)
            sid = session_id
            patient = patient_of(sid, style, severity, seed, turns, voice=voice)
        else:
            sid, patient = new_sim_session(user_id, style, severity, seed, turns,
                                           voice=voice)
    else:
        sid = session_id or new_sim_session(user_id, style, severity, seed, turns,
                                            voice=voice)[0]
    _PATIENTS[sid] = patient

    transcript: list[dict[str, Any]] = []
    doctor_reply = ""
    interrupted_at: int | None = None
    crisis_at: int | None = None

    for _ in range(turns):
        rec = run_one_turn(user_id, sid, patient, doctor_reply,
                           auto_review=auto_review)
        transcript.append(rec)
        if on_turn:
            on_turn(rec)
        doctor_reply = rec.get("doctor", "")

        if rec["crisis_mode"] and crisis_at is None:
            crisis_at = rec["turn"]
        if rec["interrupted"]:
            # 只记**第一次**挂起：危机之后每轮都会再挂一次，
            # 覆盖写会让汇总显示成最后一轮，看不出是从哪轮开始断的
            if interrupted_at is None:
                interrupted_at = rec["turn"]
            if auto_review == "none" or stop_on_crisis:
                break

    return {
        "session_id": sid,
        "user_id": user_id,
        "profile": patient.describe(),
        "auto_review": auto_review,
        "crisis_at_turn": crisis_at,
        "interrupted_at_turn": interrupted_at,
        "risk_track": [r["risk_level"] for r in transcript],
        "transcript": transcript,
    }


# ------------------------------------------------------------------ CLI
def run(args: argparse.Namespace) -> dict[str, Any]:
    from llm import using_mock

    if args.style not in patient_agent.STYLES:
        sys.exit(f"未知风格 {args.style!r}；用 --list 查看可选值")
    if args.severity not in patient_agent.SEVERITIES:
        sys.exit("严重程度须为 1~5")

    info_holder: dict[str, Any] = {}

    def on_turn(rec: dict) -> None:
        if args.quiet:
            return
        style_name = info_holder.get("style_name", "")
        print(f"\n[{rec['turn']}/{args.turns}] 病人（{style_name}·"
              f"披露上限 {rec['patient_meta']['ceiling']}）")
        print(f"  > {rec['patient']}")
        print(f"  诊断 风险={rec['risk_level']:<8} 通道 rule/model/mm/scale="
              f"{_fmt_channels(rec['channels']):<10} "
              f"情绪={rec['emotion'] or '-'}  "
              f"CBT={rec['cbt_stage'] or '-'}  {rec['latency_ms']}ms")
        if (rec.get("voice") or {}).get("tone"):
            print(f"  声音 {_fmt_voice(rec['voice'])} → mm="
                  f"{_mm_level(rec['channels'])}")
        print(f"  < {_short(rec['doctor'], 100)}")
        if rec["patient_meta"]["note"]:
            print(f"  · {rec['patient_meta']['note']}")
        if rec["crisis_mode"]:
            print(f"  ⚠ 第 {rec['turn']} 轮进入危机分支")
        if rec["interrupted"]:
            print(f"  🛑 第 {rec['turn']} 轮被 human_review 挂起"
                  f"（{rec['interrupt_reason']}）")
            if rec.get("resumed_with"):
                print(f"     已按 {rec['resumed_with']} 放行"
                      f"（{len(rec.get('resumed_reply', ''))} 字）")
            else:
                print("     --auto-review=none：停在挂起点")

    # 先建会话与病人 Agent，把 profile 打出来再开跑
    sid, patient = new_sim_session(args.user_id, args.style, args.severity,
                                   args.seed, args.turns)
    info = patient.describe()
    info_holder.update(info)
    print("=" * 74)
    print(f"会话 {sid}   用户 {args.user_id}")
    print(f"病人：{info['style_name']} / {info['severity_name']}"
          f"（目标风险 {info['target_risk']}）")
    print(f"      seed={info['seed']}  身份={info['persona']}  轮数={args.turns}")
    print(f"LLM ：{'MockLLM（离线台词）' if using_mock() else '真实模型'}"
          f"   中断后自动处理={args.auto_review}")
    print(f"语音：{args.voice}"
          + (f"（一致性={args.voice_consistency} 后端={args.tts_backend}"
             + (f" 落盘={args.keep_audio_dir}" if args.keep_audio_dir else "")
             + (" strict" if args.strict_asr else "")
             if args.voice != "off" else ""))
    print("=" * 74)

    voice = VoiceConfig(mode=args.voice, consistency=args.voice_consistency,
                        backend=args.tts_backend, audio_dir=args.keep_audio_dir,
                        strict=args.strict_asr)
    patient.voice = voice
    out = run_session(args.user_id, args.style, args.severity, args.turns,
                      seed=args.seed, auto_review=args.auto_review,
                      stop_on_crisis=args.stop_on_crisis,
                      session_id=sid, patient=patient, on_turn=on_turn,
                      voice=voice)
    out["profile"] = info

    print("\n" + "=" * 74)
    print("轮次  风险        通道(r/m/mm/s)  情绪      CBT阶段   声音")
    for r in out["transcript"]:
        tone = (r.get("voice") or {}).get("tone") or "-"
        print(f"  {r['turn']:<3}  {r['risk_level']:<10}  "
              f"{_fmt_channels(r['channels']):<14}  "
              f"{r['emotion'] or '-':<7}  {r['cbt_stage'] or '-':<9}  {tone}")
    print(f"\n风险轨迹：{' -> '.join(out['risk_track']) or '（无）'}")
    print(f"危机分支：{'第 %d 轮' % out['crisis_at_turn'] if out['crisis_at_turn'] else '未触发'}")
    ia = out["interrupted_at_turn"]
    tail = (f"（已{args.auto_review}放行）"
            if ia and args.auto_review != "none" else "（停在挂起点）")
    print(f"人工审核中断：{'第 %d 轮' % ia if ia else '未触发'}{tail if ia else ''}")
    print(f"会话 {sid}（数据已写入库，用户 {args.user_id}）")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"已写出 {args.json}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="病人 Agent 与陪伴 Agent 自动对练（测试风险升级与人工审核中断）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python3 simulate.py --style guarded --severity 5 --turns 8 --seed 7",
    )
    ap.add_argument("--list", action="store_true", help="列出可选风格与严重程度后退出")
    ap.add_argument("--style", default=patient_agent.DEFAULT_STYLE,
                    help="表达风格，如 guarded/avoidant/verbose/ruminative/somatic/hostile")
    ap.add_argument("--severity", type=int, default=3, help="严重程度 1~5（1 轻微 … 5 危急）")
    ap.add_argument("--turns", type=int, default=6, help="对练轮数")
    ap.add_argument("--seed", type=int, default=None,
                    help="随机种子；相同 seed 可复现同一段对练")
    ap.add_argument("--user-id", default="u_sim",
                    help="模拟数据挂在哪个用户下（默认 u_sim，便于清理）")
    ap.add_argument("--auto-review", default="approve",
                    choices=["approve", "override", "reject", "none"],
                    help="被 human_review 挂起时如何自动处理；none=停在挂起点")
    ap.add_argument("--stop-on-crisis", action="store_true",
                    help="进入危机分支并放行后立即结束")
    ap.add_argument("--json", metavar="PATH", help="把完整对练记录写成 JSON")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总，不打印逐轮对话")
    # ---- 语音联路 ----
    ap.add_argument("--voice", default="off", choices=["off", "synth", "tts"],
                    help="语音联路：off=纯文本（默认）｜synth=合成声学特征｜"
                         "tts=先合成音频再走 asr.transcribe()")
    ap.add_argument("--voice-consistency", default="auto",
                    choices=["auto", "mask", "consistent"],
                    help="auto=按风格修正｜mask=声音直挂内心状态（造掩饰样本）｜"
                         "consistent=声音与嘴上说的一致（对照组）")
    ap.add_argument("--tts-backend", default="auto", choices=["auto", "macos", "edge"],
                    help="TTS 后端；auto 优先离线后端（macos say）")
    ap.add_argument("--keep-audio-dir", metavar="PATH", default="",
                    help="tts 档把每轮音频落盘，供人工试听/做回归样本")
    ap.add_argument("--strict-asr", action="store_true",
                    help="ASR 转写为空时照真实行为送空文本（默认用原话兜底）")
    args = ap.parse_args()

    if args.list:
        print_profiles()
        return
    if args.voice != "off":
        import tts

        backend, why = tts.available(args.tts_backend) if args.voice == "tts" else ("synth", "")
        if args.voice == "tts" and not backend:
            print(f"⚠ 无可用 TTS 后端（{why}），将回落到 synth（合成特征）")
    run(args)


if __name__ == "__main__":
    main()
