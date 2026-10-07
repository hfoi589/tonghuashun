# Market 资金图表页签与日期联动 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Market 页资金区域改为“左侧主力流向表 + 右侧当天/近30日资金曲线页签”，并让近30日选中交易日驱动左侧三周期快照。

**Architecture:** 保留现有 `FundFlowHistoryPanel` 的日期/周期/轮询逻辑与 `FundFlowDailyChart` 的 `periods` 数据契约。由 `FundFlow` 持有右侧页签状态和 30 日覆盖值；当天页签清除覆盖值，30 日图通过选中点回调写入覆盖值，表格统一复用现有单位、方向和缺失值格式化。

**Tech Stack:** React + TypeScript + Vitest + Testing Library + CSS。

**Spec:** 用户请求“Market 资金图表页签与日期联动”。

## Global Constraints

- 不新增市场数据源或 App 协议，复用现有 `fund-flow/history`、`fund-flow/daily` 和 `periods` 字段。
- 不使用 OCR、UI 文本或截图填充资金指标。
- 主力流向表统一显示为万元；缺失值显示为空值占位符；正负方向沿用现有颜色规则。
- 桌面端保持资金区域两列布局，移动端保持单列且不依赖横向滚动。

## Review Focus

- 30 日基准零点没有 `periods` 快照时，表格必须为空而不是沿用实时值；测试归入 Task 1。
- 从 30 日页签切回当天页签时，30 日覆盖值必须清除；测试归入 Task 1。
- 30 日图鼠标、键盘左右和数据刷新后的默认选中点都必须触发同一回调；测试归入 Task 2。
- 历史日期选择和轮询刷新不能因页签重组而丢失；测试归入 Task 1。
- 移动端页签、表格和曲线必须保持可见宽度；测试归入 Task 3 的构建/样式检查。

### Task 1: Market 资金页签与表格覆盖状态

**Files:**
- Modify: `frontend/src/MarketApp.tsx`
- Test: `frontend/src/MarketApp.test.tsx`

**Interfaces:**
- Consumes: `FundFlowDailyPoint.periods` and `FundFlowHistoryPanel`'s `onHistoryChange`.
- Produces: `FundFlowChartTab`, tab buttons labelled `当天资金曲线` and `近30日资金流向`, and a table override that is `undefined` on the当天 tab and `null` when the selected 30-day point has no snapshot.

- [ ] Write failing tests for the two tabs, 30-day point table replacement, zero-point blank state, and restoring current/history values after returning to当天.
- [ ] Run the focused tests and confirm they fail because the Market tab container and override wiring do not exist.
- [ ] Implement the smallest `FundFlow` state machine and render only the active right-side panel.
- [ ] Run the focused tests and confirm the new behavior passes without changing the existing history selector/period controls.

### Task 2: Daily chart selection callback and refresh behavior

**Files:**
- Modify: `frontend/src/FundFlowDailyChart.tsx`
- Test: `frontend/src/FundFlowHistoryChart.test.tsx`

**Interfaces:**
- Consumes: `FundFlowDailyPoint` and the existing `periods` snapshot shape.
- Produces: `FundFlowDailyPanel` prop `onSelectedPointChange?: (point: FundFlowDailyPoint) => void`, invoked with the raw selected point after pointer, keyboard, and selected-index refresh changes.

- [ ] Write failing tests proving the callback receives the raw point and its periods snapshot on initial selection and keyboard movement, and receives `periods: null` for a baseline zero point.
- [ ] Run the focused chart tests and confirm the callback assertions fail before wiring the panel callback.
- [ ] Thread the callback through `FundFlowDailyPanel` into `FundFlowDailyChart`, preserving the existing chart readout and auto-reset behavior.
- [ ] Run the focused chart tests and confirm all callback cases pass.

### Task 3: Market-specific responsive styles and verification

**Files:**
- Modify: `frontend/src/market.css`
- Test/verification: `frontend/src/MarketApp.test.tsx`, `frontend/src/FundFlowHistoryChart.test.tsx`, frontend build.

**Interfaces:**
- Consumes: the tab/tabpanel class names emitted by `MarketApp`.
- Produces: two-column desktop layout, single-column mobile layout, visible focus/active states, and no nested card borders inside the fund-flow sections.

- [ ] Add focused styles for the Market tablist and tabpanel, plus responsive overrides inside the existing mobile media query.
- [ ] Run the full frontend test command and build; record any pre-existing dependency/test-runner blocker separately from feature failures.
- [ ] Run the required 8001 deployment command with OrbStack, preserving Redis and emulator volumes.
