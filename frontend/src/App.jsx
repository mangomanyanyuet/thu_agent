import { useCallback, useEffect, useRef, useState } from 'react'
import { api, session } from './api.js'
import LoginView from './views/LoginView.jsx'
import CompanionView from './views/CompanionView.jsx'
import ChatView from './views/ChatView.jsx'
import DailyView from './views/DailyView.jsx'
import ScaleView from './views/ScaleView.jsx'
import ProfileView from './views/ProfileView.jsx'
import ReviewView from './views/ReviewView.jsx'
import TuningView from './views/TuningView.jsx'
import TraceView from './views/TraceView.jsx'
import EventTraceView from './views/EventTraceView.jsx'
import HomeView from './views/HomeView.jsx'
import DiaryView from './views/DiaryView.jsx'
import { RISK_META } from './theme.js'
import { mediaDiagnosis } from './env.js'

// 专业版（原有能力）保留为一层，纯文字首页为默认入口
const PRO_TABS = [
  { key: 'companion', label: '视频语音陪伴', comp: CompanionView },
  { key: 'chat', label: '对话', comp: ChatView },
  { key: 'daily', label: '每日记录', comp: DailyView },
  { key: 'scale', label: '量表', comp: ScaleView },
  { key: 'profile', label: '画像与趋势', comp: ProfileView },
  { key: 'review', label: '人工审核台', comp: ReviewView },
  { key: 'tuning', label: '临床参数', comp: TuningView },
  { key: 'trace', label: '上下文装配', comp: TraceView },
  { key: 'events', label: '决策轨迹', comp: EventTraceView },
]

