import { describe, expect, it } from 'vitest'
import marketCss from './market.css?raw'

describe('mobile market layout', () => {
  it('lets the mobile market header scroll with the page', () => {
    expect(marketCss).toMatch(/@media \(max-width: 880px\) \{[\s\S]*?\.market-topbar \{[^}]*position: static;/)
  })

  it('keeps the Market suggestions in document flow and gives options their own layout', () => {
    expect(marketCss).toMatch(/\.market-topbar \{[^}]*height: auto;[^}]*min-height: 62px;/)
    expect(marketCss).toMatch(/\.market-symbol-suggestion-list \{[^}]*position: static;/)
    expect(marketCss).toMatch(/\.market-search button\.market-symbol-suggestion-option \{[^}]*width: 100%;/)
  })

  it('keeps the mobile fund flow sections compact', () => {
    expect(marketCss).toMatch(/@media \(max-width: 520px\) \{[\s\S]*?\.market-fund-layout \{[^}]*gap: 4px;/)
    expect(marketCss).toMatch(/@media \(max-width: 520px\) \{[\s\S]*?\.market-fund-column-head \{[^}]*min-height: 56px;/)
    expect(marketCss).toMatch(/@media \(max-width: 520px\) \{[\s\S]*?\.market-fund-tablist \{[^}]*min-height: 52px;/)
  })
})
