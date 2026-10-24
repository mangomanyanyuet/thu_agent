# MindTrace · 多模态心理陪伴 Agent

情绪识别 + 风险分级 + CBT 对话 + 人工审核的**技术演示骨架**。

> ⚠️ **不提供医疗诊断，不可用于真实用户**。涉及真人时必须在精神卫生专业人员参与下运营。
> 心理数据属敏感个人信息：`data/` 已在 `.gitignore` 中，永不入库。

---

## 快速开始

```bash
bash run.sh          # 后端 :8000 + 前端 :5173（+ 有证书时 HTTPS :8443）
```

打开 http://localhost:5173 ，登录 **demo / demo1234**（账号表非空后不再自动创建）。

手动启动：

```bash
cd backend && pip install -r requirements.txt
python3 seed.py                                   # 种子数据：14 天记录 + 量表趋势
LLM_PROVIDER=vllm python3 -m uvicorn main:app --port 8000

cd ../frontend && npm install && npm run dev      # 开发
# npm run build 后，后端 :8000 会直接托管 dist/
```

语音输入需额外装：`pip install faster-whisper`（首次自动下载 `small` 模型，约 460MB）。
不装也能跑，只是麦克风不可用。

> 模型是首次转写时才懒下载的。`huggingface.co` 不可达时会**静默返回空文本**
> （接口仍 200），麦克风看着正常但识别不出内容。此时用镜像拉一次即可：
> `HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 python3 -c "import asr; asr.get_model()"`
> 两个变量都要设 —— 只设 `HF_ENDPOINT` 会走 Xet 存储报 401。
> 拉过一次后就在本地缓存了，之后无需再设。`/api/asr/status` 的 `model_ready`
> 会明确告诉你权重是否就绪。

---

## 三个入口

| 入口 | 位置 | 说明 |
|---|---|---|
| **Web** | `frontend/` | 主界面，8 个页面 |
| **小程序 Demo** | `miniprogram/` | 微信开发者工具打开；落地方案见 `docs/miniprogram_plan.md` |
| **架构图** | `docs/architecture_flow.md` / `flow.html` | 数据流与模块关系 |

---

## 核心设计

```
                 ┌─► risk_detector(规则+量表+多模态) ─┐  ← 独立安全通道
   entry ──┬─────┤                                    ├──► persist_flags ─┬─► crisis ─► human_review ─┐
           │     └─► context_builder(情绪+模型风险) ───┘   (装配 system)   ├─► moderate ──────────────┼─► memory_writer ─► END
           └──────────────────────────────────────────────────────────────┴─► cbt ──────────────────┘
```

| 设计 | 实现 | 为什么 |
|---|---|---|
| 风险检测与对话生成**解耦** | `graph.py` 并行分支 | 避免"为维持关系而漏报" |
| 四通道**并集升级，只升不降** | `final_level()` + `keep_max` | 规则/模型/多模态/量表取最大值 |
| **风险信息永不压缩** | `risk_flag` 表 + 强制注入区 | 摘要只裁历史，绝不动安全区 |
| **人在回路** | `human_review_node` 的 `interrupt()` | high 及以上强制挂起，不做自动化闭环 |
| **模型挂了也不静默** | `safe_fallback_reply()` | 危机分支回落时给热线兜底话术，而非 CBT 台词 |

> system prompt 在 `persist_flags` 装配而非 `context_builder`：后者与 `risk_detector` 是并行分支，
> 此时读到的 state 不含规则/量表结果，装配会**低估风险等级**。故放到两分支汇聚之后。

---

## 目录

```
backend/
  graph.py         ★ LangGraph 编排图（节点、条件边、interrupt）
  graph_state.py     State + 上下文装配（含不可压缩安全区）
  llm.py            LLM 适配层：structured/stream，模型不可用时降级
  memory.py         画像渲染、事实合并、量表趋势、每日记录沉淀
  perception.py     量表计分 + 多模态特征 + 规则风险引擎
  patient_agent.py  「病人」模拟 Agent：风格×严重程度 + 随机对练 + 方法类红线
  simulate.py       病人模拟 CLI 脚本（/api/sim/* 复用同一套逻辑）
  companion/        视频语音陪伴会话（PRD 链路）
  mini.py           小程序适配层（同步接口 + 会话自愈 + 首屏聚合）
  auth.py           账号与 token（演示级）
  tracer.py         结构化事件日志 → data/traces/agent-YYYYMMDD.jsonl
  storage.py        存储层（与编排框架解耦）
frontend/src/views/ 对话 / 陪伴 / 每日记录 / 量表 / 画像 / 审核台 / 上下文装配 / 决策轨迹
miniprogram/        微信小程序 Demo（5 页面，录音走 mp3）
docs/               调研报告、PRD、小程序方案、架构说明
data/               运行时生成，已 gitignore，永不入库（详见下节）
```

