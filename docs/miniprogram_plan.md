# 视频语音情绪陪伴 Agent —— 微信小程序落地方案

> 配套文档：`情绪识别陪伴Agent_PRD_2.md`（产品需求）
> 当前 Demo：Web 端（`frontend/`），本文说明迁到小程序需要什么、要改什么。

---

## 0. 结论先行

**技术上能实现，但合规是主要门槛，不是技术。**

| 维度 | 结论 |
|---|---|
| 多模态采集 | ✅ 可行，但 API 全都要换（`camera` 组件 + `RecorderManager`，无 `getUserMedia`） |
| 面部表情识别 | ⚠️ **MediaPipe 不能直接搬**，需换成 VisionKit 或后端分析 |
| 语音 ASR | ✅ 可行，`RecorderManager` 录音 → `uploadFile` → 后端 faster-whisper |
| 实时流式 | ⚠️ 小程序**没有 EventSource/SSE**，需改为分块接收或轮询 |
| 类目资质 | 🔴 **最大风险**。心理健康类目需要资质，个人主体做不了 |
| 人脸生物特征 | 🔴 **必须走微信原生人脸识别接口**，不能自行采集/存储人脸 |
| 响应延迟 | ⚠️ PRD §7 要求单轮 < 2 秒，当前后端约 8-10 秒，**不达标** |

---

## 1. 技术能力映射（Web API → 小程序 API）

| Web 端现有实现 | 小程序替代方案 | 差异与注意点 |
|---|---|---|
| `navigator.mediaDevices.getUserMedia` | `<camera>` 组件 + `wx.createCameraContext()` | 需用户授权 `scope.camera`；`onCameraFrame` 需组件上指定 `frame-size` |
| `MediaRecorder` (webm/opus) | `wx.getRecorderManager()` | 输出 **mp3 / aac / pcm**，不是 webm；后端 PyAV 需支持对应格式 |
| 浏览器端 MediaPipe (WASM) | `wx.createVKSession({track:{face:{mode:1}}})` | 见 §2，能力不同 |
| `fetch` + `FormData` | `wx.uploadFile` | 只能单文件上传，`name` 字段必填 |
| SSE (`EventSource`) | `wx.request({enableChunked:true})` + `onChunkReceived` | 需自行解析 SSE 帧；iOS 支持较好，Android 有限制 |
| `requestAnimationFrame` | `VKSession.requestAnimationFrame` / `canvas.requestAnimationFrame` | 帧率上限 30fps |
| `AudioContext` 音量条 | `RecorderManager.onFrameRecorded` 取分片算能量 | — |

### 1.1 摄像头取帧

```js
// WXML: <camera frame-size="medium" mode="normal" />
const ctx = wx.createCameraContext()
const listener = ctx.onCameraFrame((frame) => {
  // frame.data: ArrayBuffer (RGBA), frame.width, frame.height
  // 降采样后累计亮度、或抽帧送后端
})
listener.start()
```

`frame-size` 可选 `small` / `medium` / `large`，越大越耗性能。建议 `small`（约 240p）足够做表情与亮度检测。

### 1.2 录音

```js
const rm = wx.getRecorderManager()
rm.start({
  duration: 60000,
  sampleRate: 16000,      // 16k 足够做 ASR 与声学特征
  numberOfChannels: 1,
  encodeBitRate: 48000,
  format: 'mp3',          // 或 'aac'；pcm 体积过大不推荐
  frameSize: 1024,        // 开启 onFrameRecorded 分片回调
})
rm.onStop((res) => {
  wx.uploadFile({ url: API + '/asr', filePath: res.tempFilePath, name: 'file' })
})
```

**配置要求**（来自官方文档）：
- 项目 `project.miniapp.json` 勾选 **Media SDK**
- Android：「Android 权限描述配置」→「允许应用程序录制音频」
- iOS：「隐私信息访问许可描述」→「麦克风」

---

## 2. 面部表情识别：三条路线

MediaPipe `tasks-vision` 依赖 DOM / Canvas / WebGL 与远程 WASM 模型加载，**在小程序里直接跑不通**。可选方案：

### 方案 A：VisionKit 原生人脸关键点（推荐先行验证）

```js
const session = wx.createVKSession({
  track: { face: { mode: 1 } },   // mode 1 = 摄像头实时；mode 2 = 静态图
  gl,                              // 需要 WebGL context
})
session.on('updateAnchors', (anchors) => {
  // anchors 含人脸关键点，据此计算嘴角/眉头/睁眼程度
})
session.start()
```

