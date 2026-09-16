"""LLM 适配层。

对外只暴露两个能力：
  - structured(model, system, user) -> Pydantic 对象（结构化输出，用于评估/分级/抽取）
  - stream(system, messages)        -> 文本流（用于对话生成）

无 API Key 时自动使用 MockLLM，保证骨架离线可跑通、可演示、可测试。
接入真实模型只需设置 LLM_PROVIDER=openai 与 OPENAI_API_KEY。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from typing import TypeVar

from config import get_settings
from models import (
    EmotionAssessment,
    EmotionLabel,
    ExtractedFacts,
    RiskAssessment,
    RiskLevel,
    TurnAssessment,
)
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


# ------------------------------------------------------------------ Mock
class MockLLM:
    """离线兜底实现：规则 + 启发式，保证任何环境下都有确定行为。"""

    def structured(self, schema: type[T], system: str, user: str) -> T:
        text = user.lower()
        if schema is RiskAssessment:
            return _mock_risk(text)  # type: ignore[return-value]
        if schema is EmotionAssessment:
            return _mock_emotion(text)  # type: ignore[return-value]
        if schema is ExtractedFacts:
            return _mock_facts(user)  # type: ignore[return-value]
        if schema is TurnAssessment:
            return _mock_assessment(text)  # type: ignore[return-value]
        raise LLMError(f"MockLLM 未支持的 schema: {schema}")

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        last = messages[-1]["content"] if messages else ""
        risk = _mock_risk(last.lower()).level
        if risk in (RiskLevel.high, RiskLevel.imminent):
            return _crisis_reply()
        return _cbt_reply(last)

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        for ch in self.complete(system, messages):
            yield ch


# 值统一为 EmotionLabel 的中文标签（与陪伴会话同一套口径）
NEG_WORDS = {
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


# 与 companion/fusion.py 的 EMOTION_PROTOTYPE 保持一致的原型值
_MOCK_VALENCE = {"愉悦": 0.7, "平静": 0.15, "焦虑": -0.55, "低落": -0.70,
                 "烦躁": -0.50, "疲惫": -0.35, "紧张": -0.40, "惊讶": 0.0}


def _mock_emotion(text: str) -> EmotionAssessment:
    label, hits = EmotionLabel.calm, 0
    for w, lab in NEG_WORDS.items():
        if w in text:
            label, hits = EmotionLabel(lab), hits + 1
    intensity = min(10, 3 + hits * 2)
    valence = _MOCK_VALENCE.get(label.value, 0.0)
    # arousal 量纲 [-1,1]，与陪伴侧一致
    arousal = 0.6 if label.value in ("焦虑", "紧张", "烦躁", "惊讶") else -0.3
    themes = [t for t in ["工作", "学习", "睡眠", "亲密关系", "家庭", "身体", "经济"] if t in text]
    distortions = [d for d in ["灾难化", "全或无", "读心", "过度概括", "情绪化推理"]
                   if d in text]
    return EmotionAssessment(
        primary_emotion=label, intensity=intensity, valence=valence,
        arousal=arousal, emotion_confidence=0.6 if hits else 0.3,
        themes=themes, cognitive_distortions=distortions,
        rationale="(mock) 关键词启发式判定",
    )


def _mock_risk(text: str) -> RiskAssessment:
    from config import (
        RISK_PATTERNS_HIGH,
        RISK_PATTERNS_IMMINENT,
        RISK_PATTERNS_IMPLICIT,
        RISK_PATTERNS_LOW,
        RISK_PATTERNS_MODERATE,
    )

    def hit(pats):
        for p in pats:
            if p in text:
                return p
        return ""

    if e := hit(RISK_PATTERNS_IMMINENT):
        return RiskAssessment(level=RiskLevel.imminent, has_ideation=True, has_plan=True,
                              has_preparation=True, has_intent=True, evidence=e, confidence=0.9)
    if e := hit(RISK_PATTERNS_HIGH):
        return RiskAssessment(level=RiskLevel.high, has_ideation=True, has_plan=True,
                              evidence=e, confidence=0.85)
    if e := hit(RISK_PATTERNS_MODERATE):
        return RiskAssessment(level=RiskLevel.moderate, has_ideation=True, evidence=e, confidence=0.8)
    if e := hit(RISK_PATTERNS_LOW):
        return RiskAssessment(level=RiskLevel.low, evidence=e, confidence=0.7)
    if e := hit(RISK_PATTERNS_IMPLICIT):
        return RiskAssessment(level=RiskLevel.moderate, evidence=e, confidence=0.5)
    return RiskAssessment(level=RiskLevel.none, confidence=0.6)


def _mock_assessment(text: str) -> TurnAssessment:
    """合并评估的 mock：情绪启发式 + 规则风险引擎。"""
    emo = _mock_emotion(text)
    risk = _mock_risk(text)
    return TurnAssessment(
        primary_emotion=emo.primary_emotion, intensity=emo.intensity,
        valence=emo.valence, arousal=emo.arousal,
        emotion_confidence=emo.emotion_confidence, themes=emo.themes,
        cognitive_distortions=emo.cognitive_distortions,
        emotion_rationale=emo.rationale,
        risk_level=risk.level, has_ideation=risk.has_ideation, has_plan=risk.has_plan,
        has_preparation=risk.has_preparation, has_intent=risk.has_intent,
        protective_factors=risk.protective_factors, risk_evidence=risk.evidence,
        risk_confidence=risk.confidence, risk_rationale="(mock) 规则引擎判定",
    )


def _mock_facts(text: str) -> ExtractedFacts:
    facts, trig = [], []
    for kw in ["猫", "狗", "宠物", "妈妈", "爸爸", "男朋友", "女朋友", "室友", "领导", "同事"]:
        if kw in text:
            facts.append(f"提到{kw}")
    for kw in ["被批评", "吵架", "分手", "裁员", "考试", "失眠", "加班"]:
        if kw in text:
            trig.append(kw)
    return ExtractedFacts(profile_facts=facts[:3], triggers=trig[:3])


def _crisis_reply() -> str:
    s = get_settings()
    return (
        "听到你这么说，我很担心你现在的安全。\n\n"
        "请先不要一个人待着，如果此刻有危险，请立刻拨打 "
        f"{s.hotline_emergency_medical} 或 {s.hotline_emergency_police}。\n"
        f"你也可以随时拨打心理援助热线 {s.hotline_national}，有人会接住你。\n\n"
        "我已经把情况通知了值班老师，很快会有人来陪你。你愿意现在告诉我，"
        "身边有没有可以联系的人吗？"
    )


CBT_FLOW = [
    "谢谢你愿意跟我说这些。我听到你说的是，这件事让你觉得很难熬。",
    "如果用一两个词来形容现在的感受，你会用什么？",
    "能具体说说，这种感觉最强烈是在什么时候、和谁在一起的时候吗？",
    "那一刻，你脑子里最先冒出来的想法是什么？",
    "我们一起来看看：有什么证据支持这个想法，又有什么证据反对它？",
    "如果换成你最要好的朋友遇到同样的事，你会怎么跟他说？",
    "所以除了最初那个想法，还有没有别的可能的解释？",
    "今天有没有一件很小、但你能做到的事，可以让自己稍微好受一点？",
    "今天我们先聊到这里。我把我们说的记下来了，下次可以接着往下走。",
]


def _cbt_reply(user_text: str) -> str:
    idx = min(len(user_text) // 12, len(CBT_FLOW) - 1)
    return CBT_FLOW[idx]


# ------------------------------------------------------------------ OpenAI
class OpenAILLM:
    def __init__(self) -> None:
        from openai import OpenAI  # 延迟导入，未安装也不影响 mock 模式

        s = get_settings()
        self.model = s.llm_model
        self.client = OpenAI(api_key=s.openai_api_key, base_url=s.openai_base_url or None)

    def structured(self, schema: type[T], system: str, user: str) -> T:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        return schema.model_validate(data)

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=get_settings().llm_temperature,
            messages=[{"role": "system", "content": system}, *messages],
        )
        return resp.choices[0].message.content or ""

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=get_settings().llm_temperature,
            stream=True,
            messages=[{"role": "system", "content": system}, *messages],
        )
        for chunk in resp:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta


# ------------------------------------------------------------------ HY（太极平台）
class HyLLM:
    """太极平台 hy3 适配。

    实测结论（2026-08-29，务必保留这些坑的处理）：
    1. **不能用 response_format=json_object** —— hy3 在该模式下会输出被转义/损坏的
       JSON（如 `{\\"emotion\\":...}otion": ...`）。改为在 prompt 里声明 schema，
       再用 _extract_json 从返回文本里稳健提取。
    2. hy3 是**推理模型**，返回独立的 `reasoning_content`，且 reasoning_tokens 占
       总量 90%+。因此：不要把 reasoning_content 回填进对话历史（会污染上下文、
       放大 token 成本）。
    3. **SSE 流式会出现 choices 为空的 chunk**，直接取 choices[0] 会 IndexError。
    4. 限流较紧（约 10 QPM），需要客户端限速 + 429 退避。
    """

    JSON_HINT = (
        "\n\n【输出格式要求】只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码围栏、"
        "不要前后缀文字。字段如下：\n{schema}\n"
    )

    def __init__(self) -> None:
        import requests  # 延迟导入

        self._requests = requests
        s = get_settings()
        self.model = s.hy_model
        self.base_url = s.hy_base_url.rstrip("/")
        self.endpoint = s.hy_endpoint
        self.api_key = s.hy_api_key
        self.min_interval = s.hy_min_interval
        self.timeout = s.hy_timeout
        self._last_call = 0.0
        self._lock = threading.Lock()

    # -- 限速：同一进程内串行化，保证两次调用间隔 >= min_interval --
    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _post(self, payload: dict, stream: bool):
        url = f"{self.base_url}{self.endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_exc = None
        for attempt in range(3):
            self._throttle()
            try:
                resp = self._requests.post(
                    url, headers=headers, json=payload,
                    stream=stream, timeout=self.timeout,
                )
            except Exception as exc:  # 网络层异常
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue

            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"[hy 429 限流，{wait}s 后重试...]", flush=True)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        raise LLMError(f"HY 调用失败（含重试）：{last_exc}")

    def _payload(self, system: str, messages: list[dict[str, str]], stream: bool) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": stream,
        }

    def structured(self, schema: type[T], system: str, user: str) -> T:
        hint = self.JSON_HINT.format(schema=_compact_schema(schema))
        resp = self._post(
            self._payload(system, [{"role": "user", "content": user + hint}], False),
            stream=False,
        )
        content = resp.json()["choices"][0]["message"].get("content") or ""
        data = _extract_json(content)
        if data is None:
            raise LLMError(f"HY 未返回可解析的 JSON: {content[:200]!r}")
        return schema.model_validate(data)

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        resp = self._post(self._payload(system, messages, False), stream=False)
        return resp.json()["choices"][0]["message"].get("content") or ""

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        resp = self._post(self._payload(system, messages, True), stream=True)
        for line in resp.iter_lines():
            if not line:
                continue
            s = line.decode("utf-8")
            if not s.startswith("data: "):
                continue
            data = s[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            # 关键：hy3 会推送 choices 为空的 chunk（如 usage 统计帧）
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {}).get("content") or ""
            if delta:
                yield delta


def _compact_schema(schema: type[BaseModel]) -> str:
    """把 Pydantic 模型压成一行一个字段的说明。

    不用 model_json_schema()：它会带 $defs/$ref 嵌套（枚举尤其啰嗦），
    既费 token 又容易让模型输出错结构。
    """
    lines = []
    for name, field in schema.model_fields.items():
        ann = field.annotation
        origin = getattr(ann, "__origin__", None)
        if origin in (list, set, tuple):
            args = getattr(ann, "__args__", ())
            inner = getattr(args[0], "__name__", str(args[0])) if args else "str"
            # 枚举的可选值直接列出来，模型更好遵循
            choices = _enum_values(args[0]) if args else ""
            t = f"array<{inner}>" + (f"（可选值：{choices}）" if choices else "")
        elif _enum_values(ann):
            t = f"string（可选值：{_enum_values(ann)}）"
        else:
            t = getattr(ann, "__name__", str(ann))
        desc = field.description or ""
        default = "" if field.is_required() else f"，默认{field.default!r}"
        lines.append(f'  "{name}": <{t}>  # {desc}{default}')
    return "{\n" + "\n".join(lines) + "\n}"


def _enum_values(ann) -> str:
    """取枚举/字面量的可选值，返回 'a|b|c' 形式。"""
    if ann is None:
        return ""
    values = getattr(ann, "__args__", None)
    if values and str(getattr(ann, "__origin__", "")) == "typing.Literal":
        return "|".join(str(v) for v in values)
    if hasattr(ann, "__members__"):
        return "|".join(str(m.value) for m in ann.__members__.values())
    return ""


def _extract_json(text: str) -> dict | None:
    """从模型输出中稳健提取 JSON 对象（模型常带围栏 / 前后缀文字）。"""
    if not text:
        return None
    cleaned = text.strip()
    # 去掉 Markdown 代码围栏
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    for candidate in (cleaned, _slice_json(cleaned)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _slice_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return ""
    return text[start:end + 1]


# ------------------------------------------------------------------ vLLM（本地/内网 Qwen）
class VLLMLlm:
    """vLLM 部署的模型（如 qwen3-14b-grpo）。

    实测结论（2026-08-29，29.77.193.107:9000）：
    1. **极快**：单轮 0.2~2s，比 hy3 快约 50 倍，完全不需要担心限流。
    2. **该模型被 GRPO 训练成总是输出 JSON**，无论 system 里怎么禁止都无效，
       且包裹的 key 不固定（`response` / `content` / `question` /
       `{"type":"text","content":...}`）。所以对话生成后必须 unwrap。
    3. **list 字段会退化成字符串**（如 themes="工作/睡眠"），直接 model_validate
       会失败并回落到 mock，因此必须做类型强制。
    """

    JSON_HINT = (
        "\n\n【输出格式要求】只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码围栏、"
        "不要前后缀文字。字段如下：\n{schema}\n"
        "注意：themes/cognitive_distortions/protective_factors 这类字段必须是数组，"
        "多个值用数组表示，不要写成用斜杠分隔的字符串；没有内容就给空数组 []。"
    )
    # 解包时优先取这些 key（按模型实际出现的频率排序）
    UNWRAP_KEYS = ("response", "content", "text", "reply", "message",
                   "answer", "question", "output")

    def __init__(self) -> None:
        import requests

        self._requests = requests
        s = get_settings()
        self.model = s.vllm_model
        self.base_url = s.vllm_base_url.rstrip("/")
        self.endpoint = s.vllm_endpoint
        self.api_key = s.vllm_api_key
        self.timeout = s.vllm_timeout
        self.min_interval = s.vllm_min_interval
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _list_models(self) -> list[str]:
        try:
            r = self._requests.get(f"{self.base_url}/v1/models", timeout=10)
            if r.status_code == 200:
                return [m["id"] for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            pass
        return []

    def _resolve_model(self) -> None:
        """模型名对不上时自动改用服务上实际存在的模型。

        实测踩过：vLLM 服务被换成 Qwen3.8-27B-FP8 后，配置里仍是
        qwen3-14b-grpo，每次请求 404 -> 抛异常 -> 静默回落到 MockLLM，
        表现为「三轮回复一模一样」却没有任何报错。这里主动探测并修正，
        同时打日志，避免再次出现无声降级。
        """
        try:
            available = self._list_models()
        except Exception:  # noqa: BLE001
            return
        if not available or self.model in available:
            return
        old, self.model = self.model, available[0]
        print(f"[vllm] 配置的模型 {old!r} 在服务上不存在，"
              f"已自动改用 {self.model!r}（可用：{available}）")

    def _post(self, payload: dict, stream: bool):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_exc = None
        for attempt in range(3):
            self._throttle()
            try:
                resp = self._requests.post(f"{self.base_url}{self.endpoint}",
                                           headers=headers, json=payload,
                                           stream=stream, timeout=self.timeout)
            except Exception as exc:
                last_exc = exc
                time.sleep(1 * (attempt + 1))
                continue
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 404 and attempt == 0:
                # 模型名不对 -> 自动修正后重试
                self._resolve_model()
                payload["model"] = self.model
                continue
            if resp.status_code >= 500:
                time.sleep(1 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        raise LLMError(f"vLLM 调用失败（含重试）：{last_exc}")

    def _payload(self, system: str, messages: list[dict[str, str]], stream: bool,
                 thinking: bool = True) -> dict:
        p = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": get_settings().llm_temperature,
            "max_tokens": get_settings().vllm_max_tokens,
            "stream": stream,
        }
        # Qwen3 系列支持该参数关闭思考模式。实测（2026-09，kaiwu-llm-model）：
        #   开思考 3854ms / 341 completion tokens（其中约 519 字符是思考）
        #   关思考 1069ms /  88 completion tokens      —— 快 3.6 倍
        # 但**风险判定绝不能关**：实测关闭后「帮我把东西转交，我可能用不上了」
        # 这类托付式表达从 moderate 掉到 none，属于漏报（红线）。
        # 因此只在对外的对话生成上关闭，评估/分级保持开启。
        if not thinking:
            p["chat_template_kwargs"] = {"enable_thinking": False}
        return p

    def structured(self, schema: type[T], system: str, user: str,
                   thinking: bool | None = None) -> T:
        # 默认取配置（vllm_thinking_for_assess）。
        # 该默认值的来由见 config.py 的同名注释，核心是：开思考时思考长度
        # 不可控（实测 1536 token 仍截断），导致 JSON 解析失败率 33%~100%，
        # 反而让模型通道彻底失效并静默回落到 mock。
        if thinking is None:
            thinking = get_settings().vllm_thinking_for_assess
        hint = self.JSON_HINT.format(schema=_compact_schema(schema))
        resp = self._post(
            self._payload(system, [{"role": "user", "content": user + hint}], False,
                          thinking=thinking),
            stream=False)
        content = resp.json()["choices"][0]["message"].get("content") or ""
        data = _extract_json(content)
        if data is None:
            raise LLMError(f"vLLM 未返回可解析的 JSON: {content[:200]!r}")
        return schema.model_validate(_coerce(schema, data))

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        resp = self._post(self._payload(system, messages, False,
                                        thinking=get_settings().vllm_thinking_for_reply),
                          stream=False)
        raw = resp.json()["choices"][0]["message"].get("content") or ""
        return unwrap_reply(raw)

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        resp = self._post(self._payload(system, messages, True,
                                        thinking=get_settings().vllm_thinking_for_reply),
                          stream=True)
        buf = ""
        for line in resp.iter_lines():
            if not line:
                continue
            s = line.decode("utf-8")
            if not s.startswith("data: "):
                continue
            data = s[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            buf += choices[0].get("delta", {}).get("content") or ""
        # 该模型一定包 JSON，边流边解包没有意义；生成只要 0.3s，整体解包后吐出即可
        yield unwrap_reply(buf)


_LIST_EMPTY = {"", "无", "没有", "未提及", "未提到", "none", "null", "n/a", "暂无"}


def _split_list(v: str) -> list[str]:
    import re
    parts = re.split(r"[/、,，;；|\s]{1,}", v.strip())
    return [p.strip() for p in parts if p.strip() and p.strip() not in _LIST_EMPTY]


def _coerce(schema: type[BaseModel], data: dict) -> dict:
    """把模型输出的松散类型强制成 schema 期望的类型。

    qwen3-14b-grpo 实测会把 list 字段输出成 "工作/睡眠" 这样的字符串，
    直接 validate 会失败 → 触发 mock 降级，等于白调一次模型。
    """
    out = dict(data)
    for name, field in schema.model_fields.items():
        if name not in out:
            continue
        v = out[name]
        origin = getattr(field.annotation, "__origin__", None)
        if origin in (list, set, tuple) and isinstance(v, str):
            out[name] = _split_list(v)
        elif origin in (list, set, tuple) and v is None:
            out[name] = []
    return out


# ------------------------------------------------------------------ 回复清洗
# 推理型 / GRPO 模型会把思考过程直接吐进回复。实测（2026-09）vLLM 上的 Qwen 会输出：
#   "我们需要回答用户中文。用户说…目标：…</think>\n\n听起来最近被工作压得…"
# 其中 </think> 是**孤立闭合标签**（没有对应的 <think>），必须用单独规则处理。
_THINK_RE = re.compile(r"<(think|reasoning|analysis)>.*?</\1>", re.S | re.I)
_THINK_OPEN_RE = re.compile(r"<(think|reasoning|analysis)>", re.I)
_THINK_CLOSE_RE = re.compile(r"</(think|reasoning|analysis)>", re.I)

# 模型把「任务分析」当成回复输出时的特征词（Qwen 系列实测高发）
_LEAK_MARKERS = (
    "我们需要回答", "我需要回答", "用户说", "用户提到", "上一轮", "本轮目标",
    "目标：", "策略", "思考：", "分析：", "让我先", "首先我需要",
    "接下来我要", "我会这样", "注意：", "风险 none", "风险等级",
)


def strip_thinking(text: str) -> str:
    """去掉思考块，含「只有闭合标签」和「只有开始标签」两种残缺形态。"""
    text = _THINK_RE.sub("", text)
    # 孤立闭合标签：丢弃它之前的全部内容（那一段都是思考）
    while True:
        m = _THINK_CLOSE_RE.search(text)
        if not m:
            break
        head_open = _THINK_OPEN_RE.search(text[:m.start()])
        if head_open:
            break  # 前面的开标签没被匹配到说明格式异常，交给下一轮处理
        text = text[m.end():]
    # 孤立开标签（流式被截断）：丢弃它之后的全部内容
    m = _THINK_OPEN_RE.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


def sanitize_reply(raw: str) -> str:
    """把模型原始输出清洗成「可以直接展示给用户的话」。

    三层处理：去思考块 -> 解 JSON 包裹 -> 去掉开头泄漏的分析段落。
    每一步都保证「宁可少删，不可删掉真正的回复」，删空了就回退到上一步的结果。
    """
    if not raw:
        return ""
    text = raw.strip()
    text = strip_thinking(text)
    # 解 JSON 包裹（该模型无论怎么提示都会包一层，key 不固定）
    text = unwrap_reply(text)
    text = strip_thinking(text)
    if not text:
        return ""

    # 去掉开头若干行「分析腔」的行。只砍开头的行，正文中间不动，
    # 避免误伤用户真正看到的内容。
    lines = [ln.rstrip() for ln in text.splitlines()]
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    while idx < len(lines) - 1:  # 至少保留最后一行
        ln = lines[idx].strip()
        if any(m in ln for m in _LEAK_MARKERS) or ln.startswith(("（", "(", "【", "[")):
            idx += 1
        else:
            break
    cleaned = "\n".join(lines[idx:]).strip()

    # 去掉残留的围栏标记与首尾引号
    cleaned = cleaned.strip().strip("\"'“”‘’")
    for fence in ("```", "**"):
        cleaned = cleaned.replace(fence, "")
    return cleaned.strip() or text


def unwrap_reply(raw: str) -> str:
    """把模型自动包裹的 JSON 解出真正的回复文本。

    该模型无论怎么提示都会输出 JSON，且 key 不固定：
      {"response": "..."} / {"content": "..."} / {"question": "..."}
      {"type":"text","content":"..."} ...
    解不出来就原样返回，保证不丢内容。
    """
    if not raw:
        return ""
    text = raw.strip()
    if not text.startswith("{"):
        return text
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(obj, dict):
        return text
    for key in VLLMLlm.UNWRAP_KEYS:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # 兜底：取唯一一个字符串值
    strs = [v for v in obj.values() if isinstance(v, str) and v.strip()]
    return strs[0].strip() if strs else text


# ------------------------------------------------------------------ 工厂
_llm = None

# 本次调用是否发生了降级（真实模型不可用、回落到 mock）。
# 用 ContextVar 而不是全局变量：uvicorn 线程池下并发请求会互相污染。
_degraded: ContextVar[bool] = ContextVar("mindtrace_llm_degraded", default=False)


def was_degraded() -> bool:
    """上一次 complete()/structured() 是否因模型不可用而降级。

    危机分支据此判断「这句话不是模型生成的」，必须换成兜底话术，
    不能把 mock 的 CBT 台词当成危机干预回复发出去（红线 4）。
    """
    return _degraded.get()


def get_llm():
    global _llm
    if _llm is None:
        s = get_settings()
        if s.llm_provider == "vllm":
            _llm = VLLMLlm()
        elif s.llm_provider == "hy" and s.hy_api_key:
            _llm = HyLLM()
        elif s.llm_provider == "openai" and s.openai_api_key:
            _llm = OpenAILLM()
        else:
            _llm = MockLLM()
    return _llm


def structured(schema: type[T], system: str, user: str,
               thinking: bool | None = None) -> T:
    """结构化调用；失败时降级到 mock，绝不静默抛错中断危机流程。

    ⚠ thinking 默认必须是 None（交给 VLLMLlm 读配置），不能写 True ——
    曾经这里默认 True，直接覆盖掉 `vllm_thinking_for_assess=False`，
    导致开思考 + max_tokens=512 被截断、JSON 解析失败、
    整轮静默回落 mock（表现是情绪值永远是关键词原型值）。
    """
    try:
        out = get_llm().structured(schema, system, user, thinking=thinking)
    except Exception as exc:  # noqa: BLE001
        _degraded.set(True)
        os.environ.setdefault("MINDTRACE_LLM_FALLBACK", "1")
        try:
            out = MockLLM().structured(schema, system, user)
        except Exception:
            raise LLMError(str(exc)) from exc
    else:
        _degraded.set(False)
    return out


def complete(system: str, messages: list[dict[str, str]]) -> str:
    """对话生成。统一在出口做清洗，避免思考过程泄漏给用户。"""
    try:
        raw = get_llm().complete(system, messages)
    except Exception as exc:  # noqa: BLE001
        _degraded.set(True)
        try:
            raw = MockLLM().complete(system, messages)
        except Exception:
            raise LLMError(str(exc)) from exc
    else:
        _degraded.set(False)
    return sanitize_reply(raw)


def stream(system: str, messages: list[dict[str, str]]) -> Iterator[str]:
    buf: list[str] = []
    try:
        for chunk in get_llm().stream(system, messages):
            buf.append(chunk)
    except Exception:  # noqa: BLE001
        buf = list(MockLLM().stream(system, messages))
    yield sanitize_reply("".join(buf))
