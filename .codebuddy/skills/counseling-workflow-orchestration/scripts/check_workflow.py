#!/usr/bin/env python3
"""MindTrace 工作流编排一致性静态校验（纯标准库，不依赖项目环境）。

用法：
    python3 check_workflow.py                 # 从脚本位置推断仓库根
    python3 check_workflow.py --root /path   # 显式指定仓库根

检查：阶段枚举/转移表/中文标签是否一致、转移图是否可达、流程图是否同步、
临床安全护栏的关键代码是否仍在。退出码 0 表示无 FAIL。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

FAILS: list[str] = []
WARNS: list[str] = []
OKS: list[str] = []


def record(ok: bool, ok_msg: str, fail_msg: str) -> None:
    (OKS if ok else FAILS).append(ok_msg if ok else fail_msg)


def warn(msg: str) -> None:
    WARNS.append(msg)


def read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        FAILS.append(f"缺少文件 {rel}")
        return ""
    return path.read_text(encoding="utf-8")


def top_level_dict(src: str, name: str) -> dict | None:
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        FAILS.append(f"解析 {name} 所在文件失败：{exc}")
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        FAILS.append(f"{name} 不是字面量字典，无法静态校验；请人工核对")
                        return None
                    return value if isinstance(value, dict) else None
    FAILS.append(f"找不到顶层字典 {name}")
    return None


def enum_values(src: str, cls: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            out = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    if isinstance(stmt.value.value, str):
                        out.append(stmt.value.value)
            return out
    return []


def js_object_keys(src: str, name: str) -> set[str] | None:
    m = re.search(r"\b" + re.escape(name) + r"\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        return None
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1), re.M))


def compare_labels(name: str, expected: set[str], actual: set[str] | None,
                   allowed_missing: set[str] | None = None) -> None:
    allowed_missing = allowed_missing or set()
    if actual is None:
        FAILS.append(f"找不到 {name} 的标签对象")
        return
    missing = expected - actual - allowed_missing
    extra = actual - expected
    record(not missing and not extra,
           f"{name} 标签与状态集合一致（{len(actual)} 项）",
           f"{name} 标签不一致：缺少 {sorted(missing)}，多余 {sorted(extra)}")
    for item in sorted(allowed_missing & (expected - actual)):
        warn(f"{name} 缺少 {item}（已知为前端有意不展示，确认是否符合预期）")


def check_stages(root: Path) -> None:
    gs = read(root, "backend/graph_state.py")
    models = read(root, "backend/models.py")
    theme = read(root, "frontend/src/theme.js")
    mini = read(root, "miniprogram/utils/format.js")
    flow = read(root, "frontend/public/flow.html")

    guide = top_level_dict(gs, "CBT_STAGE_GUIDE") or {}
    nxt = top_level_dict(gs, "CBT_NEXT") or {}
    stages = set(enum_values(models, "CBTStage"))
    if not stages:
        FAILS.append("解析 CBTStage 枚举失败")
        return

    record(set(guide) == stages,
           f"CBT_STAGE_GUIDE 与 CBTStage 枚举一致（{len(stages)} 个阶段）",
           f"CBT_STAGE_GUIDE 与 CBTStage 不一致：GUIDE 缺 {sorted(stages - set(guide))}，多 {sorted(set(guide) - stages)}")
    record(set(nxt) == stages,
           "CBT_NEXT 覆盖全部阶段",
           f"CBT_NEXT 键与阶段集合不一致：缺 {sorted(stages - set(nxt))}，多 {sorted(set(nxt) - stages)}")

    bad_targets = {k: v for k, v in nxt.items() if v not in stages}
    record(not bad_targets, "CBT_NEXT 所有目标阶段均已定义",
           f"CBT_NEXT 指向未定义阶段：{bad_targets}")

    # 从 none 出发按转移表走，检查可达性与闭环
    reachable, cur = set(), "none"
    for _ in range(len(stages) + 1):
        if cur in reachable or cur not in nxt:
            break
        reachable.add(cur)
        cur = nxt[cur]
    record(reachable == stages,
           "所有 CBT 阶段均可从 none 到达（无孤立阶段）",
           f"存在不可达 CBT 阶段：{sorted(stages - reachable)}")
    if cur in reachable:
        OKS.append(f"CBT 转移表构成闭环（none 为入口，循环回到 {cur}）")
    elif cur not in nxt:
        warn(f"CBT 转移表在 {cur} 处终止（无出边）；若为刻意设计的终点，请确认回落逻辑")
    else:
        warn(f"CBT 转移表在 {len(stages)} 步内未收敛（当前 {cur}），请人工核对")

    compare_labels("STAGE_LABEL", stages, js_object_keys(theme, "STAGE_LABEL"))
    compare_labels("CBT_STAGE_LABEL", stages, js_object_keys(mini, "CBT_STAGE_LABEL"))

    m = re.search(r"id:'cbt'.*?code:`(.*?)`", flow, re.S)
    if m and nxt:
        edges = {(a, b) for a, b in re.findall(r"(\w+)\s*-->\s*(\w+)", m.group(1))}
        expected = {(str(a), str(b)) for a, b in nxt.items()}
        if edges == expected:
            OKS.append("flow.html 的 CBT 流程图与转移表一致")
        else:
            warn("flow.html 的 CBT 流程图与 CBT_NEXT 不一致，请同步第 ④ 张 Mermaid 图")
    else:
        warn("未能在 flow.html 中定位 CBT 流程图，跳过该图示校验")


def check_phases(root: Path) -> None:
    state = read(root, "backend/companion/state.py")
    view = read(root, "frontend/src/views/CompanionView.jsx")
    phases = set(enum_values(state, "Phase"))
    if not phases:
        FAILS.append("解析 Phase 枚举失败")
        return
    OKS.append(f"Phase 枚举解析成功（{len(phases)} 个相位）")
    compare_labels("PHASE_LABEL", phases, js_object_keys(view, "PHASE_LABEL"),
                   allowed_missing={"aborted"})
    for required in ("consent", "quality", "opening", "main", "closing", "crisis", "done"):
        if required not in phases:
            FAILS.append(f"Phase 缺少关键相位 {required}")
    if "def next_phase" not in state:
        FAILS.append("companion/state.py 中找不到 next_phase()，相位转移入口缺失")


def check_guardrails(root: Path) -> None:
    graph = read(root, "backend/graph.py")
    gs = read(root, "backend/graph_state.py")
    rules = read(root, "backend/risk_rules.py")
    engine = read(root, "backend/companion/engine.py")

    checks = [
        (gs, "def keep_max", "graph_state.py", "风险等级只升不降的 reducer"),
        (gs, "绝对红线", "graph_state.py", "system prompt 的绝对红线"),
        (graph, "def final_level", "graph.py", "四通道取 max 的最终分级"),
        (graph, 'g.add_edge("crisis", "human_review")', "graph.py", "危机必过人工审核"),
        (graph, "interrupt(", "graph.py", "人工挂起"),
        (graph, "add_conditional_edges", "graph.py", "风险条件边"),
        (graph, 'g.add_edge("risk_detector", "persist_flags")', "graph.py", "安全通道汇聚"),
        (graph, 'g.add_edge("context_builder", "persist_flags")', "graph.py", "上下文通道汇聚"),
        (graph, "assemble_system(", "graph.py", "汇聚后装配 system prompt"),
        (rules, "def route", "risk_rules.py", "确定性分流函数"),
        (rules, "def validate_config", "risk_rules.py", "配置护栏校验"),
        (rules, "不得高于 3", "risk_rules.py", "危机门槛不得高于 3 的红线"),
        (rules, "不得为空", "risk_rules.py", "高风险词桶不得为空"),
        (engine, "Phase.CRISIS", "companion/engine.py", "陪伴会话危机跳转"),
        (engine, "should_finish", "companion/engine.py", "陪伴会话终止判定"),
        (graph, "stage_block(", "graph.py", "cbt 分支注入会谈主题"),
        (graph, "decide_next(", "graph.py", "确定性阶段决策"),
        (gs, "本轮不推进会谈阶段", "graph_state.py", "中高风险分支不注入阶段目标"),
    ]
    missing = [f"{where} 缺少「{label}」" for src, needle, where, label in checks if needle not in src]
    record(not missing, "临床安全护栏关键代码均保留", "；".join(missing))

    # 护栏不得被「配置化」削弱：配置文件里不应出现放行开关
    for forbidden in ("allow_skip_human_review", "disable_risk_max", "allow_risk_downgrade"):
        if forbidden in rules:
            FAILS.append(f"risk_rules.py 出现禁止的放行开关 {forbidden}")


def check_protocols(root: Path) -> None:
    """校验会谈方案文件、生效指针、内置回落基线与阶段决策行为。"""
    import importlib.util
    import json

    path = root / "backend" / "protocol.py"
    if not path.exists():
        FAILS.append("缺少 backend/protocol.py（会谈方案层）")
        return
    spec = importlib.util.spec_from_file_location("_mt_protocol_check", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        FAILS.append(f"加载 protocol.py 失败：{exc}")
        return

    items = mod.list_protocols()
    bad = [f"{x['id']}: {x['errors']}" for x in items if not x.get("valid")]
    record(not bad and bool(items), f"{len(items)} 套会谈方案均通过校验",
           "；".join(bad) or "没有可用的会谈方案")

    status = mod.active_status()
    record(not status.get("fallback"), f"生效方案 {status.get('id')} 正常加载",
           f"生效方案回落内置基线：{status.get('reason')}")

    active_file = root / "backend" / "protocols" / "active.json"
    if not active_file.exists():
        FAILS.append("缺少 backend/protocols/active.json")
    else:
        try:
            aid = json.loads(active_file.read_text(encoding="utf-8")).get("active")
            ids = {x["id"] for x in items}
            record(aid in ids, f"active.json 指向有效方案 {aid}",
                   f"active.json 指向不存在或无效的方案 {aid!r}")
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"active.json 不可读：{exc}")

    legacy = mod.legacy_protocol()
    classic = root / "backend" / "protocols" / "cbt_classic.json"
    if not classic.exists():
        FAILS.append("缺少默认方案 cbt_classic.json")
    else:
        try:
            cfg = json.loads(classic.read_text(encoding="utf-8"))
            same_next = all(
                legacy["stages"][k]["next"] == (cfg["stages"].get(k) or {}).get("next")
                for k in legacy["stages"]
            )
            record(same_next, "内置回落基线与默认方案阶段顺序一致",
                   "cbt_classic.json 与 legacy_protocol() 的 next 不一致")
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"解析 cbt_classic.json 失败：{exc}")

    linear = mod.decide_next(legacy, "none", {}, 0)[0]
    unknown = mod.decide_next(legacy, "old_stage", {}, 0)[0]
    record(linear == "affect_labeling" and unknown == "none",
           "阶段决策：线性推进与旧阶段回落均正常",
           f"阶段决策异常：none→{linear}，未知→{unknown}")

    sfbt = root / "backend" / "protocols" / "sfbt.json"
    if sfbt.exists():
        try:
            cfg = json.loads(sfbt.read_text(encoding="utf-8"))
            stay = mod.decide_next(cfg, "exceptions", {"emotion": {"intensity": 9}}, 0)[0]
            cap = mod.decide_next(cfg, "exceptions", {"emotion": {"intensity": 9}}, 2)[0]
            record(stay == "exceptions" and cap == "scaling",
                   "阶段决策：条件停留与轮次上限均正常",
                   f"条件编排异常：停留→{stay}，上限→{cap}")
            goto = mod.decide_next(cfg, "next_step", {"emotion": {"valence": -0.7}}, 0)[0]
            record(goto == "resources", "阶段决策：条件回退正常",
                   f"条件回退异常：→{goto}")
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"校验条件编排失败：{exc}")
    else:
        warn("缺少 sfbt.json，跳过条件编排行为校验")

    psrc = read(root, "backend/protocol.py")
    for needle, label in (("applies_to", "方案作用域校验"),
                          ("FORBIDDEN_TEXT", "覆盖基座指令黑名单"),
                          ("ALLOWED_FIELDS", "条件信号白名单"),
                          ("max_turns", "停留轮次上限")):
        if needle not in psrc:
            FAILS.append(f"protocol.py 缺少「{label}」")


def main() -> int:
    ap = argparse.ArgumentParser(description="工作流编排一致性校验")
    ap.add_argument("--root", default=None, help="仓库根目录")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[4]
    if not (root / "backend" / "graph.py").exists():
        print(f"[FAIL] 无法定位仓库根：{root}（用 --root 指定）")
        return 1

    check_stages(root)
    check_phases(root)
    check_guardrails(root)
    check_protocols(root)

    for msg in OKS:
        print(f"[OK]   {msg}")
    for msg in WARNS:
        print(f"[WARN] {msg}")
    for msg in FAILS:
        print(f"[FAIL] {msg}")
    print(f"\n合计：{len(OKS)} 通过 / {len(WARNS)} 提醒 / {len(FAILS)} 失败")
    print("提示：修改会谈方案后请同时核对 docs/会谈方案编排说明.md 与 backend/protocols/active.json。")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