---

## `data/` 目录

运行时生成，已 gitignore，永不入库（心理数据属敏感个人信息）。

```
data/
  mindtrace.db        主库：画像 / 会话 / 消息 / 风险 / 量表 / 每日记录
  checkpoints.sqlite  LangGraph 状态快照（删掉会丢进行中会话的上下文）
  traces/*.jsonl      每轮决策事件日志，一行一条
  *.bak.MMDD_HHMM     人工备份，无需回滚后可删
  *.db-wal / -shm     SQLite WAL 伴生文件，勿单独拷贝
```

```bash
tail -f data/traces/agent-$(date +%Y%m%d).jsonl | jq -r .msg   # 实时决策日志
```

备份用 `sqlite3` 的 backup API 而非 `cp`：直接拷主库会漏掉 `-wal` 里未落盘的数据。

---

## 配置

默认 `LLM_PROVIDER=mock`，**无需 API Key 即可完整跑通**（离线规则 + 启发式）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `vllm` / `hy` / `openai` / `mock` |
| `VLLM_BASE_URL` | `http://29.77.193.107:9000` | vLLM 服务地址 |
| `REQUIRE_AUTH` | `1` | 关闭则退回"客户端自报 user_id"的 demo 模式 |
| `DEMO_USERNAME` / `DEMO_PASSWORD` | `demo` / `demo1234` | 首次启动自动创建的账号 |
| `FACT_EXTRACT_EVERY` | `3` | 每 N 轮做一次事实抽取（首轮必抽） |
| `COMPANION_LLM_EMOTION` | `1` | 陪伴会话用 LLM 判情绪；`0` 退回关键词（快但漏判多） |
| `TRACE_ENABLED` | `1` | 结构化事件日志，供「决策轨迹」页实时展示 |
| `ENABLE_PATIENT_SIM` | `1` | 病人模拟开关；关闭后 `/api/sim/*` 返回 403 |

---

## 画像怎么来的

每轮对话最后由 `memory_writer_node` 调**同一个大模型**抽取事实，不是规则、也不是独立小模型。

- **时机**：首轮必抽，之后每 3 轮
- **输入**：自上次抽取以来的全部**用户原话**（不含 AI 回复）
- **落库**：`write_facts()` → 去重合并 + 按字段截断

| 模型输出 | 存进画像 | 前端显示为 |
|---|---|---|
| `triggers` | `known_triggers` | 已知触发源 |
| `profile_facts` | `social_support` | 社会支持（语义有错位，历史遗留） |
| `events` | `events` | 近期事件 |
| `coping_style` | `coping_style` | 应对方式 |
| `personality_traits` | `personality_traits` | 人格特质 |
| `preferences` | `preferences` | 沟通偏好 |

人格特质/应对方式只抽**用户明确自述**的，禁止从单次情绪反推（"今天难过" ≠ "情绪不稳定"）。

⚠️ **哨兵信号**：模型不可用时会回落关键词匹配，画像里会出现「**提到猫**」这类格式化条目。
真实模型抽出的是「养了一只叫豆豆的猫」这种自然语言。看到前者说明 vLLM 挂了。

---

## 体验路径

