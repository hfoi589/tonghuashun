import { describe, expect, it } from 'vitest'
import stylesCss from './styles.css?raw'

describe('query page symbol suggestions', () => {
  it('keeps suggestions in flow and the submit button at its normal size', () => {
    expect(stylesCss).toMatch(/\.symbol-suggestion-list \{[^}]*position: static;/)
    expect(stylesCss).toMatch(/\.form-row:has\(\.symbol-suggestion-list\) \{[^}]*align-items: start;/)
  })

  it('keeps suggestion options from inheriting the query action button color', () => {
    const actionButtonRule = stylesCss.indexOf('.form-row button:not(:disabled):hover')
    const suggestionOptionRule = stylesCss.indexOf('.form-row button.symbol-suggestion-option {', actionButtonRule)

    expect(actionButtonRule).toBeGreaterThanOrEqual(0)
    expect(suggestionOptionRule).toBeGreaterThan(actionButtonRule)
  })

  it('keeps mobile suggestions readable with the active light page palette', () => {
    expect(stylesCss).toMatch(/\.symbol-suggestion-list \{[^}]*background: var\(--surface\);[^}]*color: var\(--text\);/)
  })
})
