import { useState } from 'react'

/**
 * 情绪小结展示（PRD §9）。
 *
 * 三条硬性规定：
 * 1. Tier 1 是唯一默认展示内容。
 * 2. Tier 3 不展示给用户；仅当 degradation_notes 非空时用固定文案提示环境不理想。
 * 3. risk_triggered=true 时，报告让位于关怀+资源转介，不生成 Tier1/2。
 */
const EMOJI = {
  平静: '😌', 愉悦: '🙂', 焦虑: '😰', 低落: '😢',
  烦躁: '😠', 疲惫: '🥱', 紧张: '😬', 惊讶: '😮',
}

function ValenceArousal({ v, a }) {
  // 以 (valence, arousal) 为坐标的情绪定位图
  const x = ((v + 1) / 2) * 100
  const y = ((1 - a) / 2) * 100
  return (
    <div className="va-map">
      <div className="va-axis va-y"><span>激动</span><span>平静</span></div>
      <div className="va-axis va-x"><span>消极</span><span>积极</span></div>
      <div className="va-plane">
        <div className="va-dot" style={{ left: `${x}%`, top: `${y}%` }} />
      </div>
    </div>
  )
}

function Trajectory({ points }) {
  if (!points || points.length < 2) return null
  const w = 100, h = 40
  const step = w / (points.length - 1)
  const d = points.map((p, i) => {
    const x = i * step
    const y = h - ((p.valence + 1) / 2) * (h - 6) - 3
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  return (
    <svg className="traj" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <line x1="0" y1={h / 2} x2={w} y2={h / 2} className="traj-mid" />
      <path d={d} className="traj-line" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

const TREND_META = {
  up: { text: '上升', cls: 'trend-up', icon: '↑' },
  flat: { text: '平稳', cls: 'trend-flat', icon: '→' },
  down: { text: '下降', cls: 'trend-down', icon: '↓' },
}

/** 会话收尾后展示：已沉淀成当日记录，以及跨天风险趋势 */
function PersistedPanel({ finishResult }) {
  if (!finishResult) return null
  const rec = finishResult.daily_record
  const t = finishResult.risk_trend || {}
  const tm = TREND_META[t.trend] || TREND_META.flat
  const facts = finishResult.facts || {}
  const factCount = Object.values(facts).reduce(
    (n, v) => n + (Array.isArray(v) ? v.length : 0), 0)

  return (
    <div className="card persisted">
      <h3>已记入今日</h3>
      {rec && (
        <div className="persisted-row">
          <span className="muted">日期</span>
          <span>{rec.date}</span>
        </div>
      )}
      {rec?.modality_summary && (
        <p className="muted small persisted-summary">{rec.modality_summary}</p>
      )}

      {t.days > 0 && (
        <div className="trend-box">
          <div className="trend-head">
            <span>近 {t.days} 天风险趋势</span>
            <span className={`trend-tag ${tm.cls}`}>{tm.icon} {tm.text}</span>
          </div>
          <div className="trend-series">
            {(t.series || []).slice(-14).map((s) => (
              <span
                key={s.date}
                className={`trend-dot lvl-${s.level_num}`}
                title={`${s.date} ${s.level}`}
              />
            ))}
          </div>
          {t.consecutive_high_days >= 2 && (
            <p className="trend-warn">
              ⚠ 已连续 {t.consecutive_high_days} 天处于 high 及以上
            </p>
          )}
        </div>
      )}

      {factCount > 0 && (
        <p className="muted small">
          本次新沉淀 {factCount} 条长期事实，已更新到你的画像。
        </p>
      )}
    </div>
  )
}

export default function ReportView({ report, finishResult, onRestart }) {
  const [showDetail, setShowDetail] = useState(false)

  // ---- 危机让位（PRD §9.2 硬性规定 3）----
  if (report.risk_override) {
    return (
      <div className="card crisis-report">
        <h3>请先照顾好自己</h3>
        <p className="crisis-text">{report.message}</p>
        <ul className="hotlines">
          {report.hotlines?.map((h) => (
            <li key={h.number}><span>{h.name}</span><b>{h.number}</b></li>
          ))}
        </ul>
        <p className="muted small">
          这些渠道是 24 小时有人接听的。如果身边有可以信任的人，也建议现在就联系他们。
        </p>
        <button className="btn ghost" onClick={onRestart}>返回</button>
      </div>
    )
  }

  if (report.error === 'no_turns') {
    return (
      <div className="card">
        <h3>本次没有产生有效对话</h3>
        <p className="muted">中途退出不会生成情绪小结。想的时候随时可以再来聊一次。</p>
        <button className="btn primary" onClick={onRestart}>重新开始</button>
      </div>
    )
  }

  const t1 = report.tier1_summary || {}
  const t2 = report.tier2_detail || {}
  const t3 = report.tier3_internal || {}
  // 唯一允许的 Tier3 外显：降级提示（固定文案，不展示数值）
  const degraded = (t3.degradation_notes || []).length > 0

  return (
    <div className="report">
      {/* ---------------- Tier 1（默认展示） ---------------- */}
      <div className="card report-main">
        <div className="report-hero">
          <span className="report-emoji">{EMOJI[t1.dominant_emotion] || '🙂'}</span>
          <div>
            <div className="report-label">本次主导情绪</div>
            <div className="report-emotion">
              {t1.dominant_emotion}
              {t1.secondary_emotion && (
                <span className="muted"> · 也有一点{t1.secondary_emotion}</span>
              )}
            </div>
          </div>
        </div>

        <p className="report-mood">{t1.mood_statement}</p>

        {t1.self_care_tips?.length > 0 && (
          <div className="tips">
            <div className="report-label">可以试试</div>
            <ul>
              {t1.self_care_tips.map((t, i) => <li key={i}>{t}</li>)}
            </ul>
          </div>
        )}

        <div className="report-meta">
          本次对话 {Math.round((t1.duration_sec || 0) / 60)} 分 {(t1.duration_sec || 0) % 60} 秒
          {' · '}{t1.turn_count} 轮
        </div>

        {degraded && (
          <div className="degrade-note">
            本次识别环境不理想，结果仅供参考。
          </div>
        )}

        {report.tier2_detail && (
          <button className="btn ghost" onClick={() => setShowDetail((v) => !v)}>
            {showDetail ? '收起' : '查看详情'}
          </button>
        )}
      </div>

      {/* ---------------- Tier 2（主动展开） ---------------- */}
      {showDetail && (
        <div className="card">
          <h3>情绪坐标</h3>
          <ValenceArousal v={t2.avg_valence || 0} a={t2.avg_arousal || 0} />

          <h3 style={{ marginTop: 16 }}>各情绪占比</h3>
          <div className="dist">
            {Object.entries(t2.emotion_distribution || {})
              .sort((a, b) => b[1] - a[1])
              .map(([k, v]) => (
                <div key={k} className="dist-row">
                  <span className="dist-name">{EMOJI[k] || ''} {k}</span>
                  <div className="dist-bar">
                    <div className="dist-fill" style={{ width: `${v * 100}%` }} />
                  </div>
                  <span className="dist-val">{Math.round(v * 100)}%</span>
                </div>
              ))}
          </div>

          <h3 style={{ marginTop: 16 }}>情绪变化</h3>
          <Trajectory points={t2.emotion_trajectory} />
          {t2.key_shift_points?.length > 0 && (
            <p className="muted small">
              变化较明显的话题：
              {t2.key_shift_points.map((p, i) => (
                <span key={i} className="chip">
                  第 {p.turn_id} 轮 · {p.topic_keyword}
                </span>
              ))}
            </p>
          )}
        </div>
      )}

      {/* ---------------- 沉淀到每日记录 + 风险趋势 ---------------- */}
      <PersistedPanel finishResult={finishResult} />

      {/* ---------------- watch 级资源提示 ---------------- */}
      {t1 && report.tier3_internal?.risk_level === 'watch' && (
        <div className="card watch-card">
          <h3>如果这种状态持续</h3>
          <p className="muted small">
            和信任的人聊聊，或者了解一下专业的心理支持渠道，可能会有帮助。
            这不是紧急状况，但你值得被好好支持。
          </p>
          <p className="muted small">全国心理援助热线 12356</p>
        </div>
      )}

      <div className="report-foot">
        <p className="muted small">
          本小结由 AI 根据你本次的语音与表情生成，仅为情绪状态参考，
          <b>不构成任何诊断或医疗建议</b>。
        </p>
        <button className="btn primary" onClick={onRestart}>再聊一次</button>
      </div>
    </div>
  )
}
