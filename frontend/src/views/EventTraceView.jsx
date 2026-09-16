import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'

/**
 * 决策轨迹（可观测层）。
 *
 * 展示后端 tracer 记录的中间决策过程：情绪融合、三通道判定、
 * 最终分级、状态转移、CBT 阶段、延迟。
 *
 * 同一份数据也会以 JSONL 落盘到 data/traces/agent-YYYYMMDD.jsonl，
 * 可用 `tail -f` 实时查看。
 */

const KIND_STYLE = {
  'turn.start': { color: '#2f2c29', bg: '#eef1f6', label: '轮次' },
  'turn.end': { color: '#2f2c29', bg: '#eef1f6', label: '轮次' },
  'emotion.channel': { color: '#6b8e7f', bg: '#e8f0ec', label: '情绪通道' },
  'emotion.fused': { color: '#6b8e7f', bg: '#dbe9e2', label: '情绪融合' },
  'risk.channel': { color: '#d99a4e', bg: '#fbf0e0', label: '风险通道' },
  'risk.final': { color: '#c2604f', bg: '#fbeae7', label: '风险分级' },
  'graph.transition': { color: '#5b7fa6', bg: '#e7eef5', label: '状态转移' },
  'phase.transition': { color: '#5b7fa6', bg: '#e7eef5', label: '相位' },
  'cbt.stage': { color: '#7a6ba8', bg: '#eeebf5', label: 'CBT 阶段' },
  'context.assembled': { color: '#6b665f', bg: '#f2efe9', label: '上下文' },
  'memory.write': { color: '#6b665f', bg: '#f2efe9', label: '记忆' },
  'session.finalize': { color: '#6b8e7f', bg: '#dbe9e2', label: '会话收尾' },
}

const FILTERS = [
  { key: '', label: '全部' },
  { key: 'emotion', label: '情绪' },
  { key: 'risk', label: '风险' },
  { key: 'graph.transition', label: '状态转移' },
  { key: 'phase', label: '相位' },
  { key: 'cbt', label: 'CBT' },
]

function EventRow({ e }) {
  const [open, setOpen] = useState(false)
  const st = KIND_STYLE[e.kind] || { color: '#6b665f', bg: '#f2efe9', label: e.kind }
  const upgrade = /升级/.test(e.msg)

  return (
    <div className={`ev-row ${upgrade ? 'ev-upgrade' : ''}`} onClick={() => setOpen(!open)}>
      <div className="ev-head">
        <span className="ev-kind" style={{ color: st.color, background: st.bg }}>
          {st.label}
        </span>
        <span className="ev-msg">{e.msg}</span>
        <span className="muted small ev-ts">{String(e.ts).slice(11, 23)}</span>
      </div>
      {open && (
        <pre className="ev-json">
          {JSON.stringify(
            Object.fromEntries(
              Object.entries(e).filter(([k]) => !['ts', 'kind', 'msg'].includes(k))
            ),
            null,
            2
          )}
        </pre>
      )}
    </div>
  )
}

export default function EventTraceView() {
  const [events, setEvents] = useState([])
  const [filter, setFilter] = useState('')
  const [logFile, setLogFile] = useState('')
  const [err, setErr] = useState('')
  const [auto, setAuto] = useState(true)

  const load = useCallback(async () => {
    try {
      const d = await api.traceEvents(300, filter)
      setEvents(d.events || [])
      setLogFile(d.log_file || '')
      setErr('')
    } catch (e) {
      setErr(String(e.message || e))
    }
  }, [filter])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (!auto) return
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [auto, load])

  return (
    <div className="stack">
      <div className="card">
        <h3>决策轨迹</h3>
        <p className="muted">
          Agent 每轮的中间决策过程：情绪识别（各模态 → 融合）、自杀/自伤风险分级
          （三通道 → 并集）、状态转移、CBT 阶段推进、延迟。
        </p>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={filter === f.key ? 'btn tiny primary' : 'btn tiny ghost'}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
          <label className="muted small" style={{ marginLeft: 'auto' }}>
            <input
              type="checkbox"
              checked={auto}
              onChange={(e) => setAuto(e.target.checked)}
            />{' '}
            自动刷新（2s）
          </label>
          <button className="btn tiny ghost" onClick={load}>刷新</button>
        </div>
        {logFile && (
          <p className="muted small" style={{ marginTop: 8 }}>
            同时落盘到：<code>{logFile}</code>
            <br />
            实时查看：<code>tail -f {logFile}</code>
          </p>
        )}
      </div>

      <div className="card">
        {err && <div className="voice-err">{err}</div>}
        {!err && events.length === 0 && (
          <p className="muted">
            暂无事件。去「视频语音陪伴」或「对话」页聊几句，这里就会实时出现。
          </p>
        )}
        {events.slice().reverse().map((e, i) => (
          <EventRow key={`${e.ts}-${i}`} e={e} />
        ))}
      </div>
    </div>
  )
}
