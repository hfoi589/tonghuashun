# Query Page Overrides

> **PROJECT:** Stock Analytics
> **Generated:** 2026-09-10 16:55:22
> **Page Type:** Dashboard / Data View

> ⚠️ **IMPORTANT:** Rules in this file **override** the Master file (`design-system/MASTER.md`).
> Only deviations from the Master are documented here. For all other rules, refer to the Master.

---

## Page-Specific Rules

### Layout Overrides

- **Max Width:** 1200px (standard)
- **Layout:** Full-width sections, centered content

### Spacing Overrides

- No overrides — use Master spacing

### Typography Overrides

- No overrides — use Master typography

### Color Overrides

- Background: `#F6F9FC` with pale blue/violet ambient light.
- Surface: `rgba(255,255,255,.72)` with `backdrop-filter: blur(16px)`.
- UI primary: `#DC2626`; hover/pressed: `#B42318`.
- Preserve green for success/online and source-defined market data.

### Component Overrides

- Avoid: Default keyboard for all inputs
- Avoid: Desktop-first causing mobile issues
- Avoid: Enable by default everywhere

---

## Page-Specific Components

- No unique components for this page

---

## Recommendations

- Effects: Backdrop blur (10-20px), translucent white cards, subtle cool border, restrained shadow and red focus/active states.
- Forms: Use inputmode attribute
- Responsive: Start with mobile styles then add breakpoints
- Touch: Disable where not needed
