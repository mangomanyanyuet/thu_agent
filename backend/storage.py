"""存储层。

刻意与编排框架解耦：业务数据走自己的 SQL 表，
LangGraph 的 checkpoint 只管会话状态机。
这样未来换框架（LangGraph -> ADK / MAF）时业务数据不受影响。

当前实现用 SQLite（零依赖跑通骨架）；把 database_url 改成
postgresql://... 即可切到 Postgres（本文件只用标准 SQL）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable

from config import get_settings
from models import (
    DailyRecord,
    MessageRow,
    RiskFlag,
    ScaleScore,
    SessionRow,
    UserProfile,
    now_iso,
)

_local = threading.local()

DDL = """
CREATE TABLE IF NOT EXISTS user_profile(
  user_id TEXT PRIMARY KEY,
  nickname TEXT DEFAULT '',
  personality_traits TEXT DEFAULT '[]',
  coping_style TEXT DEFAULT '[]',
  social_support TEXT DEFAULT '[]',
  known_triggers TEXT DEFAULT '[]',
  events TEXT DEFAULT '[]',
  preferences TEXT DEFAULT '[]',
  baseline_emotion TEXT DEFAULT '{}',
  updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS daily_record(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT, date TEXT, video_uri TEXT DEFAULT '',
  transcript TEXT DEFAULT '', narrative TEXT DEFAULT '',
  video_features TEXT DEFAULT '{}', audio_features TEXT DEFAULT '{}',
  narrative_analysis TEXT DEFAULT '{}', modality_summary TEXT DEFAULT '',
  inconsistency INTEGER DEFAULT 0, created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS scale_score(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT, date TEXT, scale_name TEXT,
  item_scores TEXT DEFAULT '[]', total_score INTEGER DEFAULT 0,
  delta_vs_last INTEGER DEFAULT 0, delta_vs_baseline INTEGER DEFAULT 0,
  severity TEXT DEFAULT '', created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS risk_flag(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT, session_id TEXT, ts TEXT,
  source TEXT, evidence TEXT, level TEXT,
  cssrs_dimension TEXT DEFAULT '',
  handled INTEGER DEFAULT 0, handled_by TEXT DEFAULT '', handled_note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS session(
  id TEXT PRIMARY KEY, user_id TEXT, started_at TEXT,
  ended_at TEXT DEFAULT '', summary TEXT DEFAULT '',
  last_risk TEXT DEFAULT 'none', cbt_stage TEXT DEFAULT 'none',
  interrupted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS message(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, role TEXT, content TEXT,
  emotion TEXT DEFAULT '', risk_level TEXT DEFAULT 'none',
  meta TEXT DEFAULT '{}', ts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON message(session_id);
CREATE INDEX IF NOT EXISTS idx_risk_user ON risk_flag(user_id);
CREATE INDEX IF NOT EXISTS idx_scale_user ON scale_score(user_id, scale_name);
CREATE TABLE IF NOT EXISTS audit_trace(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, node TEXT, ts TEXT,
  input_ctx TEXT DEFAULT '', output TEXT DEFAULT ''
);
"""


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        url = get_settings().database_url
        path = url.split("sqlite:///")[-1]
        c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        # 默认配置下每轮对话要 commit 15 次，每次都 fsync，实测占 2.1s；
        # 再加上与 API 进程争锁又花 3.3s。开启 WAL + 放宽同步后基本消掉。
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(DDL)
        _local.conn = c
    return _local.conn


@contextmanager
def tx():
    c = _conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def _j(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


def _lj(s: str | None, default=None):
    if not s:
        return default if default is not None else []
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else []


# ---------------------------------------------------------------- user_profile
def get_profile(user_id: str) -> UserProfile:
    with tx() as c:
        r = c.execute("SELECT * FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return UserProfile(user_id=user_id)
    return UserProfile(
        user_id=r["user_id"],
        nickname=r["nickname"],
        personality_traits=_lj(r["personality_traits"]),
        coping_style=_lj(r["coping_style"]),
        social_support=_lj(r["social_support"]),
        known_triggers=_lj(r["known_triggers"]),
        events=_lj(r["events"]),
        preferences=_lj(r["preferences"]),
        baseline_emotion=_lj(r["baseline_emotion"], {}),
        updated_at=r["updated_at"],
    )


def upsert_profile(p: UserProfile) -> None:
    p.updated_at = now_iso()
    with tx() as c:
        c.execute(
            """INSERT INTO user_profile VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 nickname=excluded.nickname,
                 personality_traits=excluded.personality_traits,
                 coping_style=excluded.coping_style,
                 social_support=excluded.social_support,
                 known_triggers=excluded.known_triggers,
                 events=excluded.events,
                 preferences=excluded.preferences,
                 baseline_emotion=excluded.baseline_emotion,
                 updated_at=excluded.updated_at""",
            (
                p.user_id, p.nickname, _j(p.personality_traits), _j(p.coping_style),
                _j(p.social_support), _j(p.known_triggers), _j(p.events),
                _j(p.preferences), _j(p.baseline_emotion), p.updated_at,
            ),
        )


def merge_list_field(user_id: str, field_name: str, new_items: Iterable[str], cap: int = 30) -> None:
    """把新事实增量合并进画像字段（去重 + 截断），这是"长期记忆"的写入口。"""
    p = get_profile(user_id)
    cur = list(getattr(p, field_name))
    seen = {x.strip() for x in cur}
    for it in new_items:
        it = (it or "").strip()
        if it and it not in seen:
            cur.append(it)
            seen.add(it)
    setattr(p, field_name, cur[-cap:])
    upsert_profile(p)


# ---------------------------------------------------------------- daily_record
def add_daily_record(r: DailyRecord) -> int:
    r.created_at = now_iso()
    with tx() as c:
        cur = c.execute(
            """INSERT INTO daily_record
               (user_id,date,video_uri,transcript,narrative,video_features,
                audio_features,narrative_analysis,modality_summary,inconsistency,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (r.user_id, r.date, r.video_uri, r.transcript, r.narrative,
             _j(r.video_features), _j(r.audio_features), _j(r.narrative_analysis),
             r.modality_summary, int(r.inconsistency), r.created_at),
        )
        return cur.lastrowid


def find_daily_record(user_id: str, day: str, source: str = "") -> DailyRecord | None:
    """找某天（某来源）的已有记录，用于同日多次陪伴时合并而非重复插入。"""
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM daily_record WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 20",
            (user_id, day),
        ).fetchall()
    for r in rows:
        rec = DailyRecord(
            id=r["id"], user_id=r["user_id"], date=r["date"], video_uri=r["video_uri"],
            transcript=r["transcript"], narrative=r["narrative"],
            video_features=_lj(r["video_features"], {}),
            audio_features=_lj(r["audio_features"], {}),
            narrative_analysis=_lj(r["narrative_analysis"], {}),
            modality_summary=r["modality_summary"],
            inconsistency=bool(r["inconsistency"]), created_at=r["created_at"],
        )
        if source:
            if (rec.narrative_analysis or {}).get("source") == source:
                return rec
        else:
            return rec
    return None


def update_daily_record(r: DailyRecord) -> None:
    """就地更新一条已有记录（同日多次陪伴合并用）。"""
    with tx() as c:
        c.execute(
            """UPDATE daily_record SET video_uri=?,transcript=?,narrative=?,
               video_features=?,audio_features=?,narrative_analysis=?,
               modality_summary=?,inconsistency=? WHERE id=?""",
            (r.video_uri, r.transcript, r.narrative,
             _j(r.video_features), _j(r.audio_features), _j(r.narrative_analysis),
             r.modality_summary, int(r.inconsistency), r.id),
        )


def list_daily_records(user_id: str, limit: int = 30) -> list[DailyRecord]:
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM daily_record WHERE user_id=? ORDER BY date DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        DailyRecord(
            id=r["id"], user_id=r["user_id"], date=r["date"], video_uri=r["video_uri"],
            transcript=r["transcript"], narrative=r["narrative"],
            video_features=_lj(r["video_features"], {}),
            audio_features=_lj(r["audio_features"], {}),
            narrative_analysis=_lj(r["narrative_analysis"], {}),
            modality_summary=r["modality_summary"],
            inconsistency=bool(r["inconsistency"]), created_at=r["created_at"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------- scale_score
def add_scale_score(s: ScaleScore) -> int:
    s.created_at = now_iso()
    with tx() as c:
        cur = c.execute(
            """INSERT INTO scale_score
               (user_id,date,scale_name,item_scores,total_score,delta_vs_last,
                delta_vs_baseline,severity,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (s.user_id, s.date, s.scale_name, _j(s.item_scores), s.total_score,
             s.delta_vs_last, s.delta_vs_baseline, s.severity, s.created_at),
        )
        return cur.lastrowid


def list_scale_scores(user_id: str, scale_name: str | None = None, limit: int = 60) -> list[ScaleScore]:
    with tx() as c:
        if scale_name:
            rows = c.execute(
                "SELECT * FROM scale_score WHERE user_id=? AND scale_name=? ORDER BY date ASC LIMIT ?",
                (user_id, scale_name, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM scale_score WHERE user_id=? ORDER BY date ASC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return [
        ScaleScore(
            id=r["id"], user_id=r["user_id"], date=r["date"], scale_name=r["scale_name"],
            item_scores=_lj(r["item_scores"]), total_score=r["total_score"],
            delta_vs_last=r["delta_vs_last"], delta_vs_baseline=r["delta_vs_baseline"],
            severity=r["severity"], created_at=r["created_at"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------- risk_flag
def add_risk_flag(f: RiskFlag) -> int:
    with tx() as c:
        cur = c.execute(
            """INSERT INTO risk_flag
               (user_id,session_id,ts,source,evidence,level,cssrs_dimension,
                handled,handled_by,handled_note)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f.user_id, f.session_id, f.ts, f.source, f.evidence, f.level,
             f.cssrs_dimension, int(f.handled), f.handled_by, f.handled_note),
        )
        return cur.lastrowid


def list_risk_flags(user_id: str | None = None, unhandled_only: bool = False,
                    min_level: int = 0, limit: int = 100) -> list[RiskFlag]:
    levels = ["none", "low", "moderate", "high", "imminent"][max(min_level, 0):]
    ph = ",".join("?" * len(levels))
    sql = f"SELECT * FROM risk_flag WHERE level IN ({ph})"
    args: list[Any] = [*levels]
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    if unhandled_only:
        sql += " AND handled=0"
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with tx() as c:
        rows = c.execute(sql, args).fetchall()
    return [
        RiskFlag(
            id=r["id"], user_id=r["user_id"], session_id=r["session_id"], ts=r["ts"],
            source=r["source"], evidence=r["evidence"], level=r["level"],
            cssrs_dimension=r["cssrs_dimension"], handled=bool(r["handled"]),
            handled_by=r["handled_by"], handled_note=r["handled_note"],
        )
        for r in rows
    ]


def mark_risk_handled(flag_id: int, by: str, note: str = "") -> None:
    with tx() as c:
        c.execute(
            "UPDATE risk_flag SET handled=1, handled_by=?, handled_note=? WHERE id=?",
            (by, note, flag_id),
        )


def latest_risk_level(user_id: str) -> str:
    flags = list_risk_flags(user_id=user_id, limit=1)
    return flags[0].level if flags else "none"


# ---------------------------------------------------------------- session / message
def upsert_session(s: SessionRow) -> None:
    with tx() as c:
        c.execute(
            """INSERT INTO session VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 ended_at=excluded.ended_at, summary=excluded.summary,
                 last_risk=excluded.last_risk, cbt_stage=excluded.cbt_stage,
                 interrupted=excluded.interrupted""",
            (s.id, s.user_id, s.started_at, s.ended_at, s.summary,
             s.last_risk, s.cbt_stage, int(s.interrupted)),
        )


def get_session(sid: str) -> SessionRow | None:
    with tx() as c:
        r = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
    if not r:
        return None
    return SessionRow(
        id=r["id"], user_id=r["user_id"], started_at=r["started_at"],
        ended_at=r["ended_at"], summary=r["summary"], last_risk=r["last_risk"],
        cbt_stage=r["cbt_stage"], interrupted=bool(r["interrupted"]),
    )


def list_sessions(user_id: str, limit: int = 30) -> list[SessionRow]:
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM session WHERE user_id=? ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        SessionRow(id=r["id"], user_id=r["user_id"], started_at=r["started_at"],
                   ended_at=r["ended_at"], summary=r["summary"], last_risk=r["last_risk"],
                   cbt_stage=r["cbt_stage"], interrupted=bool(r["interrupted"]))
        for r in rows
    ]


def add_message(m: MessageRow) -> int:
    m.ts = m.ts or now_iso()
    with tx() as c:
        cur = c.execute(
            "INSERT INTO message(session_id,role,content,emotion,risk_level,meta,ts) VALUES(?,?,?,?,?,?,?)",
            (m.session_id, m.role, m.content, m.emotion, m.risk_level, _j(m.meta), m.ts),
        )
        return cur.lastrowid


def list_messages(session_id: str, limit: int = 200) -> list[MessageRow]:
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM message WHERE session_id=? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [
        MessageRow(id=r["id"], session_id=r["session_id"], role=r["role"],
                   content=r["content"], emotion=r["emotion"], risk_level=r["risk_level"],
                   meta=_lj(r["meta"], {}), ts=r["ts"])
        for r in rows
    ]


# ---------------------------------------------------------------- audit
def add_trace(session_id: str, node: str, input_ctx: str = "", output: str = "") -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO audit_trace(session_id,node,ts,input_ctx,output) VALUES(?,?,?,?,?)",
            (session_id, node, now_iso(), input_ctx[:4000], output[:4000]),
        )


def list_traces(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM audit_trace WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
