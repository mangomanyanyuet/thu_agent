# 修复总结：感知与记忆通道正确性（B1 / B2 / B5 / B6 / B7）

5 次提交，6 个文件，+141 / −46。全部为修复，无新增接口或页面；每个提交独立可运行。

| commit | 主题 | 文件 |
|---|---|---|
| `e1a1183` | 量表风险通道改为读取最近一次分数 | `storage.py` / `graph.py` |
| `bbf3d48` | 情绪倾向词表收敛为 config 一份（原分散 4 处） | `config.py` / `llm.py` / `perception.py` / `companion/daily_bridge.py` |
| `d8ffa94` | 多模态「失眠+语速下降」规则支持真实语音特征 | `config.py` / `perception.py` |
| `2843b4a` | 危机分支不再重复写入上下文审计轨迹 | `graph.py` |
| `45fa8e1` | 风险事件查询 `min_level/limit` 越界时不再静默漏数据 | `storage.py` |

---

## B1 量表通道读的是最早一次分数（漏报）

**问题**：`graph.py` 用 `list_scale_scores(limit=1)` 取分，而该函数按 `date ASC` 排序（供趋势图使用），
`limit=1` 拿到**最早一条**；用户后来 PHQ-9 第 9 题恶化到 3 分时，通道仍按旧分判 none。

**方案**：`storage.latest_scale_score()`（`date DESC, id DESC`，同日多次取最后一次）；量表通道改用之，
证据附分数日期便于判断陈旧程度。

**验证**：量表通道等级 `0 → 3`（high）；`list_scale_scores` 升序不变，趋势图不受影响。

---

## B2 多模态语速规则在真实语音链路恒不触发

**问题**：判断语速时只读 `words_per_min`（缺失默认 200），而 `/api/daily` 的真实语音特征只有
`syllable_rate` → 条件恒假；陪伴链路因 `daily_bridge` 做了 `syllable_rate*60` 换算才碰巧可用。

**方案**：新增 `perception._slow_speech()`，优先读 `syllable_rate`，缺失回退 `words_per_min`；
阈值常量进 `config`（注明与 `asr.tone_label` / `voice_risk_signal` 的 3.0 语义不同，不可合并）。

**验证**：真实语音 `syllable_rate=1.2` 由 `0 → 1` 级；正常语速仍 0（不误报）；
陪伴链路 60 档速率扫描与旧换算结果完全一致（零回归）。

---

## B5 查询参数越界 → 静默漏掉危急事件

**问题**：`min_level >= 5` 时等级切片为空，拼出 `level IN ()`。
实测 SQLite 3.45.2 **接受**空 IN 列表并返回 false、不报错，于是接口静默返回空 —— 审核台把 imminent 整个漏掉。
（此前"会 500"的判断有误，已更正。）

**方案**：`min_level` clamp 到 `[0, 最高等级]`（越界退化为"只看最严重"），`limit` clamp 到 `[1, 500]`；
等级名改用 `config.RISK_LEVELS`。

**验证**：库中有 low + imminent 时，`min_level=5 / 99` 由 `0 行 → 1 行`（返回 imminent）。

---

## B6 情绪倾向词表分散 4 份、口径不一致

**问题**：正负向词表共 4 处且互不相同（`llm.NEG_WORDS`、`analyze_narrative_stub` 内联、
`perception._POSITIVE/_NEGATIVE_WORDS`、`daily_bridge`）。同一句话在不同链路判出不同 tone，
例如"我挺好的，不过觉得自己真没用"：perception 口径 `positive`、daily_bridge 口径 `neutral` ——
直接影响「语气低落 + 文本偏正面」的掩饰信号（`voice_risk_signal` 的 2 级升级依据）。

**方案**：词表收进 `config.py` 取**并集**（14 正 / 16 负），4 处改为引用；
`llm.NEG_WORDS` 原样迁为 `config.EMOTION_KEYWORD_MAP`（保留遍历顺序，mock 输出不变）。

**验证**：旧 `NEG_WORDS` 与新映射逐项相等；4 份旧词表全部被并集包含（无丢词）；mock 情绪无回归。

> **待决策（本次未改）**：并集让正负识别都更全，但对掩饰信号（判据 `text_tone == "positive"`）
> 的影响是**双向**的 —— 含"没用/做不好"的"报喜"句会从 `positive` 变 `neutral`，该信号敏感度下降。
> 如需保持敏感度，可把判据放宽为 `text_tone != "negative"`（会显著增加 2 级升级与误报）。

---

## B7 危机分支重复写入上下文审计轨迹

**问题**：`persist_risk_flags` 被 `crisis` / `moderate` 节点二次调用；tracer 事件有 `_traced_turn`
幂等键，但 `add_trace()` 写在幂等块之外 → 每轮写出两条相同记录，污染 `/api/traces/{sid}` 与回放。

**方案**：`add_trace` 移入 `_traced_turn` 幂等块内，与 `risk_final` / `context` 同属一次性记录。

**验证**："节点执行 + crisis 二次调用"后 `add_trace` 调用次数 `2 → 1`。

---

## 整体验证

- 逐提交校验（`git archive` 导出 6 个状态 × 22 项）：全部可导入、可调用，无异常；
  修复按提交单调递进生效；既有功能（mock 对话、画像、每日记录、量表计分、规则通道、语音掩饰信号）
  在 6 个状态下完全一致，无回归。
- `py_compile` 全通过，linter 0 错误。
- 边界：环境未装 `langgraph`，用 stub 替换框架类（业务逻辑与数据层真实执行），完整 HTTP 服务启动未实测。
