import { useCallback, useEffect, useRef, useState } from 'react'
import VoiceRecorder from '../VoiceRecorder.jsx'
import { mediaDiagnosis } from '../env.js'
import { warmupTTS, ttsSupported } from '../tts.js'

// 推荐话题：点击即进入对话并自动带上这句话
const SUGGESTIONS = [
  '最近工作压力好大，晚上睡不着',
  '总觉得提不起劲，什么都不想做',
  '和家里人吵架了，心里很堵',
  '帮我梳理一下今天的心情',
]

export default function HomeView({ onNavigate, onStartTalk, onStartChat }) {
  const [input, setInput] = useState('')
  const [mode, setMode] = useState('chat') // chat | companion —— 视觉切换，实际都进入通话/对话
  const [err, setErr] = useState('')
  const [voice, setVoice] = useState(null)
  const [voiceKey, setVoiceKey] = useState(0)
  const [diag] = useState(() => mediaDiagnosis())
  const inputRef = useRef(null)

  useEffect(() => { warmupTTS() }, [])

  // 快捷进入通话页。kind: 'voice' | 'video'
  const startTalk = (kind) => {
    onStartTalk?.(kind)
  }

  // 首句交给对话页用 SSE 流式发出：立刻切页，用户能看到自己的气泡和逐字回复。
  // 之前这里先静默调 /api/chat 再跳到不存在的 'chat' 视图，结果是空白页。
  const send = (text) => {
    const t = (text ?? input).trim()
    if (!t) return
    setErr('')
    if (onStartChat) onStartChat({ text: t, voice })
    else onNavigate?.('chat')
    setInput(''); setVoice(null); setVoiceKey((k) => k + 1)
  }

  const onVoiceResult = useCallback((r) => {
    setVoice({
      audio_features: r.audio_features || {}, tone: r.tone || '',
      tone_reason: r.tone_reason || '', summary: r.summary || '',
    })
    if (r.text) setInput((old) => (old ? `${old} ${r.text}` : r.text))
  }, [])

  return (
    <div className="home">
      <div className="home-hero">
        <h2>有什么想聊的吗？</h2>
        <p className="muted">我在这里，随时可以听你说说。语音、视频或打字都行。</p>
      </div>

      <div className="home-modes">
        <button className={mode === 'chat' ? 'mode active' : 'mode'}
          onClick={() => setMode('chat')}>对话</button>
        <button className={mode === 'companion' ? 'mode active' : 'mode'}
          onClick={() => setMode('companion')}>陪伴</button>
      </div>

      <div className="home-suggest">
        <div className="muted small home-label">为你推荐</div>
        {SUGGESTIONS.map((s) => (
          <button key={s} className="suggest" onClick={() => send(s)}>
            {s}
          </button>
        ))}
      </div>

      <div className="home-actions">
        <button className="act" onClick={() => startTalk('voice')}>
          <span className="act-ic">🎙</span><span>语音通话</span>
        </button>
        <button className="act" onClick={() => startTalk('video')}>
          <span className="act-ic">🎬</span><span>视频通话</span>
        </button>
        <button className="act" onClick={() => onNavigate?.('diary')}>
          <span className="act-ic">📔</span><span>我的日记</span>
        </button>
        <button className="act" onClick={() => onNavigate?.('pro')}>
          <span className="act-ic">⚙️</span><span>专业版</span>
        </button>
      </div>

      {!diag.ok && (
        <p className="muted small" style={{ marginTop: 10 }}>
          ⚠ 语音/视频需要 HTTPS 或在本机访问；当前环境可能不支持，文字对话不受影响。
          {!ttsSupported() && ' 本浏览器不支持语音朗读。'}
        </p>
      )}

      <div className="home-composer">
        <VoiceRecorder key={voiceKey} onResult={onVoiceResult} onError={(m) => setErr(m)} />
        <input ref={inputRef} className="home-input"
          placeholder="发消息或按住说话…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <button className="btn primary" onClick={() => send()} disabled={!input.trim()}>
          发送
        </button>
      </div>
      {voice && (
        <div className="muted small" style={{ marginTop: 6 }}>
          🎙 本轮将附带语音特征（{voice.tone || '未知语气'}）
          <button className="btn tiny ghost" onClick={() => setVoice(null)}>移除</button>
        </div>
      )}
      {err && <div className="voice-err" style={{ marginTop: 8 }}>{err}</div>}
    </div>
  )
}
