import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { RISK_META } from '../theme.js'

const LEVEL_NAMES = ['none', 'low', 'moderate', 'high', 'imminent']
const CHANNEL_LABELS = { rule: '规则', model: '模型', multimodal: '多模态', scale: '量表' }
// 预置回归样例句（文档 5.9.3），一键试跑看改动前后的分级
const SAMPLE_SENTENCES = [
  { text: '最近工作压力有点大，晚上睡不好', hint: '无/低风险 → 常规会谈' },
  { text: '有时候觉得活着没意思', hint: '轻度' },
  { text: '我真的不想活了', hint: '中度 → 安全计划' },
  { text: '我已经写好遗书了', hint: '重度 → 危机挂起' },
  { text: '以后不用管我了', hint: '告别式/隐喻（弱信号）' },
  { text: '我在天台，已经吃了东西了', hint: '危急 → 危机挂起' },
]

function RiskBadge({ level }) {
  const name = LEVEL_NAMES[level] || 'none'
  const meta = RISK_META[name] || RISK_META.none
  return <span className="chip" style={{ color: meta.color, background: meta.bg }}>{meta.label}</span>
}

function RiskRulesCard() {
  const [overview, setOverview] = useState(null)
  const [file, setFile] = useState(null)
  const [uploadTab, setUploadTab] = useState('paste') // 'paste' | 'file'：默认粘贴，绕开文件对话框
  const [pasteText, setPasteText] = useState('')
  const [pasteFmt, setPasteFmt] = useState('csv') // csv | json
  const [reason, setReason] = useState('')
  const [sample, setSample] = useState('我已经写好遗书了')
  const [draftVersion, setDraftVersion] = useState('') // 本次上传得到的草稿，用于试跑
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const fileRef = useRef(null)

  const reload = useCallback(async () => {
    try {
      const ov = await api.riskRulesOverview()
      setOverview(ov)
    } catch (e) {
      setErr(String(e.message || e))
    }
  }, [])

  useEffect(() => { reload() }, [reload])

  const upload = async () => {
    setBusy(true); setErr(''); setMsg('')
    try {
      let out
      if (uploadTab === 'paste') {
        if (!pasteText.trim()) { setErr('请先粘贴配置内容'); setBusy(false); return }
        out = await api.riskRulesUploadText(
          pasteText, pasteFmt === 'csv' ? 'words.csv' : 'config.json', reason)
      } else {
        if (!file) { setErr('请先选择文件'); setBusy(false); return }
        out = await api.riskRulesUpload(file, reason)
      }
      setDraftVersion(out.version)
      setMsg(`已上传为草稿 ${out.version}${out.warns?.length ? `（提示：${out.warns.join('；')}）` : ''}`)
      await reload()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const canUpload = uploadTab === 'paste' ? pasteText.trim().length > 0 : !!file

  const preview = async () => {
    if (!sample.trim()) return
    setBusy(true); setErr(''); setResult(null)
    try {
      const out = await api.riskRulesPreview({ text: sample.trim(), version: draftVersion || undefined })
      setResult(out)
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const publish = async (version) => {
    // P1 起发布会真实改变线上判定：理由是审计必填项，不能用占位文本绕过
    if (!reason.trim()) {
      setErr('发布必须填写变更理由（临床依据），会写入审计记录')
      return
    }
    setBusy(true); setErr(''); setMsg('')
    try {
      await api.riskRulesPublish(version, reason)
      setMsg(`已发布版本 ${version}，下一轮对话立即按新参数判定`)
      await reload()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const rollback = async (version) => {
    setBusy(true); setErr(''); setMsg('')
    try {
      await api.riskRulesRollback(version, reason || '回滚')
      setMsg(`已回滚到版本 ${version}`)
      await reload()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <div className="row">
        <h3>风险规则配置（医生）</h3>
        {overview && (
          <span className="chip" style={{ background: '#eef2ff', color: '#4f46e5' }}>
            当前生效：{overview.active_version}
          </span>
        )}
        {overview && overview.engine_wired === false && (
          <span className="chip" style={{ background: '#fef3c7', color: '#92400e' }}>
            配置未接入线上对话
          </span>
        )}
        <button className="btn ghost" onClick={() => window.dispatchEvent(new CustomEvent('mt:goto-tuning'))}>
          前往「临床参数」可视化调参 →
        </button>
        <button className="btn ghost" onClick={() => api.riskRulesDownload('template', 'csv', '词表模板.csv')}>
          下载词表模板
        </button>
        <button className="btn ghost" onClick={() => api.riskRulesDownload('template', 'json', '配置模板.json')}>
          下载 JSON 模板
        </button>
        <button className="btn ghost" onClick={() => api.riskRulesDownload('export', 'json', '当前配置.json')}>
          导出当前配置
        </button>
      </div>
      <p className="muted small">
        上传词表 CSV（列：等级,关键词）或完整配置 JSON → 系统校验红线护栏 → 存为草稿 → 试跑预览。
        护栏：imminent/high 词表不得删空、危机门槛不得高于 3。
      </p>

      <div className="row" style={{ gap: 6 }}>
        <button className={uploadTab === 'paste' ? 'btn tiny primary' : 'btn tiny ghost'}
          onClick={() => setUploadTab('paste')}>① 直接粘贴内容</button>
        <button className={uploadTab === 'file' ? 'btn tiny primary' : 'btn tiny ghost'}
          onClick={() => setUploadTab('file')}>② 选择文件</button>
      </div>

      {uploadTab === 'paste' && (
        <div style={{ marginTop: 8 }}>
          <div className="row" style={{ marginTop: 0 }}>
            <label className="muted small">格式：</label>
            <select value={pasteFmt} onChange={(e) => setPasteFmt(e.target.value)}>
              <option value="csv">CSV 词表（等级,关键词）</option>
              <option value="json">JSON 完整配置</option>
            </select>
            <button className="btn tiny ghost" onClick={async () => {
              try {
                await api.riskRulesDownload(
                  'template', pasteFmt,
                  pasteFmt === 'csv' ? '词表模板.csv' : '配置模板.json')
              } catch (e) { setErr(String(e.message || e)) }
            }}>下载 {pasteFmt.toUpperCase()} 模板</button>
          </div>
          <textarea
            style={{ width: '100%', minHeight: 96, marginTop: 6, fontFamily: 'monospace', fontSize: 12 }}
            placeholder={pasteFmt === 'csv'
              ? '等级,关键词\nhigh,写好遗书\nmoderate,不想活了\n…（每行一个词）'
              : '{ "patterns": { "high": ["写好遗书"] }, "gates": { "crisis_min_level": 3 } }'}
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
          />
          <p className="muted small" style={{ marginTop: 4 }}>
            提示：若文件选择按钮在内置浏览器里无法弹出对话框，用这里粘贴内容即可，效果相同。
          </p>
        </div>
      )}

      {uploadTab === 'file' && (
        <div style={{ marginTop: 8 }}>
          <div className="row" style={{ marginTop: 0 }}>
            {/* 用原生可见 input，不用 display:none + click()：隐藏元素在部分
                预览浏览器里弹不出对话框；也不加 accept 过滤，避免文件被灰掉
                只剩文件夹可选。格式合法性交给后端 _parse_rules_text 校验。 */}
            <input ref={fileRef} type="file"
              onChange={(e) => setFile(e.target.files[0] || null)} />
            {file && <span className="chip" style={{ background: '#eef2ff', color: '#4f46e5' }}>已选择：{file.name}</span>}
            {file && <button className="btn tiny ghost" onClick={() => { setFile(null); if (fileRef.current) fileRef.current.value = '' }}>清除</button>}
          </div>
          <p className="muted small" style={{ marginTop: 4 }}>
            支持 .csv / .json。若对话框里看不到文件，请把底部「文件类型」过滤器切到「所有文件」。
            仍不行就用上面的「① 直接粘贴内容」。
          </p>
        </div>
      )}

      {/* 上传动作单独成行且醒目：选好文件/粘好内容后，点这里完成校验并存草稿。
          变更理由为选填提示，不阻断上传（发布时才必填）。 */}
      <div className="row" style={{ marginTop: 4 }}>
        <input
          style={{ flex: 1 }}
          placeholder="变更理由（选填；发布时必填，说明临床依据）"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <button className="btn primary" onClick={upload} disabled={!canUpload || busy}>
          {busy ? '处理中…' : '⬆ 上传并校验'}
        </button>
        {!canUpload && (
          <span className="muted small">
            {uploadTab === 'file' ? '请先选择文件' : '请先粘贴配置内容'}
          </span>
        )}
        {canUpload && (
          <span className="muted small">
            {uploadTab === 'file' ? `已就绪：${file?.name}` : '内容已就绪'}，点左侧按钮上传
          </span>
        )}
      </div>

      <div className="row" style={{ marginTop: 8 }}>
        <input
          style={{ flex: 1 }}
          placeholder="输入样例句试跑，例如：我已经写好遗书了"
          value={sample}
          onChange={(e) => setSample(e.target.value)}
        />
        <button className="btn primary" onClick={preview} disabled={busy || !sample.trim()}>
          试跑预览
        </button>
      </div>
      <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
        {SAMPLE_SENTENCES.map((s) => (
          <button key={s.text} className="btn tiny ghost" title={s.hint} onClick={() => { setSample(s.text); setResult(null) }}>
            {s.text}
          </button>
        ))}
      </div>

      {result && (
        <div className="card" style={{ marginTop: 10, background: '#fafafa' }}>
          <div className="row">
            <b>“{result.text}”</b>
            <span className="muted small">试跑版本 {result.config_version}</span>
            {result.diff_vs_active && <span className="chip danger">{result.diff_vs_active}</span>}
          </div>
          <div className="row" style={{ marginTop: 6 }}>
            {Object.entries(result.channels).map(([k, v]) => (
              <span key={k} className="chip" style={{ color: RISK_META[LEVEL_NAMES[v.level]]?.color, background: RISK_META[LEVEL_NAMES[v.level]]?.bg }}>
                {CHANNEL_LABELS[k]}：{RISK_META[LEVEL_NAMES[v.level]]?.label}
              </span>
            ))}
          </div>
          <div className="row" style={{ marginTop: 6 }}>
            <span>命中词：<b>{result.rule_hit?.pattern || '—'}</b></span>
            <span>最终等级：<RiskBadge level={result.final_level} /></span>
            <span>分流：<b>{result.route}</b></span>
            {result.would_interrupt && <span className="chip danger">会挂起等人工审核</span>}
          </div>
        </div>
      )}

      {overview?.versions?.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <b className="small">版本历史</b>
          <div className="stack" style={{ gap: 4, marginTop: 4 }}>
            {overview.versions.map((v) => (
              <div key={v.version} className="row" style={{ borderTop: '1px solid #eee', paddingTop: 4 }}>
                <span className="chip" style={{ background: v.status === 'published' ? '#ecfdf5' : '#f3f4f6', color: v.status === 'published' ? '#16a34a' : '#6b7280' }}>
                  {v.status}
                </span>
                <span className="muted small">{v.version} · {v.uploaded_by || '匿名'} · {v.created_at?.slice(0, 16).replace('T', ' ')}</span>
                <span className="muted small">{v.note}</span>
                {v.status !== 'published' && (
                  <>
                    <button className="btn tiny" disabled={busy} onClick={() => publish(v.version)}>发布</button>
                    <button className="btn tiny ghost" disabled={busy} onClick={() => rollback(v.version)}>回滚</button>
                  </>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {err && <div className="voice-err" style={{ marginTop: 8 }}>{err}</div>}
      {msg && <div className="muted small" style={{ marginTop: 8, color: '#16a34a' }}>{msg}</div>}
    </div>
  )
}

export default function ReviewView({ refresh }) {
  const [flags, setFlags] = useState([])
  const [minLevel, setMinLevel] = useState(2)
  const [note, setNote] = useState({})

  const load = useCallback(async () => {
    const rows = await api.pendingReviews(minLevel)
    setFlags(rows)
  }, [minLevel])

  useEffect(() => {
    load()
  }, [load])

  const handle = async (id) => {
    await api.handleFlag(id, 'web-reviewer', note[id] || '已人工跟进')
    await load()
    refresh?.()
  }

  return (
    <div className="stack">
      <RiskRulesCard />

      <div className="card">
        <div className="row">
          <h3>待处理风险队列</h3>
          <select value={minLevel} onChange={(e) => setMinLevel(Number(e.target.value))}>
            <option value={1}>轻度及以上</option>
            <option value={2}>中度及以上</option>
            <option value={3}>重度及以上</option>
          </select>
          <button className="btn ghost" onClick={load}>刷新</button>
        </div>
        <p className="muted small">
          高危会话在生产环境必须有值班人员跟进与 SLA；本骨架演示的是「事件队列 + 处理留痕」，
          对话侧的 interrupt/HITL 在对话页完成。
        </p>

        {flags.length === 0 && <p className="muted">队列为空</p>}
        {flags.map((f) => (
          <div key={f.id} className="review-row">
            <div className="review-head">
              <span className="chip" style={{ color: RISK_META[f.level]?.color, background: RISK_META[f.level]?.bg }}>
                {RISK_META[f.level]?.label}
              </span>
              <b>{f.user_id}</b>
              <span className="muted small">来源 {f.source} · {f.ts?.slice(0, 16).replace('T', ' ')}</span>
              {f.cssrs_dimension && <span className="chip danger">{f.cssrs_dimension}</span>}
            </div>
            <div className="review-ev">“{f.evidence}”</div>
            <div className="row">
              <input
                placeholder="处理备注"
                value={note[f.id] || ''}
                onChange={(e) => setNote({ ...note, [f.id]: e.target.value })}
              />
              <button className="btn primary" onClick={() => handle(f.id)}>标记已处理</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
