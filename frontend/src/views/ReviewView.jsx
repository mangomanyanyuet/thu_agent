import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { RISK_META } from '../theme.js'

export default function ReviewView({ refresh }) {
  const [flags, setFlags] = useState([])
  const [minLevel, setMinLevel] = useState(2)
  const [note, setNote] = useState({})

  const load = useCallback(async () => {
    const rows = await api.pendingReviews(minLevel)
    setFlags(rows)
  }, [minLevel])

  useEffect(() => {
    load()
  }, [load])

  const handle = async (id) => {
    await api.handleFlag(id, 'web-reviewer', note[id] || '已人工跟进')
    await load()
    refresh?.()
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h3>待处理风险队列</h3>
          <select value={minLevel} onChange={(e) => setMinLevel(Number(e.target.value))}>
            <option value={1}>轻度及以上</option>
            <option value={2}>中度及以上</option>
            <option value={3}>重度及以上</option>
          </select>
          <button className="btn ghost" onClick={load}>刷新</button>
        </div>
        <p className="muted small">
          高危会话在生产环境必须有值班人员跟进与 SLA；本骨架演示的是「事件队列 + 处理留痕」，
          对话侧的 interrupt/HITL 在对话页完成。
        </p>

        {flags.length === 0 && <p className="muted">队列为空</p>}
        {flags.map((f) => (
          <div key={f.id} className="review-row">
            <div className="review-head">
              <span className="chip" style={{ color: RISK_META[f.level]?.color, background: RISK_META[f.level]?.bg }}>
                {RISK_META[f.level]?.label}
              </span>
              <b>{f.user_id}</b>
              <span className="muted small">来源 {f.source} · {f.ts?.slice(0, 16).replace('T', ' ')}</span>
              {f.cssrs_dimension && <span className="chip danger">{f.cssrs_dimension}</span>}
            </div>
            <div className="review-ev">“{f.evidence}”</div>
            <div className="row">
              <input
                placeholder="处理备注"
                value={note[f.id] || ''}
                onChange={(e) => setNote({ ...note, [f.id]: e.target.value })}
              />
              <button className="btn primary" onClick={() => handle(f.id)}>标记已处理</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
