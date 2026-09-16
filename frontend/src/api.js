const BASE = '/api'

const LS = { token: 'mt_token', uid: 'mt_user', name: 'mt_username' }

/** 登录态：token + user_id + username，存 localStorage */
export const session = {
  get() {
    return {
      token: localStorage.getItem(LS.token) || '',
      user_id: localStorage.getItem(LS.uid) || '',
      username: localStorage.getItem(LS.name) || '',
    }
  },
  set(token, userId, username) {
    localStorage.setItem(LS.token, token)
    localStorage.setItem(LS.uid, userId)
    localStorage.setItem(LS.name, username)
  },
  clear() {
    Object.values(LS).forEach((k) => localStorage.removeItem(k))
  },
}

function authHeaders() {
  const t = session.get().token
  return t ? { 'X-Auth-Token': t } : {}
}

/** 401 时清掉本地登录态并通知界面回到登录页 */
function onUnauthorized() {
  session.clear()
  window.dispatchEvent(new CustomEvent('mt:unauthorized'))
}

async function req(path, options) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    ...options,
  })
  if (res.status === 401) {
    onUnauthorized()
    throw new Error('登录已过期，请重新登录')
  }
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${text.slice(0, 200)}`)
  }
  return res.json()
}

/** 语音转写（multipart 上传，不走 JSON） */
async function uploadAudio(blob) {
  const form = new FormData()
  form.append('file', blob, 'voice.webm')
  const res = await fetch(`${BASE}/asr`, {
    method: 'POST',
    body: form,
    headers: authHeaders(),
  })
  if (res.status === 401) {
    onUnauthorized()
    throw new Error('登录已过期，请重新登录')
  }
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`)
  return res.json()
}

export const api = {
  // ---- 账号 ----
  setSession: (token, userId, username) => session.set(token, userId, username),
  clearSession: () => session.clear(),
  getSession: () => session.get(),
  authHint: () => req('/auth/hint'),
  login: (username, password) =>
    req('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  register: (username, password, displayName) =>
    req('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ username, password, display_name: displayName || '' }),
    }),
  logout: () =>
    req('/auth/logout', { method: 'POST', headers: { ...authHeaders() } }),
  me: () => req('/auth/me'),

  health: () => req('/health'),
  asrStatus: () => req('/asr/status'),
  asr: (blob) => uploadAudio(blob),

  // ---- 陪伴会话（5 分钟视频语音）----
  companionStart: (userId) =>
    req('/companion/start', { method: 'POST', body: JSON.stringify({ user_id: userId }) }),
  companionConsent: (sid, consent) =>
    req(`/companion/${sid}/consent`, { method: 'POST', body: JSON.stringify(consent) }),
  companionQuality: (sid, quality) =>
    req(`/companion/${sid}/quality`, { method: 'POST', body: JSON.stringify(quality) }),
  companionTurn: (sid, payload) =>
    req(`/companion/${sid}/turn`, { method: 'POST', body: JSON.stringify(payload) }),
  companionReport: (sid) => req(`/companion/${sid}/report`),
  // 收尾：沉淀每日记录 + 更新画像 + 更新风险趋势（幂等）
  companionFinish: (sid) =>
    req(`/companion/${sid}/finish`, { method: 'POST' }),
  companionAbort: (sid) =>
    req(`/companion/${sid}/abort`, { method: 'POST' }),
  createSession: (userId) =>
    req(`/sessions?user_id=${encodeURIComponent(userId)}`, { method: 'POST' }),
  listSessions: (userId) => req(`/sessions/${encodeURIComponent(userId)}`),
  messages: (sid) => req(`/messages/${sid}`),
  overview: (userId) => req(`/overview/${encodeURIComponent(userId)}`),
  daily: (userId, limit = 14) => req(`/daily/${encodeURIComponent(userId)}?limit=${limit}`),
  reportDaily: (payload) =>
    req('/daily', { method: 'POST', body: JSON.stringify(payload) }),
  scales: () => req('/scales'),
  submitScale: (payload) => req('/scales', { method: 'POST', body: JSON.stringify(payload) }),
  // 可观测：决策轨迹（tracer 事件流）
  traceEvents: (limit = 200, kind = '') =>
    req(`/trace/events?limit=${limit}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`),
  scaleHistory: (userId, name) =>
    req(`/scales/${encodeURIComponent(userId)}?scale_name=${encodeURIComponent(name)}`),
  pendingReviews: (minLevel = 2) => req(`/review/pending?min_level=${minLevel}`),
  submitReview: (payload) => req('/review', { method: 'POST', body: JSON.stringify(payload) }),
  handleFlag: (id, reviewer, note) =>
    req(`/risk/${id}/handle?reviewer=${encodeURIComponent(reviewer)}&note=${encodeURIComponent(note)}`,
      { method: 'POST' }),
  traces: (sid) => req(`/traces/${sid}`),
}

/**
 * SSE 流式对话。
 * 事件顺序：meta(风险/情绪先到) -> delta(逐字) -> done
 * 这样前端可以在正文还没出来时就先把热线卡片顶上去。
 */
export function streamChat({ userId, sessionId, text, voice }, handlers) {
  return new Promise((resolve, reject) => {
    fetch(`${BASE}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, session_id: sessionId, text, voice }),
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        const pump = () =>
          reader.read().then(({ done, value }) => {
            if (done) {
              resolve()
              return
            }
            buffer += decoder.decode(value, { stream: true })
            const chunks = buffer.split('\n\n')
            buffer = chunks.pop() || ''
            for (const chunk of chunks) {
              const evMatch = chunk.match(/^event: (.*)$/m)
              const dataMatch = chunk.match(/^data: (.*)$/m)
              if (!evMatch || !dataMatch) continue
              const event = evMatch[1]
              let data
              try {
                data = JSON.parse(dataMatch[1])
              } catch {
                continue
              }
              if (event === 'meta') handlers.onMeta?.(data)
              else if (event === 'delta') handlers.onDelta?.(data.t)
              else if (event === 'done') handlers.onDone?.(data)
            }
            return pump()
          })
        return pump()
      })
      .catch(reject)
  })
}