1. **视频语音陪伴**：走完 知情同意 → 环境质检 → 3-4 轮对话 → Tier1/2/3 报告
2. **长期对话**发「最近工作压力大，晚上睡不着」→ 看情绪识别 + CBT 阶段推进
3. 改成「**已经写好遗书了**」→ 风险跳 `high`、热线卡片、会话挂起
4. **人工审核台**点「放行 / 改写」→ 走完 HITL 闭环
5. **量表页**把 PHQ-9 第 9 题选到 2 或 3 → 量表通道直接触发风险
6. **决策轨迹**页 → 实时看每轮的通道判定、上下文装配、图转移
7. **对话页侧栏「病人模拟」**选风格 + 严重程度 → 自动跑完升级与挂起（或用 `simulate.py` 脚本）

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` \| `/register` | 登录 / 注册 |
| POST | `/api/sessions` | 新建会话 |
| POST | `/api/chat` \| `/chat/stream` | 同步对话 / SSE 流式 |
| POST | `/api/mini/*` | 小程序适配层（同步 + 会话自愈 + 聚合） |
| POST | `/api/companion/*` | 陪伴会话完整链路 |
| POST | `/api/daily` | 每日上报 → 感知层分析 |
| GET/POST | `/api/scales` | 量表定义 / 提交计分 |
| GET | `/api/overview/{user_id}` | 用户全景（画像+趋势+风险+记录） |
| GET/POST | `/api/review/pending` \| `/api/review` | 待审队列 / 人工审核 |
| POST | `/api/asr` | 语音转写 + 声学特征 + 语气 |
| GET | `/api/sim/profiles` | 病人模拟：可选风格与严重程度 |
| POST | `/api/sim/turn` \| `/api/sim/run` | 病人模拟：跑一轮 / 连续跑多轮（需 `ENABLE_PATIENT_SIM=1`） |

---

## 病人模拟（测试用可选项）

让对话的另一侧也由 Agent 驱动，用于验证**风险逐级升级 → 危机分支 → human_review 挂起**这条链路，
不必靠人反复手打「不想活了」这类句子。

```bash
cd backend
python3 simulate.py --list                                   # 看可选风格与严重程度
python3 simulate.py --style guarded --severity 5 --turns 8    # 自动对练 8 轮
python3 simulate.py --style hostile --severity 4 --seed 42 --json out.json
```

| 维度 | 取值 | 作用 |
|---|---|---|
| 风格 `--style` | `guarded` 掩饰 / `avoidant` 回避 / `verbose` 倾诉 / `ruminative` 反刍 / `somatic` 躯体化 / `hostile` 抵触 | 决定**怎么说话** |
| 严重程度 `--severity` | 1 轻微 / 2 轻度 / 3 中度 / 4 重度 / 5 危急 | 决定**内心有多糟**，即可透露到哪一步 |

两者正交：同一个严重程度可以是掩饰型也可以是倾诉型。

设计要点：

- **随机性注入 prompt，而不是靠 temperature**。`llm.complete()` 没暴露 temperature，
  所以每轮重新抽样「情境 / 此刻感受 / 配合度 / 叙事节拍」并写进 prompt；
  同一 `--seed` 可完整复现，换 seed 就是另一段对练。
- **披露曲线单调递增**。前几轮压着不说、中段才松动，才能测到「升级」告警；
  一上来就满级只能测到终态。允许回落会让风险等级来回跳，既不像真人也没法测。
- **病人侧的红线是"不得产出具体方法"**，与陪伴侧"不得漏报"相反，两份 prompt 独立维护。
  「写好遗书 / 把东西送人 / 以后不用管我了」属意念与准备行为，允许；
  「吃了多少片 / 在天台 / 割腕」属具体方法，命中即替换。
  因此**最高只模拟到 high，不模拟 imminent** —— 后者必然要描述手段在手或正在实施。
- **mock 模式下走内置台词**：`MockLLM.complete()` 返回的是**陪伴者**的 CBT 台词，
  直接拿来当病人说话会完全跑偏，故检测到 mock 时改用 `SCRIPTED_LINES`。

⚠ 模拟数据会**真实写入数据库**（会话 / 消息 / 风险事件 / 画像），默认挂在 `u_sim` 下，
会话 summary 带 `[SIM]` 标记便于清理。开关为 `ENABLE_PATIENT_SIM`（默认 1，关闭后 `/api/sim/*` 返回 403）；
即便开启，普通 `/api/chat` 走的仍是真人输入，不受影响。

---

## 已知问题

| 问题 | 现状 |
|---|---|
| **单轮延迟 ~10s** | 瓶颈是 LLM 推理，不是框架。PRD 要求 < 2s，差约 5 倍；若指"首字 <1.5s"则可行 |
| 感知层是规则兜底 | 面部/语音用启发式，非真实模型 |
| 事实抽取用了 `temperature=0.4` | 与对话生成共用；信息搬运场景应设 0 左右 |
| 抽取只喂用户原话 | 省 token，但回答 AI 提问时说的事实可能丢失上下文 |
| 小程序合规 | 心理健康类目需企业主体 + 资质；人脸属生物特征，必须走微信原生接口 |

---

## 文档

| 文档 | 内容 |
|---|---|
| `docs/情绪识别陪伴Agent_PRD_2.md` | 产品需求（PRD v1.0） |
| `docs/prd_gap_75.md` | PRD 覆盖度自查（75 分） |
| `docs/agent_framework_research.md` | Agent 框架选型（LangGraph / ADK / AutoGen 对比） |
| `docs/agent_harness_and_user_modeling.md` | Harness 六元组 + 冷启动用户建模 |
| `docs/miniprogram_plan.md` | 小程序落地方案与合规清单 |
| `docs/architecture_flow.md` | 架构与数据流 |

---

## 生产化待办

- [ ] 感知层替换为真实模型（OpenFace / 语音特征 / ASR）
- [ ] 风险通道微调为专用分类器（当前靠 prompt，召回不稳）
- [ ] 存储切 PostgreSQL；checkpointer 用 `AsyncPostgresSaver`
- [ ] 人工审核台接入值班系统与 SLA 告警
- [ ] 离线评测集（隐晦表达 / 反讽 / 方言），每次改动跑回归
- [ ] 伦理审查、知情同意、数据加密与可删除
