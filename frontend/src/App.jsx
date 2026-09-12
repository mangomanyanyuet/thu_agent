import { useEffect, useState } from 'react'
import { api } from './api.js'
import CompanionView from './views/CompanionView.jsx'
import ChatView from './views/ChatView.jsx'
import DailyView from './views/DailyView.jsx'
import ScaleView from './views/ScaleView.jsx'
import ProfileView from './views/ProfileView.jsx'
import ReviewView from './views/ReviewView.jsx'
import TraceView from './views/TraceView.jsx'
import { RISK_META } from './theme.js'

const TABS = [
  { key: 'companion', label: '视频语音陪伴', comp: CompanionView },
  { key: 'chat', label: '对话', comp: ChatView },
  { key: 'daily', label: '每日记录', comp: DailyView },
  { key: 'scale', label: '量表', comp: ScaleView },
  { key: 'profile', label: '画像与趋势', comp: ProfileView },
  { key: 'review', label: '人工审核台', comp: ReviewView },
  { key: 'trace', label: '上下文装配', comp: TraceView },
]

export default function App() {
  const [tab, setTab] = useState('companion')
  const [userId, setUserId] = useState(localStorage.getItem('mt_user') || 'demo_user')
  const [sessionId, setSessionId] = useState(localStorage.getItem('mt_sid') || '')
  const [overview, setOverview] = useState(null)
  const [health, setHealth] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    localStorage.setItem('mt_user', userId)
  }, [userId])

  useEffect(() => {
    if (sessionId) localStorage.setItem('mt_sid', sessionId)
  }, [sessionId])

  const refreshOverview = async () => {
    try {
      const [ov, h] = await Promise.all([api.overview(userId), api.health()])
      setOverview(ov)
      setHealth(h)
      setError('')
    } catch (e) {
      setError(String(e.message || e))
    }
  }

  useEffect(() => {
    refreshOverview()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId])

  const ensureSession = async () => {
    if (sessionId) return sessionId
    const { session_id: sid } = await api.createSession(userId)
    setSessionId(sid)
    return sid
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
          <label className="field">
            <span>用户 ID</span>
            <input value={userId} onChange={(e) => setUserId(e.target.value)} />
          </label>
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
        </div>
      </header>

      {error && <div className="error-bar">接口错误：{error}（确认后端已启动：uvicorn main:app --port 8000）</div>}

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
