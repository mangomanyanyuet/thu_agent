import { useState } from 'react'

// 推荐话题：点击即进入文字对话并自动带上这句话
const SUGGESTIONS = [
  '最近工作压力好大，晚上睡不着',
  '总觉得提不起劲，什么都不想做',
  '和家里人吵架了，心里很堵',
  '帮我梳理一下今天的心情',
]

export default function HomeView({ onNavigate, onStartChat }) {
  const [input, setInput] = useState('')

  // 首句交给对话页用 SSE 流式发出：立刻切页，能看到自己的气泡和逐字回复
  const send = (text) => {
    const t = (text ?? input).trim()
    if (!t) return
    if (onStartChat) onStartChat({ text: t })
    else onNavigate?.('chat')
    setInput('')
  }

  return (
    <div className="home">
      <div className="home-hero">
        <h2>有什么想聊的吗？</h2>
        <p className="muted">我在这里，随时可以听你说说。直接打字就好。</p>
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
        <button className="act" onClick={() => onStartChat?.()}>
          <span className="act-ic">💬</span><span>开始聊天</span>
        </button>
        <button className="act" onClick={() => onNavigate?.('diary')}>
          <span className="act-ic">📔</span><span>我的日记</span>
        </button>
        <button className="act" onClick={() => onNavigate?.('pro')}>
          <span className="act-ic">⚙️</span><span>专业版</span>
        </button>
      </div>

      <div className="home-composer">
        <input className="home-input"
          placeholder="输入你想说的话…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <button className="btn primary" onClick={() => send()} disabled={!input.trim()}>
          发送
        </button>
      </div>
    </div>
  )
}
