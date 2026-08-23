import '@testing-library/jest-dom/vitest'

// Node 25 exposes an experimental global localStorage that is inert when the
// runner has no --localstorage-file. Keep the UI tests deterministic across
// Node versions by providing the small Web Storage surface the app uses.
let storage: Storage | undefined
try {
  storage = window.localStorage
} catch {
  storage = undefined
}
if (!storage || typeof storage.setItem !== 'function') {
  const values = new Map<string, string>()
  storage = {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key) },
    setItem: (key, value) => { values.set(String(key), String(value)) },
  }
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
}
