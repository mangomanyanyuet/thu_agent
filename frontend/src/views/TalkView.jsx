import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { mediaDiagnosis, explainMediaError } from '../env.js'
import { speak, stopSpeak, ttsSupported, warmupTTS } from '../tts.js'
import { RISK_META } from '../theme.js'

/**
 * 豆包式「实时通话」页（半双工）。
 *
 * 一轮流程（像对讲机）：
 *   1) 按住底部「说话」按钮开始录音；松手结束录音。
 *   2) 音频上传 /api/asr → 转写文本 + 声学特征 + 语气。
 *   3) 文本 + 语音特征一起提交 /api/chat（同步），拿到 Agent 回复与风险分级。
 *   4) 用浏览器 TTS 把回复读出来（AI 开口说话）；点「打断」或重新按住说话即取消朗读。
 *   5) crisis/high 时先弹热线卡片（安全优先，不被 TTS 掩盖）。
 *   6) 点「挂断」→ 调 /api/diary/generate 生成本次日记，然后回到首页/日记。
 *
 * 与"真全双工"的区别：这里是"说完一句 → 识别 → 回复 → 朗读"的交替，
 * 不需要流式 ASR/后端 TTS/WebSocket，零后端成本即可体验。
 */
export default function TalkView({ userId, sessionId, ensureSession, kind = 'voice', onExit, onRisk }) {
  const [status, setStatus] = useState('idle') // idle | recording | thinking | speaking
  const [subtitle, setSubtitle] = useState('按住下方按钮开始说话')
  const [lastUserText, setLastUserText] = useState('')
  const [lastReply, setLastReply] = useState('')
  const [riskLevel, setRiskLevel] = useState('none')
  const [hotlines, setHotlines] = useState([])
  const [err, setErr] = useState('')
  const [diag] = useState(() => mediaDiagnosis())
  const [videoOn, setVideoOn] = useState(kind === 'video')
  const [micOn, setMicOn] = useState(true)
  const [elapsed, setElapsed] = useState(0)
  const [turnCount, setTurnCount] = useState(0)
  const [diary, setDiary] = useState(null)
  const [finishing, setFinishing] = useState(false)

  const videoRef = useRef(null)
  const streamRef = useRef(null)   // 摄像头流
  const recRef = useRef(null)      // MediaRecorder
  const recStreamRef = useRef(null)
  const chunksRef = useRef([])
  const timerRef = useRef(null)
  const aliveRef = useRef(true)

  // ---- 摄像头 ----
  const startCamera = useCallback(async () => {
    if (!diag.ok) return
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: { width: 640, height: 480, facingMode: 'user' }, audio: false,
      })
      streamRef.current = s
      if (videoRef.current) { videoRef.current.srcObject = s; await videoRef.current.play() }
      setVideoOn(true)
    } catch (e) {
      setErr(`${explainMediaError(e, '摄像头')}（可关闭视频继续语音通话）`)
      setVideoOn(false)
    }
  }, [diag.ok])

  const stopCamera = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    setVideoOn(false)
  }, [])

  useEffect(() => {
    aliveRef.current = true
    warmupTTS()
    timerRef.current = setInterval(() => setElapsed((s) => s + 1), 1000)
    if (kind === 'video') startCamera()
    return () => {
      aliveRef.current = false
      clearInterval(timerRef.current)
      stopSpeak()
      recStreamRef.current?.getTracks().forEach((t) => t.stop())
      stopCamera()
    }
  }, [])

  const toggleVideo = async () => {
    if (videoOn) stopCamera()
    else await startCamera()
  }

  // ---- 录音 ----
  const startRecording = async () => {
    if (status === 'thinking') return
    stopSpeak() // 用户开口即打断 AI 朗读
    if (!diag.ok) { setErr(`⚠ ${diag.reason} ${diag.fix}`); return }
    if (!micOn) { setErr('麦克风已关闭，请先打开'); return }
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true })
      recStreamRef.current = s
      const mime = (() => {
        const c = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']
        for (const m of c) if (window.MediaRecorder?.isTypeSupported?.(m)) return m
        return ''
      })()
      const rec = new MediaRecorder(s, mime ? { mimeType: mime } : undefined)
      chunksRef.current = []
      rec.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunksRef.current.push(e.data) }
      rec.onstop = async () => {
        s.getTracks().forEach((t) => t.stop())
        const blob = new Blob(chunksRef.current, { type: mime || 'audio/webm' })
        if (blob.size >= 500) await handleAudio(blob)
        else { setStatus('idle'); setSubtitle('没听清，再按住说一次'); }
      }
      recRef.current = rec
      rec.start()
      setStatus('recording')
      setSubtitle('正在聆听… 松手结束')
      setErr('')
    } catch (e) {
      setErr(explainMediaError(e, '麦克风'))
    }
  }

  const stopRecording = () => {
    if (recRef.current && recRef.current.state === 'recording') {
      recRef.current.stop()
    }
  }

  // ---- 一轮：ASR → chat → TTS ----
  const handleAudio = async (blob) => {
    setStatus('thinking')
    setSubtitle('正在理解你说的话…')
    try {
      const asr = await api.asr(blob)
      const text = (asr.text || '').trim()
      if (!text) {
        setStatus('idle'); setSubtitle('没识别出内容，再说一次好吗')
        if (asr.asr_error) setErr(asr.asr_error)
        return
      }
      setLastUserText(text)
      const sid = await ensureSession()
      const voice = {
        audio_features: asr.audio_features || {},
        tone: asr.tone || '', tone_reason: asr.tone_reason || '', summary: asr.summary || '',
      }
      const out = await api.chatSync(userId, sid, text, voice)
      setLastReply(out.reply || '')
      setRiskLevel(out.risk_level || 'none')
      onRisk?.(out.risk_level || '')
      setTurnCount((n) => n + 1)

      if (out.crisis_mode || ['high', 'imminent'].includes(out.risk_level)) {
        setHotlines(out.hotlines || [])
        setSubtitle('我需要先确认你的安全')
      } else {
        setHotlines([])
      }

      // 打断状态：若被 interrupt 挂起，仍朗读回复（危机话术已含热线），但提示人工介入
      if (ttsSupported() && micOn) {
        setStatus('speaking')
        setSubtitle('我在说…（点一下可打断）')
        speak(out.reply || '', {
          onEnd: () => { if (aliveRef.current) { setStatus('idle'); setSubtitle('按住说话继续') } },
          onError: () => { if (aliveRef.current) { setStatus('idle'); setSubtitle('按住说话继续') } },
        })
      } else {
        setStatus('idle')
        setSubtitle('按住说话继续')
      }
    } catch (e) {
      setErr(String(e.message || e))
      setStatus('idle'); setSubtitle('出了点问题，再试一次')
    }
  }

  // ---- 打断 ----
  const interrupt = () => {
    stopSpeak()
    if (status === 'speaking') { setStatus('idle'); setSubtitle('已打断，按住说话继续') }
  }

  // ---- 挂断 → 生成日记 ----
  const hangup = async () => {
    stopSpeak()
    recStreamRef.current?.getTracks().forEach((t) => t.stop())
    stopCamera()
    setFinishing(true)
    setSubtitle('正在为你整理今天的日记…')
    try {
      const sid = await ensureSession()
      const r = await api.diaryGenerate({
        user_id: userId, session_id: sid, transcript: '',
        emotion_label: '', risk_level: riskLevel,
      })
      setDiary(r.diary)
      setSubtitle('今天的日记已经写好了')
    } catch (e) {
      setErr(`日记生成失败：${e.message}`)
      setSubtitle('通话已结束')
    } finally {
      setFinishing(false)
    }
  }

  const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

  // 结束后展示日记卡片
  if (diary) {
    return (
      <div className="talk-diary">
        <div className="card" style={{ maxWidth: 520, margin: '40px auto' }}>
          <div className="row" style={{ marginTop: 0 }}>
            <span style={{ fontSize: 40 }}>{diary.mood_emoji}</span>
            <div>
              <h3 style={{ margin: 0 }}>{diary.title}</h3>
              <span className="muted small">{diary.date} · 心情：{diary.mood_label}</span>
            </div>
          </div>
          <p style={{ lineHeight: 1.9, marginTop: 14 }}>{diary.content}</p>
          {diary.keywords?.length > 0 && (
            <div className="row" style={{ marginTop: 8 }}>
              {diary.keywords.map((k, i) => <span key={i} className="chip">{k}</span>)}
            </div>
          )}
          {diary.risk_level !== 'none' && (
            <p className="muted small" style={{ marginTop: 12 }}>
              如果此刻很难受，记得随时可以找信任的人聊聊，或拨打求助热线 {hotlines?.[0]?.number || '12356'}。
            </p>
          )}
          <div className="row" style={{ marginTop: 16 }}>
            <button className="btn primary" onClick={() => onExit?.('diary')}>查看我的日记</button>
            <button className="btn ghost" onClick={() => onExit?.('home')}>回到首页</button>
          </div>
        </div>
      </div>
    )
  }

  const riskMeta = RISK_META[riskLevel] || RISK_META.none

  return (
    <div className="talk">
      <div className="talk-stage" onClick={interrupt}>
        {videoOn ? (
          <video ref={videoRef} muted playsInline className="talk-video" />
        ) : (
          <div className="talk-avatar">
            <div className="talk-orb" data-state={status} />
            <div className="talk-status">{subtitle}</div>
            {lastUserText && <div className="talk-bubble you">“{lastUserText}”</div>}
            {lastReply && <div className="talk-bubble ai">{lastReply}</div>}
          </div>
        )}
        <div className="talk-top">
          <span className="badge subtle">{kind === 'video' ? '视频通话' : '语音通话'} · {fmt(elapsed)}</span>
          <span className="badge subtle">第 {turnCount} 轮</span>
          {riskLevel !== 'none' && (
            <span className="chip" style={{ color: riskMeta.color, background: riskMeta.bg }}>
              风险 {riskMeta.label}
            </span>
          )}
        </div>
        {!diag.ok && (
          <div className="talk-warn">⚠ {diag.reason}。{diag.fix}</div>
        )}
        {err && <div className="talk-warn">{err}</div>}
        {hotlines.length > 0 && (
          <div className="talk-hotlines">
            <b>先照顾好自己，这些渠道现在就能帮你：</b>
            <ul>{hotlines.map((h) => <li key={h.number}>{h.name} <b>{h.number}</b></li>)}</ul>
          </div>
        )}
      </div>

      <div className="talk-controls">
        <button className={micOn ? 'ctl' : 'ctl off'} onClick={() => setMicOn((v) => !v)}
          title={micOn ? '关闭麦克风' : '打开麦克风'}>{micOn ? '🎙' : '🔇'}</button>
        <button className={videoOn ? 'ctl' : 'ctl off'} onClick={toggleVideo}
          title={videoOn ? '关闭摄像头' : '打开摄像头'}>{videoOn ? '🎬' : '🚫'}</button>
        <button
          className={status === 'recording' ? 'ctl main recording' : 'ctl main'}
          disabled={status === 'thinking' || finishing}
          onMouseDown={startRecording} onMouseUp={stopRecording}
          onTouchStart={(e) => { e.preventDefault(); startRecording() }}
          onTouchEnd={(e) => { e.preventDefault(); stopRecording() }}
          title="按住说话，松手发送">
          {status === 'recording' ? '■ 松手发送' : status === 'thinking' ? '思考中…' : '🎤 按住说话'}
        </button>
        <button className="ctl" onClick={interrupt} title="打断 AI 说话">⏯ 打断</button>
        <button className="ctl hang" onClick={hangup} disabled={finishing} title="挂断并生成日记">
          {finishing ? '…' : '✕ 挂断'}
        </button>
      </div>
      <p className="muted small" style={{ textAlign: 'center', marginTop: 8 }}>
        按住「说话」说出你的想法，松手后我会回应并读给你听；点画面可打断。结束后自动生成日记。
      </p>
    </div>
  )
}
