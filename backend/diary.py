"""每日日记 / 心情生成（豆包式改版 P1）。

设计：
- 一次对话（session）结束后，用 LLM 把这次聊的内容写成一段温暖的**日记** +
  一个**心情标签**（emoji + 中文标签），落库到 diary_entry，供前端心情时间线展示。
- 与 daily_record 的关系：daily_record 存的是结构化指标（情绪分布/风险），
  diary_entry 存的是给人看的叙事产物。两者独立，互不覆盖。
- 模型不可用（mock/降级）时用规则兜底生成一条简短日记，保证必有产出。
- 合规：日记里绝不写"诊断"，不美化风险；高风险会话的日记措辞更克制，
  且附带求助渠道提示。

依赖复用：llm.structured / storage.tx / models.now_iso，零新增依赖。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from models import now_iso
from storage import tx

# ------------------------------------------------------------------ 表结构
DDL = """
CREATE TABLE IF NOT EXISTS diary_entry(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT,
  session_id TEXT,
  ts TEXT,
  date TEXT,                -- YYYY-MM-DD，便于按天分组
  mood_emoji TEXT DEFAULT '🙂',
  mood_label TEXT DEFAULT '平静',
  title TEXT DEFAULT '',
  content TEXT DEFAULT '',  -- 日记正文（第一人称，温暖）
  keywords TEXT DEFAULT '[]',  -- JSON 数组：本次聊到的主题
  risk_level TEXT DEFAULT 'none',
  source TEXT DEFAULT 'llm'    -- llm | fallback
);
CREATE INDEX IF NOT EXISTS idx_diary_user ON diary_entry(user_id, id DESC);
"""


def ensure_tables() -> None:
    with tx() as c:
        c.executescript(DDL)


class DiaryDraft(BaseModel):
    """LLM 生成的日记草稿（结构化输出）。"""

    mood_label: str = Field(default="平静", description="心情标签，2-4字，如 平静/有点累/焦虑/低落/被理解/有力量")
    mood_emoji: str = Field(default="🙂", description="一个最能代表这次心情的 emoji")
    title: str = Field(default="", description="日记标题，6-12字")
    content: str = Field(default="", description="第一人称日记正文，80-160字，温暖、具体、非诊断")
    keywords: list[str] = Field(default_factory=list, description="本次聊到的主题，2-5个词")


DIARY_SYS = """你是用户的私人日记助手。请把下面这段「今天的对话」写成一篇简短、温暖的日记。

要求：
1. 用第一人称（"今天我…"），像写给自己看，语气真诚、不评判、不说教。
2. 具体：提到对话里真实出现的人/事/感受，不要空泛的鸡汤。
3. 80-160 字，自然口语，可分成一两句。
4. 【绝对红线】不做任何心理诊断、不贴病理标签、不开药、不鼓励自伤；
   若对话里出现高风险表达，日记要更克制——强调"被听见""有人在乎""可以求助"，
   绝不能美化或合理化危机。
5. mood_label 给一个贴切的心情词（2-4字），mood_emoji 只给一个 emoji。
6. keywords 提炼 2-5 个主题词。

