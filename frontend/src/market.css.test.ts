import { describe, expect, it } from 'vitest'
import marketCss from './market.css?raw'

describe('mobile market layout', () => {
  it('lets the mobile market header scroll with the page', () => {
    expect(marketCss).toMatch(/@media \(max-width: 880px\) \{[\s\S]*?\.market-topbar \{[^}]*position: static;/)
  })

  it('keeps the mobile fund flow sections compact', () => {
    expect(marketCss).toMatch(/@media \(max-width: 520px\) \{[\s\S]*?\.market-fund-layout \{[^}]*gap: 4px;/)
    expect(marketCss).toMatch(/@media \(max-width: 520px\) \{[\s\S]*?\.market-fund-column-head \{[^}]*min-height: 56px;/)
    expect(marketCss).toMatch(/@media \(max-width: 520px\) \{[\s\S]*?\.market-fund-tablist \{[^}]*min-height: 52px;/)
  })
})
