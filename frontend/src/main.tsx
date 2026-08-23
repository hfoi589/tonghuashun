import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { MarketApp } from './MarketApp'

const Page = window.location.pathname.startsWith('/market') ? MarketApp : App
createRoot(document.getElementById('root')!).render(<StrictMode><Page /></StrictMode>)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => { void navigator.serviceWorker.register('/market-sw.js') })
}
