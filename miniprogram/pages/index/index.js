const api = require('../../utils/api')

const DEFAULT_DISCLAIMER =
  '本服务由 AI 提供情绪陪伴与自助练习，不是医疗诊断，也不能替代专业治疗。'

Page({
  data: {
    online: false,
    apiBase: '',
    providerText: '-',
    asrText: '-',
    disclaimer: DEFAULT_DISCLAIMER,
    hotlines: [],
  },

  onShow() {
    this.setData({ apiBase: api.base() })
    this.refreshStatus()
  },

  onPullDownRefresh() {
    this.refreshStatus().then(() => wx.stopPullDownRefresh())
  },

  refreshStatus() {
    const app = getApp()
    return api
      .health()
      .then((h) => {
        this.setData({ online: true, providerText: h.llm_provider || '-' })
        return api.bootstrap(app.globalData.userId)
      })
      .then((b) => {
        app.globalData.bootstrap = b
        this.setData({
          disclaimer: b.disclaimer || DEFAULT_DISCLAIMER,
          hotlines: b.hotlines || [],
          asrText: b.asr_available ? '可用' : '未安装',
        })
      })
      .catch(() => {
        // 拿不到配置时也要保留一份硬编码热线兜底 —— 热线永远不能因为接口挂了就消失
        this.setData({
          online: false,
          providerText: '-',
          asrText: '-',
          hotlines: [
            { name: '全国心理援助热线', number: '12356' },
            { name: '医疗急救', number: '120' },
            { name: '报警求助', number: '110' },
          ],
        })
      })
  },

  onBaseInput(e) {
    this.setData({ apiBase: e.detail.value })
  },

  saveBase() {
    const app = getApp()
    app.setApiBase(this.data.apiBase)
    this.setData({ apiBase: api.base() })
    this.refreshStatus()
  },

  callHotline(e) {
    const number = e.currentTarget.dataset.number
    wx.makePhoneCall({
      phoneNumber: String(number),
      fail() {
        // 开发者工具不支持拨号，静默忽略即可
      },
    })
  },

  goChat() {
    wx.switchTab({ url: '/pages/chat/chat' })
  },
  goScale() {
    wx.switchTab({ url: '/pages/scale/scale' })
  },
  goOverview() {
    wx.switchTab({ url: '/pages/overview/overview' })
  },
  goReview() {
    wx.navigateTo({ url: '/pages/review/review' })
  },
})
