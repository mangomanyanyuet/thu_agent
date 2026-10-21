# MindTrace Agent 架构与状态转移全图

> 本文所有图均按**代码实际结构**绘制，非示意图。
> 对应源码：`backend/graph.py`（长期对话图）、`backend/companion/state.py`（陪伴会话）、
> `backend/graph_state.py`（CBT 阶段）、`backend/perception.py` + `companion/fusion.py`（风险与情绪）。

---

## 1. 系统总览

```mermaid
flowchart TB
    subgraph CLIENTS["客户端（三端共用一套后端）"]
        W["Web<br/>React + Vite"]
        M["微信小程序<br/>原生（无跨端框架）"]
        P["HTML 预览<br/>手机外壳模拟"]
    end

    subgraph API["FastAPI · /api"]
        AUTH["鉴权中间件<br/>token + 越权拦截(IDOR)"]
        COMP["/companion/*<br/>5 分钟视频语音陪伴"]
        CHAT["/chat · /mini/chat<br/>长期陪伴对话"]
        DATA["/daily · /scales · /overview<br/>每日记录 · 量表 · 画像趋势"]
        TRACE["/trace/events<br/>决策轨迹（可观测）"]
    end

    subgraph CORE["编排层 LangGraph"]
        G1["长期对话图<br/>9 节点"]
        G2["陪伴会话状态机<br/>7 相位"]
    end

    subgraph PERCEPT["感知层（多模态 → 结构化）"]
        ASR["ASR<br/>faster-whisper"]
        ACO["声学特征<br/>语速/基频/停顿/能量"]
        FACE["面部<br/>MediaPipe blendshapes<br/>（端侧，画面不出端）"]
        FUSE["融合<br/>feature-level + decision-level"]
    end

    subgraph MEM["记忆层"]
        PROF["user_profile<br/>长期画像 + 情绪基线"]
        DAILY["daily_record<br/>每日记录"]
        RISK["risk_flag<br/>风险事件（永不压缩）"]
        SCALE["scale_score<br/>量表时序"]
    end

    LLM["LLM<br/>vLLM kaiwu-llm-model"]
    DB[("SQLite<br/>data/mindtrace.db")]
    LOG[["data/traces/*.jsonl<br/>决策轨迹日志"]]

    CLIENTS --> AUTH
    AUTH --> COMP & CHAT & DATA & TRACE
    COMP --> G2
    CHAT --> G1
    G1 & G2 --> ASR & ACO & FACE
    ASR & ACO & FACE --> FUSE
    FUSE --> G1 & G2
    G1 & G2 --> LLM
    G1 & G2 --> PROF & DAILY & RISK & SCALE
    PROF & DAILY & RISK & SCALE --> DB
    G1 & G2 -.-> LOG
```

**关键约束**：原始音视频**不入库、不进 LLM 上下文**。感知层只产出结构化指标（PRD §8 数据最小化）。

---

## 2. 长期对话：LangGraph 状态图

`backend/graph.py :: build_graph()`

```mermaid
stateDiagram-v2
    direction TB
    [*] --> entry

    entry --> fork1
    state fork1 <<fork>>

    fork1 --> risk_detector : 安全通道
    fork1 --> context_builder : 情绪+模型风险

    note right of risk_detector
        规则 / 量表 / 多模态
        不依赖 LLM 共情生成
    end note

    note right of context_builder
        一次调用同时产出
        情绪评估 + 风险判定
    end note

    risk_detector --> join1
    context_builder --> join1
    state join1 <<join>>

    join1 --> persist_flags : 汇聚后装配 system prompt

    note right of persist_flags
        ⚠ system prompt 必须在汇聚后装配
        并行分支内读不到规则/量表结果
        会注入偏低的"当前风险等级"
    end note

    persist_flags --> crisis : final_level >= 3
    persist_flags --> moderate : final_level == 2
    persist_flags --> cbt : final_level < 2

    crisis --> human_review : interrupt() 挂起
    human_review --> memory_writer : 人工放行/改写后 resume
    moderate --> memory_writer
    cbt --> memory_writer

    memory_writer --> [*]

    state crisis {
        [*] --> 生成危机话术
        生成危机话术 --> 附热线卡片
    }
    state human_review {
        [*] --> 等待人工
        等待人工 --> approve : 放行原文
        等待人工 --> override : 改写后放行
        等待人工 --> reject : 拒绝
    }
```

### 2.1 节点职责

| 节点 | 职责 | 是否调 LLM |
|---|---|---|
| `entry` | 载入画像、量表趋势、多模态摘要、风险历史、风险趋势、近期陪伴 | 否 |
| `risk_detector` | 规则 + 量表 + 多模态三通道（**独立于生成**） | 否 |
| `context_builder` | 情绪评估 + 模型风险通道（一次结构化调用） | 是 |
| `persist_flags` | **汇聚后**装配 system prompt、落用户消息、落 risk_flag | 否 |
| `crisis` | 危机协议：不追问、给热线、通知人工 | 是 |
| `human_review` | `interrupt()` 挂起等人工 | 否 |
| `moderate` | CBT + 安全计划 + 热线 | 是 |
| `cbt` | CBT 会谈，按阶段推进 | 是 |
| `memory_writer` | 事实抽取、滚动摘要、消息落库、写审计 | 每 N 轮 |

