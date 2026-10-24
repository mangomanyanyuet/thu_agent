import { useCallback, useEffect, useRef, useState } from 'react'
import { api, streamChat } from '../api.js'
import { EMOTION_META, RISK_META, STAGE_LABEL } from '../theme.js'
import VoiceRecorder from '../VoiceRecorder.jsx'

const TONE_SHORT = {
  low: '语气低落',
  flat: '语气平淡',
  neutral: '语气平稳',
  agitated: '语气激动',
  unknown: '语音',
}

export default function ChatView({ userId, sessionId, ensureSession, refresh, onRisk }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState(null) // 本轮的风险/情绪元数据
  const [pending, setPending] = useState(null) // 待人工审核
  const [reviewText, setReviewText] = useState('')
  const [voice, setVoice] = useState(null) // 本轮语音感知结果
  const [voiceKey, setVoiceKey] = useState(0) // 用于发送后重置录音组件
  const [err, setErr] = useState('')
  const listRef = useRef(null)

  // 病人模拟（测试用可选项）。sim === null 表示后端未开启，面板不显示。
  const [sim, setSim] = useState(null)
  const [simCfg, setSimCfg] = useState({
    style: 'guarded', severity: 5, turns: 6, auto_review: 'approve',
  })
  const [simBusy, setSimBusy] = useState(false)
  const [simOut, setSimOut] = useState(null)

  const loadMessages = useCallback(async (sid) => {
    if (!sid) return
    const rows = await api.messages(sid)
    setMessages(
      rows.map((r) => ({
        role: r.role,
        content: r.content,
        emotion: r.emotion,
        risk_level: r.risk_level,
      }))
    )
  }, [])

  useEffect(() => {
    loadMessages(sessionId)
  }, [sessionId, loadMessages])

  useEffect(() => {
    // 403 说明后端关掉了病人模拟 —— 这是**可选项**，不开启就不该出现在界面上
    api.simProfiles().then(setSim).catch(() => setSim(null))
  }, [])

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, live])

  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    const sid = await ensureSession()
    const thisVoice = voice
    setBusy(true)
    setInput('')
    setLive(null)
    setPending(null)
    setErr('')
    setMessages((m) => [...m, { role: 'user', content: text, voice: thisVoice }])

    let reply = ''
    setMessages((m) => [...m, { role: 'assistant', content: '', streaming: true }])

    try {
      await streamChat({ userId, sessionId: sid, text, voice: thisVoice }, {
        onMeta: (meta) => {
          setLive(meta)
          // 上报给顶部徽章：危机轮会被 interrupt 挂起，收尾节点不执行，
          // 光靠刷新 session.last_risk 不保证及时
          onRisk?.(meta.risk_level || '')
          if (meta.interrupted) setPending(meta.pending_review)
        },
        onDelta: (t) => {
          reply += t
          setMessages((m) => {
            const next = [...m]
            next[next.length - 1] = { ...next[next.length - 1], content: reply }
            return next
          })
        },
        onDone: () => {
          setMessages((m) => {
            const next = [...m]
            next[next.length - 1] = { ...next[next.length - 1], streaming: false }
            return next
          })
          setVoice(null)
          setVoiceKey((k) => k + 1) // 重置录音组件
          refresh?.()
        },
      })
    } catch (e) {
      setMessages((m) => [
        ...m.slice(0, -1),
        { role: 'assistant', content: `请求失败：${e.message}` },
      ])
    } finally {
      setBusy(false)
    }
  }

  // ---------------- 病人模拟：让 Agent 扮演来访者自动接话 ----------------
  const runSim = async (oneTurn) => {
    const sid = await ensureSession()
    setSimBusy(true)
    setErr('')
    setSimOut(null)
    setPending(null)
    setLive(null)
    try {
      const payload = {
        user_id: userId,
        session_id: sid,
        style: simCfg.style,
        severity: Number(simCfg.severity),
        turns: Number(simCfg.turns) || 6,
        auto_review: simCfg.auto_review,
      }
      const out = oneTurn ? await api.simTurn(payload) : await api.simRun(payload)
      const last = oneTurn
        ? out
        : (out.transcript || [])[out.transcript.length - 1]
      if (last) {
        setLive(last)
        onRisk?.(last.risk_level || '')
        if (last.interrupted && last.pending_review) setPending(last.pending_review)
      }
      if (!oneTurn) setSimOut(out)
      await loadMessages(sid)
      refresh?.()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setSimBusy(false)
    }
  }

  const onVoiceResult = useCallback((r) => {
    setErr('')
    setVoice({
      audio_features: r.audio_features || {},
      tone: r.tone || '',
      tone_reason: r.tone_reason || '',
      summary: r.summary || '',
    })
    // 转写文本填入输入框，用户可编辑后再发送
    if (r.text) setInput((old) => (old ? `${old} ${r.text}` : r.text))
  }, [])

  const onVoiceError = useCallback((msg) => setErr(msg), [])

  const submitReview = async (action) => {
    const sid = await ensureSession()
    const res = await api.submitReview({
      session_id: sid,
      action,
      reply: reviewText,
      reviewer: 'web-reviewer',
    })
    setPending(null)
    setLive(res)
    if (action === 'override' && reviewText) {
      setMessages((m) => {
        const next = [...m]
        next[next.length - 1] = { ...next[next.length - 1], content: reviewText }
        return next
      })
    }
    setReviewText('')
    refresh?.()
  }

  const risk = live ? RISK_META[live.risk_level] || RISK_META.none : null
  const emo = live?.emotion?.primary_emotion ? EMOTION_META[live.emotion.primary_emotion] : null

  return (
    <div className="chat-layout">
      <section className="chat-main">
        <div className="msg-list" ref={listRef}>
          {messages.length === 0 && (
            <div className="empty">
              试着说一句今天的状态，例如「最近工作压力好大，晚上睡不着」。
              <br />
              也可以试试输入包含风险的表达（如「已经写好遗书」），观察危机分支与人工审核流程。
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <div className="bubble">
                {m.content}
                {m.streaming && <span className="caret" />}
              </div>
              {m.role === 'user' && (
                <div className="msg-meta">
                  {m.voice && (
                    <span title={m.voice.summary}>
                      🎙 {TONE_SHORT[m.voice.tone] || '语音'}
                      {m.voice.audio_features?.syllable_rate
                        ? ` · ${m.voice.audio_features.syllable_rate}音节/秒`
                        : ''}
                    </span>
                  )}
                  {m.emotion && EMOTION_META[m.emotion] && (
                    <span>{EMOTION_META[m.emotion].emoji} {EMOTION_META[m.emotion].label}</span>
                  )}
                  {m.risk_level && m.risk_level !== 'none' && (
                    <span style={{ color: RISK_META[m.risk_level].color }}>
                      风险 {RISK_META[m.risk_level].label}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="composer">
          <VoiceRecorder
            key={voiceKey}
            onResult={onVoiceResult}
            onError={onVoiceError}
          />
          <textarea
            value={input}
            placeholder="说话或打字…（Enter 发送，Shift+Enter 换行）"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
          />
          <button className="btn primary" onClick={send} disabled={busy || !input.trim()}>
            {busy ? '思考中…' : '发送'}
          </button>
        </div>
        {err && <div className="voice-err">{err}</div>}
        {voice && (
          <div className="voice-attached">
            🎙 本轮将附带语音特征（{voice.tone || '未知语气'}）一起提交
            <button className="btn tiny ghost" onClick={() => setVoice(null)}>移除</button>
          </div>
        )}
      </section>

      <aside className="side">
        <div className="card">
          <h3>本轮实时判定</h3>
          {!live && <p className="muted">发送一条消息后显示</p>}
          {live && (
            <>
              <div className="kv">
                <span>风险等级</span>
                <strong style={{ color: risk.color }}>{risk.label}</strong>
              </div>
              {emo && (
                <div className="kv">
                  <span>情绪</span>
                  <strong>
                    {emo.emoji} {emo.label} · 强度 {live.emotion.intensity}/10
                  </strong>
                </div>
              )}
              <div className="kv">
                <span>CBT 阶段</span>
                <strong>{STAGE_LABEL[live.cbt_stage] || live.cbt_stage || '-'}</strong>
              </div>
              <div className="kv">
                <span>上下文占用</span>
                <strong>{live.context_stats?.usage_pct ?? 0}% (~{live.context_stats?.approx_tokens ?? 0} tok)</strong>
              </div>
            </>
          )}
        </div>

        {live?.channels && (
          <div className="card">
            <h3>三通道检测（取并集升级）</h3>
            {Object.entries(live.channels).map(([k, v]) => {
              const lvl = ['none', 'low', 'moderate', 'high', 'imminent'][v?.level ?? 0]
              const meta = RISK_META[lvl]
              return (
                <div className="channel" key={k}>
                  <span className="chan-name">
                    {k === 'rule' ? '规则' : k === 'model' ? '模型' : k === 'multimodal' ? '多模态' : '量表'}
                  </span>
                  <span className="chip" style={{ color: meta.color, background: meta.bg }}>
                    {meta.label}
                  </span>
                  <span className="chan-ev">{v?.evidence || '—'}</span>
                </div>
              )
            })}
            {live.risk_evidence?.length > 0 && (
              <div className="evidence">
                <b>证据（已写入 risk_flag，永不压缩）</b>
                <ul>
                  {live.risk_evidence.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {live?.crisis_mode && live.hotlines?.length > 0 && (
          <div className="card crisis">
            <h3>⚠ 危机干预已触发</h3>
            <p className="muted">会话已挂起等待人工审核。以下是推送给用户的求助渠道：</p>
            <ul className="hotlines">
              {live.hotlines.map((h) => (
                <li key={h.number}>
                  <span>{h.name}</span>
                  <b>{h.number}</b>
                </li>
              ))}
            </ul>
          </div>
        )}

        {pending && (
          <div className="card review">
            <h3>🛑 等待人工审核</h3>
            <p className="muted">{pending.message}</p>
            <div className="kv">
              <span>风险</span>
              <strong style={{ color: RISK_META[pending.risk_level]?.color }}>
                {RISK_META[pending.risk_level]?.label}
              </strong>
            </div>
            <textarea
              placeholder="改写回复（可选，用于 override）"
              value={reviewText}
              onChange={(e) => setReviewText(e.target.value)}
            />
            <div className="row">
              <button className="btn primary" onClick={() => submitReview('approve')}>
                确认放行
              </button>
              <button className="btn" onClick={() => submitReview('override')} disabled={!reviewText}>
                改写并放行
              </button>
              <button className="btn ghost" onClick={() => submitReview('reject')}>
                拒绝
              </button>
            </div>
          </div>
        )}

        {sim && (
          <div className="card sim">
            <h3>🧪 病人模拟（测试）</h3>
            <p className="muted small">
              让 Agent 扮演来访者自动接话，用来验证风险逐级升级与人工审核中断。
              产出会写进当前会话，请勿与真实用户数据混用。
            </p>
            <div className="kv">
              <span>风格</span>
              <select value={simCfg.style} disabled={simBusy}
                onChange={(e) => setSimCfg({ ...simCfg, style: e.target.value })}>
                {sim.styles.map((s) => (
                  <option key={s.key} value={s.key}>{s.name}</option>
                ))}
              </select>
            </div>
            <div className="kv">
              <span>严重程度</span>
              <select value={simCfg.severity} disabled={simBusy}
                onChange={(e) => setSimCfg({ ...simCfg, severity: e.target.value })}>
                {sim.severities.map((s) => (
                  <option key={s.level} value={s.level}>
                    {s.level} {s.name}（{s.target}）
                  </option>
                ))}
              </select>
            </div>
            <div className="kv">
              <span>轮数</span>
              <input type="number" min="1" max="20" value={simCfg.turns} disabled={simBusy}
                onChange={(e) => setSimCfg({ ...simCfg, turns: e.target.value })} />
            </div>
            <div className="kv">
              <span>挂起后</span>
              <select value={simCfg.auto_review} disabled={simBusy}
                onChange={(e) => setSimCfg({ ...simCfg, auto_review: e.target.value })}>
                <option value="approve">自动放行</option>
                <option value="none">停在挂起点（看中断）</option>
                <option value="override">改写并放行</option>
                <option value="reject">拒绝</option>
              </select>
            </div>
            <div className="row">
              <button className="btn primary" disabled={simBusy} onClick={() => runSim(true)}>
                {simBusy ? '对练中…' : '跑一轮'}
              </button>
              <button className="btn" disabled={simBusy} onClick={() => runSim(false)}>
                跑 {simCfg.turns} 轮
              </button>
            </div>
            {simOut && (
              <div className="sim-out small">
                <div>风险轨迹：{(simOut.risk_track || []).join(' → ') || '—'}</div>
                <div>
                  危机分支：{simOut.crisis_at_turn ? `第 ${simOut.crisis_at_turn} 轮` : '未触发'}
                </div>
                <div>
                  人工审核中断：
                  {simOut.interrupted_at_turn ? `第 ${simOut.interrupted_at_turn} 轮` : '未触发'}
                </div>
                <div className="muted">
                  seed {simOut.profile?.seed} · {simOut.profile?.persona}
                </div>
              </div>
            )}
          </div>
        )}

        {live?.facts && (
          <div className="card">
            <h3>本轮抽取的长期记忆</h3>
            {Object.entries(live.facts).map(([k, v]) =>
              Array.isArray(v) && v.length > 0 ? (
                <div key={k} className="fact-row">
                  <span className="chan-name">
                    {k === 'profile_facts' ? '生活事实' : k === 'triggers' ? '触发源'
                      : k === 'events' ? '事件' : '偏好'}
                  </span>
                  {v.join('、')}
                </div>
              ) : null
            )}
          </div>
        )}
      </aside>
    </div>
  )
}
