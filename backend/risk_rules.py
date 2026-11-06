"""风险规则配置化（P0）。

职责边界
--------
P0 只做「配置上传 / 校验 / 草稿与版本管理 / 规则试跑预览」，**不改动核心对话链路**：
`perception.py` / `graph.py` 当下仍读 `config.py` 的默认值。P1 再把引擎取值切到
本模块的 `get_active()`。这样医生可以先在页面上传、试跑、体验，而不影响线上对话。

存储
----
- 配置快照落盘到 ``data/risk_rules/*.json``（``data/`` 已 gitignore，永不入库）。
- 版本元数据 / 审计走 SQLite（复用 storage.tx()），表在本模块内自建，
  不侵入 storage.py 的 DDL。
- 区分「生效版本」（status=published）与「草稿」（status=draft）：
  试跑读草稿，真实对话将来读生效版本。

安全护栏（服务端强制，配置不得绕过）
------------------------------------
- patterns.imminent / patterns.high 不得删空（否则直接漏报）。
- gates.crisis_min_level 不得 > 3（不允许把 high 降级放行）。
- gates.moderate_level < crisis_min_level，且在 1–2 之间。
- 关键阈值限制在合理区间。
"""
from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

from config import (
    DATA_DIR,
    RISK_PATTERNS_HIGH,
    RISK_PATTERNS_IMMINENT,
    RISK_PATTERNS_IMPLICIT,
    RISK_PATTERNS_LOW,
    RISK_PATTERNS_MODERATE,
)
from models import now_iso
from storage import tx

RULE_DIR = DATA_DIR / "risk_rules"
RULE_DIR.mkdir(parents=True, exist_ok=True)

# 词表桶的固定顺序（匹配优先级即按此顺序，与 perception.rule_risk_level 一致）
PATTERN_KEYS = ["imminent", "high", "moderate", "low", "implicit"]
# 每个桶默认对应的等级数值
_LEVEL_NUM = {"imminent": 4, "high": 3, "moderate": 2, "low": 1, "implicit": 2}


# ------------------------------------------------------------------ 默认配置
def default_config() -> dict:
    """当前代码里的默认值，作为下载模板与「未发布」时的回落基线。

    与 ``config.py`` / ``perception.py`` 的现有常量保持一致；医生上传的配置
    若缺字段，用这份补齐（见 ``normalize``）。
    """
    return {
        "version": "1.0.0",
        "note": "MindTrace 默认风险规则（与当前代码常量一致）",
        "patterns": {
            "imminent": list(RISK_PATTERNS_IMMINENT),
            "high": list(RISK_PATTERNS_HIGH),
            "moderate": list(RISK_PATTERNS_MODERATE),
            "low": list(RISK_PATTERNS_LOW),
            "implicit": list(RISK_PATTERNS_IMPLICIT),
        },
        "implicit_level": 2,
        "scale": {
            "PHQ9_SUICIDE_ITEM_INDEX": 8,
            "item_level_map": {"1": 1, "2": 2, "3": 3},
        },
        "multimodal": {
            "sad_ratio_threshold": 0.6,
            "sad_ratio_level": 2,
            "negative_words_min": 4,
            "mask_signal_level": 2,
            "inconsistency_level": 1,
        },
        "voice": {"f0_std_flat": 15, "rate_slow": 3.0, "pause_high": 0.55},
        "gates": {"crisis_min_level": 3, "moderate_level": 2},
    }


def normalize(cfg: dict) -> dict:
    """补齐缺失字段：以 default_config 为底，逐层用上传值覆盖。"""
    base = default_config()
    out = dict(base)
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