| 项 | 说明 |
|---|---|
| 版本要求 | 基础库 ≥ 2.25.0；安卓微信 ≥ 8.0.25，iOS 微信 ≥ 8.0.24 |
| 输出 | 人脸关键点坐标（不是 blendshapes） |
| 优点 | 原生性能、无需自研模型、**不上传原始画面** |
| 缺点 | 只给关键点，需自行推导表情特征；没有现成 blendshapes，精度低于 MediaPipe |

需要从关键点推导的特征（与 `companion/fusion.py` 的 `FACE_RULES` 对齐）：
- 嘴角上扬/下垂 → mouthSmile / mouthFrown
- 眉间距、眉毛倾斜 → browInnerUp / browDown
- 眼睛纵横比 EAR → eyeBlink / eyeWide
- 张嘴程度 → jawOpen

### 方案 B：抽帧上传后端分析

小程序端只做采样（如每 500ms 取 1 帧，缩到 160×160 JPEG），上传到后端用 OpenFace / MediaPipe Python 版分析。

| 优点 | 缺点 |
|---|---|
| 精度最高，可用任意模型 | **原始人脸画面上传，合规风险高**（见 §4） |
| 后端可迭代模型 | 流量与延迟大，5 分钟会话约 600 帧 |

### 方案 C：降级为纯语音

PRD §6.4 已明确允许。若面部通道不可用，`modality_availability.face = 0`，融合自动降权到语音+文本，报告里给降级提示。**这是必须实现的兜底路径。**

---

## 3. 需要改动的后端

当前后端基本可复用，改动集中在四处：

| 改动 | 说明 |
|---|---|
| **音频格式** | `asr.py` 的 `decode_audio` 现在靠 PyAV，需覆盖 mp3/aac（PyAV 支持，但需实测微信产出的编码参数） |
| **SSE 兼容** | 小程序无 EventSource。建议**新增一个非流式接口** `POST /companion/{sid}/turn_sync`，返回完整 reply；流式作为 Web 端专属 |
| **面部特征输入** | `FaceFeatures.blendshapes` 保持字段名不变，方案 A 只需在小程序端把关键点映射成同名 blendshapes |
| **响应延迟** | 当前约 8-10s/轮，需优化（见 §5） |

后端已有的 `/asr`、`/companion/*`、`/scales` 等接口均为标准 REST + JSON，可直接复用。
需配置**服务器域名白名单**：`request` / `uploadFile` / `socket` 合法域名，且必须 **HTTPS + 已备案**。

---

## 4. 合规：这是真正的门槛

### 4.1 人脸生物特征（🔴 最高优先级）

官方文档明确要求：

> 若小程序人脸识别功能涉及采集、存储用户生物特征（如人脸照片或视频…），
> **此类型服务需使用微信原生人脸识别接口**。

对本产品的直接影响：

- ✅ **允许**：本地实时提取表情特征，只上传数值，不留存原始画面
- ❌ **不允许**：自行上传/存储人脸照片或视频（方案 B 基本被排除）
- ❌ 不做身份识别/人脸核身（PRD §3 已声明不做，这点没问题）

因此**方案 A（VisionKit 本地提取）是唯一稳妥路线**。

### 4.2 类目与主体资质

| 项 | 要求 |
|---|---|
| 主体 | 个人主体大概率无法通过；需**企业/组织主体** + 营业执照 |
| 类目 | 心理健康相关属敏感类目。若涉及「心理咨询」「医疗信息」需相应资质（如与持证医疗机构合作、或《医疗机构执业许可证》相关材料）。**建议先以「工具 - 效率/健康管理」类目咨询微信侧审核意见，再定稿** |
| ICP 备案 | 小程序必须完成 ICP 备案（2024 起强制） |
| 隐私保护指引 | 必须在 mp 后台配置《小程序用户隐私保护指引》，并调用 `wx.requirePrivacyAuthorize` 获取同意 |
| 内容安全 | 用户输入文本需过 `security.msgSecCheck`；涉及 UGC 展示时过 `mediaCheckAsync` |

### 4.3 本 PRD 特别需要的合规动作

PRD §8 已列出知情同意、数据最小化、未成年人保护、免责声明。落到小程序上：

1. **强制前置同意页**：未勾选不同意不能进入（当前 Demo 已实现，小程序端同理）
2. **隐私指引二次确认**：`wx.requirePrivacyAuthorize` 是微信平台级强制要求，与产品内同意页**是两个独立环节**，都要做
3. **未成年人**：小程序没有可靠的年龄核验手段，建议入口处做年龄声明 + 监护人同意提示
4. **数据留存**：面部原始画面不落库；音频/转写文本留存需明确告知期限并可删除
5. **危机热线**：必须可按地区配置且定期核验（当前已走 `config.py`，小程序端同样从接口拉取，不硬编码）

