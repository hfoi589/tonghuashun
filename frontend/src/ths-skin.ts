import { useCallback, useEffect, useState } from 'react'

export type ThsSkin = 'dark' | 'light'

export const THS_SKIN_STORAGE_KEY = 'ths_ui_skin'
export const VR_THEME_STORAGE_KEY = 'vr-theme'
const SKIN_CHANGE_EVENT = 'ths-skin-change'

function safeGetStorage(key: string): string | null {
  try {
    return typeof window !== 'undefined' && window.localStorage
      ? window.localStorage.getItem(key)
      : null
  } catch {
    return null
  }
}

function safeSetStorage(key: string, value: string): void {
  try {
    if (typeof window !== 'undefined' && window.localStorage) {
      window.localStorage.setItem(key, value)
    }
  } catch {
    // Ignore storage quota or private browsing errors.
  }
}

export function readThsSkin(): ThsSkin {
  const stored = safeGetStorage(THS_SKIN_STORAGE_KEY) ?? safeGetStorage(VR_THEME_STORAGE_KEY)
  return stored === 'light' ? 'light' : 'dark'
}

export function applyThsSkin(skin: ThsSkin, notify = false): void {
  if (typeof document !== 'undefined') {
    const root = document.documentElement
    root.dataset.thsSkin = skin
    root.classList.toggle('light', skin === 'light')
    root.classList.toggle('dark', skin === 'dark')
  }
  safeSetStorage(THS_SKIN_STORAGE_KEY, skin)
  safeSetStorage(VR_THEME_STORAGE_KEY, skin)
  if (notify && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent<ThsSkin>(SKIN_CHANGE_EVENT, { detail: skin }))
  }
}

export function useThsSkin(): [ThsSkin, () => void] {
  const [skin, setSkin] = useState<ThsSkin>(() => readThsSkin())

  useEffect(() => {
    applyThsSkin(skin, false)
  }, [skin])

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onSkinEvent = (event: Event) => {
      const next = (event as CustomEvent<ThsSkin>).detail
      if (next === 'dark' || next === 'light') {
        setSkin((current) => (current === next ? current : next))
      }
    }
    const onStorage = (event: StorageEvent) => {
      if (event.key === THS_SKIN_STORAGE_KEY || event.key === VR_THEME_STORAGE_KEY) {
        const next = readThsSkin()
        setSkin((current) => (current === next ? current : next))
      }
    }
    window.addEventListener(SKIN_CHANGE_EVENT, onSkinEvent)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(SKIN_CHANGE_EVENT, onSkinEvent)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  const toggleSkin = useCallback(() => {
    setSkin((current) => {
      const next: ThsSkin = current === 'dark' ? 'light' : 'dark'
      applyThsSkin(next, true)
      return next
    })
  }, [])

  return [skin, toggleSkin]
}
