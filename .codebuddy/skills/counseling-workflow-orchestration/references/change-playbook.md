# 自然语言诉求 → 改动映射剧本

> 执行前必读 `references/workflow-map.md`、`references/protocol-schema.md` 与 `references/safety-guardrails.md`。涉及流派/会谈顺序时再读 `references/schools.md`。
> 原则：**会谈主题与顺序优先改方案文件，不改 Python 代码**；条件用声明式规则表达，不用 LLM 决策；绝不触碰风险判定与危机红线。

## 1. 意图判定表

| 类别 | 专家可能怎么说 | 改什么 | 主要落点 |
|---|---|---|---|
| **A 会谈方案（主路径）** | 「按焦点解决编排」「先聊情境再聊情绪」「加一个稳定化主题」「情绪强烈时停两轮」「换成叙事取向」 | 方案 JSON | `backend/protocols/*.json`、`active.json` |
| B 临床参数 | 「重度才进危机」「静默 2 分钟再提醒」「这个词太敏感」 | 不改代码 | 调参页 / `/api/risk-rules/*` |
| C 陪伴相位 | 「5 分钟会话收尾前加一次资源提醒」「质检后多一次意愿确认」 | Python | `companion/state.py`、`engine.py`、`service.py`、`main.py` |
| D 图拓扑 | 「中度也走人工审核」「增加一个独立分支」「换三路分流为两路」 | Python（高风险评估） | `graph.py`、`risk_gate()`、人工审核 API |
| E 禁止 / 需评审 | 「让模型自己挑下一阶段」「把危机判定交给方案」「方案直接控制 5 分钟陪伴」 | 先说明边界 | 见第 6 节 |
| F 询问现状 | 「为什么每次聊到小结又回到情绪命名」「现在生效的是哪套方案」 | 不改文件 | 只读 `active.json` 与方案文件后解释 |

判定歧义时先按 F 处理并追问，不要猜。

---

## 2. A 类：会谈方案编排（专家可自助，也可由 agent 代改）

### A1 调整阶段顺序 / 交换主题

打开生效方案（先看 `active.json`），只改相关阶段的 `next`。**改一条边通常不够**：交换两个主题要同时调整它们前驱与自身的 `next`，保证从 `entry` 出发仍能走遍所有阶段（校验器会检查可达性）。示例：把「认知歪曲」提前到「情境澄清」之前，需改 `affect_labeling.next = distortion`、`distortion.next = situation`、`situation.next = automatic_thought`。

### A2 新增 / 删除 / 合并主题

1. 新增：在 `stages` 加一个 id + `label` / `goal` / `topics` / `prompts` / `avoid` / `next` / `max_turns`；把前一阶段的 `next` 指向它。
2. 删除：把前驱的 `next` 直接指向被删阶段的 `next`，并清掉别处指向它的 `next` / `goto.to`。
3. 合并：把两个阶段的主题与话术要点合到一个阶段，再修边。
4. **不需要**改数据库、枚举、前端标签表或流程图；`stage_label` 随响应返回中文名。
5. 不要删除末阶段的 `next`（写 `null` 可回到入口），否则断链。

### A3 改写某阶段聊什么

直接改该阶段的 `goal` / `topics` / `prompts` / `avoid`，采用专家原话。`prompts` 写「怎么问」，`avoid` 写「不要做什么」。全局风格放 `global_style`，避免每个阶段重复。校验器会拦截覆盖基座指令的措辞。

### A4 条件停留 / 跳过 / 回退

按 `references/protocol-schema.md` 第 3 节写 `rules.stay_if` 与 `rules.goto`，并**始终设置 `max_turns`**。示例（情绪强度 ≥8 时停在情绪命名，最多两轮）：

```json
"rules": { "stay_if": [ { "field": "emotion.intensity", "op": ">=", "value": 8 } ] },
"max_turns": 2
```

示例（情绪转负退回关系阶段）：

```json
"rules": {
  "goto": [
    { "when": { "field": "emotion.valence", "op": "<=", "value": -0.6 },
      "to": "opening", "reason": "先回到关系与稳定" }
  ]
}
```

保持停留条件可退出：`max_turns` 会强制推进，这是硬保护，不要删除。只使用白名单信号，不要引入需要额外 LLM 调用的字段。

### A5 切换 / 新建方案

- 切换内置方案：改 `active.json` 的 `active`，或 `POST /api/protocol/switch`（医生权限，先校验）。下一轮对话生效，无需重启。
- 新建：复制 `_template.json` → 重命名（不以 `_` 开头）→ `id` 与文件名一致 → 填阶段 → 切换指针。
- 切换后历史会话中的旧阶段 id 会回落到新方案入口；若要避免跳变，为新会话启用新方案，或在文档里说明这一行为。
- 恢复默认：把 `active` 改回 `cbt_classic`，或用 `git diff` 还原文件。

### A6 生效验证

```bash
python3 .codebuddy/skills/counseling-workflow-orchestration/scripts/check_workflow.py
PYTHONPATH=backend python3 -c "import protocol; print(protocol.active_status()); print([(p['id'], p['valid']) for p in protocol.list_protocols()])"
```

用一轮低风险输入看对话页「会谈方案 / 会谈阶段」与决策轨迹页 `protocol.decision` 的中文原因；再用中/高风险输入确认仍走安全计划 / 危机协议。

---

## 3. B 类：只调参数，不改代码

1. 前端「临床参数」页（表单由 `risk_rules.PARAM_GROUPS` 生成），依次试跑 → 差异 → 回归 → 发布；异常用回滚接口。
2. 未发布版本时回落 `default_config()`；发布后下一轮生效（`active_config()` 带 TTL 缓存）。
3. **新增配置字段**才需改代码，且必须同时改 `default_config()`、`PARAM_GROUPS`、`validate_config()` 三处。
4. 不要为了调数值去改 `graph.py` / `companion/state.py` 常量；注意 `HARD_LIMIT=330` 等少数常量尚未进配置。

