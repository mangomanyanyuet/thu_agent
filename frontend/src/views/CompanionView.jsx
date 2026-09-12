import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import VoiceRecorder from '../VoiceRecorder.jsx'
import ReportView from './ReportView.jsx'
import { createFaceSampler, loadFaceModel } from '../faceAnalyzer.js'
import { mediaDiagnosis, explainMediaError } from '../env.js'

const PHASE_LABEL = {
  consent: '知情同意',
  quality: '环境检测',
  opening: '开场',
  main: '对话中',
  closing: '收尾',
  crisis: '危机干预',
  done: '已完成',
}

export default function CompanionView({ userId, refresh }) {
  const [step, setStep] = useState('consent') // consent|quality|talking|report
  const [finishResult, setFinishResult] = useState(null)
  const [sid, setSid] = useState('')
  const [phase, setPhase] = useState('consent')
  const [msgs, setMsgs] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [voice, setVoice] = useState(null)
  const [voiceKey, setVoiceKey] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [report, setReport] = useState(null)
  const [faceReady, setFaceReady] = useState(false)
  const [faceOn, setFaceOn] = useState(false)
  const [quality, setQuality] = useState(null)
  const [checking, setChecking] = useState(false)
  const [consent, setConsent] = useState({
    understood_ai: false, consent_media: false, adult_confirmed: false,
    audio: true, video: true, data_retention_ok: false,
  })

  const videoRef = useRef(null)
  const draftRef = useRef(null)
  const streamRef = useRef(null)
  const samplerRef = useRef(null)
  const rafRef = useRef(null)
  const timerRef = useRef(null)

  // ---------------- 摄像头 ----------------
  const startCamera = useCallback(async () => {
    // 先做安全上下文诊断：非 https 时 navigator.mediaDevices 为 undefined，
    // 直接调用会抛 TypeError（读不到 getUserMedia），而不是有意义的权限错误。
    const diag = mediaDiagnosis()
    if (!diag.ok) {
      setErr(`⚠️ ${diag.reason}\n${diag.fix}`)
      setStep('talking') // 降级为纯文本模式，不阻断会话
      return false
    }
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: { width: 640, height: 480, facingMode: 'user' },
      })
      streamRef.current = s
      if (videoRef.current) {
        videoRef.current.srcObject = s
        await videoRef.current.play()
      }
      return true
    } catch (e) {
      setErr(`${explainMediaError(e, '摄像头')}（将降级为纯语音模式）`)
      return false
    }
  }, [])

  const stopCamera = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    setFaceOn(false)
  }, [])

  useEffect(() => () => stopCamera(), [stopCamera])

  // ---------------- 开始会话 ----------------
  const begin = async () => {
    setErr('')
    setBusy(true)
    try {
      const r = await api.companionStart(userId)
      setSid(r.session_id)
      const c = await api.companionConsent(r.session_id, consent)
      setPhase(c.phase)
      setStep('quality')
      loadFaceModel().then(() => setFaceReady(true)).catch(() => setFaceReady(false))
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  // ---------------- 环境质检（PRD §6.3.2） ----------------
  const runQuality = async () => {
    setChecking(true)
    setErr('')
    const ok = consent.video && (await startCamera())
    let q = { face_visible: false, single_person: true, low_light: false,
              multi_person: false, brightness: null }
    if (ok && faceReady) {
      samplerRef.current = createFaceSampler()
      const t0 = performance.now()
      await new Promise((res) => {
        const tick = () => {
          samplerRef.current.sample(videoRef.current)
          if (performance.now() - t0 > 2000) return res()
          rafRef.current = requestAnimationFrame(tick)
        }
        tick()
      })
      const s = samplerRef.current.finish()
      q = {
        face_visible: s.face_detected,
        single_person: !s.quality.multi_person,
        low_light: s.quality.low_light,
        multi_person: s.quality.multi_person,
        brightness: s.quality.brightness,
      }
      setQuality(s.quality)
      samplerRef.current.reset()
    }
    try {
      const r = await api.companionQuality(sid, q)
      setPhase(r.phase)
      setMsgs([{ role: 'assistant', content: r.opening }])
      setStep('talking')
      if (ok) setFaceOn(true)
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setChecking(false)
    }
  }

  const skipQuality = async () => {
    setErr('')
    try {
      const r = await api.companionQuality(sid, {
        face_visible: false, single_person: true, low_light: false,
        multi_person: false,
      })
      setPhase(r.phase)
      setMsgs([{ role: 'assistant', content: r.opening }])
      setStep('talking')
    } catch (e) {
      setErr(String(e.message || e))
    }
  }

  // ---------------- 计时（PRD §6.1） ----------------
  useEffect(() => {
    if (step !== 'talking') return
    timerRef.current = setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => clearInterval(timerRef.current)
  }, [step])

  // 面部采样循环
  useEffect(() => {
    if (step !== 'talking' || !faceOn || !faceReady) return
    if (!samplerRef.current) samplerRef.current = createFaceSampler()
    const tick = () => {
      samplerRef.current.sample(videoRef.current)
      rafRef.current = requestAnimationFrame(tick)
    }
    tick()
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [step, faceOn, faceReady])

  // ---------------- 发送一轮 ----------------
  const send = async () => {
    if (busy) return
    const userText = draftRef.current?.value?.trim()
    if (!userText) return
    setBusy(true)
    setErr('')

    const faceSnap = faceOn && samplerRef.current ? samplerRef.current.finish() : null
    if (samplerRef.current) samplerRef.current.reset()

    const thisVoice = voice
    setMsgs((m) => [...m, { role: 'user', content: userText, voice: thisVoice }])
    draftRef.current.value = ''

    try {
      const r = await api.companionTurn(sid, {
        text: userText,
        audio: thisVoice
          ? { audio_features: thisVoice.audio_features, tone: thisVoice.tone,
              tone_reason: thisVoice.tone_reason, summary: thisVoice.summary }
          : null,
        face: faceSnap,
      })
      setPhase(r.phase)
      setMsgs((m) => [...m, { role: 'assistant', content: r.reply }])
      setVoice(null)
      setVoiceKey((k) => k + 1)
      if (r.should_finish) await finish()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const finish = async () => {
    stopCamera()
    if (timerRef.current) clearInterval(timerRef.current)
    try {
      // 收尾而非只读报告：后端会同时沉淀每日记录、更新画像与风险趋势
      const r = await api.companionFinish(sid)
      setFinishResult(r)
      setReport(r.report)
      setStep('report')
      refresh?.()
    } catch (e) {
      setErr(String(e.message || e))
    }
  }

  const onVoiceResult = useCallback((r) => {
    setVoice(r)
    if (r.text && draftRef.current) {
      draftRef.current.value = draftRef.current.value
        ? `${draftRef.current.value} ${r.text}` : r.text
    }
  }, [])

  const allChecked =
    consent.understood_ai && consent.consent_media && consent.adult_confirmed

  // ---------------- 渲染 ----------------
  if (step === 'report' && report) {
    return (
      <ReportView
        report={report}
        finishResult={finishResult}
        onRestart={() => {
          setStep('consent'); setMsgs([]); setReport(null)
          setFinishResult(null); setElapsed(0); setSid('')
        }}
      />
    )
  }

  const turnCount = msgs.filter((m) => m.role === 'user').length

  return (
    <div className="cmp">
      {(step === 'quality' || step === 'talking') && (
        <div className="cmp-video">
          <video ref={videoRef} muted playsInline />
          {!faceOn && (
            <div className="cmp-video-off">
              摄像头未开启{faceReady ? '' : '（模型加载中）'}
            </div>
          )}
          {quality && faceOn && (
            <div className="cmp-quality">
              人脸 {Math.round((quality.face_ratio || 0) * 100)}%
              {quality.low_light && <span className="chip warn">光线偏暗</span>}
              {quality.multi_person && <span className="chip danger">多人入镜</span>}
            </div>
          )}
        </div>
      )}

      <div className="cmp-main">
        {step === 'consent' && (
          <div className="card">
            <h3>开始前，请先确认</h3>
            <p className="muted small">
              本工具是一款 AI 情绪陪伴助手，通过分析你的语音和表情提供情绪状态参考，
              <b>不能替代专业心理咨询或医疗诊断</b>。
              如果你正经历持续的情绪困扰，建议寻求专业帮助。
            </p>
            <label className="chk">
              <input type="checkbox" checked={consent.understood_ai}
                onChange={(e) => setConsent({ ...consent, understood_ai: e.target.checked })} />
              <span>我已了解这是 AI 工具，不是医疗/心理咨询服务</span>
            </label>
            <label className="chk">
              <input type="checkbox" checked={consent.consent_media}
                onChange={(e) => setConsent({ ...consent, consent_media: e.target.checked })} />
              <span>我同意采集我的语音与面部表情用于情绪分析</span>
            </label>
            <label className="chk">
              <input type="checkbox" checked={consent.adult_confirmed}
                onChange={(e) => setConsent({ ...consent, adult_confirmed: e.target.checked })} />
              <span>我已成年（或已获得监护人同意）</span>
            </label>
            <label className="chk">
              <input type="checkbox" checked={consent.data_retention_ok}
                onChange={(e) => setConsent({ ...consent, data_retention_ok: e.target.checked })} />
              <span className="muted">（可选）同意匿名化数据用于产品优化</span>
            </label>
            <div className="row">
              <button className="btn primary" disabled={!allChecked || busy} onClick={begin}>
                {busy ? '准备中…' : '我同意，开始'}
              </button>
              {!allChecked && <span className="muted small">请先勾选前三项</span>}
            </div>
            <p className="muted small">
              面部画面仅在本地实时分析，<b>不会上传原始视频</b>，只上传表情特征数值。
            </p>
          </div>
        )}

        {step === 'quality' && (
          <div className="card">
            <h3>环境检测</h3>
            <p className="muted small">
              请面向摄像头，确保光线充足、画面中只有你一人。检测约 2 秒。
            </p>
            <div className="row">
              <button className="btn primary" onClick={runQuality} disabled={checking}>
                {checking ? '检测中…' : '开始检测并进入对话'}
              </button>
              <button className="btn ghost" onClick={skipQuality}>
                跳过（纯语音模式）
              </button>
            </div>
            {!faceReady && (
              <p className="muted small">
                面部模型加载中或加载失败，可跳过使用纯语音模式。
              </p>
            )}
          </div>
        )}

        {step === 'talking' && (
          <>
            <div className="cmp-head">
              <span className="badge subtle">{PHASE_LABEL[phase] || phase}</span>
              <span className="muted small">{fmt(elapsed)} / 5:00</span>
              <span className="muted small">第 {Math.min(turnCount + 1, 4)} / 4 轮</span>
              <button className="btn tiny ghost" onClick={finish}>结束并查看小结</button>
            </div>

            <div className="cmp-msgs">
              {msgs.map((m, i) => (
                <div key={i} className={`msg ${m.role}`}>
                  <div className="bubble">{m.content}</div>
                  {m.voice && (
                    <div className="msg-meta small">🎙 {m.voice.tone || '语音'}</div>
                  )}
                </div>
              ))}
              {busy && <div className="msg assistant"><div className="bubble">…</div></div>}
            </div>

            <div className="composer">
              <VoiceRecorder key={voiceKey} onResult={onVoiceResult}
                onError={(m) => setErr(m)} />
              <textarea ref={draftRef} placeholder="说点什么…（Enter 发送）"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
                }} />
              <button className="btn primary" onClick={send} disabled={busy}>
                {busy ? '思考中…' : '发送'}
              </button>
            </div>
          </>
        )}

        {err && <div className="voice-err">{err}</div>}
      </div>
    </div>
  )
}

function fmt(s) {
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}