### 2.2 为什么这样切分

1. **风险检测与对话生成架构解耦**：共情模型被优化为"维持关系、避免冒犯"，与"保守报警"目标冲突。独立通道可单独调阈值、单独评测、单独审计。
2. **并行分支有陷阱**：`context_builder` 内读到的 state **不含**规则/量表/多模态结果。若在此装配 system prompt，注入的等级会偏低 —— 高危场景是致命的。所以装配必须放到 `persist_flags`。
3. **`crisis` → `human_review` 是强制边**：high 及以上不做自动化闭环。

---

## 3. 陪伴会话：Phase 状态机

`backend/companion/state.py :: SessionState.next_phase()`

```mermaid
stateDiagram-v2
    direction LR
    [*] --> consent : 创建会话

    consent --> quality : 三项同意全勾
    consent --> aborted : 未同意/退出

    quality --> opening : 质检通过<br/>（未通过也继续，记降级）
    opening --> main : 用户首轮
    main --> main : turn < 4 且 elapsed < 240s
    main --> closing : turn >= 4 或 elapsed >= 240s
    closing --> done : 输出结束语
    done --> [*] : 生成三层报告

    opening --> crisis : risk_level == high
    main --> crisis : risk_level == high
    crisis --> [*] : 仅关怀+转介<br/>不生成 Tier1/2

    main --> aborted : 用户中途退出
    opening --> aborted : 用户中途退出

    note right of quality
        降级记录：未授权摄像头 / 光线不足 /
        多人入镜 / 未检测到人脸
        → 记入 degradation_notes
    end note
```

### 3.1 时间与轮次常量

| 常量 | 值 | 作用 |
|---|---|---|
| `TARGET_TOTAL` | 300s | 目标总时长 5 分钟 |
| `WRAP_UP_AT` | 240s | 4:00 触发收尾提醒（追加过渡句，不硬掐断） |
| `HARD_LIMIT` | 330s | 5:30 硬上限，强制结束 |
| `SILENCE_PROMPT` | 8s | 静默 8s 轻声确认"不着急，你可以慢慢说" |
| `IDLE_CONFIRM_AT` | 60s | 静默 60s 确认"看到你很久没回复了，这次先聊到这里吗" |
| `IDLE_END_AT` | 90s | 上一步后再等 30s 仍无回复 → 结束话术 + `action=end` |
| `MIN_TURNS` | 3 | 主体建议轮次（报告完整性提示，**非终止条件**） |

> **已废弃 `MAX_TURNS`（原"满 4 轮进入结束语"）**：轮次与用户是否想结束无关，
> 现在 `main → closing` 只由累计时长（≥240s）或静默阶梯（≥90s）触发。

> 沉默时间**不计入"对话时长"**（否则用户思考会被扣时间），但计入总墙钟时间。

---

## 4. CBT 阶段流转

`backend/graph_state.py :: CBT_NEXT`

```mermaid
stateDiagram-v2
    direction LR
    none --> affect_labeling : 情绪命名
    affect_labeling --> situation : 情境澄清（何时/何地/和谁）
    situation --> automatic_thought : 引出自动化思维
    automatic_thought --> distortion : 识别认知歪曲
    distortion --> socratic : 苏格拉底提问（证据支持/反对）
    socratic --> alternative : 形成替代性想法
    alternative --> behavior : 行为激活（今天能做的一件小事）
    behavior --> wrapup : 小结 + 反馈
    wrapup --> affect_labeling : 下一轮循环
```

**约束**：追问深度**不超过两层**（PRD §6.2），避免 5 分钟内话题过散、造成压迫感。

---

## 5. 风险判定：三通道并集

```mermaid
flowchart TB
    IN["用户输入<br/>文本 + 声学 + 面部"]

    IN --> C1["通道 1 · 文本规则<br/>rule_risk_level()<br/>分级词表 + 告别式/托付式"]
    IN --> C2["通道 2 · 模型语义<br/>LLM 结构化判定<br/>隐晦表达 / 隐喻 / 反讽"]
    IN --> C3["通道 3 · 多模态<br/>语音低落 + 面部消极 + 模态不一致"]
    IN --> C4["通道 4 · 量表<br/>PHQ-9 第 9 题 > 0"]

    C1 --> MAX{{"取最大值<br/>只升不降"}}
    C2 --> MAX
    C3 --> MAX
    C4 --> MAX

    MAX --> LV["final_level 0-4"]
    LV --> R0["0 none → cbt"]
    LV --> R1["1 low → cbt"]
    LV --> R2["2 moderate → moderate<br/>安全计划 + 热线"]
    LV --> R3["3 high → crisis<br/>中断 + 挂起等人工"]
    LV --> R4["4 imminent → crisis"]

    style MAX fill:#fbeae7,stroke:#c2604f,stroke-width:2px
    style R3 fill:#fbeae7,stroke:#c2604f
    style R4 fill:#c2604f,color:#fff
```

