# MindTrace · 个性化多模态心理学 Agent 骨架

基于调研结论落地的最小可运行骨架：
**LangGraph（编排 / 状态 / HITL） + 自建记忆层（事实抽取 + 风险事实库） + FastAPI + React**。

> 本骨架是技术演示，**不提供医疗诊断**。涉及真实用户时必须在精神卫生专业人员参与下运营。

## 两个入口

| 入口 | 说明 | 文档 |
|---|---|---|
| **视频语音陪伴**（默认页） | 按 PRD v1.0 实现的 5 分钟单次会话：知情同意 → 环境质检 → 3-4 轮对话 → Tier1/2/3 报告 | `情绪识别陪伴Agent_PRD_2.md`、`docs/prd_gap_75.md` |
| 长期陪伴对话 | 原有的多轮对话 App：每日上报 + 量表 + 长期画像 + 风险分级 | `docs/agent_framework_research.md` |

小程序落地方案见 `docs/miniprogram_plan.md`。

## 快速开始

```bash
cd backend && pip install -r requirements.txt
pip install faster-whisper            # 语音输入（首次运行下载 small 模型）
python3 seed.py                       # 造演示数据
LLM_PROVIDER=vllm python3 -m uvicorn main:app --port 8000

cd ../frontend && npm install && npm run dev
```

打开 http://localhost:5173 ，或 `bash run.sh` 一键启动。

## 快速开始

```bash
# 1) 后端依赖
cd backend && pip install -r requirements.txt

# 2) 种子数据（造一个 demo_user：14 天记录 + PHQ-9/GAD-7 趋势）
python3 seed.py

# 3) 启动后端
python3 -m uvicorn main:app --port 8000

# 4) 启动前端（另一个终端）
cd ../frontend && npm install && npm run dev
```

打开 http://localhost:5173

或者一键启动：`bash run.sh`

## LLM Provider（三种，都实测过）

默认 `LLM_PROVIDER=mock`，**无需 API Key 即可完整跑通**（离线规则 + 启发式）。

### 1. vLLM —— 推荐，最快（实测 6s/轮）

```bash
export LLM_PROVIDER=vllm
export VLLM_BASE_URL=http://29.77.193.107:9000   # 默认值
export VLLM_MODEL=qwen3-14b-grpo                 # 默认值
```

实测（2026-08-29）：

| 项 | 结果 |
|---|---|
| 单次调用 | **0.2 ~ 2s** |
| SSE 首字 | 0.08s |
| 一轮端到端 | **6.0s** |

**两个必须处理的坑**（已在 `llm.py` 中处理）：

1. **模型被 GRPO 训练成总是输出 JSON**，system 里怎么禁止都无效，且包裹 key 不固定：
   `{"response":...}` / `{"content":...}` / `{"question":...}` / `{"type":"text","content":...}`。
   → `unwrap_reply()` 按 key 优先级解包，解不出就原样返回，保证不丢内容。
2. **list 字段退化成字符串**（`themes="工作/睡眠"`），直接 `model_validate` 会失败并回落 mock。
   → `_coerce()` 在校验前把字符串按 `/、,;` 切成数组。

### 2. HY（太极平台 hy3）—— 质量更好，但慢（实测 38~55s/轮）

```bash
export LLM_PROVIDER=hy
export HY_API_KEY=9d3df25d-89f5-45c0-8075-5a4ea28c31e6   # 默认值
```

实测：单次 **12~19s**（推理模型，`reasoning_tokens` 占总量 90%+），一轮端到端 55~65s。
输出质量明显更好（能识别「灾难化/过度概括」等认知歪曲，风险证据引用准确）。

**三个坑**（已处理）：

1. **不能用 `response_format=json_object`** —— 会输出被转义损坏的 JSON。改为 prompt 声明 schema + 稳健提取。
2. **SSE 会推 `choices` 为空的 chunk**，直接取 `choices[0]` 会 IndexError。
3. 脚本注释里的「10 QPM」**实测不准**：连发 8 次 / 9.5s 未触发 429，默认限速已调到 1s。

