import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'

/** 心情时间线：把每次对话结束后自动生成的日记按天倒序展示。 */
export default function DiaryView({ userId, onBack }) {
  const [diaries, setDiaries] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [expanded, setExpanded] = useState(null)

  const load = useCallback(async () => {
    setLoading(true); setErr('')
    try {
      const r = await api.diaryList(userId, 60)
      setDiaries(r.diaries || [])
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }, [userId])

  useEffect(() => { load() }, [load])

  // 按天分组
  const groups = []
  for (const d of diaries) {
    const last = groups[groups.length - 1]
    if (last && last.date === d.date) last.items.push(d)
    else groups.push({ date: d.date, items: [d] })
  }

  return (
    <div className="diary-page">
      <div className="row" style={{ marginTop: 0 }}>
        <button className="btn ghost" onClick={onBack}>← 返回</button>
        <h3 style={{ margin: 0 }}>我的日记</h3>
        <button className="btn ghost" onClick={load}>刷新</button>
        <span className="muted small">共 {diaries.length} 篇</span>
      </div>
      <p className="muted small">每次聊天结束后，我会帮你写下当天的心情小记。</p>

      {err && <div className="voice-err">{err}</div>}
      {loading && <p className="muted">加载中…</p>}
      {!loading && diaries.length === 0 && (
        <div className="card" style={{ textAlign: 'center', padding: 40 }}>
          <div style={{ fontSize: 36 }}>📔</div>
          <p className="muted">还没有日记。去聊一聊，结束后这里就会出现你的心情小记。</p>
        </div>
      )}

      {groups.map((g) => (
        <div key={g.date} className="diary-group">
          <div className="diary-date">{g.date}</div>
          {g.items.map((d) => {
            const open = expanded === d.id
            return (
              <div key={d.id} className="diary-card card" onClick={() => setExpanded(open ? null : d.id)}>
                <div className="row" style={{ marginTop: 0 }}>
                  <span style={{ fontSize: 28 }}>{d.mood_emoji}</span>
                  <div style={{ flex: 1 }}>
                    <b>{d.title || '今天的一点感受'}</b>
                    <div className="muted small">
                      心情：{d.mood_label} · {d.ts?.slice(11, 16)}
                      {d.source === 'fallback' && ' · 简版'}
                    </div>
                  </div>
                  <span className="muted small">{open ? '收起 ▲' : '展开 ▼'}</span>
                </div>
                {open && (
                  <>
                    <p style={{ lineHeight: 1.9, marginTop: 10, whiteSpace: 'pre-wrap' }}>{d.content}</p>
                    {d.keywords?.length > 0 && (
                      <div className="row" style={{ marginTop: 6 }}>
                        {d.keywords.map((k, i) => <span key={i} className="chip">{k}</span>)}
                      </div>
                    )}
                  </>
                )}
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}