# ------------------------------------------------------------------ 表结构
def ensure_tables() -> None:
    with tx() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS risk_rule_version(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content_json TEXT NOT NULL,
                status TEXT NOT NULL,
                validation TEXT DEFAULT '',
                uploaded_by TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS risk_rule_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT,
                action TEXT,
                actor TEXT DEFAULT '',
                note TEXT DEFAULT '',
                ts TEXT DEFAULT ''
            )"""
        )


def _audit(c, version: str, action: str, actor: str, note: str = "") -> None:
    c.execute(
        "INSERT INTO risk_rule_audit(version,action,actor,note,ts) VALUES(?,?,?,?,?)",
        (version, action, actor, note, now_iso()),
    )


# ------------------------------------------------------------------ 校验（护栏）
def validate_config(cfg: dict) -> tuple[bool, list[str], list[str]]:
    """返回 (是否通过, 错误列表, 警告列表)。错误会阻断上传。"""
    errors: list[str] = []
    warns: list[str] = []

    pats = cfg.get("patterns")
    if not isinstance(pats, dict):
        return False, ["缺少 patterns 对象"], warns
    for k in PATTERN_KEYS:
        v = pats.get(k)
        if not isinstance(v, list):
            errors.append(f"patterns.{k} 必须是数组")
            continue
        # 去重检查
        if len(v) != len(set(v)):
            warns.append(f"patterns.{k} 存在重复词，已保留但建议清理")
        if not v and k in ("imminent", "high"):
            errors.append(f"红线：patterns.{k} 不得为空（会直接漏报）")
        elif not v:
            warns.append(f"patterns.{k} 词表为空，该等级将完全依赖模型通道")

    gates = cfg.get("gates", {}) or {}
    cml = gates.get("crisis_min_level", 3)
    ml = gates.get("moderate_level", 2)
    if not isinstance(cml, int) or cml > 3:
        errors.append("红线：gates.crisis_min_level 不得高于 3（不允许把 high 降级放行）")
    if not isinstance(cml, int) or cml < 1:
        errors.append("gates.crisis_min_level 至少为 1")
    if not isinstance(ml, int) or not (1 <= ml <= 2):
        errors.append("gates.moderate_level 需在 1–2 之间")
    if isinstance(cml, int) and isinstance(ml, int) and ml >= cml:
        errors.append("gates.moderate_level 必须小于 crisis_min_level")

    implicit = cfg.get("implicit_level", 2)
    if not isinstance(implicit, int) or not (1 <= implicit <= 3):
        errors.append("implicit_level 需在 1–3 之间")

    mm = cfg.get("multimodal", {}) or {}
    sad = mm.get("sad_ratio_threshold", 0.6)
    if not isinstance(sad, (int, float)) or not (0.3 <= float(sad) <= 0.9):
        errors.append("multimodal.sad_ratio_threshold 需在 0.3–0.9 之间")

    scale = cfg.get("scale", {}) or {}
    imap = scale.get("item_level_map", {})
    if not isinstance(imap, dict):
        errors.append("scale.item_level_map 必须是对象")
    else:
        for k, lv in imap.items():
            if not isinstance(lv, int) or not (0 <= lv <= 4):
                errors.append(f"scale.item_level_map 的等级需为 0–4（收到 {k}->{lv}）")

    return (len(errors) == 0), errors, warns


# ------------------------------------------------------------------ CSV 词表
def merge_csv(cfg: dict, csv_text: str) -> tuple[dict, list[str]]:
    """把医生上传的 CSV 词表（两列：等级,关键词）合并进 patterns（增量、去重）。"""
    cfg = json.loads(json.dumps(cfg))  # 深拷贝，避免改到生效对象
    errs: list[str] = []
    added = 0
    reader = csv.DictReader(io.StringIO((csv_text or "").strip()))
    if not reader.fieldnames or not {"等级", "关键词"}.issubset(reader.fieldnames):
        return cfg, ["CSV 表头必须是：等级,关键词（可用模板）"]
    for i, row in enumerate(reader, start=2):
        bucket = (row.get("等级") or "").strip()
        kw = (row.get("关键词") or "").strip()
        if bucket not in PATTERN_KEYS:
            if bucket or kw:
                errs.append(f"第 {i} 行等级 '{bucket}' 非法（可选 {PATTERN_KEYS}）")
            continue
        if not kw:
            continue
        if kw not in cfg["patterns"][bucket]:
            cfg["patterns"][bucket].append(kw)
            added += 1
    return cfg, errs


# ------------------------------------------------------------------ 试跑判定（P0 只读草稿，不写库）
def rule_level(cfg: dict, text: str) -> tuple[int, str, str]:
    """按配置的词表判「规则通道」，返回 (等级数值, 命中词, 命中桶)。

    匹配优先级与 perception.rule_risk_level 一致：先高后低，首个命中即返回。
    """
    order = [(k, _LEVEL_NUM[k]) for k in ("imminent", "high", "moderate", "low")]
    order.append(("implicit", int(cfg.get("implicit_level", 2))))
    for key, lvl in order:
        for p in cfg.get("patterns", {}).get(key, []):
            if p and p in (text or ""):
                return lvl, p, key
    return 0, "", ""


def scale_level(cfg: dict, item_scores: list[int] | None) -> tuple[int, str]:
    """按配置判量表通道：取 PHQ-9 指定条目的分值映射。"""
    if not item_scores:
        return 0, ""
    idx = int(cfg.get("scale", {}).get("PHQ9_SUICIDE_ITEM_INDEX", 8))
    if idx >= len(item_scores):
        return 0, ""
    v = item_scores[idx]
    imap = {int(k): int(x) for k, x in cfg.get("scale", {}).get("item_level_map", {}).items()}
    lvl = imap.get(v, 0)
    return lvl, (f"PHQ-9 第{idx + 1}题={v}" if lvl else "")


def route(cfg: dict, final_level: int) -> str:
    """按配置门槛判定分流目标（与 graph.risk_gate 语义对齐）。"""
    gates = cfg.get("gates", {}) or {}
    crisis = int(gates.get("crisis_min_level", 3))
    moderate = int(gates.get("moderate_level", 2))
    if final_level >= crisis:
        return "crisis"
    if final_level >= moderate:
        return "moderate"
    return "cbt"


# ------------------------------------------------------------------ 版本管理
def _next_version(cfg: dict) -> str:
    base = str(cfg.get("version") or "1.0")
    return f"{base}-draft-{time.strftime('%Y%m%d_%H%M%S')}"


def save_draft(cfg: dict, by: str, note: str,
               errors: list[str], warns: list[str]) -> dict:
    """落草稿 + 生成版本号 + 写审计。返回版本摘要。"""
    ensure_tables()
    cfg = normalize(cfg)
    version = _next_version(cfg)
    path = RULE_DIR / f"risk_rules_{version}.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    validation = json.dumps({"errors": errors, "warns": warns}, ensure_ascii=False)
    with tx() as c:
        c.execute(
            """INSERT INTO risk_rule_version
               (version,file_path,content_json,status,validation,uploaded_by,note,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (version, str(path), json.dumps(cfg, ensure_ascii=False),
             "draft", validation, by, note, now_iso()),
        )
        _audit(c, version, "upload", by, note)
    return {"version": version, "status": "draft", "file_path": str(path),
            "warns": warns, "created_at": now_iso()}