export default function App() {
  const [auth, setAuth] = useState(() => session.get())
  const [view, setView] = useState('home')   // home | chat | diary | pro
  const [chatSeed, setChatSeed] = useState(null) // 从首页带进文字对话的首句 {text, key}
  const [tab, setTab] = useState('companion') // 专业版内部 tab
  const [sessionId, setSessionId] = useState(localStorage.getItem('mt_sid') || '')
  const [overview, setOverview] = useState(null)
  const [health, setHealth] = useState(null)
  const [sessions, setSessions] = useState([])
  const [liveRisk, setLiveRisk] = useState('')
  const [error, setError] = useState('')
  const [mediaDiag] = useState(() => mediaDiagnosis())
  const creatingRef = useRef(null)
  const seedKeyRef = useRef(0)

  const userId = auth.user_id || ''

  const httpsHint = !mediaDiag.ok ? `https://${window.location.hostname}:8443` : ''

  useEffect(() => {
    if (sessionId) localStorage.setItem('mt_sid', sessionId)
    setLiveRisk('')
  }, [sessionId])

  useEffect(() => {
    const onUnauth = () => setAuth({ token: '', user_id: '', username: '' })
    const onGotoTuning = () => { setView('pro'); setTab('tuning') }
    window.addEventListener('mt:unauthorized', onUnauth)
    window.addEventListener('mt:goto-tuning', onGotoTuning)
    return () => {
      window.removeEventListener('mt:unauthorized', onUnauth)
      window.removeEventListener('mt:goto-tuning', onGotoTuning)
    }
  }, [])

  const refreshOverview = useCallback(async () => {
    if (!userId) return
    try {
      const [ov, h, sl] = await Promise.all([
        api.overview(userId),
        api.health(),
        api.listSessions(userId).catch(() => []),
      ])
      setOverview(ov); setHealth(h); setSessions(sl || []); setError('')
    } catch (e) { setError(String(e.message || e)) }
  }, [userId])

  useEffect(() => { refreshOverview() }, [refreshOverview])

  const ensureSession = useCallback(async () => {
    if (sessionId) return sessionId
    if (creatingRef.current) return creatingRef.current
    creatingRef.current = (async () => {
      try {
        const { session_id: sid } = await api.createSession(userId)
        setSessionId(sid); refreshOverview(); return sid
      } finally { creatingRef.current = null }
    })()
    return creatingRef.current
  }, [sessionId, userId, refreshOverview])

  const handleLogout = async () => {
    try { await api.logout() } catch { /* ignore */ }
    session.clear(); localStorage.removeItem('mt_sid')
    setSessionId(''); setOverview(null); setSessions([]); setLiveRisk('')
    setAuth({ token: '', user_id: '', username: '' })
    setView('home'); setTab('companion')
  }

  // 首页首句直达文字对话：只切视图，发送与流式回复由 ChatView 负责
  const startChat = useCallback((seed) => {
    setChatSeed(seed?.text ? { ...seed, key: ++seedKeyRef.current } : null)
    setView('chat')
  }, [])

  if (!auth.token || !auth.user_id) {
    return <LoginView onAuthed={setAuth} />
  }

  const currentSession = (sessions || []).find((s) => s.id === sessionId)
  const latestRisk = liveRisk || currentSession?.last_risk || 'none'
  const risk = RISK_META[latestRisk] || RISK_META.none

  const childProps = {
    userId, sessionId, ensureSession, overview,
    refresh: refreshOverview, onRisk: setLiveRisk,
  }

  // 顶部账号条（所有视图共用，做得比原控制台简洁）
  const accountBar = (compact) => (
    <div className={compact ? 'mini-top' : 'topbar'}>
      <div className="brand" onClick={() => setView('home')} style={{ cursor: 'pointer' }}>
        <span className="logo">MT</span>
        {!compact && <div><h1>MindTrace</h1><p>你的情绪陪伴助手</p></div>}
      </div>
      <div className="controls">
        <div className="who">
          <span className="who-avatar">{(auth.username || '?')[0].toUpperCase()}</span>
          {!compact && <div className="who-text"><b>{auth.username}</b></div>}
        </div>
        {view !== 'home' && (
          <button className="btn ghost" onClick={() => setView('home')}>首页</button>
        )}
        {compact && view === 'pro' && (
          <button className="btn ghost" onClick={() => setView('home')}>返回简洁版</button>
        )}
        {!compact && (
          <>
            <span className="badge" style={{ color: risk.color, background: risk.bg }}>
              当前风险：{risk.label}
            </span>
            {health && <span className="badge subtle">LLM: {health.llm_provider}</span>}
          </>
        )}
        <button className="btn ghost" onClick={handleLogout}>登出</button>
      </div>
    </div>
  )

  // 语音/视频提示只对专业版的陪伴页有意义；简洁版已是纯文字
  const mediaNotice = !mediaDiag.ok && (
    <div className="media-notice">
      <b>⚠ 专业版语音与视频不可用</b>：{mediaDiag.reason}。
      {httpsHint && (
        <> 请用 HTTPS 打开：<a href={httpsHint} target="_blank" rel="noreferrer">{httpsHint}</a>
          （证书自签，提示"不安全"时选"继续访问"）；纯文字对话不受影响。</>
      )}
      {!httpsHint && <> {mediaDiag.fix}</>}
    </div>
  )

  return (
    <div className="app">
      {accountBar(view !== 'pro')}

      {view === 'pro' && mediaNotice}

      {view === 'home' && (
        <main className="content">
          <HomeView
            {...childProps}
            onNavigate={(t) => (t === 'chat' ? startChat() : setView(t))}
            onStartChat={startChat}
          />
        </main>
      )}

      {view === 'chat' && (
        <main className="content">
          <ChatView {...childProps} initial={chatSeed} />
        </main>
      )}

      {view === 'diary' && (
        <main className="content">
          <DiaryView userId={userId} onBack={() => setView('home')} />
        </main>
      )}

      {view === 'pro' && (
        <>
          <nav className="tabs">
            {PRO_TABS.map((t) => (
              <button key={t.key}
                className={tab === t.key ? 'tab active' : 'tab'}
                onClick={() => setTab(t.key)}>
                {t.label}
                {t.key === 'review' && overview?.risk_flags?.filter(
                  (f) => !f.handled && ['moderate', 'high', 'imminent'].includes(f.level)).length > 0 && (
                  <span className="dot" />
                )}
              </button>
            ))}
          </nav>
          <main className="content">
            {(() => {
              const Active = PRO_TABS.find((t) => t.key === tab).comp
              return <Active {...childProps} />
            })()}
          </main>
        </>
      )}

      <footer className="footnote">
        本系统为技术演示骨架，不提供医疗诊断。紧急情况请拨打
        {overview?.hotlines?.map((h) => ` ${h.name} ${h.number}`).join(' / ') || ' 12356'}。
      </footer>
    </div>
  )
}
