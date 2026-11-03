import { useCallback, useEffect, useMemo, useState } from 'react'
import { RouterProvider } from 'react-router-dom'
import { hydrateNotes } from '../../vibe_research/desktop/src/verticals/finance/lib/notes'
import { hydrateWatch } from '../../vibe_research/desktop/src/verticals/finance/lib/watchlist'
import { setWorkspaceScope } from '../../vibe_research/desktop/src/core/ai/workspace-scope'
import { clearLegacyUserLlm } from '../../vibe_research/desktop/src/verticals/finance/lib/llmStore'
import { router } from '../../vibe_research/desktop/src/verticals/finance/router'
import { Login, PasswordChange } from './MarketApp'
import { MarketSessionContext } from './market-session'
import { marketApi, marketAuthExpiredEvent, notifyMarketWatchlistChanged, subscribeMarketWatchlistChanges, type MarketUser } from './market-api'
import '../../vibe_research/desktop/src/index.css'
import './theme-glass.css'

type GateState = 'loading' | 'anonymous' | 'authenticated' | 'unavailable'

export function VibeWorkspace() {
  const [user, setUser] = useState<MarketUser | null>(null)
  const [state, setState] = useState<GateState>('loading')
  const [readyUserId, setReadyUserId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [watchSyncWarning, setWatchSyncWarning] = useState<string | null>(null)

  const loadSession = useCallback(() => {
    setState('loading')
    setError('')
    void marketApi.session().then((current) => {
      setUser(current)
      setState('authenticated')
    }).catch((reason: unknown) => {
      setUser(null)
      if (reason instanceof Error && 'status' in reason && (reason as { status?: number }).status === 401) {
        setState('anonymous')
        return
      }
      setError(reason instanceof Error ? reason.message : '账号状态暂时不可用')
      setState('unavailable')
    })
  }, [])

  useEffect(() => {
    clearLegacyUserLlm()
    loadSession()
    const expire = () => loadSession()
    window.addEventListener(marketAuthExpiredEvent, expire)
    return () => window.removeEventListener(marketAuthExpiredEvent, expire)
  }, [loadSession])

  useEffect(() => {
    if (!user || user.must_change_password) return
    let cancelled = false
    setReadyUserId(null)
    setWatchSyncWarning(null)
    setWorkspaceScope(user.id)
    void marketApi.bootstrapVibeWatchlist().then((result) => {
      if (!cancelled) setWatchSyncWarning(result.warning)
      if (result.synced) notifyMarketWatchlistChanged()
      return Promise.all([hydrateNotes(), hydrateWatch()])
    }).then(() => {
      if (!cancelled) setReadyUserId(user.id)
    }).catch((reason: unknown) => {
      if (!cancelled) {
        setError(reason instanceof Error ? reason.message : '工作台数据暂时无法加载')
        setState('unavailable')
      }
    })
    return () => { cancelled = true }
  }, [user?.id, user?.must_change_password])

  useEffect(() => {
    if (!user || user.must_change_password || state !== 'authenticated') return
    return subscribeMarketWatchlistChanges(() => {
      void hydrateWatch().catch(() => undefined)
    })
  }, [state, user?.id, user?.must_change_password])

  const login = (current: MarketUser) => {
    setUser(current)
    setReadyUserId(null)
    setState('authenticated')
    setError('')
  }

  const logout = useCallback(async () => {
    try { await marketApi.logout() } catch { /* The local workspace still closes if the session expired. */ }
    setUser(null)
    setReadyUserId(null)
    setState('anonymous')
    window.dispatchEvent(new Event(marketAuthExpiredEvent))
  }, [])

  const session = useMemo(() => user ? { user, logout, watchSyncWarning } : null, [user, logout, watchSyncWarning])

  if (state === 'loading') return <main className="market-loading"><div className="market-brand-mark">顺</div><p>正在连接投研工作台…</p></main>
  if (state === 'unavailable') return <main className="market-auth-shell"><section className="market-auth-card"><div className="market-brand-mark">顺</div><p className="market-kicker">VIBE RESEARCH</p><h1>暂时无法打开工作台</h1><p>{error || '账号服务暂时不可用。'}</p><button type="button" onClick={loadSession}>重试</button></section></main>
  if (state === 'anonymous') return <Login onLogin={login} />
  if (user?.must_change_password) return <PasswordChange user={user} onDone={login} />
  if (!user || readyUserId !== user.id || !session) return <main className="market-loading"><div className="market-brand-mark">顺</div><p>正在加载账号工作区…</p></main>

  return <MarketSessionContext.Provider value={session}><RouterProvider router={router} /></MarketSessionContext.Provider>
}