def list_versions(limit: int = 30) -> list[dict]:
    ensure_tables()
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM risk_rule_version ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        validation = {}
        try:
            validation = json.loads(r["validation"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "id": r["id"], "version": r["version"], "status": r["status"],
            "uploaded_by": r["uploaded_by"], "note": r["note"],
            "created_at": r["created_at"], "errors": validation.get("errors", []),
            "warns": validation.get("warns", []),
        })
    return out


def latest_draft_version() -> str | None:
    ensure_tables()
    with tx() as c:
        r = c.execute(
            "SELECT version FROM risk_rule_version WHERE status='draft' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return r["version"] if r else None


def get_version(version: str) -> dict | None:
    """读取指定版本内容；不存在返回 None。"""
    ensure_tables()
    with tx() as c:
        r = c.execute(
            "SELECT content_json FROM risk_rule_version WHERE version=? "
            "ORDER BY id DESC LIMIT 1", (version,)
        ).fetchone()
    if not r:
        return None
    return json.loads(r["content_json"])


def get_active() -> tuple[dict, str]:
    """读取当前生效版本；无发布版本则回落到 code 默认值。

    返回 (配置, 版本标识)。P1 引擎会改从这里取值。
    """
    ensure_tables()
    with tx() as c:
        r = c.execute(
            "SELECT version, content_json FROM risk_rule_version "
            "WHERE status='published' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not r:
        return default_config(), "default-1.0.0"
    return json.loads(r["content_json"]), r["version"]


def _activate(version: str, action: str, actor: str, note: str) -> dict:
    """把某版本置为 published，其余 published 置为 rolled_back，写审计。"""
    ensure_tables()
    with tx() as c:
        r = c.execute(
            "SELECT id FROM risk_rule_version WHERE version=? ORDER BY id DESC LIMIT 1",
            (version,),
        ).fetchone()
        if not r:
            raise KeyError(version)
        c.execute("UPDATE risk_rule_version SET status='rolled_back' WHERE status='published'")
        c.execute("UPDATE risk_rule_version SET status='published' WHERE id=?", (r["id"],))
        _audit(c, version, action, actor, note)
    return {"ok": True, "active": version, "action": action}


def publish(version: str, actor: str, note: str) -> dict:
    if not (note or "").strip():
        raise ValueError("发布必须填写变更理由")
    return _activate(version, "publish", actor, note)


def rollback(version: str, actor: str, note: str) -> dict:
    return _activate(version, "rollback", actor, note)
