const api = require('../../utils/api')
const fmt = require('../../utils/format')

Page({
  data: {
    items: [],
  },

  onShow() {
    this.load()
  },

  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh())
  },

  load() {
    return api
      .pendingReviews()
      .then((rows) => {
        this.setData({
          items: (rows || []).map((f) => ({
            id: f.id,
            session_id: f.session_id,
            shortSid: String(f.session_id || '').slice(-8),
            levelText: fmt.riskMeta(f.level).label,
            cls: fmt.riskMeta(f.level).cls,
            sourceText: fmt.sourceLabel(f.source),
            timeText: fmt.shortTime(f.ts),
            evidence: f.evidence,
            cssrs_dimension: f.cssrs_dimension || '',
            editing: false,
            draft: '',
          })),
        })
      })
      .catch((err) => wx.showToast({ title: err.message || '加载失败', icon: 'none' }))
  },

  toggleEdit(e) {
    this.patch(e.currentTarget.dataset.id, { editing: true, draft: '' })
  },

  onDraft(e) {
    this.patch(e.currentTarget.dataset.id, { draft: e.detail.value })
  },

  approve(e) {
    this.submit(e.currentTarget.dataset.id, 'approve', '')
  },

  override(e) {
    const id = e.currentTarget.dataset.id
    const item = this.data.items.find((x) => x.id === id)
    const reply = (item && item.draft || '').trim()
    if (!reply) {
      wx.showToast({ title: '请先填写改写内容', icon: 'none' })
      return
    }
    this.submit(id, 'override', reply)
  },

  markHandled(e) {
    const id = e.currentTarget.dataset.id
    api
      .handleFlag(id, '小程序审核台标记')
      .then(() => {
        wx.showToast({ title: '已标记', icon: 'success' })
        this.load()
      })
      .catch((err) => wx.showToast({ title: err.message || '操作失败', icon: 'none' }))
  },

  /**
   * 提交人工决定 -> 后端 Command(resume=...) 放行被挂起的会话。
   * 注意 resume 是服务端阻塞式的，这一轮可能要等模型跑完。
   */
  submit(id, action, reply) {
    const item = this.data.items.find((x) => x.id === id)
    if (!item) return
    if (!item.session_id || item.session_id === 'daily' || item.session_id === 'scale') {
      // 这类 flag 来自量表/每日上报，没有挂起的会话，直接标记即可
      api
        .handleFlag(id, `${action}:${reply}`)
        .then(() => {
          wx.showToast({ title: '已处理', icon: 'success' })
          this.load()
        })
        .catch((err) => wx.showToast({ title: err.message || '操作失败', icon: 'none' }))
      return
    }

    api
      .submitReview(item.session_id, action, reply)
      .then(() => {
        wx.showToast({ title: action === 'override' ? '已改写放行' : '已放行', icon: 'success' })
        return api.handleFlag(id, action)
      })
      .then(() => this.load())
      .catch((err) => wx.showToast({ title: err.message || '处理失败', icon: 'none' }))
  },

  patch(id, changes) {
    const items = this.data.items.map((x) => (x.id === id ? Object.assign({}, x, changes) : x))
    this.setData({ items })
  },
})
