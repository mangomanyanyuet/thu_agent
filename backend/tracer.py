"""结构化事件日志（Agent 可观测层）。

对应 ETCLOVG 的 **O（Observability）** 层 —— 此前是本系统最薄弱的一环：
只有 `audit_trace` 表记了上下文快照，中间决策过程全靠猜。

设计取舍
--------
1. **JSONL 落盘**（一行一个事件）。相比 SQLite：
   - `tail -f` 能直接看，调试时不用写查询
   - 追加写无锁争用，不进关键路径
   - 便于离线回归分析（pandas / jq 直接读）
2. **每条带 `msg` 字段**：人类可读的一句话摘要。这样 `tail -f` 出来就是
   有意义的日志，而不是一堆 JSON。
3. **同时保留内存环形缓冲**：供 `/api/trace/recent` 实时查看，
   不落盘也能在界面上看最近 N 条（默认 500 条，超出丢弃最旧的）。
4. **日志脱敏**：用户原话默认截断（`trace_text_max_len`），避免
   心理自述全文落盘。风险证据（原话片段）因安全审计需要保留原样。

⚠ 心理数据属敏感个人信息，日志文件同样需要按敏感数据管理：
   不进版本库（已在 .gitignore 的 data/ 下）、定期清理、限制访问。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from config import get_settings

_lock = threading.Lock()
_buffer: deque[dict] = deque(maxlen=500)
_dir_ready = False

# 日志目录：放在 data/ 下（已被 .gitignore 排除）
LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "traces"


def _ensure_dir() -> None:
    global _dir_ready
    if _dir_ready:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _dir_ready = True


def _log_path() -> Path:
    return LOG_DIR / f"agent-{datetime.now().strftime('%Y%m%d')}.jsonl"


def _trim(v: Any, n: int) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"…({len(v)}字)"
    return v


# ------------------------------------------------------------------ 核心 API
def log(kind: str, msg: str, **fields: Any) -> dict:
    """记录一个事件。

    kind  事件类型，如 `emotion.fused` / `risk.final` / `graph.transition`
    msg   人类可读摘要（写入日志的 msg 字段，便于 tail -f）
    **fields  结构化字段
    """
    s = get_settings()
    if not s.trace_enabled:
        return {}

    ev = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "kind": kind,
        "msg": msg,
    }
    ev.update(fields)

    # 用户原话脱敏（证据字段不脱敏，安全审计需要原话）
    n = s.trace_text_max_len
    for k in ("text", "narrative", "reply"):
        if k in ev:
            ev[k] = _trim(ev[k], n)

    with _lock:
        _buffer.append(ev)

    if s.trace_to_file:
        try:
            _ensure_dir()
            line = json.dumps(ev, ensure_ascii=False, default=str)
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:  # noqa: BLE001
            # 日志失败绝不能影响主流程
            print(f"[tracer] 写日志失败: {exc}", flush=True)

    if s.trace_echo:
        print(f"[trace] {ev['ts'][11:]} {kind}: {msg}", flush=True)

    return ev


# ------------------------------------------------------------------ 便捷封装
def turn_start(session_id: str, user_id: str, turn: int, text: str) -> None:
    log("turn.start", f"── 第 {turn} 轮 ──",
        session_id=session_id, user_id=user_id, turn=turn, text=text)


def emotion(session_id: str, turn: int, label: str, valence: float,
            arousal: float, confidence: float, intensity: Any = None,
            themes: Any = None, distortions: Any = None, **extra: Any) -> None:
    log("emotion.fused",
        f"情绪融合 → {label}（愉悦度 {valence:+.2f}，唤醒度 {arousal:+.2f}，置信度 {confidence:.2f}）",
        session_id=session_id, turn=turn, label=label, valence=valence,
        arousal=arousal, confidence=confidence, intensity=intensity,
        themes=themes, cognitive_distortions=distortions, **extra)


def emotion_channels(session_id: str, turn: int, channels: dict) -> None:
    """各模态通道的原始判断（融合前的输入）。"""
    for name, c in (channels or {}).items():
        if not c:
            continue
        label = c.get("label") or ""
        conf = c.get("confidence")
        if label:
            log("emotion.channel", f"  · {name} 通道 = {label}（置信度 {conf}）",
                session_id=session_id, turn=turn, channel=name,
                label=label, confidence=conf)
        elif "valence" in c:
            log("emotion.channel", f"  · {name} 通道 = (v={c.get('valence')}, a={c.get('arousal')})",
                session_id=session_id, turn=turn, channel=name,
                valence=c.get("valence"), arousal=c.get("arousal"))


_RISK_NAMES = ["none", "low", "moderate", "high", "imminent"]


def risk_channel(session_id: str, turn: int, name: str, level: Any,
                 evidence: str = "", **extra: Any) -> None:
    lvl = _RISK_NAMES[level] if isinstance(level, int) and 0 <= level < 5 else str(level)
    log("risk.channel",
        f"  · {name} 通道 = {lvl}" + (f"（{evidence[:40]}）" if evidence else ""),
        session_id=session_id, turn=turn, channel=name, level=lvl,
        level_num=int(level) if isinstance(level, int) else None,
        evidence=evidence, **extra)


def risk_final(session_id: str, turn: int, level: Any, channels: dict,
               prev_level: Any = None, **extra: Any) -> None:
    lvl = _RISK_NAMES[level] if isinstance(level, int) and 0 <= level < 5 else str(level)
    note = "（取各通道最大值，只升不降）"
    if isinstance(prev_level, int) and isinstance(level, int) and level > prev_level:
        note = f"⚠ 升级：{_RISK_NAMES[prev_level]} → {lvl}"
    log("risk.final", f"风险等级 = {lvl} {note}",
        session_id=session_id, turn=turn, level=lvl,
        level_num=int(level) if isinstance(level, int) else None,
        prev_level=_RISK_NAMES[prev_level] if isinstance(prev_level, int) else prev_level,
        channels={k: (v.get("level") if isinstance(v, dict) else v)
                  for k, v in (channels or {}).items()},
        **extra)


def transition(session_id: str, turn: int, src: str, dst: str,
               condition: str = "", reason: str = "") -> None:
    """状态转移：图节点跳转 / 相位推进 / CBT 阶段推进。"""
    log("graph.transition",
        f"状态转移 {src} → {dst}" + (f"（{condition}）" if condition else ""),
        session_id=session_id, turn=turn, **{"from": src}, to=dst,
        condition=condition, reason=reason)


def phase(session_id: str, src: str, dst: str, reason: str = "") -> None:
    """陪伴会话相位推进（consent/quality/opening/main/closing/done/crisis）。"""
    log("phase.transition",
        f"相位 {src} → {dst}" + (f"（{reason}）" if reason else ""),
        session_id=session_id, **{"from": src}, to=dst, reason=reason)


def cbt_stage(session_id: str, src: str, dst: str) -> None:
    if src == dst:
        return
    log("cbt.stage", f"CBT 阶段：{src} → {dst}",
        session_id=session_id, **{"from": src}, to=dst)


def context(session_id: str, turn: int, stats: dict, risk_level: str = "") -> None:
    log("context.assembled",
        f"上下文装配完成：{stats.get('chars', 0)} 字符 / 约 {stats.get('approx_tokens', 0)} token"
        f"（预算 {stats.get('usage_pct', 0)}%）",
        session_id=session_id, turn=turn, risk_level=risk_level, **stats)


def memory_write(session_id: str, user_id: str, facts: dict, baseline: dict = None) -> None:
    n = sum(len(v) for v in (facts or {}).values() if isinstance(v, list))
    log("memory.write", f"记忆写入：{n} 条事实" +
        (f"，情绪基线 v={baseline.get('valence')} a={baseline.get('arousal')}" if baseline else ""),
        session_id=session_id, user_id=user_id, facts=facts, baseline=baseline)


def turn_end(session_id: str, turn: int, latency_ms: int, reply_len: int,
             crisis: bool = False, degraded: bool = False) -> None:
    flags = []
    if crisis:
        flags.append("危机分支")
    if degraded:
        flags.append("⚠ 模型降级")
    log("turn.end",
        f"回复已生成（{latency_ms}ms，{reply_len} 字）" + (f" [{'/'.join(flags)}]" if flags else ""),
        session_id=session_id, turn=turn, latency_ms=latency_ms,
        reply_len=reply_len, crisis=crisis, degraded=degraded)


# ------------------------------------------------------------------ 查询
def recent(limit: int = 100, kind: str = "", session_id: str = "") -> list[dict]:
    with _lock:
        items = list(_buffer)
    if kind:
        items = [e for e in items if e.get("kind", "").startswith(kind)]
    if session_id:
        items = [e for e in items if e.get("session_id") == session_id]
    return items[-limit:]


def log_file_today() -> str:
    return str(_log_path())
