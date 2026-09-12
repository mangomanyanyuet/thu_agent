import { useEffect, useState } from 'react'
import { api } from '../api.js'

/**
 * 上下文装配可视化 —— 这是整个系统最值得检查的地方。
 * 直接把注入给 LLM 的 system prompt 原样展示，确认：
 *  1. 安全区（当前风险等级 + 历史风险原话）确实在最前面且未被截断
 *  2. 画像 / 多模态 / 量表趋势 / 摘要 各自占了多少
 */
export default function TraceView({ sessionId, userId }) {
  const [traces, setTraces] = useState([])
  const [tab, setTab] = useState(0)

  useEffect(() => {
    if (!sessionId) return
    api.traces(sessionId).then(setTraces).catch(() => setTraces([]))
  }, [sessionId])

  const ctxs = traces.filter((t) => t.node === 'context')

  return (
    <div className="stack">
      <div className="card">
        <h3>注入给 LLM 的 System Prompt（原样）</h3>
        <p className="muted small">
          {ctxs.length === 0
            ? '当前会话还没有上下文记录，先去对话页发一条消息。'
            : `共 ${ctxs.length} 轮，选择查看：`}
        </p>
        {ctxs.length > 0 && (
          <>
            <div className="row wrap">
              {ctxs.map((_, i) => (
                <button
                  key={i}
                  className={tab === i ? 'btn tiny primary' : 'btn tiny'}
                  onClick={() => setTab(i)}
                >
                  第 {ctxs.length - i} 轮
                </button>
              ))}
            </div>
            <pre className="prompt-box">{ctxs[tab]?.input_ctx}</pre>
          </>
        )}
      </div>

      <div className="card">
        <h3>审计 Trace</h3>
        <p className="muted small">
          每次推理的输入上下文与输出都被记录（LangGraph checkpointer + audit_trace 表），
          用于事故回放与合规审计。
        </p>
        <table className="table">
          <thead>
            <tr><th>ID</th><th>节点</th><th>时间</th><th>输出摘要</th></tr>
          </thead>
          <tbody>
            {traces.map((t) => (
              <tr key={t.id}>
                <td className="small">{t.id}</td>
                <td className="small">{t.node}</td>
                <td className="small">{t.ts?.slice(0, 19).replace('T', ' ')}</td>
                <td className="small">{(t.output || '').slice(0, 120)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
