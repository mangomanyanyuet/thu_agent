import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { RISK_META } from '../theme.js'

const LEVEL_NAMES = ['none', 'low', 'moderate', 'high', 'imminent']
const ROUTE_LABELS = { cbt: '常规会谈', moderate: '安全计划', crisis: '危机挂起' }

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj)
}

/** 不可变地写嵌套路径：表单每次改动都产生新对象，React 才会重渲染。 */
function setPath(obj, path, value) {
  const keys = path.split('.')
  const next = { ...obj }
  let cur = next
  for (let i = 0; i < keys.length - 1; i++) {
    cur[keys[i]] = { ...(cur[keys[i]] || {}) }
    cur = cur[keys[i]]
  }
  cur[keys[keys.length - 1]] = value
  return next
}

const clone = (o) => JSON.parse(JSON.stringify(o))

function LevelChip({ level }) {
  const name = LEVEL_NAMES[level] || 'none'
  const m = RISK_META[name] || RISK_META.none
  return <span className="chip" style={{ color: m.color, background: m.bg }}>{m.label}</span>
}

/** 单个标量字段：类型、范围、临床含义、是否改动全部由后端 PARAM_GROUPS 驱动。 */
function Field({ f, draft, baseline, onChange, disabled }) {
  const value = getPath(draft, f.path)
  const base = getPath(baseline, f.path)
  const changed = JSON.stringify(value) !== JSON.stringify(base)
  const label = (
    <div className="tune-label">
      <span>{f.label}</span>
      {changed && <span className="tune-dot" title="本次已修改" />}
      {f.help && <span className="muted small tune-help" title={f.help}>ⓘ</span>}
    </div>
  )

  let control
  if (f.type === 'enum') {
    control = (
      <select value={value ?? f.default} disabled={disabled}
        onChange={(e) => onChange(f.path, Number(e.target.value))}>
        {(f.options || []).map((o) => <option key={o.v} value={o.v}>{o.t}</option>)}
      </select>
    )
  } else if (f.type === 'bool') {
    control = (
      <label className="tune-bool">
        <input type="checkbox" checked={!!value} disabled={disabled}
          onChange={(e) => onChange(f.path, e.target.checked)} />
        {value ? '启用' : '关闭'}
      </label>
    )
  } else if (f.type === 'text') {
    control = (
      <textarea className="tune-text" value={value || ''} disabled={disabled}
        placeholder="例：青少年用「想消失」表达主动意念时按 moderate 起判"
        onChange={(e) => onChange(f.path, e.target.value)} />
    )
  } else {
    control = (
      <span className="tune-num">
        <input type="number" value={value ?? f.default} disabled={disabled}
          min={f.min} max={f.max} step={f.step || 1}
          onChange={(e) => onChange(f.path, e.target.value === '' ? '' : Number(e.target.value))} />
        {f.unit && <span className="muted small">{f.unit}</span>}
      </span>
    )
  }

  return (
    <div className={`tune-field ${changed ? 'changed' : ''}`}>
      {label}
      <div className="tune-control">
        {control}
        {f.type !== 'text' && f.min !== undefined && (
          <span className="muted small">默认 {f.default} · 范围 {f.min}~{f.max}</span>
        )}
      </div>
      {f.help && <div className="tune-desc muted small">{f.help}</div>}
    </div>
  )
}

