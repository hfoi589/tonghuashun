import { describe, expect, it } from 'vitest'
import themeCss from './theme-glass.css?raw'

describe('aurora glass theme', () => {
  it('gives the console containers a real frosted-glass surface', () => {
    expect(themeCss).toMatch(/\.theme-console \.panel,[\s\S]*?\{[^}]*backdrop-filter: var\(--ag-blur\);/)
    expect(themeCss).toMatch(/--ag-blur: blur\(\d+px\) saturate/)
  })

  it('keeps market suggestions floating on desktop instead of stretching the sticky top bar', () => {
    expect(themeCss).toMatch(/@media \(min-width: 881px\) \{\s*html\.theme-market \.market-symbol-suggestion-list \{[^}]*position: absolute;/)
  })

  it('degrades gracefully without blur support or with reduced transparency', () => {
    expect(themeCss).toContain('@supports not ((backdrop-filter: blur(1px))')
    expect(themeCss).toContain('@media (prefers-reduced-transparency: reduce)')
  })

  it('scopes market dark skin so the light workspace theme keeps its own palette', () => {
    expect(themeCss).not.toMatch(/^\.theme-market /m)
  })
})
