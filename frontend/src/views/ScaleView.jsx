import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function ScaleView({ userId, refresh }) {
  const [defs, setDefs] = useState({})
  const [name, setName] = useState('PHQ-9')
  const [items, setItems] = useState([])
  const [result, setResult] = useState(null)

  useEffect(() => {
    api.scales().then((d) => {
      setDefs(d)
      setName('PHQ-9')
    })
  }, [])

  useEffect(() => {
    const n = defs[name]?.items?.length ?? 0
    setItems(Array(n).fill(0))
    setResult(null)
  }, [name, defs])

  const submit = async () => {
    const r = await api.submitScale({ user_id: userId, scale_name: name, item_scores: items })
    setResult(r)
    refresh?.()
  }

  const max = defs[name]?.max ?? 3
  const total = items.reduce((a, b) => a + b, 0)

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h3>心理量表</h3>
          <select value={name} onChange={(e) => setName(e.target.value)}>
            {Object.entries(defs).map(([k, v]) => (
              <option key={k} value={k}>{v.name}</option>
            ))}
          </select>
        </div>

        {defs[name]?.items?.map((q, i) => (
          <div className="scale-item" key={i}>
            <div className="q">
              <span className="idx">{i + 1}</span>
              {q}
              {name === 'PHQ-9' && i === 8 && <span className="chip danger">自杀相关条目 · 直接触发风险通道</span>}
            </div>
            <div className="opts">
              {Array.from({ length: max + 1 }, (_, v) => (
                <label key={v} className={items[i] === v ? 'opt on' : 'opt'}>
                  <input
                    type="radio"
                    name={`q${i}`}
                    checked={items[i] === v}
                    onChange={() => setItems((old) => old.map((x, j) => (j === i ? v : x)))}
                  />
                  {v}
                </label>
              ))}
            </div>
          </div>
        ))}

        <div className="row">
          <span className="muted">0 = 完全没有 … {max} = 几乎每天</span>
          <b>总分 {total}</b>
          <button className="btn primary" onClick={submit}>提交</button>
        </div>

        {result && (
          <div className="result">
            <div>
              <b>{result.score.scale_name}</b> 总分 {result.score.total_score}（{result.score.severity}）
              · 较上次 <b>{result.score.delta_vs_last > 0 ? '+' : ''}{result.score.delta_vs_last}</b>
              · 较基线 <b>{result.score.delta_vs_baseline > 0 ? '+' : ''}{result.score.delta_vs_baseline}</b>
            </div>
            {result.risk_level > 0 && (
              <div className="chip danger">
                触发量表风险通道：{['无', '轻度', '中度', '重度', '危急'][result.risk_level]} · {result.risk_evidence}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
