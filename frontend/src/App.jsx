import { useCallback, useEffect, useState } from 'react'
import { api, session } from './api.js'
import LoginView from './views/LoginView.jsx'
import CompanionView from './views/CompanionView.jsx'
import ChatView from './views/ChatView.jsx'
import DailyView from './views/DailyView.jsx'
import ScaleView from './views/ScaleView.jsx'
import ProfileView from './views/ProfileView.jsx'
import ReviewView from './views/ReviewView.jsx'
import TraceView from './views/TraceView.jsx'
import EventTraceView from './views/EventTraceView.jsx'
import { RISK_META } from './theme.js'

const TABS = [
  { key: 'companion', label: '视频语音陪伴', comp: CompanionView },
  { key: 'chat', label: '对话', comp: ChatView },
  { key: 'daily', label: '每日记录', comp: DailyView },
  { key: 'scale', label: '量表', comp: ScaleView },
  { key: 'profile', label: '画像与趋势', comp: ProfileView },
  { key: 'review', label: '人工审核台', comp: ReviewView },
  { key: 'trace', label: '上下文装配', comp: TraceView },
  { key: 'events', label: '决策轨迹', comp: EventTraceView },
]

export default function App() {
  // 登录态：user_id 由服务端在登录时下发，不再由客户端随意指定
  const [auth, setAuth] = useState(() => session.get())
  const [tab, setTab] = useState('companion')
  const [sessionId, setSessionId] = useState(localStorage.getItem('mt_sid') || '')
  const [overview, setOverview] = useState(null)
  const [health, setHealth] = useState(null)
  const [error, setError] = useState('')

  const userId = auth.user_id || ''

  useEffect(() => {
    if (sessionId) localStorage.setItem('mt_sid', sessionId)
  }, [sessionId])

  // 任意请求返回 401 时回到登录页（api.js 会派发该事件）
  useEffect(() => {
    const onUnauth = () => setAuth({ token: '', user_id: '', username: '' })
    window.addEventListener('mt:unauthorized', onUnauth)
    return () => window.removeEventListener('mt:unauthorized', onUnauth)
  }, [])

  const refreshOverview = useCallback(async () => {
    if (!userId) return
    try {
      const [ov, h] = await Promise.all([api.overview(userId), api.health()])
      setOverview(ov)
      setHealth(h)
      setError('')
    } catch (e) {
      setError(String(e.message || e))
    }
  }, [userId])

  useEffect(() => {
    refreshOverview()
  }, [refreshOverview])

  const ensureSession = async () => {
    if (sessionId) return sessionId
    const { session_id: sid } = await api.createSession(userId)
    setSessionId(sid)
    return sid
  }

  const handleLogout = async () => {
    try {
      await api.logout()
    } catch {
      /* 服务端 token 可能已过期，本地照常清理 */
    }
    session.clear()
    localStorage.removeItem('mt_sid')
    setSessionId('')
    setOverview(null)
    setAuth({ token: '', user_id: '', username: '' })
    setTab('companion')
  }

  // 未登录 -> 登录页
  if (!auth.token || !auth.user_id) {
    return <LoginView onAuthed={setAuth} />
  }

  const latestRisk =
    overview?.risk_flags?.filter((f) => !f.handled).slice(0, 1)[0]?.level || 'none'
  const risk = RISK_META[latestRisk] || RISK_META.none

  const Active = TABS.find((t) => t.key === tab).comp

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">MT</span>
          <div>
            <h1>MindTrace</h1>
            <p>多模态心理陪伴 Agent · LangGraph 骨架</p>
          </div>
        </div>

        <div className="controls">
          {/* 当前登录用户：只读展示，不再是可随意改的输入框 */}
          <div className="who">
            <span className="who-avatar">{(auth.username || '?')[0].toUpperCase()}</span>
            <div className="who-text">
              <b>{auth.username || '未知用户'}</b>
              <span className="muted small">{userId}</span>
            </div>
          </div>
          <button className="btn ghost" onClick={refreshOverview}>刷新</button>
          <button
            className="btn ghost"
            onClick={async () => {
              const sid = await api.createSession(userId)
              setSessionId(sid)
            }}
          >
            新会话
          </button>
          <span className="badge" style={{ color: risk.color, background: risk.bg }}>
            当前风险：{risk.label}
          </span>
          {health && (
            <span className="badge subtle">
              LLM: {health.llm_provider} · CKPT: {health.checkpointer}
            </span>
          )}
          <button className="btn ghost" onClick={handleLogout}>登出</button>
        </div>
      </header>

      {error && <div className="error-bar">接口错误：{error}</div>}

      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={tab === t.key ? 'tab active' : 'tab'}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === 'review' && overview?.risk_flags?.filter((f) => !f.handled && ['moderate', 'high', 'imminent'].includes(f.level)).length > 0 && (
              <span className="dot" />
            )}
          </button>
        ))}
      </nav>

      <main className="content">
        <Active
          userId={userId}
          sessionId={sessionId}
          ensureSession={ensureSession}
          overview={overview}
          refresh={refreshOverview}
        />
      </main>

      <footer className="footnote">
        本系统为技术演示骨架，不提供医疗诊断。紧急情况请拨打
        {overview?.hotlines?.map((h) => ` ${h.name} ${h.number}`).join(' / ') || ' 12356'}。
      </footer>
    </div>
  )
}
