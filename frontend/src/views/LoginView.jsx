import { useEffect, useState } from 'react'
import { api } from '../api.js'

/**
 * 登录 / 注册。
 *
 * 说明：账号体系是"演示级"的，用于区分不同用户的数据，
 * 不是生产级身份认证（详见 backend/auth.py 顶部的安全说明）。
 */
export default function LoginView({ onAuthed }) {
  const [mode, setMode] = useState('login') // login | register
  const [form, setForm] = useState({ username: '', password: '', display_name: '' })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [hint, setHint] = useState(null)

  useEffect(() => {
    api.authHint().then(setHint).catch(() => setHint(null))
  }, [])

  const submit = async (e) => {
    e?.preventDefault()
    setErr('')
    const username = form.username.trim()
    if (!username || !form.password) {
      setErr('请填写用户名和密码')
      return
    }
    setBusy(true)
    try {
      const r = mode === 'login'
        ? await api.login(username, form.password)
        : await api.register(username, form.password, form.display_name.trim())
      api.setSession(r.token, r.user_id, r.username)
      onAuthed?.({ token: r.token, user_id: r.user_id, username: r.username })
    } catch (e2) {
      setErr(String(e2.message || e2))
    } finally {
      setBusy(false)
    }
  }

  const useDemo = () => {
    setMode('login')
    setForm({ username: hint.demo.username, password: hint.demo.password, display_name: '' })
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <span className="logo">MT</span>
          <div>
            <h1>心迹 MindTrace</h1>
            <p className="muted">多模态心理陪伴 Agent</p>
          </div>
        </div>

        <div className="login-tabs">
          <span
            className={mode === 'login' ? 'on' : ''}
            onClick={() => { setMode('login'); setErr('') }}
          >登录</span>
          <span
            className={mode === 'register' ? 'on' : ''}
            onClick={() => { setMode('register'); setErr('') }}
          >注册</span>
        </div>

        <label className="login-field">
          <span>用户名</span>
          <input
            autoComplete="username"
            value={form.username}
            placeholder="字母、数字、下划线"
            onChange={(e) => setForm({ ...form, username: e.target.value })}
          />
        </label>

        <label className="login-field">
          <span>密码</span>
          <input
            type="password"
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            value={form.password}
            placeholder="至少 6 位"
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
        </label>

        {mode === 'register' && (
          <label className="login-field">
            <span>昵称（可选）</span>
            <input
              value={form.display_name}
              placeholder="留空则用用户名"
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            />
          </label>
        )}

        {err && <div className="login-err">{err}</div>}

        <button className="btn primary" disabled={busy}>
          {busy ? '处理中…' : mode === 'login' ? '登录' : '注册并进入'}
        </button>

        {mode === 'login' && hint?.demo && (
          <div className="login-demo">
            <div className="muted small">
              首次使用可用演示账号：
              <b> {hint.demo.username} / {hint.demo.password}</b>
            </div>
            <button type="button" className="btn ghost tiny" onClick={useDemo}>
              填入演示账号
            </button>
          </div>
        )}

        <p className="login-note muted small">
          本系统为技术演示，<b>不提供医疗诊断</b>。涉及自杀风险时请拨打 12356。
        </p>
      </form>
    </div>
  )
}
