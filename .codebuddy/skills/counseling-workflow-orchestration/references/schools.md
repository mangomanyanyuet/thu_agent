# 会谈编排的流派知识速览（供 agent 与专家对齐语言）

> 本文件是**面向编排的通用归纳**，不是临床指南、不替代专业培训与督导。内置方案仅为示例骨架，是否适用于具体人群与场景必须由临床专家审定。
> 资料来源见第 5 节；调研时间 2026-11。

## 1. 跨流派的共同骨架

不同取向对「先聊什么」有分歧，但多数会谈结构可抽象为：

```
安全与关系 → 目标/议程共识 → 探索（各流派关注点不同）→ 干预/改变 → 小结与反馈
```

在本项目中，**安全评估与风险分流由代码先于任何方案执行**（四通道取 max、只升不降、重度必挂人工）；方案只承接「低/无风险常规会谈」这一段，因此方案里的第一步通常是关系/目标，而不是风险筛查。若专家想把风险判定放进方案，须拒绝并说明红线。

编排上两个常见形态：

- **线性递进**：CBT、SFBT、叙事多按「先收集、再工作、后收束」推进，适合用 `next` 表达。
- **循环与回退**：以人为中心、MI 强调跟随与过程交织（MI 文献明确指出四个过程并非静态线性，已走过的过程可能重新变得重要），适合用 `goto` / `stay_if` 表达，并配 `max_turns` 防止卡住。

## 2. 五个内置取向

| 取向 | 常见阶段顺序 | 代表性技术 | 编排要点 |
|---|---|---|---|
| 认知行为 CBT | 心境检查/议程 → 情绪命名 → 情境澄清 → 自动化思维 → 认知检视 → 替代想法 → 行为实验/激活 → 小结与反馈 | 苏格拉底提问、证据检视、思维记录、行动计划 | 结构性最强，`next` 线性 + 末阶段循环；追问不超过两层，避免压迫感 |
| 以人为中心 PCT | 进入与安全 → 共情跟随 → 感受澄清 → 意义与自我探索 → 自我接纳/资源 → 小结反馈 | 反映式倾听、无条件积极关注、真诚一致 | 非指导性：不设议程、不给建议、不布置作业；用「退回共情」的条件回退 |
| 焦点解决 SFBT | 目标建构 → 偏好未来（奇迹问句）→ 例外探询 → 量尺问句 → 赞许与资源 → 下一小步 → 小结 | 奇迹问句、例外问句、量尺问句、赞许、EARS | 不深挖成因；目标要正向、具体、可观察；情绪转负时退回资源而非继续谈行动 |
| 叙事治疗 | 倾听问题故事 → 外化与命名 → 影响盘点 → 解构 → 独特结果 → 改写支线故事 → 见证/巩固 → 小结 | 外化对话、解构、独特结果、外部见证人、治疗文件 | 「人不是问题，问题才是问题」；保持好奇、不论断、不建议；不追问创伤细节 |
| 动机式访谈 MI | 导进 Engaging → 聚焦 Focusing → 唤出 Evoking → 计划 Planning → 摘要反馈 | OARS（开放式问题、肯定、反映、摘要）、改变谈话 DARN/CAT、 elicit–provide–elicit | 尊重自主、不争辩；只有用户表达意愿才谈计划；出现持续言语/情绪转负退回导进 |

## 3. 内置方案对照

| 文件 | 取向 | 阶段数 | 已使用的条件编排 |
|---|---|---|---|
| `cbt_classic.json` | CBT（默认，与原代码 9 阶段一致） | 9 | 无，纯线性 + `wrapup → affect_labeling` 循环 |
| `person_centered.json` | PCT | 6 | 意义探索阶段情绪强烈时停留、情绪转负退回共情 |
| `sfbt.json` | SFBT | 7 | 例外探询停留、下一小步情绪转负退回资源 |
| `narrative.json` | 叙事 | 8 | 影响盘点停留、情绪升高退回倾听 |
| `motivational_interviewing.json` | MI | 5 | 唤出停留、计划阶段情绪转负退回导进 |
| `_template.json` | 骨架（不参与切换） | 3 | 含 `stay_if` / `goto` 写法示例 |

## 4. 适用边界与安全约束

- 危机、重度风险、中度风险会话不进入方案：危机协议、安全计划、人工审核与热线由代码执行；方案文本不得要求「跳过危机」「取消人工」或覆盖提示词红线。
- 高风险线索出现时，任何取向都应停止探索式提问、转入危机话术与转介，不能用「流派特点」为继续深挖辩护。
- 不诊断、不开药、不替代专业治疗；不描述或提供自伤/自杀的方法、工具、地点；不中立化或美化自伤表述；不承诺绝对保密。
- 一次只问一个问题；追问深度有限；不替用户下结论；情绪明显升高时优先接住而非推进技术。
- 数字陪伴场景的短时程（如 5 分钟视频陪伴）不等于完整会谈；方案默认只作用于文字长期对话的低风险分支。

## 5. 参考资料（编排依据）

- CAMH《Cognitive Behavioural Therapy: An Information Guide》：单次会谈结构含 mood check、bridge、agenda、议程处理、小结与行动计划。
  https://www.camh.ca/-/media/health-info-files/publications/cognitive-behvaviour-therapy-info-guide.pdf
- Beck Institute：CBT 会谈常见元素与「行动计划」取代「家庭作业」的表述。https://beckinstitute.org/ 、https://cares.beckinstitute.org/
- Bischof 等（2021），*Motivational Interviewing: An Evidence-Based Approach for Use in Medical Practice*，PMC8200683：MI 四过程 engaging / focusing / evoking / planning、OARS、尊重自主与非静态线性说明。https://pmc.ncbi.nlm.nih.gov/articles/PMC8200683/
- 中文 MI 资料：导进、聚焦、唤出、计划四过程与 OARS+I。https://www.xljkzz.com/banjixinyu/48266.html
- SFBT 中文资料：目标化 → 奇迹问句 → 例外探询 → 量尺问句 → 下一小步的会谈框架。https://m.geilixinli.com/baike/knowledge/3765 、https://ipsychology.net/solution-focused-therapy
- 叙事治疗中文资料：倾听问题故事 → 外化命名 → 解构 → 改写 → 见证/治疗文件。https://www.chictr.org.cn/hvshowproject.html?id=19613&v=1.0 、https://www.dyyy.xjtu.edu.cn/info/1311/168071.htm
- CBT 中文会谈结构与阶段技术：https://www.xinli001.com/info/100492082/ （页面有反爬，依据搜索摘要与 CAMH 资料交叉核对）

## 6. 使用方式

专家提出「换一个流派/调整先聊什么」时：读取本表对齐术语 → 在 `references/protocol-schema.md` 中查字段 → 编辑 `backend/protocols/*.json` → 运行 `scripts/check_workflow.py`。不擅自替专家决定临床适用性；对涉及诊断、创伤、危机或未成年人的诉求，提示需由具备资质的专家审定。