/** 量表的「分值 → 等级」映射；3 分有下限护栏，界面上直接禁用低等级选项。 */
function MapFields({ fields, draft, baseline, onChange, disabled }) {
  return (
    <div className="tune-map">
      {fields.map((f) => {
        const v = getPath(draft, f.path)
        const b = getPath(baseline, f.path)
        const lo = f.locked_min ?? 0
        return (
          <div key={f.path} className={`tune-field ${v !== b ? 'changed' : ''}`}>
            <div className="tune-label"><span>{f.label}</span></div>
            <div className="tune-control">
              <select value={v ?? f.default} disabled={disabled}
                onChange={(e) => onChange(f.path, Number(e.target.value))}>
                {[0, 1, 2, 3, 4].map((n) => (
                  <option key={n} value={n} disabled={n < lo}>
                    {LEVEL_NAMES[n]}{n < lo ? '（系统锁定，不可降级）' : ''}
                  </option>
                ))}
              </select>
              {f.locked_min && <span className="muted small">🔒 满分至少判 {LEVEL_NAMES[lo]}</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/** 词表编辑器：增删改 + 「挪到更高等级桶」（收紧敏感度最直接的手段）。 */
function WordsEditor({ words, draft, onChange, disabled }) {
  const [adding, setAdding] = useState({})
  const buckets = words.map((w) => w.key)

  const add = (key) => {
    const kw = (adding[key] || '').trim()
    if (!kw) return
    const cur = getPath(draft, `patterns.${key}`) || []
    if (!cur.includes(kw)) onChange(`patterns.${key}`, [...cur, kw])
    setAdding({ ...adding, [key]: '' })
  }
  const remove = (key, kw) =>
    onChange(`patterns.${key}`, (getPath(draft, `patterns.${key}`) || []).filter((x) => x !== kw))
  const move = (from, to, kw) => {
    if (from === to) return
    const dest = getPath(draft, `patterns.${to}`) || []
    // 挪桶要同时改两个数组；必须基于同一份 draft 一次性合并提交，
    // 连续两次 onChange 会让后一次覆盖前一次（React 批处理 + 闭包旧值）。
    const step = setPath(draft, `patterns.${from}`,
      (getPath(draft, `patterns.${from}`) || []).filter((x) => x !== kw))
    const merged = setPath(step, `patterns.${to}`,
      dest.includes(kw) ? dest : [...dest, kw])
    onChange('__batch__', merged)
  }

  return (
    <div className="tune-words">
      {words.map((w) => {
        const list = getPath(draft, `patterns.${w.key}`) || []
        return (
          <div key={w.key} className="tune-bucket">
            <div className="row" style={{ marginTop: 0 }}>
              <b className="small">{w.label}</b>
              <span className="muted small">{list.length} 词</span>
              {w.locked_nonempty && <span className="chip" style={{ background: '#fef3c7', color: '#92400e' }}>不得删空</span>}
            </div>
            <div className="tune-chips">
              {list.length === 0 && <span className="muted small">（空）</span>}
              {list.map((kw) => (
                <span key={kw} className="tune-kw">
                  {kw}
                  {!disabled && (
                    <>
                      <select className="tune-move" title="移到其它等级"
                        value="" onChange={(e) => e.target.value && move(w.key, e.target.value, kw)}>
                        <option value="">⇄</option>
                        {buckets.filter((b) => b !== w.key).map((b) => (
                          <option key={b} value={b}>移到 {words.find((x) => x.key === b)?.label.split(' · ')[0]}</option>
                        ))}
                      </select>
                      <button className="tune-x" title="删除" onClick={() => remove(w.key, kw)}>×</button>
                    </>
                  )}
                </span>
              ))}
            </div>
            {!disabled && (
              <div className="row" style={{ marginTop: 4 }}>
                <input placeholder="新增关键词，回车或点添加" style={{ maxWidth: 260 }}
                  value={adding[w.key] || ''}
                  onChange={(e) => setAdding({ ...adding, [w.key]: e.target.value })}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(w.key) } }} />
                <button className="btn tiny ghost" onClick={() => add(w.key)}>添加</button>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function TuningView() {
  const [meta, setMeta] = useState(null)
  const [overview, setOverview] = useState(null)
  const [draft, setDraft] = useState(null)
  const [baseline, setBaseline] = useState(null)
  const [reason, setReason] = useState('')
  const [draftVersion, setDraftVersion] = useState('')
  // 首屏只展开「门槛 / 词表 / 量表」——专家抱怨告警频率时最常动的三组；
  // 多模态、模型、会话节奏默认折叠，避免 25 个字段一次铺满一屏
  const [collapsed, setCollapsed] = useState({ multimodal: true, assess: true, session: true })
  const [diff, setDiff] = useState(null)
  const [reg, setReg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    try {
      const [m, ov] = await Promise.all([api.riskRulesParams(), api.riskRulesOverview()])
      setMeta(m)
      setOverview(ov)
      setDraft(clone(ov.active_config))
      setBaseline(clone(ov.active_config))
    } catch (e) { setErr(String(e.message || e)) }
  }, [])

  useEffect(() => { load() }, [load])

  // 任何编辑都会让「已保存的草稿」与屏幕上的内容不再一致。
  // 必须同时清掉 draftVersion：否则「发布」按钮发的是**上一次保存的旧草稿**，
  // 专家以为发布了眼前的改动，实际线上跑的是另一份配置。
  const markDirty = () => {
    setDraftVersion('')
    setDiff(null)
    setReg(null)
  }

  const onChange = (path, value) => {
    // WordsEditor 的跨桶移动会一次性提交合并后的整份配置
    if (path === '__batch__') setDraft(value)
    else setDraft((d) => setPath(d, path, value))
    markDirty()
  }

  const loadPreset = (cfg, text) => {
    setDraft(clone(cfg))
    markDirty()
    setMsg(text)
  }

  const changeCount = useMemo(() => {
    if (!draft || !baseline) return 0
    const flat = (o, p = '', out = {}) => {
      Object.entries(o || {}).forEach(([k, v]) => {
        const q = p ? `${p}.${k}` : k
        if (v && typeof v === 'object' && !Array.isArray(v)) flat(v, q, out)
        else out[q] = JSON.stringify(v)
      })
      return out
    }
    const a = flat(baseline), b = flat(draft)
    return Object.keys({ ...a, ...b }).filter((k) => k !== 'version' && k !== 'note' && a[k] !== b[k]).length
  }, [draft, baseline])

  const guard = async (fn) => {
    setBusy(true); setErr(''); setMsg('')
    try { await fn() } catch (e) { setErr(String(e.message || e)) } finally { setBusy(false) }
  }

  const saveDraft = () => guard(async () => {
    const out = await api.riskRulesSaveVisual(draft, reason)
    setDraftVersion(out.version)
    setMsg(`已存为草稿 ${out.version}${out.warns?.length ? `（${out.warns.join('；')}）` : ''}`)
    // 只刷新版本历史：不要把正在编辑的 draft 覆盖回生效版本
    setOverview(await api.riskRulesOverview())
  })

  const showDiff = () => guard(async () => {
    setDiff(await api.riskRulesDiff({ config: draft }))
  })

  const regression = () => guard(async () => {
    setReg(await api.riskRulesRegression({ config: draft }))
  })

  const publish = (version) => guard(async () => {
    if (!reason.trim()) { setErr('发布必须填写变更理由（临床依据），会写入审计记录'); return }
    await api.riskRulesPublish(version, reason)
    setMsg(`已发布 ${version}，下一轮对话立即按新参数判定`)
    await load()
    setDraftVersion('')
    setDiff(null); setReg(null)
  })

  const rollback = (version) => guard(async () => {
    await api.riskRulesRollback(version, reason || '回滚到历史版本')
    setMsg(`已回滚到 ${version}`)
    await load(); setDiff(null); setReg(null)
  })

  if (!meta || !draft) {
    return <div className="card">{err ? <div className="voice-err">{err}</div> : '正在加载临床参数…'}</div>
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h3>临床参数配置</h3>
          <span className="chip" style={{ background: '#ecfdf5', color: '#16a34a' }}>
            生效版本 {overview?.active_version}
          </span>
          <span className="chip" style={{ background: '#eef2ff', color: '#4f46e5' }}>
            引擎已接入 · 发布即生效
          </span>
          {changeCount > 0 && <span className="chip" style={{ background: '#fef3c7', color: '#92400e' }}>本次改动 {changeCount} 项</span>}
        </div>
        <p className="muted small">
          专家在体验中发现「告警太频繁 / 不够敏感」时，直接在这里调门槛与阈值：
          改动先存草稿 → 一键回归看哪些句子会变级 → 确认后发布。
          <b>未发布的编辑不会影响线上对话</b>，发布后可一键回滚。
        </p>

        <div className="row" style={{ flexWrap: 'wrap', marginTop: 6 }}>
          <span className="muted small">以预设为起点：</span>
          {Object.entries(meta.presets || {}).map(([k, p]) => (
            <button key={k} className="btn tiny ghost" title={p.desc}
              onClick={() => loadPreset(p.config, `已载入「${p.label}」：${p.desc}。请检查后再存草稿`)}>
              {p.label}
            </button>
          ))}
          <button className="btn tiny ghost" disabled={changeCount === 0}
            onClick={() => loadPreset(baseline, '已还原为当前生效版本')}>
            还原
          </button>
        </div>
      </div>

      <div className="card">
        {meta.groups.map((g) => {
          const isCollapsed = !!collapsed[g.key]
          return (
            <section key={g.key} className="tune-group">
              <div className="tune-head" onClick={() => setCollapsed({ ...collapsed, [g.key]: !isCollapsed })}>
                <b>{isCollapsed ? '▷' : '▼'} {g.title}</b>
                <span className="muted small">{g.subtitle}</span>
              </div>
              {!isCollapsed && (
                <div className="tune-body">
                  {g.scope && <div className="tune-scope small">作用范围：{g.scope}</div>}
                  <div className="tune-grid">
                    {(g.fields || []).map((f) => (
                      <Field key={f.path} f={f} draft={draft} baseline={baseline}
                        onChange={onChange} disabled={busy} />
                    ))}
                  </div>
                  {g.map_fields && (
                    <MapFields fields={g.map_fields} draft={draft} baseline={baseline}
                      onChange={onChange} disabled={busy} />
                  )}
                  {g.words && (
                    <WordsEditor words={g.words} draft={draft} onChange={onChange} disabled={busy} />
                  )}
                </div>
              )}
            </section>
          )
        })}

        <section className="tune-group tune-locked">
          <div className="tune-head"><b>⛔ 系统锁定（不可配置）</b></div>
          <div className="tune-body">
            {(meta.locked || []).map((x) => <div key={x} className="muted small">🔒 {x}</div>)}
          </div>
        </section>
      </div>

      <div className="card">
        <div className="row">
          <input style={{ flex: 1 }} placeholder="变更理由（发布时必填，说明临床依据，写入审计）"
            value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        <div className="row" style={{ marginTop: 8, flexWrap: 'wrap' }}>
          <button className="btn primary" disabled={busy || changeCount === 0} onClick={saveDraft}>
            ⬆ 保存为草稿
          </button>
          <button className="btn" disabled={busy} onClick={showDiff}>查看变更对照</button>
          <button className="btn" disabled={busy} onClick={regression}>一键回归试跑</button>
          {draftVersion && (
            <button className="btn" disabled={busy} onClick={() => publish(draftVersion)}>
              发布草稿 {draftVersion.replace(/^.*-draft-/, '')} 生效
            </button>
          )}
          <button className="btn ghost" onClick={load}>重新载入</button>
        </div>
        {!draftVersion && changeCount > 0 && (
          <p className="muted small" style={{ marginTop: 6 }}>先「保存为草稿」，才能发布该版本。</p>
        )}
        {err && <div className="voice-err" style={{ marginTop: 8 }}>{err}</div>}
        {msg && <div className="muted small" style={{ marginTop: 8, color: '#16a34a' }}>{msg}</div>}
      </div>

      {diff && (
        <div className="card">
          <div className="row"><h3>变更对照</h3>
            <span className={`chip ${diff.valid ? '' : 'danger'}`}>
              {diff.valid ? '护栏校验通过' : '校验未通过'}
            </span>
          </div>
          {diff.errors?.length > 0 && <div className="voice-err">{diff.errors.join('；')}</div>}
          {diff.warns?.length > 0 && (
            <div className="tune-warn small">
              {diff.warns.map((w, i) => <div key={i}>⚠ {w}</div>)}
            </div>
          )}
          {diff.changes.length === 0
            ? <p className="muted">与当前生效版本一致，无改动。</p>
            : (
              <div className="tune-diff">
                {diff.changes.map((d) => (
                  <div key={d.path} className="row tune-diff-row">
                    <b className="small">{d.label}</b>
                    <span className="muted small">{String(d.old)}</span><span>→</span>
                    <b className="small">{String(d.new)}</b>
                    {d.added?.length > 0 && <span className="muted small">+{d.added.join('、')}</span>}
                    {d.removed?.length > 0 && <span className="chip danger">-{d.removed.join('、')}</span>}
                  </div>
                ))}
              </div>
            )}
        </div>
      )}

      {reg && (
        <div className="card">
          <div className="row">
            <h3>回归试跑</h3>
            <span className="muted small">生效版本 {reg.active_version} → 当前编辑</span>
            <span className="chip">{reg.changed} / {reg.rows.length} 句发生变化</span>
          </div>
          <div className="tune-reg">
            {reg.rows.map((r) => (
              <div key={r.text} className={`row tune-reg-row ${r.delta < 0 || (r.old.route === 'crisis' && r.new.route !== 'crisis') ? 'relaxed' : ''}`}>
                <span className="small" style={{ flex: 1 }}>“{r.text}”</span>
                <LevelChip level={r.old.level} />
                <span className="muted small">{ROUTE_LABELS[r.old.route]}</span>
                <span>→</span>
                <LevelChip level={r.new.level} />
                <span className="muted small">{ROUTE_LABELS[r.new.route]}</span>
                {r.delta !== 0 && <span className={`chip ${r.delta > 0 ? '' : 'danger'}`}>{r.delta > 0 ? '更敏感' : '放宽'}</span>}
                {r.delta === 0 && r.route_changed && <span className="chip">分流变化</span>}
              </div>
            ))}
          </div>
          <p className="muted small" style={{ marginTop: 6 }}>
            只跑确定性通道（规则 + 量表），不调用模型、不落库。「放宽」标红，发布前请确认临床可接受。
          </p>
        </div>
      )}

      {overview?.versions?.length > 0 && (
        <div className="card">
          <h3>版本历史与回滚</h3>
          <div className="stack" style={{ gap: 4 }}>
            {overview.versions.map((v) => (
              <div key={v.version} className="row tune-ver">
                <span className="chip" style={{
                  background: v.status === 'published' ? '#ecfdf5' : '#f3f4f6',
                  color: v.status === 'published' ? '#16a34a' : '#6b7280',
                }}>{v.status}</span>
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
    </div>
  )
}
