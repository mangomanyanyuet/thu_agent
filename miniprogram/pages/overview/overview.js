const api = require('../../utils/api')
const fmt = require('../../utils/format')

const LEVEL_NAMES = ['none', 'low', 'moderate', 'high', 'imminent']
const CHART_SCALES = ['PHQ-9', 'GAD-7']

Page({
  data: {
    profileRendered: '',
    chartSeries: [],
    riskFlags: [],
    dailyRecords: [],
    hotlines: [],
  },

  onShow() {
    this.userId = getApp().globalData.userId
    this.load()
  },

  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh())
  },

  load() {
    return api
      .overview(this.userId)
      .then((d) => {
        const flags = (d.risk_flags || []).map((f) => ({
          id: f.id,
          levelText: fmt.riskMeta(f.level).label,
          cls: fmt.riskMeta(f.level).cls,
          sourceText: fmt.sourceLabel(f.source),
          timeText: fmt.shortTime(f.ts),
          evidence: f.evidence,
          handled: !!f.handled,
          handled_by: f.handled_by || '',
        }))

        const records = (d.daily_records || []).map((r) => ({
          id: r.id,
          date: r.date,
          narrative: r.narrative,
          modality_summary: r.modality_summary,
          riskText: r.narrative_analysis ? '已分析' : '',
        }))

        this.setData({
          profileRendered: d.profile_rendered || '',
          riskFlags: flags,
          dailyRecords: records,
          hotlines: d.hotlines || [],
          chartSeries: this.buildSeries(d.scales || {}),
        })

        // canvas 必须在 setData 渲染完成后才能拿到节点
        this.data.chartSeries.forEach((_, i) => this.drawChart(i))
      })
      .catch((err) => wx.showToast({ title: err.message || '加载失败', icon: 'none' }))
  },

  buildSeries(scales) {
    const out = []
    CHART_SCALES.forEach((name) => {
      const rows = (scales[name] || []).slice().sort((a, b) => (a.date > b.date ? 1 : -1))
      if (!rows.length) return
      out.push({
        name,
        points: rows.map((r) => ({ x: r.date, y: r.total_score })),
        last: rows[rows.length - 1].total_score,
        lastSeverity: rows[rows.length - 1].severity || '',
      })
    })
    return out
  },

  /**
   * 用 canvas 2d 手绘折线图。
   * 小程序没有 DOM，也没有 echarts 的轻量版能直接塞进来，
   * 一两条折线自己画最省事，也避免引入几百 KB 的图表库。
   */
  drawChart(index) {
    const series = this.data.chartSeries[index]
    if (!series || !series.points.length) return

    wx.createSelectorQuery()
      .in(this)
      .select(`#chart-${index}`)
      .fields({ node: true, size: true })
      .exec((res) => {
        if (!res || !res[0] || !res[0].node) return
        const canvas = res[0].node
        const ctx = canvas.getContext('2d')
        const dpr = (wx.getWindowInfo && wx.getWindowInfo().pixelRatio) ||
          wx.getSystemInfoSync().pixelRatio || 2
        const W = res[0].width
        const H = res[0].height
        canvas.width = W * dpr
        canvas.height = H * dpr
        ctx.scale(dpr, dpr)
        ctx.clearRect(0, 0, W, H)

        const pad = { l: 28, r: 12, t: 14, b: 22 }
        const pts = series.points
        const maxY = Math.max(10, ...pts.map((p) => p.y))
        const minY = 0
        const n = pts.length

        const px = (i) =>
          n === 1 ? pad.l + (W - pad.l - pad.r) / 2 : pad.l + (i * (W - pad.l - pad.r)) / (n - 1)
        const py = (v) =>
          pad.t + (1 - (v - minY) / (maxY - minY || 1)) * (H - pad.t - pad.b)

        // 网格与 Y 轴刻度
        ctx.strokeStyle = '#ece7e1'
        ctx.lineWidth = 1
        ctx.fillStyle = '#b5aea5'
        ctx.font = '10px sans-serif'
        ctx.textAlign = 'right'
        for (let g = 0; g <= 2; g++) {
          const v = minY + ((maxY - minY) * g) / 2
          const y = py(v)
          ctx.beginPath()
          ctx.moveTo(pad.l, y)
          ctx.lineTo(W - pad.r, y)
          ctx.stroke()
          ctx.fillText(String(Math.round(v)), pad.l - 6, y + 3)
        }

        // 折线：单个点画成圆，多个点连线
        ctx.strokeStyle = '#6b8e7f'
        ctx.fillStyle = '#6b8e7f'
        ctx.lineWidth = 2
        if (n === 1) {
          ctx.beginPath()
          ctx.arc(px(0), py(pts[0].y), 4, 0, Math.PI * 2)
          ctx.fill()
        } else {
          ctx.beginPath()
          pts.forEach((p, i) => {
            const x = px(i)
            const y = py(p.y)
            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
          })
          ctx.stroke()
          pts.forEach((p, i) => {
            ctx.beginPath()
            ctx.arc(px(i), py(p.y), 3.5, 0, Math.PI * 2)
            ctx.fill()
          })
        }

        // X 轴：只标首尾日期，避免拥挤
        ctx.fillStyle = '#b5aea5'
        ctx.textAlign = 'left'
        ctx.fillText(shortDate(pts[0].x), pad.l, H - 6)
        if (n > 1) {
          ctx.textAlign = 'right'
          ctx.fillText(shortDate(pts[n - 1].x), W - pad.r, H - 6)
        }
      })
  },

  clearProfile() {
    wx.showModal({
      title: '清空画像',
      content: '将删除系统沉淀的关于你的长期事实（触发源、应对方式等）。风险记录不会被删除，因为它们是安全相关的。',
      confirmText: '确认清空',
      confirmColor: '#c2604f',
      success: (m) => {
        if (!m.confirm) return
        api
          .request('/api/profile', {
            method: 'POST',
            data: {
              user_id: this.userId,
              personality_traits: [],
              coping_style: [],
              social_support: [],
            },
            loading: '处理中',
          })
          .then(() => {
            wx.showToast({ title: '已清空', icon: 'success' })
            this.load()
          })
          .catch((err) => wx.showToast({ title: err.message || '操作失败', icon: 'none' }))
      },
    })
  },

  callHotline(e) {
    wx.makePhoneCall({ phoneNumber: String(e.currentTarget.dataset.number), fail() {} })
  },
})

function shortDate(d) {
  return String(d || '').slice(5)
}