只输出 JSON：{"mood_label":..., "mood_emoji":..., "title":..., "content":..., "keywords":[...]}
不要输出 JSON 以外的内容。"""

MOOD_FALLBACK = {
    "低落": "🌧️", "焦虑": "😟", "疲惫": "😮‍💨", "烦躁": "😤", "紧张": "😬",
    "愉悦": "🌤️", "平静": "🙂", "惊讶": "😮",
}


def _fallback_draft(emotion_label: str, risk_level: str) -> DiaryDraft:
    """模型不可用时的规则兜底：必有产出，且不触碰诊断红线。"""
    mood = (emotion_label or "平静").strip() or "平静"
    emoji = MOOD_FALLBACK.get(mood, "🙂")
    if risk_level in ("high", "imminent", "moderate"):
        content = ("今天我把心里压着的事说了一些出来。能说出口并不容易。"
                   "我不是一个人在扛，身边还有人愿意听、愿意帮。"
                   "如果很难受，随时可以找信任的人聊聊，或拨打求助热线。"
                   "今天先到这里，慢慢来。")
        mood, emoji, title = "被倾听", "🤝", "今天，说出来了"
    else:
        content = (f"今天聊了聊最近的状态，整体感觉「{mood}」。"
                   "有些事说出来了，心里就轻一点。记录一下此刻的感受，"
                   "明天再接着慢慢走。")
        title = "今天的一点感受"
    return DiaryDraft(mood_label=mood, mood_emoji=emoji, title=title,
                      content=content, keywords=[mood])


def generate_diary(user_id: str, session_id: str, transcript: str,
                   emotion_label: str = "", risk_level: str = "none") -> dict:
    """生成并落库一篇日记。transcript 为本次对话的用户原话拼接。"""
    ensure_tables()
    transcript = (transcript or "").strip()

    used_llm = False
    if transcript:
        try:
            from llm import structured

            draft: DiaryDraft = structured(
                DiaryDraft, DIARY_SYS,
                f"今天的对话（用户说的话）：\n{transcript[:1500]}"
                f"\n\n情绪线索：{emotion_label or '未知'}；风险等级：{risk_level}",
            )
            if not (draft.content or "").strip():
                raise ValueError("空日记")
            # 高风险时不信任模型可能的花哨措辞，强制收敛到克制的兜底文案
            if risk_level in ("high", "imminent", "moderate"):
                safe = _fallback_draft(emotion_label, risk_level)
                draft = DiaryDraft(
                    mood_label=draft.mood_label or safe.mood_label,
                    mood_emoji=draft.mood_emoji or safe.mood_emoji,
                    title=draft.title or safe.title,
                    content=safe.content,   # 正文用克制的兜底，避免模型美化风险
                    keywords=draft.keywords or safe.keywords,
                )
            used_llm = True
        except Exception:  # noqa: BLE001  任何失败都兜底，绝不让"生成日记"中断收尾
            draft = _fallback_draft(emotion_label, risk_level)
    else:
        draft = _fallback_draft(emotion_label, risk_level)

    import json

    ts = now_iso()
    date = ts[:10]
    with tx() as c:
        cur = c.execute(
            """INSERT INTO diary_entry
               (user_id,session_id,ts,date,mood_emoji,mood_label,title,content,
                keywords,risk_level,source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, session_id, ts, date, draft.mood_emoji, draft.mood_label,
             draft.title, draft.content, json.dumps(draft.keywords, ensure_ascii=False),
             risk_level, "llm" if used_llm else "fallback"),
        )
        did = cur.lastrowid

    return {
        "id": did, "user_id": user_id, "session_id": session_id, "ts": ts,
        "date": date, "mood_emoji": draft.mood_emoji, "mood_label": draft.mood_label,
        "title": draft.title, "content": draft.content, "keywords": draft.keywords,
        "risk_level": risk_level, "source": "llm" if used_llm else "fallback",
    }


def list_diaries(user_id: str, limit: int = 30) -> list[dict]:
    ensure_tables()
    import json

    with tx() as c:
        rows = c.execute(
            "SELECT * FROM diary_entry WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 100))),
        ).fetchall()
    out = []
    for r in rows:
        try:
            kws = json.loads(r["keywords"] or "[]")
        except Exception:  # noqa: BLE001
            kws = []
        out.append({
            "id": r["id"], "session_id": r["session_id"], "ts": r["ts"],
            "date": r["date"], "mood_emoji": r["mood_emoji"],
            "mood_label": r["mood_label"], "title": r["title"],
            "content": r["content"], "keywords": kws,
            "risk_level": r["risk_level"], "source": r["source"],
        })
    return out