---

## 5. 性能：当前不达标，需优化

PRD §7 要求**单轮响应延迟 < 2 秒**。当前实测（Qwen3.8-27B-FP8）：

| 阶段 | 耗时 |
|---|---|
| 一轮端到端（含 ASR + 风险检测 + 生成） | **约 8-10 秒** |
| 其中风险检测（额外一次 LLM 调用） | 约 2-4 秒 |

优化路径（按性价比排序）：

1. **合并风险检测与回复生成**：当前 `_detect_risk` 单独调一次 LLM，可与生成合并为一次结构化调用，省 2-4 秒
2. **换更小的模型**：当前 27B FP8；若用 7-14B + 针对性微调，延迟可降到 2-3 秒
3. **ASR 模型降级**：`small` → `base`/`tiny`（中文精度会降，需评估）
4. **并行化**：ASR 与面部特征聚合可并行
5. **流式首字优先**：先返回一句共情话术（"嗯，我在听…"）占位，再返回实质内容 —— 感知延迟可压到 1 秒内

**建议将 PRD §7 的 < 2 秒重新评审**：在当下模型能力下，「完整生成 < 2 秒」很难达到，更现实的目标是「**首字 < 1.5 秒 + 完整 < 5 秒**」。这需要产品侧确认。

---

## 6. 落地清单

### 6.1 需要准备的东西

**资质与账号**
- [ ] 企业/组织主体小程序账号 + 营业执照
- [ ] ICP 备案
- [ ] 已备案域名 + HTTPS 证书
- [ ] 心理健康相关类目资质（**先与微信侧确认类目可行性**）
- [ ] mp 后台配置《小程序用户隐私保护指引》

**技术资源**
- [ ] 后端服务（当前 FastAPI，需部署到可外网 HTTPS 访问的服务器）
- [ ] vLLM / LLM 推理服务（当前 `29.77.193.107:9000`，生产需独立部署与扩容）
- [ ] 对象存储（如需临时存放音频，建议用完即删）
- [ ] （可选）实时语音方案：若要做"边说边出字"，需接入微信同声传译插件或腾讯云 ASR

**人员**
- [ ] 小程序前端（熟悉 `camera` / `RecorderManager` / VisionKit）
- [ ] 精神卫生专业顾问（PRD §10 已提出，用于风险话术与分级评审）

### 6.2 开发工作量估计

| 模块 | 工作量 | 说明 |
|---|---|---|
| 小程序端采集（相机+录音+授权） | 1-2 周 | |
| 面部特征（VisionKit 关键点 → blendshapes 映射） | 2-3 周 | **含调参验证，风险最高** |
| 页面：同意/质检/对话/报告 | 1-2 周 | 可复用现有 React 逻辑思路 |
| 后端适配（格式/非流式接口） | 3-5 天 | |
| 性能优化到可上线 | 2-3 周 | 见 §5 |
| 合规报审 | 2-4 周 | **周期不可控，建议并行启动** |

---

## 7. 建议的推进顺序

1. **立刻启动合规咨询**（周期最长、不确定性最高）：与微信侧确认心理健康类目可行性，与法务确认人脸数据处理方式
2. **并行做技术验证（1 周）**：用 VisionKit 跑通「关键点 → 表情特征 → 融合」，验证精度是否可接受；若不可行，及时决定降级为纯语音首版
3. **首版建议砍掉视觉模态**：PRD §6.4 已允许降级。先用「语音 + 文本」上线验证核心价值，视觉作为二期 —— 可规避人脸合规风险，且大幅缩短周期
4. **性能优化与模型选型**作为上线前专项

---

## 参考

- `wx.getRecorderManager` — https://developers.weixin.qq.com/miniprogram/dev/api/media/recorder/wx.getRecorderManager.html
- `CameraContext.onCameraFrame` — https://developers.weixin.qq.com/miniprogram/dev/api/media/camera/CameraContext.onCameraFrame.html
- `VKSession.detectFace` / VisionKit — https://developers.weixin.qq.com/miniprogram/dev/api/ai/visionkit/VKSession.detectFace.html
  （含关键限制：涉及采集/存储人脸生物特征需使用微信原生人脸识别接口）
- 小程序用户隐私保护指引：`wx.requirePrivacyAuthorize`
- 内容安全：`security.msgSecCheck`

> 本文档基于 2026-09 的公开文档整理，小程序平台规则会调整，**上线前需以微信官方最新规则为准**。
