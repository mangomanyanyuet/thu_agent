const api = require('../../utils/api')
const fmt = require('../../utils/format')

// 后端 perception.SCALES 的 max 是「单题最高分」，据此决定选项文案
const OPTION_SETS = {
  3: ['完全没有', '有几天', '一半以上天数', '几乎每天'],
  4: ['从不', '很少', '有时', '经常', '总是'],
}

Page({
  data: {
    scaleKeys: [],
    scales: {},
    current: 'PHQ-9',
    meta: { name: '', items: [], max: 3 },
    options: OPTION_SETS[3],
    answers: [],
    answered: 0,
    total: 0,
    maxTotal: 0,
    result: null,
    riskLevel: 0,
    riskMeta: fmt.riskMeta('none'),
    riskEvidence: '',
    hotlines: [],
  },

  onLoad() {
    this.userId = getApp().globalData.userId
    this.loadScales()
  },

  loadScales() {
    api
      .scaleList()
      .then((scales) => {
        const keys = Object.keys(scales)
        this.setData({ scales, scaleKeys: keys, current: keys[0] || 'PHQ-9' })
        this.applyScale()
      })
      .catch((err) => {
        wx.showToast({ title: err.message || '加载量表失败', icon: 'none' })
      })

    // 热线随量表一起预取：PHQ-9 第 9 题可能直接触发风险通道
    const app = getApp()
    if (app.globalData.bootstrap) {
      this.setData({ hotlines: app.globalData.bootstrap.hotlines || [] })
    } else {
      api.bootstrap(this.userId).then((b) => this.setData({ hotlines: b.hotlines || [] })).catch(() => {})
    }
  },

  applyScale() {
    const meta = this.data.scales[this.data.current] || { name: '', items: [], max: 3 }
    const opts = OPTION_SETS[meta.max] || OPTION_SETS[3]
    this.setData({
      meta,
      options: opts,
      answers: new Array(meta.items.length).fill(-1),
      answered: 0,
      total: 0,
      maxTotal: meta.items.length * meta.max,
      result: null,
      riskLevel: 0,
      riskEvidence: '',
    })
  },

  switchScale(e) {
    this.setData({ current: e.currentTarget.dataset.key })
    this.applyScale()
  },

  pick(e) {
    const q = e.currentTarget.dataset.q
    const v = e.currentTarget.dataset.v
    const answers = this.data.answers.slice()
    answers[q] = v
    const answered = answers.filter((x) => x >= 0).length
    const total = answers.reduce((s, x) => s + (x > 0 ? x : 0), 0)
    this.setData({ answers, answered, total })
  },

  reset() {
    this.applyScale()
  },

  submit() {
    const { current, answers, meta } = this.data
    if (answers.some((x) => x < 0)) {
      wx.showToast({ title: '还有题目没答', icon: 'none' })
      return
    }
    api
      .submitScale(this.userId, current, answers)
      .then((r) => {
        const level = Number(r.risk_level || 0)
        this.setData({
          result: r.score,
          riskLevel: level,
          riskMeta: fmt.riskMeta(['none', 'low', 'moderate', 'high', 'imminent'][level]),
          riskEvidence: r.risk_evidence || '',
        })
        if (level > 0) {
          wx.showModal({
            title: '请留意',
            content: '量表反映出你可能有伤害自己的念头。你愿意现在和心理援助热线聊聊吗？',
            confirmText: '拨打热线',
            cancelText: '稍后',
            success: (m) => {
              if (m.confirm && this.data.hotlines.length) {
                wx.makePhoneCall({ phoneNumber: String(this.data.hotlines[0].number), fail() {} })
              }
            },
          })
        }
      })
      .catch((err) => wx.showToast({ title: err.message || '提交失败', icon: 'none' }))
  },

  callHotline(e) {
    wx.makePhoneCall({ phoneNumber: String(e.currentTarget.dataset.number), fail() {} })
  },
})
