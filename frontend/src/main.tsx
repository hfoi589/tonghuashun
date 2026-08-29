import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'

const App = lazy(() => import('./App'))
const MarketApp = lazy(() => import('./MarketApp').then((module) => ({ default: module.MarketApp })))

const Page = window.location.pathname.startsWith('/market') ? MarketApp : App
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