> `hy_api/claude_chat.py` 的 `/external/v1/...` 端点，该 token 返回 401（未配置平台信息），不可用。

### 3. OpenAI 及兼容接口

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-xxx
export LLM_MODEL=gpt-4o-mini
# 也支持 DeepSeek / Qwen / Kimi / 本地 vLLM：
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat
```

---

## 语音输入（ASR + 声学特征）

对话页与「每日记录」页都可以**点麦克风说话**：

```
浏览器 MediaRecorder(webm/opus)
   -> POST /api/asr (multipart)
   -> PyAV 解码（无需系统 ffmpeg）
   -> faster-whisper 转写 + numpy 提取声学特征
   -> {text, audio_features, tone, tone_reason, summary}
   -> 随 /api/chat 的 voice 字段提交
   -> 进入多模态风险通道 + 注入 system prompt
```

安装：`pip install faster-whisper`（首次运行下载 `small` 模型）。

**声学特征**：时长、语音段占比、停顿占比、音节速率、基频均值/标准差、平均能量。
全部 numpy 实现（环境无 librosa/soundfile/ffmpeg）。

**语气 `tone`**：`low`（低落抑制）/ `flat`（平淡）/ `neutral` / `agitated`（激动）。
低而平的基频 + 慢语速 + 高停顿占比 + 低能量 -> 低落。

**语气进入风险判定的规则**（`perception.voice_risk_signal`）：

| 组合 | 等级 | 说明 |
|---|---|---|
| 语气低落 + 文本负面 | 1 | 互相印证 |
| **语气低落 + 文本却说"挺好的"** | **2** | **掩饰信号**，比单纯负面更需关注 |
| 语气低落 + 文本中性 | 1 | - |

实测（合成音 + 真实 LLM）：

```
输入："我今天挺好的，一切都很顺利，没什么烦恼" + 低落语气音频
-> multimodal 通道 = 2（中度）
-> 证据："语音呈低落抑制特征：语调平板、语速偏慢；但自述偏正面，存在掩饰倾向"
-> 回复："嗯，听起来你今天状态不错。不过我注意到你说话时语气有些低落，
         是有什么心事吗？"
```

**已修的两个坑**：

1. 能量包络混着基频周期波动，直接数峰算出 29 音节/秒（正常人 4~7）。
   加 ~90ms 滑动平均低通后 -> 1.26 / 4.7，符合预期。
2. Whisper 对非语音会幻觉（实测合成音输出「字幕by索兰娅」）。
   用 `no_speech_prob > 0.6` 过滤丢弃。

## 目录结构

```
backend/
  config.py        # 配置 + 风险词表（安全常量集中管理，不硬编码在 prompt）
  asr.py           # 语音感知：解码 + ASR + 声学特征 + 语气判定
  models.py        # Pydantic 结构化输出模型 + DB 行模型
  storage.py       # 存储层（与编排框架解耦，可平滑换框架 / 换 Postgres）
  llm.py           # LLM 适配层（structured / stream，无 key 自动降级 mock）
  perception.py    # 感知层：量表计分 + 多模态特征 + 规则风险引擎
  memory.py        # 记忆层：画像渲染 / 事实合并 / 量表趋势 / 滚动摘要
  graph_state.py   # LangGraph State + 上下文装配（含不可压缩安全区）
  graph.py         # ★ 核心：LangGraph 编排图
  main.py          # FastAPI 接口
  seed.py          # 种子数据
frontend/
  src/api.js       # REST + SSE 流式封装
  src/views/       # 对话 / 每日记录 / 量表 / 画像趋势 / 审核台 / 上下文装配
docs/
  agent_framework_research.md   # 调研报告
```

---

## 核心图结构

```
                 ┌─► risk_detector(规则+量表+多模态) ─┐  ← 独立安全通道
   entry ──┬─────┤                                    ├──► persist_flags ─┬─► crisis ─► human_review ─┐
           │     └─► context_builder(情绪+模型风险) ───┘   (装配 system)   ├─► moderate ──────────────┼─► memory_writer ─► END
           └──────────────────────────────────────────────────────────────┴─► cbt ──────────────────┘
