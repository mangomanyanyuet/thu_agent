# MindTrace 工作流地图（供 agent 修改前必读）

> 路径相对仓库根；括号内行号为 2026-11 的代码快照，**定位一律以符号名为准**。
> 结构现在是「**方案文件定义会谈主题与顺序** + 代码定义图拓扑/相位 + 配置定义阈值」。
> 方案文件位于 `backend/protocols/*.json`，由 `backend/protocol.py` 读取校验；图拓扑仍无注册表/插件机制。

## 0. 四层结构

| 层 | 决定什么 | 主文件 | 谁可以改 |
|---|---|---|---|
| **会谈方案层（本 skill 主路径）** | 低风险会谈先聊什么、每阶段的主题/目标/话术、何时停留/跳过/回退 | `backend/protocols/*.json`、`active.json`、`backend/protocol.py` | 专家可直接编辑（无需改代码） |
| 长期对话图 | 并行分叉、风险分流、危机与人工审核、节点拓扑 | `backend/graph.py` | 开发者 |
| 5 分钟陪伴会话相位 | 开场/主体/收尾/静默/报告 | `backend/companion/state.py` | 开发者 |
| 临床参数配置 | 风险词表、四通道阈值、分流门槛、节奏秒数 | `backend/risk_rules.py` + 前端调参页 | 专家（可视化调参/发布/回滚） |

三套状态机彼此独立；会谈方案只挂在长期对话图的 `cbt` 分支上，不进入相位状态机。

---

## 1. 长期对话图（`/api/chat`、`/api/mini/chat` → `graph.run_turn()`）

`entry` → 并行分叉 `risk_detector`（规则/量表/多模态，不依赖 LLM）与 `context_builder`（情绪 + 模型风险）→ 汇聚 `persist_flags`（装配 system prompt、落库）→ `risk_gate()` 按四通道最大等级分流到 `crisis / moderate / cbt` → `memory_writer` → `END`。

- `crisis → human_review → memory_writer`：`interrupt()` 挂起，人工 `approve/override` 后 `Command(resume=...)` 恢复。
- 分流默认门槛：`crisis_min_level=3`、`moderate_level=2`（`risk_rules.route()`），数值可配但 `crisis_min_level` 不得高于 3。
- LLM 只输出情绪与风险字段，**不决定任何状态转移**。
- `keep_max` reducer：风险等级只升不降；system prompt 必须在汇聚后装配。
- `assemble_system()` 在中/高风险时不注入任何探索性阶段目标，只写「本轮不推进会谈阶段」。

## 2. 会谈方案层（决定主题与顺序）

### 生效链路

```
backend/protocols/active.json  {"active": "sfbt"}
        ↓ protocol.active_protocol()（按文件 mtime 自动重载，异常回落内置方案）
backend/protocols/<id>.json
        ↓ validate_protocol()（结构/可达性/条件白名单/红线措辞/文本长度）
graph.cbt_node()  ── stage_block() ──→ 注入该阶段的目标/主题/话术到 cbt 分支提示词
                 ── decide_next() ──→ 确定性算出下一阶段（写 cbt.stage 与 protocol.decision 事件）
run_turn()       ──→ 返回 stage_label / protocol_name / protocol_id 给三端显示
```

- 方案只作用于 `applies_to == ["cbt"]`；校验强制，写别的会被拒绝。
- 决策优先级：**`max_turns` 轮次上限 > `goto` 条件跳转 > `stay_if` 停留 > `next` 顺序推进**。
- 可用条件信号：`emotion.intensity`、`emotion.valence`、`emotion.arousal`、`risk_level`、`turn`、`stage_turns`、`modality_inconsistent`。
- 中/高风险轮次不调用 `cbt_node`，方案完全不参与。
- 切换方案或旧 checkpoint 残留阶段 id 时，`stage` 回落到 `entry`；文件损坏或校验失败时回落到内置 `legacy_protocol()`（原 9 个 CBT 阶段），会话不中断。
- 每轮响应带 `stage_label`（中文）与 `protocol_name`，Web / 小程序 / 预览页优先使用，静态标签表只作回落；**新增阶段不需要改前端**。

### 接口

| 接口 | 作用 |
|---|---|
| `GET /api/protocol` | 生效方案、阶段中文标签映射、全部方案列表与校验状态 |
| `GET /api/protocol/{id}` | 查看某方案全文与校验结果（不切换） |
| `POST /api/protocol/switch` | 切换生效方案（医生权限，先校验） |

