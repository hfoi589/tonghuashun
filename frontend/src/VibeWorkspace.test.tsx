import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { VibeWorkspace } from './VibeWorkspace'

vi.mock('./market-api', () => ({
  marketApi: {
    session: async () => ({ id: 1, must_change_password: false }),
    bootstrapVibeWatchlist: async () => { throw new Error('Vibe 自选同步暂不可用，请重试。') },
  },
  marketAuthExpiredEvent: 'market-auth-expired',
  subscribeMarketWatchlistChanges: () => () => undefined,
}))
vi.mock('./MarketApp', () => ({ Login: () => null, PasswordChange: () => null }))
vi.mock('../../vibe_research/desktop/src/verticals/finance/lib/notes', () => ({ hydrateNotes: async () => undefined }))
vi.mock('../../vibe_research/desktop/src/verticals/finance/lib/watchlist', () => ({ hydrateWatch: async () => undefined }))
vi.mock('../../vibe_research/desktop/src/verticals/finance/lib/llmStore', () => ({ clearLegacyUserLlm: () => undefined }))
vi.mock('../../vibe_research/desktop/src/verticals/finance/router', async () => {
  const { createMemoryRouter } = await import('react-router-dom')
  const { useContext } = await import('react')
  const { MarketSessionContext } = await import('./market-session')

  function Workspace() {
    return <main>投研工作台 <p role="alert">{useContext(MarketSessionContext)?.watchSyncWarning}</p></main>
  }

  return { router: createMemoryRouter([{ path: '/', Component: Workspace }]) }
})

it('opens the workspace with a warning when watchlist synchronization fails', async () => {
  render(<VibeWorkspace />)

  expect(await screen.findByText('投研工作台', { exact: true })).toBeInTheDocument()
  expect(await screen.findByRole('alert')).toHaveTextContent('Vibe 自选同步暂不可用')
})