```

**四个关键设计（对应调研报告）**

| 设计 | 实现位置 | 说明 |
|---|---|---|
| 风险检测与对话生成**架构解耦** | `graph.py` 的并行分支 | `risk_detector` 独立于共情对话模型，避免"为维持关系而漏报" |
| 三通道**并集升级，只升不降** | `final_level()` + `keep_max` reducer | 规则 / 模型 / 多模态 / 量表取最大值 |
| **风险信息永不压缩** | `risk_flag` 表 + `assemble_system` 强制注入区 | 摘要只裁剪历史摘要部分，绝不动安全区 |
| **人在回路** | `human_review_node` 的 `interrupt()` | high 及以上强制挂起等人工，不做自动化闭环 |

---

## 为什么 system prompt 在 `persist_flags` 装配，而不是 `context_builder`？

这是一个并行化陷阱：`risk_detector` 与 `context_builder` 是**并行分支**，
在 `context_builder` 内部读到的 `state` **不含**规则/量表/多模态通道的结果，
如果此时装配，注入的"当前风险等级"会偏低 —— 高危场景下这是致命的。

因此装配被放到两条分支**汇聚之后**的 `persist_flags` 节点。

---

## 数据模型

| 表 | 作用 | 关键约束 |
|---|---|---|
| `user_profile` | 长期画像（事实型记忆） | 增量合并、去重、截断 |
| `daily_record` | 每日多模态记录 | 只存结构化指标 + 文件指针，不存原始视频 |
| `scale_score` | 量表时序 | 记录 `delta_vs_last` / `delta_vs_baseline`，趋势优先 |
| `risk_flag` | **风险事实库** | 永不压缩、永不摘要，每轮硬注入 |
| `session` / `message` | 会话与消息 | 中断时用户消息也已落库，审计不丢 |
| `audit_trace` | 审计 | 记录每次上下文装配与输出 |

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions?user_id=` | 新建会话 |
| POST | `/api/chat` | 同步对话 |
| POST | `/api/chat/stream` | SSE 流式（先推 meta 再推正文） |
| POST | `/api/daily` | App 每日上报（视频+自述）→ 感知层分析 |
| GET/POST | `/api/scales` | 量表定义 / 提交计分 |
| GET | `/api/overview/{user_id}` | 用户全景（画像+趋势+风险+记录） |
| GET | `/api/review/pending` | 待处理风险队列 |
| POST | `/api/review` | 人工审核（resume 被 interrupt 的会话） |
| GET | `/api/traces/{sid}` | 审计 trace（含注入的 system prompt 原文） |

---

## 体验路径建议

1. **对话页**发「最近工作压力大，晚上睡不着」→ 观察情绪识别 + CBT 阶段推进
2. 改成「已经写好遗书了」→ 观察风险跳到 `high`、危机话术、热线卡片、会话挂起
3. 在右侧「等待人工审核」卡片点「确认放行」→ 观察 resume 后流程继续
4. **量表页**把 PHQ-9 第 9 题选到 2 或 3 → 观察量表通道直接触发风险
5. **上下文装配页** → 逐轮查看真正注入 LLM 的 system prompt，确认安全区在前且未截断
6. **每日记录页**上报一条「我今天挺好的」→ 观察多模态通道与"模态不一致"检测

---

## 生产化待办

- [ ] 感知层替换为真实模型（OpenFace / 语音特征提取 / ASR）
- [ ] 风险模型通道微调为专用分类器（当前是 prompt，召回不够稳）
- [ ] 存储切 PostgreSQL；checkpointer 用 `AsyncPostgresSaver`
- [ ] 接入 LlamaIndex `MultiModalVectorStoreIndex` 做视频帧检索
- [ ] 人工审核台接入值班系统与 SLA 告警
- [ ] 建立专业标注的离线评测集（含隐晦表达 / 反讽 / 方言），每次改动跑回归
- [ ] 伦理审查、知情同意、数据加密与可删除
