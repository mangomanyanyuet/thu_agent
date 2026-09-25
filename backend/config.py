"""全局配置。

所有与安全相关的常量（热线号码、风险词表、阈值）都集中在此处，
生产环境应改为从数据库 / 配置中心读取，避免硬编码在 prompt 里。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    # ---- 服务 ----
    app_name: str = "MindTrace Agent"
    api_prefix: str = "/api"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # ---- 存储 ----
    # 用 sqlite:///path 或 postgresql://user:pwd@host/db
    database_url: str = f"sqlite:///{DATA_DIR / 'mindtrace.db'}"
    # LangGraph checkpointer：memory | sqlite | postgres
    checkpointer: str = "sqlite"
    checkpoint_conn: str = str(DATA_DIR / "checkpoints.sqlite")

    # ---- 模型 ----
    # 不配置真实 key 时自动降级到 MockLLM，保证骨架可离线跑通
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")  # mock | openai
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    llm_temperature: float = 0.4

    # ---- HY（太极平台 api.taiji.woa.com）----
    hy_api_key: str = os.getenv(
        "HY_API_KEY", "9d3df25d-89f5-45c0-8075-5a4ea28c31e6")
    hy_base_url: str = os.getenv("HY_BASE_URL", "http://api.taiji.woa.com")
    # 平台有两个端点，hy3 走 openapi/v2，外部模型走 external/v1
    hy_endpoint: str = os.getenv("HY_ENDPOINT", "/openapi/v2/chat/completions")
    hy_model: str = os.getenv("HY_MODEL", "hy3")
    # 客户端限速。实测（2026-08-29）连发 8 次 / 9.5s 未触发 429，
    # 脚本注释里的"10 QPM"并不准确；这里留 1s 余量即可，过大会白白拉长等待。
    hy_min_interval: float = float(os.getenv("HY_MIN_INTERVAL", "1.0"))
    hy_timeout: int = int(os.getenv("HY_TIMEOUT", "120"))

    # ---- vLLM（内网 Qwen 等，OpenAI 兼容 /v1）----
    vllm_base_url: str = os.getenv("VLLM_BASE_URL", "http://29.77.193.107:9000")
    vllm_endpoint: str = os.getenv("VLLM_ENDPOINT", "/v1/chat/completions")
    vllm_model: str = os.getenv("VLLM_MODEL", "qwen3-14b-grpo")
    vllm_api_key: str = os.getenv("VLLM_API_KEY", "")
    vllm_timeout: int = int(os.getenv("VLLM_TIMEOUT", "90"))
    vllm_max_tokens: int = int(os.getenv("VLLM_MAX_TOKENS", "512"))
    # 对话生成是否开启思考模式。关闭可省约 60% 延迟（3854ms -> 1069ms）。
    vllm_thinking_for_reply: bool = os.getenv(
        "VLLM_THINKING_FOR_REPLY", "0").lower() in ("1", "true", "yes")

    # 评估/分级（structured）是否开启思考模式。默认**关闭**，理由（实测 2026-09）：
    #   开思考时思考长度不可控 —— max_tokens=512 时 3/3 全部截断失败，
    #   1024 时 1/3 失败，1536 时 2/3 失败且平均 12s。
    #   即：开思考 + 512 的生产配置下，模型通道 100% 失效并静默回落到 mock，
    #   那 5.9s 是纯浪费。关闭后 3/3 解析成功、平均 3.9s，模型通道才真正生效。
    # 代价：对极隐晦表达（反讽、未登录词表的托付式表达）的召回弱于开思考。
    # 缓解：① 规则通道必须兜底（已修 RISK_PATTERNS_IMPLICIT 未生效的 bug）
    #       ② 三通道并集只升不降  ③ 扩充规则词表（零成本）
    vllm_thinking_for_assess: bool = os.getenv(
        "VLLM_THINKING_FOR_ASSESS", "0").lower() in ("1", "true", "yes")
    # 本地推理无平台限流，留极小间隔即可
    vllm_min_interval: float = float(os.getenv("VLLM_MIN_INTERVAL", "0.2"))

    # ---- 账号体系（演示级，见 auth.py 顶部的安全说明）----
    # 是否要求登录才能访问 /api/*（关闭则退回"客户端自报 user_id"的 demo 模式）
    require_auth: bool = os.getenv("REQUIRE_AUTH", "1").lower() in ("1", "true", "yes")
    # 首次启动自动创建的演示账号；账号表非空时不再创建
    demo_username: str = os.getenv("DEMO_USERNAME", "demo")
    demo_password: str = os.getenv("DEMO_PASSWORD", "demo1234")
    demo_display_name: str = os.getenv("DEMO_DISPLAY_NAME", "演示用户")

    # ---- 陪伴会话的文本情绪判定方式 ----
    # 1（默认）：在生成回复的同一次 LLM 调用里，让模型额外输出一段机器可读的
    #   情绪判定（不展示给用户）。不增加调用次数，但 prompt + 输出变长，
    #   实测单轮 +1~2s；换来的是关键词覆盖不到的表达也能判对。
    # 0：退回关键词匹配（快，但"心里空落落的"这类句子会判成平静）。
    companion_llm_emotion: bool = os.getenv(
        "COMPANION_LLM_EMOTION", "1").lower() in ("1", "true", "yes")

    # ---- 可观测：结构化事件日志（见 tracer.py）----
    trace_enabled: bool = os.getenv("TRACE_ENABLED", "1").lower() in ("1", "true", "yes")
    # 落盘到 data/traces/agent-YYYYMMDD.jsonl
    trace_to_file: bool = os.getenv("TRACE_TO_FILE", "1").lower() in ("1", "true", "yes")
    # 同时打印到控制台（tail 后端日志时也能看到）
    trace_echo: bool = os.getenv("TRACE_ECHO", "0").lower() in ("1", "true", "yes")
    # 用户原话在日志里的截断长度（风险证据不截断，安全审计需要原话）
    trace_text_max_len: int = int(os.getenv("TRACE_TEXT_MAX_LEN", "120"))

    # ---- 上下文预算（token 近似按字符/3 估算）----
    ctx_max_chars: int = 12000
    ctx_recent_turns: int = 8
    # 每 N 轮做一次事实抽取（用户事实是慢变量，不必每轮重抽）
    fact_extract_every: int = int(os.getenv("FACT_EXTRACT_EVERY", "3"))

    # ---- 危机热线（按上线地区配置，务必定期核验）----
    hotline_national: str = "12356"
    hotline_emergency_medical: str = "120"
    hotline_emergency_police: str = "110"
    hotlines_extra: str = "北京心理危机干预中心 010-82951332|希望24热线 400-161-9995"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------- 风险规则（规则通道，零漏报底线） ----------------
# 分级：0 none / 1 low / 2 moderate / 3 high / 4 imminent
RISK_LEVELS = ["none", "low", "moderate", "high", "imminent"]

# 级别 4：正在实施 / 手段在手 / 即刻意图
RISK_PATTERNS_IMMINENT = [
    "正在", "马上就", "现在就要", "已经吃了", "已经吞", "已经买了",
    "站在楼顶", "在天台", "手腕已经", "准备好了一切", "最后一次",
]
# 级别 3：有计划 / 有准备 / 有意图
RISK_PATTERNS_HIGH = [
    "打算自杀", "准备自杀", "计划好了", "写遗书", "写好遗书", "写了遗书",
    "遗嘱", "想好了怎么", "方法都想好了", "什么时候去死", "决定去死",
    "安排后事", "把东西都送人", "想好怎么死",
]
# 级别 2：主动自杀意念（无计划）
RISK_PATTERNS_MODERATE = [
    "想自杀", "想死", "自杀", "结束生命", "不想活了", "活不下去",
    "死了算了", "想结束一切", "希望自己消失", "一了百了", "轻生",
]
# 级别 1：被动死亡愿望 / 无意义感
RISK_PATTERNS_LOW = [
    "活着没意思", "活着没有意义", "还不如死了", "好累想睡很久",
    "希望睡着不要再醒", "没有人在乎我", "我是负担", "拖累别人",
    "不如消失", "没意义",
]

# 告别式 / 隐喻表达（弱信号，需模型通道兜底）
RISK_PATTERNS_IMPLICIT = [
    "再见了", "最后一次说", "谢谢你们照顾", "以后不用管我了",
    "你们会过得更好", "帮我把东西转交", "记得替我",
]

# 保护性因素（出现时提示降级参考，但不覆盖升级规则）
PROTECTIVE_PATTERNS = [
    "家人", "朋友", "孩子", "宠物", "不会做", "舍不得", "还有人需要我",
]

# ---------------- 文本情绪倾向词表（正负向） ----------------
# ⚠ 历史问题：这两张表曾分散维护 **4 份**，口径互不相同：
#     llm.MockLLM（情绪类别映射）、perception.analyze_narrative_stub（内联）、
#     perception.text_tone_of、companion.daily_bridge（自述分析）。
#   后果是同一句话在不同链路判出不同倾向，最直接的影响是「语气低落 +
#   文本偏正面」的掩饰信号（voice_risk_signal 的 2 级升级依据）随链路时而丢失。
#
# 这里取**并集**统一：正负向识别都更全，任何一条链路漏掉的词在其他链路仍能命中。
POSITIVE_WORDS = (
    "挺好", "还好", "不错", "开心", "高兴", "顺利", "喜欢", "期待", "谢谢",
    "还行", "可以", "没事", "很好", "放松",
)
NEGATIVE_WORDS = (
    "难过", "累", "烦", "焦虑", "失眠", "压力", "害怕", "孤独",
    "没意思", "不想", "难受", "低落", "绝望", "委屈", "没用", "做不好",
)

# ---------------- 关键词情绪映射（仅供 mock 兜底） ----------------
# 用途与上面两张表不同：这里要的是"情绪类别"而非正负倾向，所以单独一张。
# 仅 llm.MockLLM 使用（模型不可用时的确定性兜底）。
# ⚠ 遍历顺序即优先级：后命中的词覆盖先命中的，调整顺序会改变 mock 输出。
EMOTION_KEYWORD_MAP: dict[str, str] = {
    "难过": "低落", "悲伤": "低落", "哭": "低落", "低落": "低落",
    "沮丧": "低落", "失望": "低落", "孤独": "低落", "委屈": "低落",
    "提不起劲": "疲惫", "没意思": "低落", "不想活": "低落",
    "焦虑": "焦虑", "紧张": "紧张", "担心": "焦虑", "压力": "焦虑",
    "压力大": "焦虑", "睡不着": "焦虑", "失眠": "焦虑", "睡不好": "焦虑",
    "害怕": "紧张", "恐慌": "紧张", "恐惧": "紧张",
    "生气": "烦躁", "愤怒": "烦躁", "烦躁": "烦躁", "烦": "烦躁",
    "内疚": "低落", "丢脸": "低落", "自责": "低落", "负担": "低落",
    "麻木": "低落", "没感觉": "低落", "空的": "低落",
    "开心": "愉悦", "高兴": "愉悦", "不错": "愉悦", "挺好": "愉悦",
    "还好": "平静", "平静": "平静", "放松": "平静",
    "累": "疲惫", "疲惫": "疲惫", "没劲": "疲惫", "撑不住": "疲惫",
}
