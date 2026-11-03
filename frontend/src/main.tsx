import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'

const App = lazy(() => import('./App'))
const VibeWorkspace = lazy(() => import('./VibeWorkspace').then((module) => ({ default: module.VibeWorkspace })))

const isMarketPage = window.location.pathname.startsWith('/market')
const Page = isMarketPage ? VibeWorkspace : App
document.documentElement.classList.add(isMarketPage ? 'theme-market' : 'theme-console')
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Suspense fallback={<main className="page-shell" aria-busy="true">正在加载页面…</main>}>
      <Page />
    </Suspense>
  </StrictMode>,
)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => { void navigator.serviceWorker.register('/market-sw.js') })
}
