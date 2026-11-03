import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'
import type { MarketUser } from './market-api'

export interface MarketSessionContextValue {
  user: MarketUser
  logout: () => Promise<void>
  watchSyncWarning?: string | null
}

export const MarketSessionContext = createContext<MarketSessionContextValue | null>(null)

export function useMarketSession(): MarketSessionContextValue {
  const value = useContext(MarketSessionContext)
  if (!value) throw new Error('MarketSessionContext is not available')
  return value
}

export function WilsonOnly({ children }: { children: ReactNode }) {
  const { user } = useMarketSession()
  return user.role === 'admin' ? <>{children}</> : null
}
