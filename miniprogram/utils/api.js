/**
 * 后端接口封装。
 *
 * 小程序侧的两个硬约束：
 * 1. 没有 EventSource/SSE —— 全部走同步接口（/api/mini/*），不用 /api/chat/stream。
 * 2. 本地缓存的 session_id 可能因服务端重启/换机失效 —— /api/mini/chat 会自动重建会话，
 *    客户端只需把返回的新 session_id 存回来即可。
 */

function base() {
  const app = getApp()
  return (app && app.globalData.apiBase) || wx.getStorageSync('apiBase') || ''
}

function request(path, options = {}) {
  const { method = 'GET', data, timeout = 90000, loading = '' } = options
  if (loading) wx.showLoading({ title: loading, mask: true })
  return new Promise((resolve, reject) => {
    wx.request({
      url: base() + path,
      method,
      data,
      timeout,
      header: { 'Content-Type': 'application/json' },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data)
        } else {
          const detail = (res.data && (res.data.detail || res.data.message)) || ''
          reject(new Error(`HTTP ${res.statusCode} ${detail}`.trim()))
        }
      },
      fail(err) {
        reject(new Error(humanizeNetworkError(err)))
      },
      complete() {
        if (loading) wx.hideLoading()
      },
    })
  })
}

// 小程序给的网络错误很晦涩（"request:fail url not in domain list"），这里翻译成人话
function humanizeNetworkError(err) {
  const msg = (err && err.errMsg) || ''
  if (msg.includes('url not in domain list')) {
    return '域名不在小程序合法域名列表中。开发阶段请在「详情-本地设置」勾选「不校验合法域名」，生产需在 mp 后台配置 request 合法域名（HTTPS）。'
  }
  if (msg.includes('ssl')) return 'HTTPS 证书校验失败，请检查服务器证书。'
  if (msg.includes('timeout')) return '请求超时，服务可能过载，请稍后重试。'
  if (msg.includes('fail')) return '无法连接服务器，请检查服务是否已启动、地址是否正确。'
  return msg || '网络请求失败'
}

// ------------------------------------------------------------------ 接口
const api = {
  base,
  request,

  /** 首屏一次性配置：热线 / 量表定义 / ASR 可用性 / 免责声明 */
  bootstrap(userId) {
    return request(`/api/mini/bootstrap?user_id=${encodeURIComponent(userId)}`, { timeout: 15000 })
  },

  createSession(userId) {
    return request('/api/mini/session', {
      method: 'POST',
      data: { user_id: userId },
      timeout: 15000,
    })
  },

  /** 同步对话。sessionId 可为空或已失效，服务端会自愈。 */
  chat(userId, sessionId, text, voice) {
    return request('/api/mini/chat', {
      method: 'POST',
      data: { user_id: userId, session_id: sessionId || null, text, voice: voice || null },
    })
  },

  /** 语音 -> 转写 + 声学特征 + 语气（multipart 上传） */
  uploadAudio(filePath) {
    return new Promise((resolve, reject) => {
      wx.showLoading({ title: '识别中', mask: true })
      wx.uploadFile({
        url: base() + '/api/asr?language=zh',
        filePath,
        name: 'file',
        timeout: 120000,
        success(res) {
          wx.hideLoading()
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(res.data))
            } catch (e) {
              reject(new Error('语音服务返回格式异常'))
            }
          } else {
            reject(new Error(`HTTP ${res.statusCode}`))
          }
        },
        fail(err) {
          wx.hideLoading()
          reject(new Error(humanizeNetworkError(err)))
        },
      })
    })
  },

  asrStatus() {
    return request('/api/asr/status', { timeout: 10000 })
  },

  scaleList() {
    return request('/api/scales', { timeout: 15000 })
  },

  submitScale(userId, scaleName, itemScores) {
    return request('/api/mini/scales', {
      method: 'POST',
      data: { user_id: userId, scale_name: scaleName, item_scores: itemScores },
      loading: '计分中',
    })
  },

  overview(userId) {
    return request(`/api/mini/overview?user_id=${encodeURIComponent(userId)}`, { loading: '加载中' })
  },

  scaleHistory(userId, scaleName) {
    return request(`/api/scales/${encodeURIComponent(userId)}?scale_name=${encodeURIComponent(scaleName)}`)
  },

  pendingReviews() {
    return request('/api/mini/review/pending?min_level=1', { loading: '加载中' })
  },

  submitReview(sessionId, action, reply) {
    return request('/api/mini/review', {
      method: 'POST',
      data: { session_id: sessionId, action, reply: reply || '', reviewer: 'mini_reviewer' },
      loading: '提交中',
    })
  },

  handleFlag(flagId, note) {
    return request(`/api/risk/${flagId}/handle?reviewer=mini_reviewer&note=${encodeURIComponent(note || '')}`, {
      method: 'POST',
    })
  },

  health() {
    return request('/api/health', { timeout: 8000 })
  },
}

module.exports = api
