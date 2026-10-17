import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { mediaDiagnosis, explainMediaError } from './env.js'

const TONE_META = {
  low: { label: '语气低落', color: '#dc2626', bg: '#fef2f2', icon: '😔' },
  flat: { label: '语气平淡', color: '#ea580c', bg: '#fff7ed', icon: '😐' },
  neutral: { label: '语气平稳', color: '#16a34a', bg: '#ecfdf5', icon: '🙂' },
  agitated: { label: '语气激动', color: '#d97706', bg: '#fffbeb', icon: '😰' },
  unknown: { label: '语气未知', color: '#6b7280', bg: '#f3f4f6', icon: '❓' },
}

/**
 * 录音 -> /api/asr -> 转写文本 + 声学特征 + 语气判定。
 *
 * 用 MediaRecorder + webm/opus，后端用 PyAV 解码（不依赖系统 ffmpeg）。
 * Safari 不支持 webm，会回退到 mp4。
 */
export function pickMime() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ]
  for (const m of candidates) {
    if (window.MediaRecorder?.isTypeSupported?.(m)) return m
  }
  return ''
}

export default function VoiceRecorder({ onResult, onError }) {
  const [supported, setSupported] = useState(false)
  const [asrStat, setAsrStat] = useState(null)
  const [recording, setRecording] = useState(false)
  const [busy, setBusy] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [result, setResult] = useState(null)
  const [level, setLevel] = useState(0)
  const [diag, setDiag] = useState({ ok: true, reason: '', fix: '' })

  const mediaRef = useRef(null)
  const recRef = useRef(null)
  const chunksRef = useRef([])
  const timerRef = useRef(null)
  const audioCtxRef = useRef(null)
  const rafRef = useRef(null)

  useEffect(() => {
    const diag = mediaDiagnosis()
    setDiag(diag)
    setSupported(diag.ok)
    api.asrStatus().then(setAsrStat).catch(() =>
      setAsrStat({ available: false, model_ready: false, hint: '' })
    )
    return () => stopMetering()
  }, [])

  // 装了包只是前提，权重在本地缓存里才真能转写。
  // 只看 available 会出现"按钮亮着但永远识别不出内容"。
  const asrBlocked = asrStat ? !(asrStat.available && asrStat.model_ready !== false) : false

  const stopMetering = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    audioCtxRef.current?.close?.()
    audioCtxRef.current = null
    setLevel(0)
  }, [])

  /** 实时音量条：只做可视化，不用它做 VAD（后端另算） */
  const startMetering = useCallback((stream) => {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)()
      const src = ctx.createMediaStreamSource(stream)
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      src.connect(analyser)
      audioCtxRef.current = ctx
      const buf = new Uint8Array(analyser.frequencyBinCount)
      const tick = () => {
        analyser.getByteTimeDomainData(buf)
        let sum = 0
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128
          sum += v * v
        }
        setLevel(Math.min(1, Math.sqrt(sum / buf.length) * 3))
        rafRef.current = requestAnimationFrame(tick)
      }
      tick()
    } catch {
      /* 音量条失败不影响录音 */
    }
  }, [])

  const start = async () => {
    if (!supported || busy) return
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      })
      mediaRef.current = stream
      startMetering(stream)

      const mime = pickMime()
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
      chunksRef.current = []
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data)
      }
      rec.onstop = async () => {
        stopMetering()
        stream.getTracks().forEach((t) => t.stop())
        const blob = new Blob(chunksRef.current, { type: mime || 'audio/webm' })
        await upload(blob)
      }
      recRef.current = rec
      rec.start()
      setRecording(true)
      setResult(null)
      setElapsed(0)
      timerRef.current = setInterval(() => setElapsed((s) => s + 1), 1000)
    } catch (e) {
      onError?.(explainMediaError(e, '麦克风'))
    }
  }

  const stop = () => {
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = null
    setRecording(false)
    recRef.current?.stop()   // 触发 onstop -> upload
  }

  const upload = async (blob) => {
    if (blob.size < 500) {
      onError?.('录音太短，请再说一遍')
      return
    }
    setBusy(true)
    try {
      const r = await api.asr(blob)
      setResult(r)
      onResult?.(r)
    } catch (e) {
      onError?.(`语音识别失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const tone = result ? TONE_META[result.tone] || TONE_META.unknown : null

  return (
    <div className="voice">
      <div className="voice-row">
        <button
          className={recording ? 'mic rec' : 'mic'}
          onClick={() => (recording ? stop() : start())}
          disabled={!supported || busy || asrBlocked}
          title={asrBlocked ? '后端 ASR 未就绪' : '点击录音'}
        >
          {recording ? '■' : '🎙'}
        </button>
        <div className="voice-info">
          {busy && <span className="muted">识别中…</span>}
          {!busy && recording && (
            <span className="rec-label">
              <i /> 录音中 {elapsed}s · 再点一次结束
            </span>
          )}
          {!busy && !recording && !result && (
            <span className="muted small">
              {asrBlocked
                ? asrStat?.hint || '后端 ASR 未启用（pip install faster-whisper）'
                : '点击麦克风说话，自动转文字并分析语气'}
            </span>
          )}
          {/* 不可用时必须说清原因，不能只把按钮置灰 */}
          {!supported && diag.reason && (
            <div className="media-block">
              <div className="media-block-title">⚠️ {diag.reason}</div>
              {diag.fix && <div className="media-block-fix">{diag.fix}</div>}
            </div>
          )}
          {!busy && result && (
            <div className="voice-result">
              <div className="transcript">“{result.text || '（未识别出内容）'}”</div>
              {/* 失败必须说清是"没说话"还是"模型没下载"，否则无法自查 */}
              {result.asr_error && (
                <div className="muted small reason" style={{ color: '#b45309' }}>
                  ⚠️ {result.asr_error}
                </div>
              )}
              <div className="voice-meta">
                <span className="chip" style={{ color: tone.color, background: tone.bg }}>
                  {tone.icon} {tone.label}
                </span>
                {result.audio_features?.f0_mean_hz > 0 && (
                  <span className="muted small">
                    基频 {result.audio_features.f0_mean_hz}Hz · 标准差{' '}
                    {result.audio_features.f0_std}
                  </span>
                )}
                {result.audio_features?.syllable_rate && (
                  <span className="muted small">
                    语速 {result.audio_features.syllable_rate} 音节/秒
                  </span>
                )}
                {result.audio_features?.pause_ratio != null && (
                  <span className="muted small">
                    停顿 {Math.round(result.audio_features.pause_ratio * 100)}%
                  </span>
                )}
              </div>
              {result.tone_reason && (
                <div className="muted small reason">依据：{result.tone_reason}</div>
              )}
            </div>
          )}
        </div>
      </div>
      {recording && (
        <div className="meter">
          <div className="meter-fill" style={{ width: `${Math.round(level * 100)}%` }} />
        </div>
      )}
    </div>
  )
}