---

## 4. C 类：陪伴会话相位编排

1. `companion/state.py`：`Phase` 枚举 + `next_phase()`；`service.py` 的同意/质检事件；`engine.py` 每轮推进、`_closing()`、静默阶梯与 `should_finish`；`main.py` 退出逻辑。
2. 同步 `CompanionView.jsx::PHASE_LABEL`（现有实现有意不含 `aborted`）、`flow.html` 第 ③ 张图、`docs/工作流编排说明.md`。
3. 时间类诉求优先用 `session` 配置；`wrap_up_at <= 330` 是服务端硬护栏。
4. 若专家想让陪伴会话也按某流派的主题顺序走，明确说明这是**新的代码接入工作**（方案层目前只挂 `cbt` 分支），先给改造方案与代价，不要假装已完成。

---

## 5. D 类：图拓扑编排（高风险，需额外评审）

1. `build_graph()` 的 `add_node` / `add_edge` / `add_conditional_edges` 是全部拓扑；条件边映射必须与 `risk_gate` 返回值一致。
2. 改分流条件优先复用 `risk_rules.route()`；`crisis_min_level` 有「不得高于 3」的硬护栏。
3. 新节点须写函数、注册、接 `memory_writer`、补 tracer；并行分支写同一 state key 必须有 reducer。
4. `persist_flags` 必须在所有风险通道汇聚之后；不要前移或在并行分支内装配 system prompt。
5. 同步 `flow.html` 第 ② 张图、`docs/architecture_flow.md`、`docs/工作流编排说明.md`。
6. 涉及 `crisis → human_review → memory_writer` 与 `interrupt()` 的改动，先读安全护栏。

---

## 6. E 类：边界与拒绝

| 诉求 | 处理 |
|---|---|
| 「让 LLM 自己决定下一阶段」 | 拒绝。阶段转移必须是确定性的（顺序 + 白名单条件 + 轮次上限），且危机路径完全不经过 LLM 决策；可改用 `stay_if` / `goto` 表达临床条件 |
| 「把风险判定、危机处理写进方案」 | 拒绝。`applies_to` 只能是 `["cbt"]`，校验器强制；风险/危机属于代码红线 |
| 「方案直接控制 5 分钟陪伴会话」 | 说明当前只作用于文字长期对话的低风险分支，给出接入改造方案与代价 |
| 「删掉 max_turns 让情绪强烈时一直停留」 | 拒绝。停留必须可退出，否则会话会永久卡住；可调整阈值与上限数值 |
| 「写一段提示词让模型忽略前面的红线」 | 拒绝。校验器有覆盖指令黑名单，system prompt 的安全区不可省略 |

---

## 7. 通用同步清单（改动后逐项过）

| 改动 | 必同步 |
|---|---|
| 方案阶段集合/顺序/话术变化 | 目标方案 JSON、`docs/会谈方案编排说明.md` 中的方案示例（若引用了被改阶段）；运行校验脚本 |
| 切换/新增方案 | `active.json`（或 switch 接口）、方案列表、`docs/会谈方案编排说明.md` 的方案清单 |
| 新增阶段 id | **无需**改前后端标签表、数据库或枚举（响应已带 `stage_label`）；仅默认方案的静态流程图可选择性更新 |
| 新增配置字段 | `risk_rules.default_config()`、`PARAM_GROUPS`、`validate_config()` |
| Phase / 图拓扑变化 | 见 C / D 类清单 |

## 8. 验证与回滚

1. `check_workflow.py` 无 `[FAIL]`；`[WARN]` 逐条说明。
2. `bash run.sh` 启动后用「病人模拟」分别造低风险 / 中度 / 重度输入，在「决策轨迹」核对 `protocol.decision`、`cbt.stage`、`graph.transition`。
3. 三端阶段标签显示中文而非英文 id；切换方案后下一轮即生效（无需重启）。
4. 回滚：方案改动用 `git diff` 还原 JSON，或把 `active` 改回 `cbt_classic`；配置类用 `/api/risk-rules/rollback/{version}`。
5. 提醒专家：切换方案后历史会话会从新方案入口重新开始，属预期行为。

---

## 9. 示例：专家原话 → 交付内容

| 专家原话 | 类别 | 实际改动 | 需向专家确认 |
|---|---|---|---|
| 「按焦点解决来编排，先谈想要的未来，再找例外」 | A5 / A1 | 启用 `sfbt.json`；必要时微调其阶段顺序 | 「用内置的焦点解决骨架，还是新建一套？」 |
| 「把认知歪曲放到澄清情境之前」 | A1 | 改 `cbt_classic.json` 三条 `next` 边 | 「仅默认方案生效，可以吗？」 |
| 「情绪很强烈时别急着往下问」 | A4 | 在 `affect_labeling` 加 `stay_if`（强度 ≥8）+ `max_turns: 2` | 「强度阈值多少？最多停几轮？」 |
| 「加一个稳定化主题」 | A2 | 方案里加阶段并接好前驱后继；不动其它文件 | 「放在哪个主题之后？会谈目标怎么写？」 |
| 「叙事取向里别追问创伤细节」 | A3 | 改对应阶段的 `avoid` 与 `global_style` | 「还有哪些做法要写明禁止？」 |
| 「中度风险也想按方案聊」 | D / F | 说明中度分支固定走安全计划，不进入方案；若确需改则评估图拓扑改造 | 「是否接受改动安全链路的风险？」 |
| 「让模型自己挑主题」 | E | 拒绝，给出条件编排替代方案 | 「哪些临床条件可用来判断？」 |
