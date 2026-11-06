"""风险规则 / 临床超参数配置化（P0 上传闭环 + P1 引擎接入）。

职责
----
- 配置上传、校验、草稿与版本管理、试跑预览、发布 / 回滚、审计。
- **P1 起 `get_active()` 是真实对话链路的唯一取值来源**：
  `perception.py` 的四个风险通道、`graph.py` 的分流门槛 / 量表有效期 /
  审核队列门槛 / 评估 prompt、`companion/*` 的会话节奏都从这里读。
  未发布任何版本时回落到 `default_config()`（与代码原常量逐项一致）。

  ⚠ 这一点是「UI 调参」能不能真正解决问题的关键：P0 版本里 publish 只做标记，
  页面上改了数、点了发布，线上对话仍然按硬编码常量跑（`engine_wired=False`）。
  专家体验后调整参数却看不到任何变化，比没有配置界面更糟。

存储
----
- 配置快照落盘到 ``data/risk_rules/*.json``（``data/`` 已 gitignore，永不入库）。
- 版本元数据 / 审计走 SQLite（复用 storage.tx()），表在本模块内自建。
- 区分「生效版本」（status=published）与「草稿」（status=draft）：
  试跑读草稿，真实对话读生效版本。

``get_active()`` 带进程内短 TTL 缓存：每轮对话会调用它多次（四个通道 + gate），
不能每次都开一次 SQLite 事务；publish / rollback 时显式失效，多进程场景由 TTL 兜底。

安全护栏（服务端强制，配置不得绕过）
------------------------------------
- patterns.imminent / patterns.high 不得删空（否则直接漏报）。
- 词桶对应的风险等级是 C-SSRS 临床定义，**锁死不开放配置**；
  专家想调敏感度只能「把词挪到更高的桶」，不能把整个桶降级。
- gates.crisis_min_level 不得 > 3（不允许把 high 降级放行）。
- gates.moderate_level < crisis_min_level，且在 1–2 之间。
- scale.item_level_map["3"] 不得 < 3（PHQ-9 第 9 题满分必须触发危机）。
- 四通道取 max / keep_max / high 必 interrupt / 必给热线 —— 代码级锁定，
  配置文件里根本不存在这些开关。
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
    """代码原常量，作为模板 / 未发布时的回落基线。

    每一项都必须与它在引擎里的硬编码默认值**逐字一致**，否则「未发布」
    和「发布默认配置」会跑出两种行为。新增字段见各分组注释。
    """
    return {
        "version": "1.0.0",
        "note": "MindTrace 默认临床参数（与代码常量一致）",
        # ① 规则词表：临床定义决定词归哪个桶；桶对应等级锁死（见护栏）
        "patterns": {
            "imminent": list(RISK_PATTERNS_IMMINENT),
            "high": list(RISK_PATTERNS_HIGH),
            "moderate": list(RISK_PATTERNS_MODERATE),
            "low": list(RISK_PATTERNS_LOW),
            "implicit": list(RISK_PATTERNS_IMPLICIT),
        },
        "implicit_level": 2,
        # ② 量表通道
        "scale": {
            "PHQ9_SUICIDE_ITEM_INDEX": 8,
            "item_level_map": {"1": 1, "2": 2, "3": 3},
            # 陈旧量表分的有效期（天）。0 = 不限制，与旧代码行为一致。
            # ⚠ 默认必须是 0：设成 30 会让「未发布任何版本」时就悄悄不再对
            #   31 天前第 9 题=3 的 PHQ-9 告警 —— 那是没人批准过的敏感度下调。
            #   专家抱怨「同一份 PHQ-9 让每次对话都挂起」时，在界面里主动调大。
            "max_age_days": 0,
        },
        # ③ 多模态通道（每日记录）
        "multimodal": {
            "enabled": True,
            "sad_ratio_threshold": 0.6,
            "sad_ratio_level": 2,
            "negative_words_min": 4,
            "mask_signal_level": 2,     # 掩饰信号（嘴上说好 / 声音低落）起判等级
            "inconsistency_level": 1,
        },
        # ④ 本轮语音通道
        "voice": {
            "enabled": True,
            "f0_std_flat": 15,          # 语调平板：F0 标准差 < 该值
            "rate_slow": 3.0,           # 语速偏慢：音节/秒 < 该值
            "pause_high": 0.55,         # 停顿占比 > 该值
            "rate_fast": 6.5,           # 激动：音节/秒 > 该值
            "f0_std_high": 45,          # 激动：F0 标准差 > 该值
        },
        # ⑤ 分流与告警门槛
        "gates": {"crisis_min_level": 3, "moderate_level": 2},
        # 进入「待处理风险队列」的最低等级。只影响队列噪音，
        # **不影响** crisis 挂起（挂起由 crisis_min_level 决定，且是硬红线）。
        "flags": {"min_level": 1},
        # ⑥ 模型评估通道：追加临床说明，不替换基座 prompt（防止把红线写没）
        "assess": {"prompt_extra": ""},
        # ⑦ 陪伴会话节奏（秒）
        "session": {"idle_confirm_at": 60, "idle_end_at": 90, "wrap_up_at": 240},
    }


def normalize(cfg: dict) -> dict:
    """补齐缺失字段：以 default_config 为底**递归**覆盖上传值。

    P0 只合并了一层，`{"gates": {"crisis_min_level": 2}}` 会把 gates 里其它
    字段一起冲掉；可视化表单按分组提交时尤其容易踩到。
    """
    return coerce(_deep_merge(default_config(), cfg or {}))


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_path(obj: dict, path: str, value) -> None:
    keys = path.split(".")
    cur = obj
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def _get_path(obj: dict, path: str):
    cur = obj
    for k in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def coerce(cfg: dict) -> dict:
    """把 JSON/CSV 里常见的字符串数值转成正确类型。

    HTML `<select>` 和手写 JSON 很容易给出 `"2"`；不转换会让 `gates` 收到
    字符串、比较时抛 TypeError，护栏校验还来不及拦。转换失败保留原值，
    由 `validate_config` 报出可读错误。
    """
    out = json.loads(json.dumps(cfg))
    for g in PARAM_GROUPS:
        for f in g.get("fields", []) + g.get("map_fields", []):
            typ = f.get("type", "int")
            if typ not in ("int", "float", "bool", "enum"):
                continue
            v = _get_path(out, f["path"])
            if v is None or not isinstance(v, str):
                continue
            try:
                if typ == "int":
                    _set_path(out, f["path"], int(v))
                elif typ == "float":
                    _set_path(out, f["path"], float(v))
                elif typ == "enum":
                    # 下拉框 / 手写 JSON 常给 "2"；选项都是整数，统一转 int
                    _set_path(out, f["path"], int(v))
                else:
                    _set_path(out, f["path"], v.strip().lower() in ("1", "true", "yes", "on"))
            except (ValueError, TypeError):
                pass
    return out


# ================================================================== 参数元数据
# 前端表单**完全由这份元数据生成**：中文标签、临床含义、默认值、允许范围、
# 分组都只在这里维护一份。新增一个旋钮只需在此登记 + 在引擎里读它，
# 不必再去改一遍 React 组件。
#
# type: enum | int | float | bool | text | words
# sensitivity: 数值往哪个方向调 = 更敏感（前端据此给"放宽/收紧"上色）
PARAM_GROUPS: list[dict] = [
    {
        "key": "gates", "title": "① 分流与告警门槛",
        "subtitle": "最直接决定「告警太频繁 / 不够敏感」。改完先看回归试跑。",
        "scope": "长期对话（文字 / 语音 / 小程序）、量表与每日上报的入队。"
                 "5 分钟陪伴会话有独立的判定逻辑，不受本组影响。",
        "fields": [
            {"path": "gates.crisis_min_level", "label": "危机触发等级", "type": "enum",
             "default": 3, "options": [{"v": 2, "t": "2 中度即挂起（更敏感）"},
                                      {"v": 3, "t": "3 重度才挂起（默认）"}],
             "help": "最终风险等级 ≥ 此值即转危机协议、给热线并挂起等人工审核。"
                     "调低会明显增加人工挂起量；上限锁死为 3，不允许把重度降级放行。"},
            {"path": "gates.moderate_level", "label": "安全计划触发等级", "type": "enum",
             "default": 2, "options": [{"v": 1, "t": "1 轻度即出安全计划"},
                                      {"v": 2, "t": "2 中度才出（默认）"}],
             "help": "≥ 此值走「CBT + 安全计划 + 热线」分支。必须小于危机触发等级。"},
            {"path": "flags.min_level", "label": "审核队列最低等级", "type": "enum",
             "default": 1, "options": [{"v": 1, "t": "1 轻度及以上入队"},
                                      {"v": 2, "t": "2 中度及以上入队（队列更安静）"}],
             "help": "只影响人工审核台「待处理队列」的噪音，**不影响**会话挂起。"
                     "高危会话无论怎么设都会挂起并留痕。"},
        ],
    },
    {
        "key": "patterns", "title": "② 风险词表与隐喻起判",
        "subtitle": "词归哪个等级由临床定义决定；桶本身的等级锁死。想更敏感就把词挪到更高的桶。",
        "scope": "长期对话 + 5 分钟陪伴会话（陪伴会话中规则通道 ≥ 中度即判高危）。",
        "fields": [
            {"path": "implicit_level", "label": "告别式 / 隐喻起判等级", "type": "enum",
             "default": 2, "options": [{"v": 1, "t": "1 轻度"}, {"v": 2, "t": "2 中度（默认）"},
                                      {"v": 3, "t": "3 重度（更敏感）"}],
             "help": "「以后不用管我了」「帮我把东西转交」这类不直说的表达，规则通道直接按几级判。"
                     "设为 3 时单凭这类说法就会触发危机挂起；设为 1 则主要依赖模型通道兜底。"},
        ],
        "words": [
            {"key": "imminent", "label": "危急 · 正在实施 / 手段在手", "locked_nonempty": True},
            {"key": "high", "label": "重度 · 有计划 / 准备行为", "locked_nonempty": True},
            {"key": "moderate", "label": "中度 · 主动意念（无计划）"},
            {"key": "low", "label": "轻度 · 被动死亡愿望"},
            {"key": "implicit", "label": "隐喻 · 告别 / 托付式"},
        ],
    },
    {
        "key": "scale", "title": "③ 量表通道",
        "subtitle": "陈旧分数是「无缘无故反复告警」的常见来源。",
        "scope": "长期对话的量表通道与量表提交入队。陪伴会话不读量表。",
        "fields": [
            {"path": "scale.PHQ9_SUICIDE_ITEM_INDEX", "label": "PHQ-9 自伤条目序号", "type": "int",
             "default": 8, "min": 0, "max": 8, "step": 1, "unit": "（0 起）",
             "help": "默认 8 = 第 9 题。换量表版本时调整，一般不动。"},
            {"path": "scale.max_age_days", "label": "量表分数有效期", "type": "int",
             "default": 0, "min": 0, "max": 365, "step": 1, "unit": "天（0 = 不限制）",
             "help": "超过该天数的量表分不再进入风险判定。默认 0 = 永不失效（与原行为一致）。"
                     "一份很久以前第 9 题=3 的 PHQ-9 会让**每一轮**都触发危机挂起，"
                     "这是专家最容易发现的误报源；设为 30 等值即可止住，代价是"
                     "用户长期不复测时量表通道不再提供保护。"},
        ],
        "map_fields": [
            {"path": "scale.item_level_map.1", "label": "第 9 题 = 1 分 →", "default": 1},
            {"path": "scale.item_level_map.2", "label": "第 9 题 = 2 分 →", "default": 2},
            {"path": "scale.item_level_map.3", "label": "第 9 题 = 3 分 →", "default": 3,
             "locked_min": 3},
        ],
    },
    {
        "key": "multimodal", "title": "④ 多模态 / 语音通道",
        "subtitle": "掩饰信号、面部悲伤占比这类弱信号最容易造成误报，可单独调阈值或整条关闭。",
        "scope": "「每日多模态」相关项只作用于长期对话与每日上报；"
                 "「本轮语音」相关项（voice.*、语音掩饰信号等级）同时作用于陪伴会话。两个开关互相独立。",
        "fields": [
            {"path": "multimodal.enabled", "label": "启用每日多模态通道", "type": "bool",
             "default": True, "help": "关闭后不再从每日记录取面部/自述信号。"
                                     "该通道最高只判到中度，不影响高危词表与模型通道。"},
            {"path": "multimodal.sad_ratio_threshold", "label": "面部悲伤占比阈值", "type": "float",
             "default": 0.6, "min": 0.3, "max": 0.9, "step": 0.05,
             "help": "悲伤占比 > 该值即产生风险信号。调高 = 更少误报，调低 = 更敏感。",
             "sensitivity": "higher"},
            {"path": "multimodal.sad_ratio_level", "label": "面部悲伤判定的等级", "type": "enum",
             "default": 2, "options": [{"v": 1, "t": "1 轻度"}, {"v": 2, "t": "2 中度"}],
             "help": "单靠表情就判到中度往往偏激进，保守方案可降到 1。"},
            {"path": "multimodal.negative_words_min", "label": "自述负面词密度下限", "type": "int",
             "default": 4, "min": 1, "max": 10, "step": 1, "unit": "个",
             "help": "自述偏负面且命中的负面词 ≥ 该值才产生轻度信号。",
             "sensitivity": "higher"},
            {"path": "multimodal.inconsistency_level", "label": "模态不一致信号等级", "type": "enum",
             "default": 1, "options": [{"v": 1, "t": "1 轻度"}, {"v": 2, "t": "2 中度"}],
             "help": "「嘴上说好、表情/语音偏负面」的掩饰倾向判几级。"},
            {"path": "multimodal.mask_signal_level", "label": "语音掩饰信号等级", "type": "enum",
             "default": 2, "options": [{"v": 1, "t": "1 轻度"}, {"v": 2, "t": "2 中度"}],
             "help": "语气低落但文本说「挺好的」时判几级。临床认为掩饰需要更谨慎，默认 2。"},
            {"path": "voice.enabled", "label": "启用本轮语音通道", "type": "bool",
             "default": True, "help": "关闭后忽略语气/声学特征。规则、量表、模型通道不受影响。"},
            {"path": "voice.f0_std_flat", "label": "语调平板阈值（F0 标准差）", "type": "float",
             "default": 15, "min": 5, "max": 30, "step": 1, "unit": "Hz",
             "help": "低于该值视为语调平板。", "sensitivity": "lower"},
            {"path": "voice.rate_slow", "label": "语速偏慢阈值", "type": "float",
             "default": 3.0, "min": 1.0, "max": 6.0, "step": 0.1, "unit": "音节/秒",
             "help": "低于该值视为精神运动性抑制。", "sensitivity": "lower"},
            {"path": "voice.pause_high", "label": "停顿占比阈值", "type": "float",
             "default": 0.55, "min": 0.2, "max": 0.9, "step": 0.05,
             "help": "高于该值视为停顿过多。", "sensitivity": "higher"},
            {"path": "voice.rate_fast", "label": "激动语速阈值", "type": "float",
             "default": 6.5, "min": 4.0, "max": 10.0, "step": 0.1, "unit": "音节/秒",
             "help": "高于该值视为激动/焦虑特征。", "sensitivity": "higher"},
            {"path": "voice.f0_std_high", "label": "激动基频起伏阈值", "type": "float",
             "default": 45, "min": 25, "max": 80, "step": 1, "unit": "Hz",
             "help": "高于该值视为唤醒度偏高。", "sensitivity": "higher"},
        ],
    },
    {
        "key": "assess", "title": "⑤ 模型评估通道（高级）",
        "subtitle": "只追加临床补充，不替换基座 prompt —— 红线不可被写掉。",
        "scope": "长期对话的情绪 + 风险评估。陪伴会话的模型判定器不读本项。",
        "fields": [
            {"path": "assess.prompt_extra", "label": "分级补充说明（追加到评估 prompt）",
             "type": "text", "default": "",
             "help": "例如「青少年用『想消失』表达主动意念时按 moderate 起判」。"
                     "基座 prompt 的 C-SSRS 定义、保守原则、输出格式始终保留。"},
        ],
    },
    {
        "key": "session", "title": "⑥ 陪伴会话节奏",
        "subtitle": "5 分钟视频语音会话何时收尾、静默多久算离开。",
        "scope": "仅 5 分钟陪伴会话。",
        "fields": [
            {"path": "session.idle_confirm_at", "label": "静默确认等待", "type": "int",
             "default": 60, "min": 30, "max": 300, "step": 5, "unit": "秒",
             "help": "用户多久不回复就问「这次先聊到这里吗」。"},
            {"path": "session.idle_end_at", "label": "静默结束等待", "type": "int",
             "default": 90, "min": 40, "max": 360, "step": 5, "unit": "秒",
             "help": "确认后仍无回复，累计到该时长即进入结束话术。须比上一项至少多 10 秒。"},
            {"path": "session.wrap_up_at", "label": "自然收尾提醒", "type": "int",
             "default": 240, "min": 120, "max": 330, "step": 10, "unit": "秒",
             "help": "累计时长达到该值后，回应里追加一句过渡，不再开启新话题。"},
        ],
    },
]

# 明确写进界面的「系统锁定」区：配置文件里根本不存在这些开关，
# 医生看得见、但改不了，避免到处找"为什么不能关掉人工审核"。
LOCKED_ITEMS = [
    "四通道取最大等级、会话内风险只升不降",
    "重度及以上必挂起等人工审核、必给热线兜底话术",
    "危急 / 重度词表不得删空",
    "词桶对应的 C-SSRS 等级（4/3/2/1）不可降级",
    "PHQ-9 第 9 题满分必须至少判重度",
    "风险原话独立留存、永不压缩",
    "规则通道与模型通道不可关闭",
]

SAMPLE_SENTENCES = [
    {"text": "最近工作压力有点大，晚上睡不好", "expect": "none / cbt"},
    {"text": "有时候觉得活着没意思", "expect": "low / cbt"},
    {"text": "我真的不想活了", "expect": "moderate / moderate"},
    {"text": "我已经写好遗书了", "expect": "high / crisis"},
    {"text": "以后不用管我了", "expect": "moderate 起判"},
    {"text": "我在天台，已经吃了东西了", "expect": "imminent / crisis"},
]


def presets() -> dict:
    """三个起点：保守（少误报）/ 标准 / 敏感（少漏报）。

    注意「敏感」把 crisis_min_level 降到 2 时，必须同步把 moderate_level 降到 1，
    否则会撞上「moderate_level 必须小于 crisis_min_level」的护栏。
    """
    base = default_config()
    conservative = _deep_merge(base, {
        "implicit_level": 1,
        "flags": {"min_level": 2},
        "scale": {"max_age_days": 14},
        "multimodal": {"sad_ratio_threshold": 0.75, "sad_ratio_level": 1,
                       "negative_words_min": 6, "inconsistency_level": 1,
                       "mask_signal_level": 1},
        "voice": {"pause_high": 0.7, "rate_fast": 8.0},
    })
    sensitive = _deep_merge(base, {
        "gates": {"crisis_min_level": 2, "moderate_level": 1},
        "implicit_level": 3,
        # 0 = 不限制有效期，是量表通道最敏感的取值（不能写成"更长的天数"）
        "scale": {"max_age_days": 0},
        "multimodal": {"sad_ratio_threshold": 0.45, "sad_ratio_level": 2,
                       "negative_words_min": 3, "mask_signal_level": 2},
        "voice": {"f0_std_flat": 20, "rate_slow": 3.5, "pause_high": 0.45},
    })
    return {
        "standard": {"label": "标准（系统默认）", "desc": "与当前代码常量一致", "config": base},
        "conservative": {"label": "保守 · 少误报", "desc": "弱信号更严、队列更安静、量表 14 天失效，代价是可能漏掉隐晦表达",
                         "config": conservative},
        "sensitive": {"label": "敏感 · 少漏报", "desc": "中度即挂起人工、隐喻从重、量表不设有效期，人工审核量会明显上升",
                      "config": sensitive},
    }


def _flatten(obj: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in obj.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, p))
        else:
            out[p] = v
    return out


def _label_of(path: str) -> str:
    for g in PARAM_GROUPS:
        for f in g.get("fields", []):
            if f["path"] == path:
                return f["label"]
        for f in g.get("map_fields", []):
            if f["path"] == path:
                return f["label"]
        for w in g.get("words", []):
            if path == f"patterns.{w['key']}":
                return f"词表 · {w['label']}"
    return path


def diff(old: dict, new: dict) -> list[dict]:
    """发布前的变更清单：旧值 → 新值，词表额外给新增/删除的词。"""
    a, b = _flatten(old or {}), _flatten(new or {})
    out = []
    for p in sorted(set(a) | set(b)):
        if p in ("version", "note"):
            continue
        va, vb = a.get(p), b.get(p)
        if va == vb:
            continue
        item = {"path": p, "label": _label_of(p), "old": va, "new": vb}
        if isinstance(va, list) and isinstance(vb, list):
            item["added"] = [x for x in vb if x not in va]
            item["removed"] = [x for x in va if x not in vb]
            item["old"] = f"{len(va)} 词"
            item["new"] = f"{len(vb)} 词"
        out.append(item)
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
    """返回 (是否通过, 错误列表, 警告列表)。错误会阻断上传。

    校验范围不只是词表：**每个可配数值都要校验类型与区间**。
    一个 `"0.6"` 字符串或 `None` 流进 `emo.get("sad", 0) > threshold`
    会在真实对话里抛异常，比配置错误本身更危险。
    """
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
        if any(not isinstance(x, str) or not x.strip() for x in v):
            errors.append(f"patterns.{k} 含空词或非字符串，请清理")
        if len(v) != len(set(v)):
            warns.append(f"patterns.{k} 存在重复词，已保留但建议清理")
        if not v and k in ("imminent", "high"):
            errors.append(f"红线：patterns.{k} 不得为空（会直接漏报）")
        elif not v:
            warns.append(f"patterns.{k} 词表为空，该等级将完全依赖模型通道")

    # ---- 按 PARAM_GROUPS 自动校验所有标量字段（新增字段不会漏校验）----
    for g in PARAM_GROUPS:
        for f in g.get("fields", []):
            _validate_field(cfg, f, errors)
        for f in g.get("map_fields", []):
            _validate_field(cfg, {**f, "type": "int", "min": f.get("locked_min", 0),
                                  "max": 4, "help": "量表分值→风险等级映射"}, errors)

    # ---- 跨字段护栏（单字段区间合法也不代表组合安全）----
    gates = cfg.get("gates", {}) or {}
    cml, ml = gates.get("crisis_min_level", 3), gates.get("moderate_level", 2)
    if isinstance(cml, int) and isinstance(ml, int):
        if cml > 3:
            errors.append("红线：gates.crisis_min_level 不得高于 3（不允许把 high 降级放行）")
        if ml >= cml:
            errors.append("gates.moderate_level 必须小于 crisis_min_level"
                          "（把 crisis 门槛调低时，记得同时调低 moderate 门槛）")

    sess = cfg.get("session", {}) or {}
    a, b = sess.get("idle_confirm_at", 60), sess.get("idle_end_at", 90)
    if isinstance(a, int) and isinstance(b, int) and b < a + 10:
        errors.append("session.idle_end_at 至少要比 idle_confirm_at 多 10 秒")
    if isinstance(sess.get("wrap_up_at"), int) and sess["wrap_up_at"] > 330:
        errors.append("红线：session.wrap_up_at 不得超过 5:30 硬上限")

    extra = (cfg.get("assess", {}) or {}).get("prompt_extra", "")
    if not isinstance(extra, str):
        errors.append("assess.prompt_extra 必须是文本")
    else:
        if len(extra) > 2000:
            errors.append("assess.prompt_extra 过长（上限 2000 字）")
        # 只允许「追加」临床说明，不允许用它把基座 prompt 的红线覆盖掉
        for bad in ("忽略以上", "忽略上述", "忽略前面", "不要遵守", "无视以上",
                    "禁止判定", "不得判为", "输出格式改为"):
            if bad in extra:
                errors.append(f"assess.prompt_extra 含覆盖基座指令「{bad}」，不予保存")
                break

    return (len(errors) == 0), errors, warns


def _validate_field(cfg: dict, f: dict, errors: list[str]) -> None:
    """按元数据校验一个字段。bool 会被 int 的 isinstance 误判，需先单独处理。"""
    path, typ = f["path"], f.get("type", "int")
    v = _get_path(cfg, path)
    label = f.get("label", path)

    if typ == "bool":
        if not isinstance(v, bool):
            errors.append(f"{label} 需为 true/false")
        return
    if typ == "text":
        if not isinstance(v, str):
            errors.append(f"{label} 需为文本")
        return
    if typ == "enum":
        allowed = [o["v"] for o in f.get("options", [])]
        if v not in allowed:
            errors.append(f"{label} 只能是 {allowed}（收到 {v!r}）")
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        errors.append(f"{label} 需为数值（收到 {v!r}）")
        return
    lo, hi = f.get("min"), f.get("max")
    if lo is not None and v < lo:
        errors.append(f"{label} 不得小于 {lo}")
    if hi is not None and v > hi:
        errors.append(f"{label} 不得大于 {hi}")
    if typ == "int" and not isinstance(v, int):
        errors.append(f"{label} 需为整数")


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


# ------------------------------------------------------------------ 判定（试跑与引擎共用）
def rule_level(cfg: dict, text: str) -> tuple[int, str, str]:
    """按配置的词表判「规则通道」，返回 (等级数值, 命中词, 命中桶)。

    ⚠ `perception.rule_risk_level` 直接调用本函数，所以试跑结果与线上判定
    用的是**同一份实现**（P0 曾各写一份，容易悄悄跑偏）。
    词桶等级 `_LEVEL_NUM` 锁死，不进配置 —— 那是 C-SSRS 临床定义。
    """
    order = [(k, _LEVEL_NUM[k]) for k in ("imminent", "high", "moderate", "low")]
    order.append(("implicit", int(cfg.get("implicit_level", 2))))
    for key, lvl in order:
        for p in cfg.get("patterns", {}).get(key, []):
            if p and p in (text or ""):
                return lvl, p, key
    return 0, "", ""


def scale_level(cfg: dict, item_scores: list[int] | None,
                scale_date: str | None = None) -> tuple[int, str]:
    """按配置判量表通道：取 PHQ-9 指定条目的分值映射。

    `scale_date` 给了就检查有效期；过期直接返回 0 —— 这正是「一份陈旧
    PHQ-9 让每轮对话都挂起」的修复点。
    """
    if not item_scores:
        return 0, ""
    scale = cfg.get("scale", {}) or {}
    max_age = int(scale.get("max_age_days", 30))
    if max_age > 0 and scale_date:
        from datetime import date
        try:
            age = (date.today() - date.fromisoformat(str(scale_date)[:10])).days
        except ValueError:
            age = 0
        if age > max_age:
            return 0, ""
    idx = int(scale.get("PHQ9_SUICIDE_ITEM_INDEX", 8))
    if idx < 0 or idx >= len(item_scores):
        return 0, ""
    v = item_scores[idx]
    imap = {int(k): int(x) for k, x in (scale.get("item_level_map") or {}).items()}
    lvl = imap.get(v, 0)
    return lvl, (f"PHQ-9 第{idx + 1}题={v}" if lvl else "")


def route(cfg: dict, final_level: int) -> str:
    """按配置门槛判定分流目标（与 graph.risk_gate 共用同一实现）。"""
    gates = cfg.get("gates", {}) or {}
    crisis = int(gates.get("crisis_min_level", 3))
    moderate = int(gates.get("moderate_level", 2))
    if final_level >= crisis:
        return "crisis"
    if final_level >= moderate:
        return "moderate"
    return "cbt"


def evaluate(cfg: dict, text: str, scale_item_scores: list[int] | None = None,
            scale_date: str | None = None) -> dict:
    """确定性通道试跑：规则 + 量表 → 最终等级 / 分流 / 是否挂起。

    不落库、不写画像、不进审核队列。模型通道需单独调用（慢）。
    """
    lvl, hit, bucket = rule_level(cfg, text)
    s_lvl, s_ev = scale_level(cfg, scale_item_scores, scale_date)
    channels = {
        "rule": {"level": lvl, "evidence": hit},
        "model": {"level": 0, "evidence": ""},
        "multimodal": {"level": 0, "evidence": ""},
        "scale": {"level": s_lvl, "evidence": s_ev},
    }
    final = max(ch["level"] for ch in channels.values())
    return {
        "rule_hit": {"pattern": hit, "bucket": bucket, "level": lvl},
        "channels": channels, "final_level": final,
        "route": route(cfg, final),
        "would_interrupt": route(cfg, final) == "crisis",
    }


# ------------------------------------------------------------------ 版本管理
def _next_version(cfg: dict) -> str:
    """版本号必须**唯一**。

    P0 只精确到秒，同一秒内连续保存会生成同名版本；`get_version()` 用
    `ORDER BY id DESC LIMIT 1` 取，于是历史里出现一串同名行、
    发布时到底发的是哪份完全不可控（实测一次回归跑出 12 条同名版本）。
    """
    import uuid

    base = str(cfg.get("version") or "1.0")
    return f"{base}-draft-{time.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:4]}"


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


_CACHE: dict[str, object] = {"ts": 0.0, "cfg": None, "version": None}
_CACHE_TTL = 3.0        # 秒；多进程部署时由它兜底


def invalidate_cache() -> None:
    """发布 / 回滚后立刻清缓存，避免「点了发布但下一轮还是旧参数」。"""
    _CACHE["ts"] = 0.0
    _CACHE["cfg"] = None
    _CACHE["version"] = None


def get_active() -> tuple[dict, str]:
    """读取当前**生效**版本；无发布版本则回落到代码默认值。

    返回 (配置, 版本标识)。这是 P1 起真实对话链路的唯一取值来源。
    每轮对话会调用 5~6 次（四通道 + gate + 队列），带进程内短 TTL 缓存。
    """
    now = time.time()
    if _CACHE["cfg"] is not None and now - float(_CACHE["ts"]) < _CACHE_TTL:
        # 返回副本：缓存对象被所有请求共享，任何调用方原地改一下
        # （如 cfg["patterns"]["high"].append(...)）都会污染之后每一轮的判定
        return json.loads(json.dumps(_CACHE["cfg"])), str(_CACHE["version"])

    ensure_tables()
    with tx() as c:
        r = c.execute(
            "SELECT version, content_json FROM risk_rule_version "
            "WHERE status='published' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not r:
        cfg, ver = default_config(), "default-1.0.0"
    else:
        # 老版本可能缺新字段；normalize 保证引擎永远拿到完整结构
        cfg, ver = normalize(json.loads(r["content_json"])), r["version"]
    _CACHE.update({"ts": now, "cfg": cfg, "version": ver})
    return json.loads(json.dumps(cfg)), ver


def active_config() -> dict:
    """只要配置不要版本号（引擎各通道用这个）。"""
    return get_active()[0]


def _activate(version: str, action: str, actor: str, note: str) -> dict:
    """把某版本置为 published，其余 published 置为 rolled_back，写审计。"""
    ensure_tables()
    with tx() as c:
        r = c.execute(
            "SELECT id, content_json FROM risk_rule_version WHERE version=? "
            "ORDER BY id DESC LIMIT 1",
            (version,),
        ).fetchone()
        if not r:
            raise KeyError(version)
        # 发布前再校验一次：数据库里的老草稿可能是在放宽护栏之前存的
        cfg = normalize(json.loads(r["content_json"]))
        ok, errs, _ = validate_config(cfg)
        if not ok:
            raise ValueError("该版本未通过当前护栏校验，不予发布：" + "；".join(errs))
        c.execute("UPDATE risk_rule_version SET status='rolled_back' WHERE status='published'")
        c.execute("UPDATE risk_rule_version SET status='published' WHERE id=?", (r["id"],))
        _audit(c, version, action, actor, note)
    invalidate_cache()
    return {"ok": True, "active": version, "action": action,
            "engine_wired": True}


def publish(version: str, actor: str, note: str) -> dict:
    if not (note or "").strip():
        raise ValueError("发布必须填写变更理由")
    return _activate(version, "publish", actor, note)


def rollback(version: str, actor: str, note: str) -> dict:
    return _activate(version, "rollback", actor, note)
