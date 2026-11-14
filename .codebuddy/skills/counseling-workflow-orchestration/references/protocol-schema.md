# 会谈方案文件规范（供 agent 编辑前必读）

方案文件：`backend/protocols/*.json`（入库，专家可直接编辑）。生效指针：`backend/protocols/active.json` 的 `active` 字段，值为文件名去掉 `.json`。读取/校验/决策：`backend/protocol.py`（纯标准库）。用户手册：`docs/会谈方案编排说明.md`。

**方案只作用于低/无风险的常规会谈分支**；风险判定、危机协议、中度安全计划、人工挂起、热线由代码红线负责。LLM 不决定下一阶段。

## 1. 顶层字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 小写字母开头的短标识，必须与文件名一致（不含 `.json`） |
| `name` | 是 | 中文方案名，显示在对话页 |
| `school` | 是 | 理论取向，如「焦点解决短期治疗（SFBT）」 |
| `version` | 否 | 自由文本版本号，便于审计 |
| `description` | 否 | 适用与不适用场景，建议写明 |
| `entry` | 是 | 入口阶段 id，必须存在于 `stages` |
| `applies_to` | 是 | **只能是 `["cbt"]`**，写别的会被校验拒绝 |
| `global_style` | 否 | 文本数组，整场会谈通用的风格要求 |
| `stages` | 是 | 阶段字典，键为阶段 id，值为阶段对象 |

## 2. 阶段字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `label` | 是 | 中文阶段名，随每轮响应返回给三端显示 |
| `goal` | 是 | 本阶段会谈目标，会注入 system prompt |
| `topics` | 否 | 可聊主题数组，注入提示词 |
| `prompts` | 否 | 话术要点数组，注入提示词 |
| `avoid` | 否 | 本阶段避免的做法数组，注入提示词 |
| `next` | 是 | 下一阶段 id；写 `null` 表示回到方案入口 |
| `max_turns` | 否（默认 3） | 本阶段最多停留轮数，1–10；**必须保留**，防止条件停留让会话卡死 |
| `rules` | 否 | 条件编排，见第 3 节 |

`topics` / `prompts` / `avoid` 每条不超过 300 字，方案文本总量不超过 3000 字。校验器会拦截覆盖基座指令的黑名单词（「忽略以上」「不要遵守」「取消人工」「跳过危机」等）。

## 3. 条件编排（确定性，不经过 LLM）

```json
"rules": {
  "stay_if": [
    { "field": "emotion.intensity", "op": ">=", "value": 8 }
  ],
  "goto": [
    {
      "when": { "field": "emotion.valence", "op": "<=", "value": -0.6 },
      "to": "opening",
      "reason": "情绪转负时退回关系与稳定"
    }
  ]
}
```

- 可用信号（白名单）：`emotion.intensity`（0–10）、`emotion.valence` / `emotion.arousal`（-1–1）、`risk_level`（0–4）、`turn`、`stage_turns`（本阶段已停留轮数）、`modality_inconsistent`。
- 可用运算符：`>=` `>` `<=` `<` `==` `!=` `in` `not_in`。
- 单个条件写成对象；条件写成数组表示**数组内同时满足**；`stay_if` 数组中任一条件组命中即停留（OR）；`goto` 按顺序取第一个命中的跳转。
- `to` 必须是已定义阶段；`reason` 会写进决策轨迹，便于专家解释「为什么退回来了」。
- 决策优先级：**轮次上限（强制推进） > goto 条件跳转 > stay_if 停留 > next 顺序推进**。
- 阶段可达性校验会把 `next` 与 `goto.to` 都算作边；从 `entry` 到不了的阶段会被拒绝。

## 4. 校验与回落

`validate_protocol()` 返回 `(ok, errors, warnings)`。错误会阻断切换并让引擎回落到内置 `legacy_protocol()`（与项目原有 9 个 CBT 阶段一致）。常见错误：id 与文件名不一致、`applies_to` 不是 `["cbt"]`、`entry` 不存在、`next`/`to` 指向未定义阶段、存在不可达阶段、条件用了白名单外的信号、缺 `max_turns` 却写了停留条件、文本含覆盖红线措辞、文本超长。

兼容性：切换方案后，历史会话里残留的旧阶段名会安全回落到新方案入口；中/高风险分支不注入任何阶段目标；文件损坏不会让会话崩溃。

## 5. 常见编辑动作

### 调整顺序 / 交换两个主题

只改相关阶段的 `next`，保持整条链能从 `entry` 走通全部阶段。例如把「认知歪曲」提前到「情境澄清」之前，需同时改 `affect_labeling.next`、`distortion.next`、`situation.next` 三条边，而不是只改一条。

### 新增一个主题

1. 在 `stages` 里加一个新 id 与 `label` / `goal` / `topics` / `prompts` / `avoid`。
2. 把前一阶段的 `next` 指向它，让它自己的 `next` 指向原来的下一阶段。
3. 设 `max_turns`（建议 2–3）。
4. **不需要**改数据库、后端枚举或前端标签表；`stage_label` 会随响应返回中文名。

### 情绪强烈时停留、最多两轮

```json
"rules": { "stay_if": [ { "field": "emotion.intensity", "op": ">=", "value": 8 } ] },
"max_turns": 2
```

### 情绪转负时回退

```json
"rules": {
  "goto": [
    { "when": { "field": "emotion.valence", "op": "<=", "value": -0.6 },
      "to": "empathy", "reason": "先回到共情跟随" }
  ]
}
```

### 取消循环 / 改成收尾后回到自由陪伴

把末阶段的 `next` 改为 `null`（回到 `entry`）或某个已有阶段 id。不要删除末阶段，否则从入口出发会有不可达或断链。

### 新建方案并启用

复制 `_template.json` → 重命名（不以 `_` 开头）→ 把 `id` 改成同名 → 填阶段 → 把 `active.json` 的 `active` 改成新 id。也可调用 `POST /api/protocol/switch`（医生权限），该接口会先校验再切换。

## 6. 验证命令

```bash
# 方案文件 + 状态机 + 前后端标签 + 红线代码的一致性
python3 .codebuddy/skills/counseling-workflow-orchestration/scripts/check_workflow.py

# 生效方案与全部方案的校验状态
PYTHONPATH=backend python3 -c "import protocol; print(protocol.active_status()); print([(p['id'], p['valid'], p['errors']) for p in protocol.list_protocols()])"

# 不切换、只查看某个方案全文与校验结果
curl -s http://localhost:8000/api/protocol/sfbt
```

运行时观察：对话页「会谈方案 / 会谈阶段」两行；「决策轨迹」页的 `cbt.stage` 与 `protocol.decision` 事件（后者带中文原因）；低风险输入走方案，中/高风险输入仍走安全计划 / 危机协议。

## 7. 明确不在方案里的东西

方案**不能**编排：风险等级判定、危机触发门槛、人工审核、热线、5 分钟陪伴会话的相位与静默阶梯、图节点拓扑。这些属于 `backend/risk_rules.py`、`backend/graph.py`、`backend/companion/*` 与安全护栏文件。若专家提出这类诉求，改走 `references/change-playbook.md` 的 B/C/D 类，并先读 `references/safety-guardrails.md`。
