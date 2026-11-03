import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { API_MODELS, PROVIDER_BASE, SUBSCRIPTION_MODELS, apiPresetForSaved, providerOfModel } from '@/lib/ai-models'
import type { LlmConfig } from '@/lib/llmStore'
import { api, readCsrfToken, type AdminVibeSettings } from './api'

type Mode = 'api' | 'subscription'

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '设置请求失败'
}

function runtimeName(provider: string | null): string {
  if (!provider) return '未配置'
  return SUBSCRIPTION_MODELS.find((item) => item.provider === provider)?.name
    ?? API_MODELS.find((item) => item.provider === provider)?.name
    ?? provider
}

export function VibeAdminSettings({ active }: { active: boolean }) {
  const [settings, setSettings] = useState<AdminVibeSettings | null>(null)
  const [mode, setMode] = useState<Mode>('api')
  const [apiPreset, setApiPreset] = useState(API_MODELS[0]!.id)
  const [cliProvider, setCliProvider] = useState<string>(SUBSCRIPTION_MODELS[0]!.provider)
  const [baseURL, setBaseURL] = useState(PROVIDER_BASE[API_MODELS[0]!.provider] ?? '')
  const [model, setModel] = useState(API_MODELS[0]!.id)
  const [apiKey, setApiKey] = useState('')
  const [secContact, setSecContact] = useState('')
  const [secContactDirty, setSecContactDirty] = useState(false)
  const [codebuddyPath, setCodebuddyPath] = useState('')
  const [codebuddyPathDirty, setCodebuddyPathDirty] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const next = await api.vibeSettings()
      setSettings(next)
      if (next.ai.configured && next.ai.provider) {
        if (next.ai.subscription) {
          setMode('subscription')
          setCliProvider(next.ai.provider)
        } else {
          setMode('api')
          const preset = apiPresetForSaved({ provider: next.ai.provider, model: next.ai.model ?? '' })
          setApiPreset(preset)
          setBaseURL(next.ai.baseURL ?? PROVIDER_BASE[providerOfModel(preset)] ?? '')
          setModel(next.ai.model ?? preset)
        }
      }
      setSecContact('')
      setSecContactDirty(false)
      setCodebuddyPath('')
      setCodebuddyPathDirty(false)
    } catch (reason) {
      setError(errorText(reason))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (active) void load()
  }, [active])

  const selectedApi = useMemo(() => API_MODELS.find((item) => item.id === apiPreset) ?? API_MODELS[0]!, [apiPreset])
  const selectedCli = useMemo(() => SUBSCRIPTION_MODELS.find((item) => item.provider === cliProvider) ?? SUBSCRIPTION_MODELS[0]!, [cliProvider])
  const candidate: LlmConfig = mode === 'subscription'
    ? { provider: selectedCli.provider, baseURL: '', apiKey: '', model: selectedCli.id }
    : { provider: selectedApi.provider, baseURL: baseURL.trim(), apiKey: apiKey.trim(), model: model.trim() }

  async function testAndSave(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setMessage('正在连接测试并保存…')
    try {
      const csrf = readCsrfToken()
      const replacement = await api.saveVibeSettings({ llm: candidate }, csrf)
      setApiKey('')
      setMessage(`连接测试成功。当前全站模型：${replacement.ai.model || runtimeName(replacement.ai.provider)}。`)
      await load()
    } catch (reason) {
      setMessage('')
      setError(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  async function clearAi() {
    if (!window.confirm('清除全站 AI 配置？各账号的工作区和研究记录会保留。')) return
    setBusy(true)
    setError('')
    try {
      await api.clearVibeSettings(readCsrfToken())
      setApiKey('')
      setMessage('已清除全站 AI 配置。')
      await load()
    } catch (reason) { setError(errorText(reason)) }
    finally { setBusy(false) }
  }

  async function runUpdateAction(action: 'disable' | 'enable' | 'check') {
    if (!settings) return
    setBusy(true)
    setError('')
    try {
      const update = await api.updateVibeSettings(action, undefined, readCsrfToken())
      setSettings({ ...settings, update })
      setMessage(action === 'check' ? '已完成版本检查。' : action === 'enable' ? '已开启版本提示。' : '已关闭版本提示。')
    } catch (reason) { setError(errorText(reason)) }
    finally { setBusy(false) }
  }

  if (!active) return null
  return <div className="vibe-admin-settings">
    <section className="admin-controls"><div><h2>Vibe-Research AI 设置</h2><p>此配置对所有已登录的行情账号生效。密钥只在保存或连接测试时提交，读取时只返回配置状态。</p></div><button className="secondary" type="button" onClick={() => void load()} disabled={loading || busy}>刷新状态</button></section>
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="success-message" role="status">{message}</p>}

    <section className="admin-controls">
      <div><h3>模型来源</h3><p>{settings?.ai.configured ? `当前：${runtimeName(settings.ai.provider)} · ${settings.ai.model ?? ''}` : '尚未配置全站模型。'}</p></div>
      <form className="vibe-admin-ai-form" onSubmit={testAndSave}>
        <div className="admin-ops-grid two">
          <label>接入方式<select value={mode} onChange={(event) => setMode(event.target.value as Mode)}><option value="api">模型 API</option><option value="subscription">本机 CLI 订阅</option></select></label>
          {mode === 'subscription' ? <label>本机订阅<select value={cliProvider} onChange={(event) => setCliProvider(event.target.value)}>{SUBSCRIPTION_MODELS.map((item) => <option value={item.provider} key={item.provider}>{item.name}</option>)}</select></label> : <label>模型来源<select value={apiPreset} onChange={(event) => { const next = API_MODELS.find((item) => item.id === event.target.value) ?? API_MODELS[0]!; setApiPreset(next.id); setBaseURL(PROVIDER_BASE[next.provider] ?? ''); setModel(next.id) }}>{API_MODELS.map((item) => <option value={item.id} key={item.id}>{item.name} — {item.description}</option>)}</select></label>}
          {mode === 'api' && <>
            <label>Base URL<input value={baseURL} onChange={(event) => setBaseURL(event.target.value)} placeholder="https://api.example.com/v1" autoComplete="url" /></label>
            <label>Model<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="模型名称" autoComplete="off" /></label>
            <label>API Key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={settings?.ai.key_present ? '已保存；留空沿用当前密钥' : '输入 API Key'} autoComplete="new-password" /></label>
          </>}
        </div>
        {mode === 'subscription' && <div className="vibe-cli-status"><strong>本机 CLI 状态</strong>{settings?.agents.map((agent) => <p key={agent.provider}>{runtimeName(agent.provider)}：{agent.status ?? (agent.available ? '可用' : '不可用')}{agent.detail ? ` · ${agent.detail}` : ''}</p>)}</div>}
        <div className="button-row"><button type="submit" disabled={busy || loading}>{busy ? '正在处理…' : '测试并保存为全站默认'}</button>{settings?.ai.configured && <button type="button" className="secondary" onClick={() => void clearAi()} disabled={busy}>清除 AI 配置</button>}</div>
      </form>
    </section>

    <section className="admin-controls">
      <div><h3>SEC 联系方式</h3><p>访问 SEC 数据时使用的英文姓名与邮箱。读取时只显示遮蔽结果。</p>{settings?.sec_contact?.masked && <p className="minor">当前：{settings.sec_contact.masked}（{settings.sec_contact.source === 'env' ? '由服务环境配置' : '已保存'}）</p>}</div>
      <form className="vibe-admin-inline-form" onSubmit={async (event) => { event.preventDefault(); setBusy(true); setError(''); try { const result = await api.saveVibeSecContact(secContact.trim() || null, readCsrfToken()); setSettings((current) => current ? { ...current, sec_contact: result } : current); setSecContact(''); setSecContactDirty(false); setMessage('SEC 联系方式已保存。') } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) } }}>
        <label>姓名 邮箱<input value={secContact} onChange={(event) => { setSecContact(event.target.value); setSecContactDirty(true) }} placeholder="Zhang San name@example.com" disabled={settings?.sec_contact?.source === 'env'} /></label>
        <button className="secondary" type="submit" disabled={busy || settings?.sec_contact?.source === 'env'}>保存联系方式</button>
      </form>
    </section>

    <section className="admin-controls">
      <div><h3>WorkBuddy / CodeBuddy 路径</h3><p>仅在主机自动检测不到安装位置时填写。</p></div>
      <form className="vibe-admin-inline-form" onSubmit={async (event) => { event.preventDefault(); setBusy(true); setError(''); try { await api.saveVibeCodeBuddyPath(codebuddyPath.trim() || null, readCsrfToken()); setMessage('WorkBuddy 路径已保存。'); setCodebuddyPathDirty(false); await load() } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) } }}>
        <label>应用目录<input value={codebuddyPath} onChange={(event) => { setCodebuddyPath(event.target.value); setCodebuddyPathDirty(true) }} placeholder="可留空以自动检测" /></label>
        <button className="secondary" type="submit" disabled={busy}>保存路径</button>
        <button className="secondary" type="button" onClick={async () => { setBusy(true); setError(''); try { await api.saveVibeCodeBuddyPath(null, readCsrfToken()); setCodebuddyPath(''); setCodebuddyPathDirty(false); await load() } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) } }} disabled={busy}>恢复自动检测</button>
      </form>
    </section>

    <section className="admin-controls">
      <div><h3>新版提示</h3><p>当前 v{settings?.update.current ?? '—'}。{settings?.update.error ?? (settings?.update.update_available ? `发现新版本 v${settings.update.latest?.version}` : settings?.update.checked_at ? '已是最新版本或已忽略可选更新。' : '尚未检查。')}</p></div>
      <div className="button-row"><label className="vibe-update-toggle"><input type="checkbox" checked={Boolean(settings?.update.enabled && !settings.update.env_disabled)} disabled={busy || settings?.update.env_disabled} onChange={(event) => void runUpdateAction(event.target.checked ? 'enable' : 'disable')} /> 有新版时提示</label><button className="secondary" type="button" onClick={() => void runUpdateAction('check')} disabled={busy || !settings?.update.enabled || settings.update.env_disabled}>现在检查</button></div>
    </section>
  </div>
}
