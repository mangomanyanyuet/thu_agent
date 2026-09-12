import { useEffect, useState } from 'react'
import { api } from '../api.js'

/**
 * 每日记录（只读）。
 *
 * 数据源已统一为「视频语音陪伴」会话 —— 会话结束时后端自动沉淀为当日记录，
 * 这里不再提供单独的自述/语音输入。这样避免两套输入口径不一致，
 * 用户也只需要一个入口。
 */
const TREND_META = {
  up: { text: '上升', cls: 'trend-up', icon: '↑' },
  flat: { text: '平稳', cls: 'trend-flat', icon: '→' },
  down: { text: '下降', cls: 'trend-down', icon: '↓' },
}

const EMOJI = {
  平静: '😌', 愉悦: '🙂', 焦虑: '😰', 低落: '😢',
  烦躁: '😠', 疲惫: '🥱', 紧张: '😬', 惊讶: '😮',
}

export default function DailyView({ userId, overview }) {
  const [records, setRecords] = useState([])
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(null)

  const load = async () => {
    try {
      const rows = await api.daily(userId, 30)
      // 新的在前
      setRecords([...rows].reverse())
      setErr('')
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId])

  const trend = overview?.risk_trend
  const tm = TREND_META[trend?.trend] || TREND_META.flat

  return (
    <div className="stack">
      <div className="card">
        <h3>每日记录</h3>
        <p className="muted">
          每次「视频语音陪伴」结束后会自动沉淀为一条当日记录。
          这里不再需要单独填写 —— 陪伴会话就是唯一输入入口。
        </p>
        <button className="btn ghost" onClick={load}>刷新</button>
      </div>

      {/* ---------- 风险趋势 ---------- */}
      {trend && trend.days > 0 && (
        <div className="card">
          <h3>风险趋势</h3>
          <div className="trend-head">
            <span>近 {trend.days} 天</span>
            <span className={`trend-tag ${tm.cls}`}>{tm.icon} {tm.text}</span>
          </div>
          <div className="trend-series">
            {(trend.series || []).slice(-14).map((s) => (
              <span
                key={s.date}
                className={`trend-dot lvl-${s.level_num}`}
                title={`${s.date} · ${s.level}`}
              />
            ))}
          </div>
          <div className="daily-metrics">
            <span>当前 <b>{trend.latest}</b></span>
            <span>区间峰值 {trend.peak}</span>
            {trend.high_days > 0 && <span>high 及以上 {trend.high_days} 天</span>}
          </div>
          {trend.consecutive_high_days >= 2 && (
            <p className="trend-warn">
              ⚠ 已连续 {trend.consecutive_high_days} 天处于 high 及以上，建议转人工复核
            </p>
          )}
          <p className="muted small">
            趋势比单点数值更有意义：连续上升比某天突然高更需要关注。
          </p>
        </div>
      )}

      {/* ---------- 记录列表 ---------- */}
      <div className="card">
        <h3>历史记录（{records.length}）</h3>
        {err && <div className="voice-err">{err}</div>}
        {loading && <p className="muted">加载中…</p>}
        {!loading && records.length === 0 && (
          <p className="muted">暂无记录。去做一次「视频语音陪伴」，结束后会自动出现在这里。</p>
        )}

        {records.map((r) => {
          const na = r.narrative_analysis || {}
          const isCompanion = na.source === 'companion'
          const open = expanded === r.id
          return (
            <div
              key={r.id}
              className={`daily-row ${r.inconsistency ? 'warn' : ''}`}
              onClick={() => setExpanded(open ? null : r.id)}
            >
              <div className="daily-head">
                <b>{r.date}</b>
                {isCompanion && <span className="chip">陪伴会话</span>}
                {na.dominant_emotion && (
                  <span className="chip">
                    {EMOJI[na.dominant_emotion] || ''} {na.dominant_emotion}
                  </span>
                )}
                {na.risk_level && na.risk_level !== 'none' && (
                  <span className="chip warn">风险 {na.risk_level}</span>
                )}
                {r.inconsistency && <span className="chip warn">⚠ 模态不一致</span>}
                <span className="muted small">{open ? '收起' : '展开'}</span>
              </div>

              {na.avg_valence != null && (
                <div className="daily-metrics">
                  <span>愉悦度 {na.avg_valence > 0 ? '+' : ''}{na.avg_valence}</span>
                  {na.avg_arousal != null && <span>唤醒度 {na.avg_arousal}</span>}
                  {na.turn_count ? <span>{na.turn_count} 轮</span> : null}
                  {na.duration_sec ? <span>{Math.round(na.duration_sec)}s</span> : null}
                </div>
              )}

              {r.modality_summary && (
                <div className="muted small">{r.modality_summary}</div>
              )}

              {open && (
                <div className="daily-detail">
                  {r.narrative && (
                    <div className="daily-narrative">{r.narrative}</div>
                  )}
                  {na.themes?.length > 0 && (
                    <div className="daily-metrics">
                      <span>主题：{na.themes.join('、')}</span>
                    </div>
                  )}
                  {na.risk_evidence?.length > 0 && (
                    <div className="daily-evidence">
                      <b>风险证据：</b>
                      {na.risk_evidence.map((e, i) => <div key={i}>· {e}</div>)}
                    </div>
                  )}
                  {r.audio_features && Object.keys(r.audio_features).length > 0 && (
                    <pre className="daily-json">
                      {JSON.stringify(r.audio_features, null, 2)}
                    </pre>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