### 内置方案

`cbt_classic`（默认）、`person_centered`、`sfbt`、`narrative`、`motivational_interviewing`、`_template`（骨架，不参与切换）。流派依据见 `references/schools.md`，字段规范见 `references/protocol-schema.md`。

## 3. 兼容基线（不再是编排入口）

`backend/graph_state.py` 的 `CBT_STAGE_GUIDE` / `CBT_NEXT` 与 `backend/models.py` 的 `CBTStage` 枚举仍保留，但**只作为 `protocol.legacy_protocol()` 的内置回落基线**，不再决定线上阶段顺序。`frontend/src/theme.js::STAGE_LABEL`、`miniprogram/utils/format.js::CBT_STAGE_LABEL` 同样是旧阶段的显示回落。`frontend/public/flow.html` 第 ④ 张图只描述默认方案 `cbt_classic`。

修改这些文件**不会**改变默认方案的顺序；要改顺序请编辑 `backend/protocols/cbt_classic.json`。

## 4. 5 分钟陪伴会话相位

状态：`consent → quality → opening → main → closing → done`，旁路 `crisis` / `aborted`。`next_phase()` 中风险 `high` 优先跳危机；`elapsed >= wrap_up_at` 转 `closing`；`main` 自循环；静默阶梯 8s/60s/90s，用户开口清零 `idle_stage`；`should_finish = phase in (DONE, CRISIS)`。**方案层不影响这条链路**；若专家要求改陪伴会话的主题顺序，属于代码改动（见 `change-playbook.md` C 类）。

## 5. 可编排边界速查

| 项 | 是否可编排 | 位置 |
|---|---|---|
| 会谈主题、阶段顺序、话术、条件停留/回退、切换流派方案 | **可编辑方案文件** | `backend/protocols/*.json` |
| 风险词表、四通道阈值、分流门槛、会话节奏秒数 | 可配置（生效版本 + 发布/回滚） | `risk_rules.py` / 调参页 |
| 图节点、条件边、并行结构、人工挂起链路 | 硬编码 | `graph.py::build_graph()` |
| 陪伴相位顺序、静默阶梯、危机跳转 | 硬编码 | `companion/state.py`、`engine.py` |
| 危机判定、人工审核、热线、系统提示词绝对红线 | **代码级锁定，方案里没有开关** | `graph.py`、`graph_state.py`、`risk_rules.py` |

## 6. 可观测

| 事件 / 字段 | 含义 |
|---|---|
| `cbt.stage` | 阶段 id 变化（src == dst 时不记） |
| `protocol.decision` | 方案 id、src/dst 与中文原因（顺序推进 / 停留 / 回退 / 轮次上限） |
| `graph.transition` | 风险分流跳转 |
| `phase.transition` | 陪伴会话相位 |
| `stage_label` / `protocol_name` | 每轮响应字段，三端展示 |

查看：对话页「会谈方案 / 会谈阶段」、决策轨迹页（含「方案决策」筛选）、`/api/protocol`。

## 7. 文件索引

| 文件 | 职责 |
|---|---|
| `backend/protocols/*.json` | 会谈方案定义（专家可编辑） |
| `backend/protocols/active.json` | 生效方案指针 |
| `backend/protocol.py` | 加载、校验、切换、阶段决策、提示词块生成 |
| `backend/graph.py` | 图拓扑、条件边、cbt 分支调用方案层、run_turn 返回 |
| `backend/graph_state.py` | `AgentState`、reducer、`SYSTEM_TEMPLATE`、内置回落基线 |
| `backend/models.py` | `CBTStage`（仅回落基线）、结构化输出模型 |
| `backend/risk_rules.py` | 配置默认值、护栏校验、`route()`、版本管理 |
| `backend/perception.py` / `tracer.py` | 四通道风险信号 / 事件日志 |
| `backend/companion/*` | 陪伴会话相位与引擎 |
| `backend/main.py` | 全部 API（chat / companion / review / risk-rules / protocol） |
| `frontend/src/theme.js`、`miniprogram/utils/format.js` | 旧阶段中文标签（显示回落） |
| `frontend/public/flow.html` | 静态流程图（第 ④ 张仅对应默认方案） |
