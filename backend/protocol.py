"""会谈方案（protocol）层：把「先聊什么、后聊什么、每阶段怎么聊」变成可编辑文件。

设计约束
--------
- 方案只作用于**低/无风险常规会谈分支**（`cbt`）。风险判定、危机协议、中度安全计划、
  人工挂起、热线与红线提示词全部不经过本模块，配置文件里也没有对应开关。
- 方案文件是仓库内 `backend/protocols/*.json`，纯标准库解析，不新增依赖，
  不写数据库、不做版本表；生效方案由同目录 `active.json` 指定，改文件后下一轮即生效。
- 阶段转移是**确定性**的：`next` + 条件 `goto` / `stay_if` + `max_turns` 上限。
  LLM 只生成回复文本，不决定下一阶段。
- 任何文件缺失、JSON 损坏或校验失败，都回落到内置 `legacy_protocol()`
  （与项目原有的 9 个 CBT 阶段逐项一致），绝不因编排错误让会话崩溃。

方案结构见 `docs/会谈方案编排说明.md` 与
`.codebuddy/skills/counseling-workflow-orchestration/references/protocol-schema.md`。
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

PROTOCOL_DIR = Path(
    os.getenv("PROTOCOL_DIR", str(Path(__file__).resolve().parent / "protocols"))
)
ACTIVE_FILE = PROTOCOL_DIR / "active.json"
DEFAULT_ID = "cbt_classic"

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_BLOCK_CHARS = 3000
MAX_ITEM_CHARS = 300

# 条件只能读取这些已在 AgentState 中存在的信号，避免方案偷偷引入新状态或新 LLM 调用
ALLOWED_FIELDS = {
    "emotion.intensity", "emotion.valence", "emotion.arousal",
    "risk_level", "turn", "stage_turns", "modality_inconsistent",
}
ALLOWED_OPS = {">=", ">", "<=", "<", "==", "!=", "in", "not_in"}

# 与 risk_rules.validate_config 的追加文本黑名单保持一致：只允许追加，不允许覆盖基座
FORBIDDEN_TEXT = (
    "忽略以上", "忽略上述", "忽略前面", "不要遵守", "无视以上",
    "禁止判定", "不得判为", "输出格式改为", "取消人工", "跳过危机",
)

# 内置回落：与 graph_state.CBT_STAGE_GUIDE / CBT_NEXT 完全一致
_LEGACY_ROWS: list[tuple[str, str, str, str | None]] = [
    ("none", "自由陪伴", "自由陪伴，捕捉情绪", "affect_labeling"),
    ("affect_labeling", "情绪命名", "帮助用户命名情绪", "situation"),
    ("situation", "情境澄清", "澄清具体情境：何时、何地、和谁", "automatic_thought"),
    ("automatic_thought", "自动化思维", "引出自动化思维：那一刻脑子里冒出什么", "distortion"),
    ("distortion", "认知歪曲", "识别认知歪曲（灾难化/全或无/读心/过度概括等）", "socratic"),
    ("socratic", "苏格拉底提问", "苏格拉底式提问：证据支持/反对/其他解释", "alternative"),
    ("alternative", "替代性想法", "形成替代性、更平衡的想法", "behavior"),
    ("behavior", "行为激活", "行为激活：约定今天可做的一件小事", "wrapup"),
    ("wrapup", "小结", "小结 + 收集反馈", "affect_labeling"),
]


def legacy_protocol() -> dict:
    """内置回落方案（不可被外部文件删除）。"""
    return {
        "id": "cbt_classic",
        "name": "经典 CBT 会谈（内置回落）",
        "school": "认知行为疗法（CBT）",
        "version": "1.0.0",
        "description": "与代码原常量一致的 9 阶段线性轮转，用于方案文件不可用时的安全回落。",
        "entry": "none",
        "applies_to": ["cbt"],
        "global_style": ["先共情确认，再推进；一次只问一个问题。"],
        "stages": {
            sid: {
                "label": label, "goal": goal, "topics": [], "prompts": [],
                "avoid": [], "next": nxt, "max_turns": 3,
            }
            for sid, label, goal, nxt in _LEGACY_ROWS
        },
    }


# ------------------------------------------------------------------ 校验
def _text_of(cfg: dict) -> str:
    parts = [str(cfg.get(k, "")) for k in ("id", "name", "school", "description")]
    stages = cfg.get("stages")
    if not isinstance(stages, dict):
        return "\n".join(parts)
    for stage in stages.values():
        if not isinstance(stage, dict):
            continue
        parts.append(str(stage.get("goal", "")))
        for key in ("topics", "prompts", "avoid"):
            items = stage.get(key) or []
            if isinstance(items, list):
                parts.extend(str(x) for x in items if isinstance(x, str))
        rules = stage.get("rules")
        if isinstance(rules, dict):
            for rule in (rules.get("goto") or []):
                if isinstance(rule, dict):
                    parts.append(str(rule.get("reason", "")))
    style = cfg.get("global_style") or []
    if isinstance(style, list):
        parts.extend(str(x) for x in style if isinstance(x, str))
    return "\n".join(parts)


def _check_condition(cond: Any, where: str, errors: list[str]) -> None:
    if not isinstance(cond, dict):
        errors.append(f"{where} 条件必须是对象")
        return
    field, op = cond.get("field"), cond.get("op")
    if field not in ALLOWED_FIELDS:
        errors.append(f"{where} 使用了不允许的信号 {field!r}（可用：{sorted(ALLOWED_FIELDS)}）")
    if op not in ALLOWED_OPS:
        errors.append(f"{where} 使用了不允许的运算符 {op!r}（可用：{sorted(ALLOWED_OPS)}）")
    if "value" not in cond:
        errors.append(f"{where} 缺少 value")
    elif op in ("in", "not_in") and not isinstance(cond["value"], list):
        errors.append(f"{where} 的 in/not_in 需要数组 value")


def _check_when(when: Any, where: str, errors: list[str]) -> None:
    """单条件对象，或条件数组（数组内同时满足）。"""
    if isinstance(when, list):
        for i, item in enumerate(when):
            _check_condition(item, f"{where}[{i}]", errors)
    else:
        _check_condition(when, where, errors)


def validate_protocol(cfg: Any, expected_id: str | None = None) -> tuple[bool, list[str], list[str]]:
    """返回 (是否通过, 错误列表, 警告列表)。错误会让方案不生效并回落。"""
    errors: list[str] = []
    warns: list[str] = []
    if not isinstance(cfg, dict):
        return False, ["方案根节点必须是 JSON 对象"], warns

    pid = cfg.get("id")
    if not isinstance(pid, str) or not ID_RE.match(pid):
        errors.append("id 必须是小写字母开头的短标识（字母/数字/下划线，≤32 字）")
    elif expected_id and pid != expected_id:
        errors.append(f"id={pid} 与文件名 {expected_id}.json 不一致")
    for key in ("name", "school", "entry"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            errors.append(f"缺少必填字段 {key}（非空字符串）")

    if cfg.get("applies_to") != ["cbt"]:
        errors.append("红线：applies_to 只能是 ['cbt']；方案不得作用于 crisis / moderate / 风险判定")

    stages = cfg.get("stages")
    if not isinstance(stages, dict) or not stages:
        return False, errors + ["stages 必须是非空对象"], warns
    if len(stages) > 16:
        errors.append("阶段数不得超过 16（提示词预算与安全可控性）")
    for sid in stages:
        if not isinstance(sid, str) or not ID_RE.match(sid):
            errors.append(f"阶段 id {sid!r} 非法（小写字母开头，字母/数字/下划线）")

    entry = cfg.get("entry")
    if isinstance(entry, str) and entry not in stages:
        errors.append(f"entry={entry!r} 不在 stages 中")

    edges: dict[str, list[str]] = {}
    total_chars = 0
    for sid, stage in stages.items():
        where = f"stages.{sid}"
        if not isinstance(stage, dict):
            errors.append(f"{where} 必须是对象")
            continue
        for key in ("label", "goal"):
            if not isinstance(stage.get(key), str) or not stage[key].strip():
                errors.append(f"{where}.{key} 必须是非空文本")
        for key in ("topics", "prompts", "avoid"):
            items = stage.get(key) or []
            if not isinstance(items, list) or any(not isinstance(x, str) for x in items):
                errors.append(f"{where}.{key} 必须是文本数组")
                continue
            for item in items:
                if len(item) > MAX_ITEM_CHARS:
                    errors.append(f"{where}.{key} 单条超过 {MAX_ITEM_CHARS} 字")
            total_chars += sum(len(x) for x in items)
        total_chars += len(str(stage.get("goal", ""))) + len(str(stage.get("label", "")))
        if not (stage.get("topics") or stage.get("prompts")):
            warns.append(f"{where} 没有 topics / prompts，本阶段只会注入会谈目标")

        mt = stage.get("max_turns", 3)
        if not isinstance(mt, int) or isinstance(mt, bool) or not (1 <= mt <= 10):
            errors.append(f"{where}.max_turns 必须是 1–10 的整数（防止停留条件让会话卡死）")

        nxt = stage.get("next")
        if nxt is not None and (not isinstance(nxt, str) or nxt not in stages):
            errors.append(f"{where}.next={nxt!r} 必须是已定义的阶段，或 null（回到入口）")
        edges[sid] = [nxt] if isinstance(nxt, str) else [entry] if nxt is None else []

        rules = stage.get("rules") or {}
        if not isinstance(rules, dict):
            errors.append(f"{where}.rules 必须是对象")
            continue
        for i, cond in enumerate(rules.get("stay_if") or []):
            _check_when(cond, f"{where}.rules.stay_if[{i}]", errors)
        for i, goto in enumerate(rules.get("goto") or []):
            gwhere = f"{where}.rules.goto[{i}]"
            if not isinstance(goto, dict):
                errors.append(f"{gwhere} 必须是对象")
                continue
            _check_when(goto.get("when"), f"{gwhere}.when", errors)
            to = goto.get("to")
            if not isinstance(to, str) or to not in stages:
                errors.append(f"{gwhere}.to={to!r} 必须是已定义的阶段")
            else:
                edges[sid].append(to)
        if (rules.get("stay_if") or []) and not stage.get("max_turns"):
            warns.append(f"{where} 有停留条件但没写 max_turns，将使用默认 3 轮")

    style = cfg.get("global_style") or []
    if not isinstance(style, list):
        errors.append("global_style 必须是文本数组")
    else:
        for item in style:
            if not isinstance(item, str):
                errors.append("global_style 必须是文本数组")
            elif len(item) > MAX_ITEM_CHARS:
                errors.append(f"global_style 单条超过 {MAX_ITEM_CHARS} 字")
        total_chars += sum(len(x) for x in style if isinstance(x, str))
    if total_chars > MAX_BLOCK_CHARS:
        errors.append(f"方案文本总量 {total_chars} 字，超过 {MAX_BLOCK_CHARS} 字上限")

    text = _text_of(cfg)
    for bad in FORBIDDEN_TEXT:
        if bad in text:
            errors.append(f"红线：方案文本含覆盖基座/绕过安全的措辞「{bad}」")
            break

    # 可达性：把顺序边与条件跳转边都算上，避免编排出永远进不去的阶段
    if isinstance(entry, str) and entry in stages:
        seen, stack = set(), [entry]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in edges:
                continue
            seen.add(cur)
            stack.extend(edges[cur])
        unreachable = sorted(set(stages) - seen)
        if unreachable:
            errors.append(f"存在从入口无法到达的阶段：{unreachable}（检查 next / goto）")

    return (len(errors) == 0), errors, warns


# ------------------------------------------------------------------ 读取与缓存
_CACHE: dict[str, Any] = {}


def _signature() -> tuple:
    try:
        return tuple(sorted(
            (p.name, p.stat().st_mtime_ns) for p in PROTOCOL_DIR.glob("*.json")
        ))
    except OSError:
        return ()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def invalidate() -> None:
    _CACHE.clear()


def active_status() -> dict:
    active_protocol()
    return copy.deepcopy(_CACHE.get("status", {}))


def active_protocol() -> dict:
    """返回生效方案（深拷贝）。文件变化自动重载，异常一律回落内置方案。"""
    sig = _signature()
    if _CACHE.get("sig") == sig and _CACHE.get("data"):
        return copy.deepcopy(_CACHE["data"])

    reason = ""
    pid = DEFAULT_ID
    try:
        if ACTIVE_FILE.exists():
            pid = str(_read_json(ACTIVE_FILE).get("active") or DEFAULT_ID)
        path = PROTOCOL_DIR / f"{pid}.json"
        cfg = _read_json(path)
        ok, errors, _ = validate_protocol(cfg, expected_id=pid)
        if not ok:
            raise ValueError("；".join(errors))
        data = cfg
    except Exception as exc:  # noqa: BLE001  编排文件出错绝不能让会话崩溃
        data, reason = legacy_protocol(), str(exc)

    _CACHE.update({
        "sig": sig,
        "data": data,
        "status": {
            "id": data["id"], "name": data["name"], "school": data["school"],
            "version": data.get("version", ""),
            "fallback": bool(reason), "reason": reason,
            "active_file": str(ACTIVE_FILE),
        },
    })
    return copy.deepcopy(data)


def list_protocols() -> list[dict]:
    """列出所有可选方案（跳过模板与 active.json）及其校验结果。"""
    out = []
    for path in sorted(PROTOCOL_DIR.glob("*.json")):
        if path.name.startswith("_") or path.name == ACTIVE_FILE.name:
            continue
        item: dict[str, Any] = {"id": path.stem, "file": path.name}
        try:
            cfg = _read_json(path)
            ok, errors, warns = validate_protocol(cfg, expected_id=path.stem)
            item.update({
                "name": cfg.get("name", ""), "school": cfg.get("school", ""),
                "version": cfg.get("version", ""), "valid": ok,
                "errors": errors, "warnings": warns,
                "stage_count": len(cfg.get("stages") or {}),
            })
        except Exception as exc:  # noqa: BLE001  单个方案损坏不影响列表其它项
            item.update({"valid": False, "errors": [str(exc)], "warnings": []})
        out.append(item)
    return out


def set_active(pid: str) -> dict:
    """切换生效方案；只允许切换到已定义且校验通过的文件。"""
    if not isinstance(pid, str) or not ID_RE.match(pid) or pid.startswith("_"):
        raise ValueError("方案 id 非法")
    path = PROTOCOL_DIR / f"{pid}.json"
    cfg = _read_json(path)
    ok, errors, _ = validate_protocol(cfg, expected_id=pid)
    if not ok:
        raise ValueError("；".join(errors))
    payload = {
        "active": pid,
        "note": "切换生效方案：改这个文件，或调用 POST /api/protocol/switch。下一轮对话生效。",
    }
    tmp = ACTIVE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ACTIVE_FILE)
    invalidate()
    return {"active": pid, "name": cfg["name"], "school": cfg["school"]}


def stage_label(protocol: dict, stage: str) -> str:
    item = (protocol.get("stages") or {}).get(stage) or {}
    return str(item.get("label") or stage)


def stage_labels(protocol: dict) -> dict[str, str]:
    return {
        sid: str((item or {}).get("label") or sid)
        for sid, item in (protocol.get("stages") or {}).items()
    }


def summary(protocol: dict) -> dict:
    return {
        "id": protocol["id"], "name": protocol["name"], "school": protocol["school"],
        "version": protocol.get("version", ""), "entry": protocol["entry"],
        "applies_to": protocol.get("applies_to", ["cbt"]),
        "stage_labels": stage_labels(protocol),
        "stages": [
            {"id": sid, "label": (s or {}).get("label", sid),
             "goal": (s or {}).get("goal", ""), "next": (s or {}).get("next"),
             "max_turns": (s or {}).get("max_turns", 3),
             "has_rules": bool((s or {}).get("rules"))}
            for sid, s in (protocol.get("stages") or {}).items()
        ],
    }


# ------------------------------------------------------------------ 决策与提示词
def _signal(signals: dict, field: str) -> Any:
    cur: Any = signals
    for part in field.split("."):
        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
    return cur


def _compare(actual: Any, op: str, expected: Any) -> bool:
    try:
        if op == "in":
            return actual in expected
        if op == "not_in":
            return actual not in expected
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if actual is None:
            return False
        if op == ">=":
            return float(actual) >= float(expected)
        if op == ">":
            return float(actual) > float(expected)
        if op == "<=":
            return float(actual) <= float(expected)
        if op == "<":
            return float(actual) < float(expected)
    except (TypeError, ValueError):
        return False
    return False


def _match(when: Any, signals: dict) -> bool:
    if isinstance(when, list):
        return all(_match(item, signals) for item in when) if when else False
    if not isinstance(when, dict):
        return False
    return _compare(_signal(signals, str(when.get("field", ""))),
                    str(when.get("op", "")), when.get("value"))


def decide_next(protocol: dict, stage: str, signals: dict,
                stage_turns: int = 0) -> tuple[str, str]:
    """确定性阶段决策，返回 (下一阶段, 原因)。优先级：轮次上限 > 条件跳转 > 停留 > 顺序。"""
    stages = protocol.get("stages") or {}
    st = stages.get(stage)
    if not isinstance(st, dict):
        return protocol["entry"], "历史会话中的旧阶段名，回落到方案入口"

    fallback = st.get("next") or protocol["entry"]
    max_turns = int(st.get("max_turns") or 3)
    if stage_turns + 1 >= max_turns:
        return fallback, f"本阶段已到 {max_turns} 轮上限，按方案推进"

    rules = st.get("rules") or {}
    for goto in rules.get("goto") or []:
        if isinstance(goto, dict) and _match(goto.get("when"), signals):
            return str(goto.get("to") or fallback), str(goto.get("reason") or "命中条件跳转")
    for cond in rules.get("stay_if") or []:
        if _match(cond, signals):
            return stage, "命中停留条件，本轮不推进"

    return str(fallback), "按方案顺序推进"


def stage_block(protocol: dict, stage: str) -> str:
    """生成注入 cbt 分支的会谈主题/话术块。安全指令由代码固定，方案无法覆盖。"""
    st = (protocol.get("stages") or {}).get(stage) or {}
    lines = [
        "## 本次会谈方案（仅适用于常规会谈）",
        f"方案：{protocol['name']}（{protocol['school']}）｜当前阶段："
        f"{st.get('label', stage)}（{stage}）",
        "",
        f"本阶段会谈目标：{st.get('goal', '（未填写）')}",
    ]
    for key, title in (("topics", "可聊主题"), ("prompts", "话术要点"),
                       ("avoid", "本阶段避免")):
        items = [x for x in (st.get(key) or []) if isinstance(x, str) and x.strip()]
        if items:
            lines.append(f"{title}：")
            lines.extend(f"- {x}" for x in items)
    style = [x for x in (protocol.get("global_style") or []) if isinstance(x, str) and x.strip()]
    if style:
        lines.append("通用风格：")
        lines.extend(f"- {x}" for x in style)
    lines += [
        "",
        "请只围绕当前阶段推进，一次只问一个问题；若用户抗拒或情绪明显升高，先共情确认，再询问是否先停在这里。",
        "以上方案不改变风险判定与危机处理；出现高风险时按系统红线执行。",
    ]
    return "\n".join(lines)[:MAX_BLOCK_CHARS]