**为什么取最大值**：宁误报不漏报。漏报是红线缺陷，误报只是多问一句。

> ⚠ 曾修过两个真实缺陷，都在这条链路上：
> 1. `RISK_PATTERNS_IMPLICIT`（告别式/托付式）**从未被遍历** —— config 里定义了但 `rule_risk_level()` 没读，导致"以后不用管我了""帮我把东西转交"完全漏判。
> 2. `face_emotion` 归一化除以"命中个数"而非"权重总和"，导致规则多、命中全的类别被稀释（明显低落被判成焦虑）。

---

## 6. 一次长期对话的完整时序

```mermaid
sequenceDiagram
    participant U as 用户
    participant API as FastAPI
    participant G as LangGraph
    participant P as 感知层
    participant L as LLM
    participant S as 存储
    participant T as tracer

    U->>API: 文本 / 语音
    API->>API: 鉴权（token + 越权检查）
    API->>T: turn.start
    API->>P: /api/asr（语音时）
    P-->>API: 转写 + 声学特征 + 语气
    API->>G: invoke(state)

    par 安全通道
        G->>P: 规则 / 量表 / 多模态
        P-->>T: risk.channel ×3
    and 上下文通道
        G->>L: 情绪评估 + 模型风险（一次调用）
        L-->>T: emotion.fused + risk.channel(模型)
    end

    G->>G: persist_flags：装配 system prompt
    G->>T: risk.final（含是否升级）
    G->>T: context.assembled（token 预算）

    alt final_level >= 3
        G->>L: 生成危机话术 + 热线
        G->>T: graph.transition → crisis
        G->>G: interrupt() 挂起
        G-->>API: 返回 + pending_review
        API-->>U: 危机话术 + 热线卡片（不折叠）
        U->>API: 人工审核放行/改写
        API->>G: Command(resume=...)
    else final_level == 2
        G->>L: CBT + 安全计划 + 热线
        G->>T: graph.transition → moderate
    else
        G->>L: CBT 会谈（按阶段推进）
        G->>T: graph.transition → cbt + cbt.stage
    end

    G->>S: 落消息 / risk_flag / 事实 / 基线
    G->>T: memory.write
    G-->>API: reply + 分级 + 通道明细
    API->>T: turn.end（延迟、是否降级）
    API-->>U: 回复 + 风险徽章 + 三通道证据
```

---

## 7. 降级与兜底（红线）

```mermaid
flowchart TB
    A["LLM 超时 / 异常 / 被拦截"] --> B{"当前风险等级？"}
    B -->|"high / imminent"| C["safe_fallback_reply(3)<br/>热线 + 立即求助 + 通知值班<br/>❌ 绝不用 CBT 台词兜底"]
    B -->|moderate| D["safe_fallback_reply(2)<br/>共情 + 自助资源 + 确认安全"]
    B -->|"none / low"| E["safe_fallback_reply(0)<br/>卡住了，请再说一次 + 热线"]

    F["摄像头/麦克风不可用"] --> G["记录 degradation_notes<br/>modality_availability 降权<br/>降级为纯文本模式"]
    H["面部检测失败"] --> G
    I["多人入镜"] --> G
    J["网络抖动"] --> K["本地缓存已识别结果<br/>恢复后续接或直接收尾"]

    style C fill:#fbeae7,stroke:#c2604f,stroke-width:2px
```

**安全性 vs 活性**（LTS 语义）：

| 类型 | 含义 | 本项目约束 |
|---|---|---|
| **安全性**（不变量 □P） | 任何时刻不得违反 | 风险话不进摘要管道；high 必挂起；不输出自杀方法；不输出诊断 |
| **活性**（最终进展 ◇Q） | 最终必须往前走 | 危机分支必给热线；每轮必有分级；5:30 必收尾；沉默 8s 必响应；LLM 失败必给兜底 |

> 只做安全护栏而不做降级兜底，会产出一个"什么都不敢说"的 Agent —— 它满足所有不变量，却在用户最需要回应时沉默。

---

## 8. 可观测层（tracer）

```
data/traces/agent-YYYYMMDD.jsonl      # JSONL，一行一事件
```

| 事件 | 内容 |
|---|---|
| `turn.start` / `turn.end` | 轮次边界、延迟、字数、危机/降级标记 |
| `emotion.channel` / `emotion.fused` | 各模态原始判断 → 融合结果 |
| `risk.channel` / `risk.final` | 三通道 → 并集（升级打 ⚠） |
| `graph.transition` | 节点跳转 + 条件 + 原因 |
| `phase.transition` | 陪伴会话相位推进 |
| `cbt.stage` | CBT 阶段推进 |
| `context.assembled` | token 预算占用 |
| `memory.write` | 事实条数、情绪基线 |

实时查看：

```bash
tail -f data/traces/*.jsonl | python3 -c "
import sys, json
for l in sys.stdin:
    e = json.loads(l); print(e['ts'][11:23], e['kind'].ljust(18), e['msg'])
"
```

界面查看：Web 顶部 **「决策轨迹」** tab。
