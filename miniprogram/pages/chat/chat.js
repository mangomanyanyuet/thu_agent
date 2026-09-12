const api = require('../../utils/api')
const recorder = require('../../utils/recorder')
const fmt = require('../../utils/format')

// 首字前的等待提示。单轮约 3~10s，空着不动会让用户以为卡死了。
const WAIT_TIPS = ['我在听…', '让我想想怎么回应你…', '嗯，我在整理你说的…']

let seq = 0
const nextId = () => ++seq

Page({
  data: {
    messages: [],
    draft: '',
    loading: false,
    loadingText: WAIT_TIPS[0],
    toView: '',

    riskMeta: fmt.riskMeta('none'),
    cbtStage: '',
    degraded: false,
    pendingReview: false,
    suspendedReason: '',

    recording: false,
    recordSec: 0,
    volume: 0,
    asrTip: '语音不留存原始音频，只提取声学特征用于情绪识别',
  },

  onLoad() {
    const app = getApp()
    this.sessionId = wx.getStorageSync('sessionId') || ''
    this.userId = app.globalData.userId
    this.waitTimer = null
    this.secTimer = null

    this.pushAgent(
      '嗨，很高兴今天能和你聊聊。接下来你想说什么都可以，没有对错。今天整体感觉怎么样？'
    )
  },

  onUnload() {
    this.clearTimers()
  },

  clearTimers() {
    if (this.waitTimer) clearInterval(this.waitTimer)
    if (this.secTimer) clearInterval(this.secTimer)
    this.waitTimer = this.secTimer = null
  },

  pushUser(text, voice) {
    const id = nextId()
    const list = this.data.messages.concat([
      { id, role: 'user', text: voice ? `🎙️ ${text}` : text },
    ])
    this.setData({ messages: list })
    this.scrollTo(id)
  },

  pushAgent(text, extra) {
    const id = nextId()
    const msg = Object.assign({ id, role: 'agent', text, evidenceOpen: false }, extra || {})
    this.setData({ messages: this.data.messages.concat([msg]) })
    this.scrollTo(id)
    return id
  },

  scrollTo(id) {
    this.setData({ toView: 'm' + id })
  },

  scrollBottom() {
    this.setData({ toView: 'anchor' })
  },

  // ---------------------------------------------------------------- 发送
  onDraft(e) {
    this.setData({ draft: e.detail.value })
  },

  sendText() {
    const text = (this.data.draft || '').trim()
    if (!text || this.data.loading) return
    this.setData({ draft: '' })
    this.send(text, null)
  },

  /**
   * @param {string} text
   * @param {object|null} voice /api/asr 的返回（{audio_features, tone, tone_reason, summary}）
   */
  send(text, voice) {
    if (this.data.loading) return
    this.pushUser(text, voice)

    this.setData({ loading: true, loadingText: WAIT_TIPS[0], degraded: false })
    let tipIdx = 0
    this.waitTimer = setInterval(() => {
      tipIdx = (tipIdx + 1) % WAIT_TIPS.length
      this.setData({ loadingText: WAIT_TIPS[tipIdx] })
    }, 2200)

    api
      .chat(this.userId, this.sessionId, text, voice)
      .then((r) => {
        // 服务端可能自愈重建了会话，客户端要以返回的为准
        if (r.session_id && r.session_id !== this.sessionId) {
          this.sessionId = r.session_id
          wx.setStorageSync('sessionId', r.session_id)
        }
        this.applyResult(r)
      })
      .catch((err) => {
        // 网络层彻底失败：仍然要给出可操作的话术，不能让用户对着空白页
        this.pushAgent('刚才网络不太稳，我没能接住你的话。你愿意再说一次吗？\n\n如果现在很难受，可以先拨打全国心理援助热线 12356。')
        wx.showToast({ title: err.message || '请求失败', icon: 'none', duration: 3000 })
      })
      .finally(() => {
        this.clearTimers()
        this.setData({ loading: false })
        this.scrollBottom()
      })
  },

  /** 把后端一轮的完整结果渲染出来 */
  applyResult(r) {
    const level = r.risk_level || 'none'
    const meta = fmt.riskMeta(level)
    const crisis = !!r.crisis_mode || level === 'high' || level === 'imminent'

    // 三通道证据（rule / model / multimodal / scale）
    const channelList = []
    const channels = r.channels || {}
    Object.keys(channels).forEach((k) => {
      const c = channels[k] || {}
      const lv = Number(c.level || 0)
      channelList.push({
        key: k,
        label: fmt.channelLabel(k),
        levelText: ['none', 'low', 'moderate', 'high', 'imminent'][lv] || 'none',
        cls: fmt.riskMeta(['none', 'low', 'moderate', 'high', 'imminent'][lv]).cls,
        evidence: c.evidence || '',
      })
    })

    this.pushAgent(r.reply || '', {
      emotion: fmt.emotionLabel(r.emotion && r.emotion.primary_emotion),
      stage: fmt.cbtLabel(r.cbt_stage),
      latency: fmt.ms(r.latency_ms),
      channels: channels,
      channelList,
      hasAnyChannel: channelList.some((c) => c.levelText !== 'none' || c.evidence),
      hotlines: crisis || level === 'moderate' ? r.hotlines || [] : [],
    })

    this.setData({
      riskMeta: meta,
      cbtStage: fmt.cbtLabel(r.cbt_stage),
      degraded: !!r.degraded,
      pendingReview: !!r.interrupted,
      suspendedReason: r.interrupt_reason || '',
    })
    this.scrollBottom()
  },

  toggleEvidence(e) {
    const id = e.currentTarget.dataset.id
    const list = this.data.messages.map((m) =>
      m.id === id ? Object.assign({}, m, { evidenceOpen: !m.evidenceOpen }) : m
    )
    this.setData({ messages: list })
  },

  callHotline(e) {
    wx.makePhoneCall({
      phoneNumber: String(e.currentTarget.dataset.number),
      fail() {},
    })
  },

  goReview() {
    wx.navigateTo({ url: '/pages/review/review' })
  },

  // ---------------------------------------------------------------- 录音
  startRecord() {
    if (this.data.loading) return
    recorder
      .start((v) => this.setData({ volume: v }))
      .then(() => {
        this.setData({ recording: true, recordSec: 0, volume: 0 })
        this.secTimer = setInterval(() => {
          if (this.data.recordSec >= 120) {
            this.endRecord()
            return
          }
          this.setData({ recordSec: this.data.recordSec + 1 })
        }, 1000)
      })
      .catch((err) => {
        wx.showToast({ title: err.message || '无法录音', icon: 'none' })
      })
  },

  endRecord() {
    if (!this.data.recording) return
    if (this.secTimer) clearInterval(this.secTimer)
    this.secTimer = null
    this.setData({ recording: false })

    recorder
      .stop()
      .then(({ filePath }) => api.uploadAudio(filePath))
      .then((asr) => {
        // 转写失败不该阻断对话：文本为空时让用户补说，但声学特征仍然带着走，
        // 因为「语气低落 + 说没事」本身就是重要的掩饰信号。
        const text = (asr.text || '').trim()
        const voice = {
          audio_features: asr.audio_features || {},
          tone: asr.tone || '',
          tone_reason: asr.tone_reason || '',
          summary: asr.summary || '',
        }
        if (!text) {
          wx.showModal({
            title: '没听清',
            content: '没能识别出你说的话。可以直接打字，或再录一次。',
            showCancel: false,
          })
          return
        }
        this.send(text, voice)
      })
      .catch((err) => {
        wx.showToast({ title: err.message || '语音识别失败', icon: 'none', duration: 2500 })
      })
  },

  cancelRecord() {
    if (!this.data.recording) return
    if (this.secTimer) clearInterval(this.secTimer)
    this.secTimer = null
    recorder.cancel()
    this.setData({ recording: false, volume: 0 })
    wx.showToast({ title: '已取消', icon: 'none' })
  },
})
