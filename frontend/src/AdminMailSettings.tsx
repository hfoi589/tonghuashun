import { useEffect, useState, type FormEvent } from 'react'
import { api, readCsrfToken, type AdminMailConfig } from './api'

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求未完成'
}

export function AdminMailSettings({ active }: { active: boolean }) {
  const [config, setConfig] = useState<AdminMailConfig | null>(null)
  const [host, setHost] = useState('')
  const [port, setPort] = useState('465')
  const [ssl, setSsl] = useState(true)
  const [registration, setRegistration] = useState(false)
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [sender, setSender] = useState('')
  const [testTo, setTestTo] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  function apply(next: AdminMailConfig) {
    setConfig(next)
    setHost(next.host)
    setPort(String(next.port))
    setSsl(next.ssl)
    setRegistration(next.registration_enabled)
    setUser(next.user)
    setSender(next.sender)
    setPassword('')
  }

  useEffect(() => {
    if (!active || config) return
    api.mailConfig().then(apply).catch((reason) => setError(errorText(reason)))
  }, [active, config])

  const payload = () => ({
    host: host.trim(), port: Number(port), ssl, user: user.trim(), password, sender: sender.trim(), registration_enabled: registration,
  })

  async function save(event: FormEvent) {
    event.preventDefault()
    setBusy(true); setError(''); setMessage('')
    try { apply(await api.saveMailConfig(payload(), readCsrfToken())); setMessage('邮件配置已保存。') }
    catch (reason) { setError(errorText(reason)) }
    finally { setBusy(false) }
  }

  async function sendTest() {
    setBusy(true); setError(''); setMessage('正在保存并发送测试邮件…')
    try {
      apply(await api.saveMailConfig(payload(), readCsrfToken()))
      await api.testMail(testTo.trim(), readCsrfToken())
      setMessage(`测试邮件已发送至 ${testTo.trim()}，请到收件箱确认。`)
    } catch (reason) { setMessage(''); setError(errorText(reason)) }
    finally { setBusy(false) }
  }

  return <section className="admin-controls admin-mail-settings">
    <div><h2>邮件与注册</h2><p>用于用户注册和找回密码的验证码邮件。密码只在保存时提交，读取时只显示是否已配置。</p></div>
    <form onSubmit={save}>
      <div className="admin-ops-grid two">
        <label>SMTP Host<input value={host} onChange={(event) => setHost(event.target.value)} autoComplete="off" placeholder="smtp.exmail.qq.com" /></label>
        <label>SMTP 端口<input type="number" min="1" max="65535" value={port} onChange={(event) => setPort(event.target.value)} /></label>
        <label><input type="checkbox" checked={ssl} onChange={(event) => setSsl(event.target.checked)} />SSL/TLS（465 端口勾选；587 请取消并使用 STARTTLS）</label>
        <label><input type="checkbox" checked={registration} onChange={(event) => setRegistration(event.target.checked)} />允许邮箱注册</label>
        <label>SMTP 用户<input value={user} onChange={(event) => setUser(event.target.value)} autoComplete="off" /></label>
        <label>SMTP 密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" placeholder={config?.password_configured ? '已配置，留空保持不变' : '输入密码或授权码'} /></label>
        <label>发件人<input value={sender} onChange={(event) => setSender(event.target.value)} autoComplete="off" placeholder="name@example.com" /></label>
      </div>
      <div className="button-row">
        <button type="submit" disabled={busy}>保存系统配置</button>
        <input type="email" value={testTo} onChange={(event) => setTestTo(event.target.value)} placeholder="测试收件邮箱" aria-label="测试收件邮箱" autoComplete="off" />
        <button type="button" className="secondary" onClick={() => void sendTest()} disabled={busy || !testTo.trim()}>发送测试邮件</button>
      </div>
      {config && <p className="minor">状态：{config.ready ? '配置完整' : '配置未完成'} · 注册{config.registration_enabled && config.ready ? '已开放' : '未开放'}</p>}
    </form>
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
  </section>
}
