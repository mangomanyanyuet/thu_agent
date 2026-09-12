import { api } from '../api.js'
import { RISK_META, SCALE_COLORS } from '../theme.js'

function Sparkline({ points, color, height = 60 }) {
  if (!points || points.length === 0) return <div className="muted">暂无数据</div>
  const vals = points.map((p) => p.v)
  const max = Math.max(...vals, 1)
  const min = Math.min(...vals, 0)
  const span = Math.max(max - min, 1)
  const w = 100
  const step = w / Math.max(points.length - 1, 1)
  const d = points
    .map((p, i) => {
      const x = i * step
      const y = height - ((p.v - min) / span) * (height - 10) - 5
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
  const last = points[points.length - 1]
  return (
    <div className="spark">
      <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none">
        <path d={d} fill="none" stroke={color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="spark-foot">
        <span>{points[0]?.d}</span>
        <b style={{ color }}>{last.v}</b>
        <span>{last.d}</span>
      </div>
    </div>
  )
}

export default function ProfileView({ overview, userId }) {
  if (!overview) return <div className="card">加载中…</div>
  const p = overview.profile || {}

  return (
    <div className="stack">
      <div className="grid-2">
        <div className="card">
          <h3>用户长期画像</h3>
          <pre className="profile">{overview.profile_rendered}</pre>
          <div className="chips">
            {(p.known_triggers || []).map((t, i) => <span className="chip" key={`t${i}`}>触发源 · {t}</span>)}
            {(p.social_support || []).map((t, i) => <span className="chip ok" key={`s${i}`}>支持 · {t}</span>)}
            {(p.preferences || []).map((t, i) => <span className="chip" key={`p${i}`}>偏好 · {t}</span>)}
          </div>
          <p className="muted small">
            画像由记忆层从对话中增量抽取（去重 + 冲突消解），跨会话持久化，
            每次推理按 token 预算注入上下文。
          </p>
        </div>

        <div className="card">
          <h3>量表趋势（趋势比绝对值更有临床意义）</h3>
          {Object.entries(overview.scales || {}).map(([name, rows]) => (
            <div className="trend" key={name}>
              <div className="trend-head">
                <b style={{ color: SCALE_COLORS[name] }}>{name}</b>
                <span className="muted">
                  {rows.length ? `最新 ${rows[rows.length - 1].total_score}（${rows[rows.length - 1].severity}）` : '暂无'}
                </span>
              </div>
              <Sparkline
                color={SCALE_COLORS[name]}
                points={rows.map((r) => ({ v: r.total_score, d: r.date.slice(5) }))}
              />
            </div>
          ))}
          <pre className="muted small">{overview.scale_trend}</pre>
        </div>
      </div>

      <div className="card">
        <h3>风险事件（risk_flag · 永不压缩）</h3>
        <p className="muted small">
          所有涉及自杀/自伤的原话都会单独落库，不进入摘要/压缩管道，每轮推理强制注入。
        </p>
        {overview.risk_flags?.length === 0 && <p className="muted">暂无风险事件</p>}
        <table className="table">
          <thead>
            <tr><th>时间</th><th>等级</th><th>来源</th><th>C-SSRS 维度</th><th>证据原话</th><th>处理</th></tr>
          </thead>
          <tbody>
            {(overview.risk_flags || []).map((f) => (
              <tr key={f.id}>
                <td className="small">{f.ts?.slice(0, 16).replace('T', ' ')}</td>
                <td><span className="chip" style={{ color: RISK_META[f.level]?.color, background: RISK_META[f.level]?.bg }}>{RISK_META[f.level]?.label}</span></td>
                <td className="small">{f.source}</td>
                <td className="small">{f.cssrs_dimension || '-'}</td>
                <td>{f.evidence}</td>
                <td>
                  {f.handled ? (
                    <span className="chip ok">已处理 {f.handled_by}</span>
                  ) : (
                    <button className="btn tiny" onClick={() => api.handleFlag(f.id, 'web', '已查看').then(() => location.reload())}>
                      标记已处理
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
