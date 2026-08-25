# 股票页签删除与名称联想搜索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为采集页增加当前浏览器单页签删除确认，以及基于同花顺 App 内部接口的股票名称联想、精确确认和提交流程。

**Architecture:** 后端复用现有 Frida `queryStockDataList()` 桥接，新增只返回受支持股票/基金候选的 `search_symbols()` 与公共候选接口；现有六位代码精确接口继续作为提交前唯一确认路径。前端将录入区改为可访问 combobox，并把页签拆成独立选择按钮和删除按钮，通过本地确认弹窗只修改当前浏览器状态。

**Tech Stack:** Python 3.12、FastAPI、Frida RPC、React、TypeScript、Vitest、pytest、OrbStack Docker Compose

**Spec:** `docs/superpowers/specs/2026-08-25-stock-tab-delete-symbol-search-design.md`

## Global Constraints

- 候选、精确确认和任务指标只能来自同花顺 App 内部接口；禁止使用 UI 文本、OCR 或截图补值。
- 模糊候选不能直接提交，选择后必须再次调用六位代码精确确认接口。
- 搜索只操作 `core_metrics / emulator-5556 / 27043`，不得导航或操作受保护的 `main_fund_flow / emulator-5554`。
- 页签删除只修改当前浏览器 `localStorage`，不得删除、取消或改变服务端任务。
- 保留现有任务唯一化、24 小时截图保留、永久任务元数据和点击页签自动更新行为。
- 不得提交或覆盖工作区中与本需求无关的 `MarketApp`、`.impeccable/`、`PRODUCT.md` 等用户改动。

---

### Task 1: App 内部股票候选查询

**Files:**
- Modify: `level2_service/parsed_values.py`
- Test: `tests/test_parsed_values.py`

**Interfaces:**
- Consumes: 现有 `_lookup_reader(endpoint, package, timeout_seconds, query)` 与 `_FRIDA_SYMBOL_LOOKUP_SCRIPT`。
- Produces: `FridaParsedValueSource.search_symbols(query: str, limit: int = 8) -> list[SymbolLookup]`；`DualAccountParsedValueSource.search_symbols(...)` 转发到核心数据源。

- [ ] **Step 1: 写候选过滤失败测试**

```python
def test_symbol_search_returns_supported_unique_app_candidates_in_app_order() -> None:
    source = FridaParsedValueSource(
        "127.0.0.1:27043",
        lookup_reader=lambda *_args: {
            "results": [
                {"stock_code": "688027", "stock_name": "国盾量子", "market_id": "17", "market_label": "科创", "securities_code": None},
                {"stock_code": "123456", "stock_name": "不支持市场", "market_id": "17", "market_label": "其他", "securities_code": None},
                {"stock_code": "688027", "stock_name": "国盾量子", "market_id": "17", "market_label": "科创", "securities_code": None},
                {"stock_code": "501018", "stock_name": "债券碰撞", "market_id": "35", "market_label": "债券", "securities_code": None},
                {"stock_code": "501018", "stock_name": "南方原油LOF", "market_id": "20", "market_label": "沪基", "securities_code": None},
            ]
        },
    )

    assert source.search_symbols("国盾", limit=8) == [
        SymbolLookup(symbol="688027", name="国盾量子", market="17", market_label="科创", securities_code=None),
        SymbolLookup(symbol="501018", name="南方原油LOF", market="20", market_label="沪基", securities_code=None),
    ]
```

- [ ] **Step 2: 写参数、限制和错误码测试**

```python
@pytest.mark.parametrize("query", ["", "国", "x" * 33])
def test_symbol_search_rejects_invalid_query_length(query: str) -> None:
    source = FridaParsedValueSource("127.0.0.1:27043", lookup_reader=lambda *_args: {"results": []})
    with pytest.raises(ValueError):
        source.search_symbols(query)

def test_symbol_search_limits_results_and_preserves_app_error_code() -> None:
    source = FridaParsedValueSource(
        "127.0.0.1:27043",
        lookup_reader=lambda *_args: {"error_code": "SYMBOL_LOOKUP_TIMEOUT", "error_message": "timeout"},
    )
    with pytest.raises(DirectRequestError, match="SYMBOL_LOOKUP_TIMEOUT"):
        source.search_symbols("科技", limit=3)
```

- [ ] **Step 3: 运行 RED 测试**

Run: `PYTHONPATH=. uv run pytest -q tests/test_parsed_values.py -k 'symbol_search'`

Expected: FAIL because `search_symbols` does not exist.

- [ ] **Step 4: 实现共享结果解析和候选查询**

```python
def search_symbols(self, query: str, limit: int = 8) -> list[SymbolLookup]:
    normalized = str(query).strip()
    if not 2 <= len(normalized) <= 32:
        raise ValueError("query must contain 2 to 32 characters")
    if not 1 <= limit <= 8:
        raise ValueError("limit must be between 1 and 8")
    payload = self._read_symbol_candidates(normalized)
    candidates: list[SymbolLookup] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.get("results", ()):
        stock_code = _text(item.get("stock_code")) if isinstance(item, dict) else None
        market = _text(item.get("market_id")) if isinstance(item, dict) else None
        name = _text(item.get("stock_name")) if isinstance(item, dict) else None
        if stock_code is None or market is None or name is None or not re.fullmatch(r"[0-9]{6}", stock_code):
            continue
        try:
            expected_market = market_code_for_symbol(stock_code)
        except UnsupportedMarketError:
            continue
        identity = (stock_code, market)
        if market != expected_market or identity in seen:
            continue
        seen.add(identity)
        candidates.append(SymbolLookup(stock_code, name, market, _text(item.get("market_label")), _text(item.get("securities_code"))))
        if len(candidates) == limit:
            break
    return candidates
```

Extract the current Frida lock/reader/error handling in `lookup_symbol()` into `_read_symbol_candidates(query)` so exact and fuzzy paths share the same App call without weakening exact filtering.

- [ ] **Step 5: 运行 GREEN 测试和现有精确查询回归**

Run: `PYTHONPATH=. uv run pytest -q tests/test_parsed_values.py -k 'symbol_lookup or symbol_search'`

Expected: PASS; existing exact lookup ambiguity and fund/bond collision tests remain green.

- [ ] **Step 6: 提交 Task 1**

```bash
git add -- level2_service/parsed_values.py tests/test_parsed_values.py
git commit -m "feat: add app-internal symbol suggestions"
```

### Task 2: 公共候选 API 与生产依赖注入

**Files:**
- Modify: `level2_service/api.py`
- Modify: `level2_service/main.py`
- Test: `tests/test_public_api.py`
- Test: `tests/test_deployment.py`

**Interfaces:**
- Consumes: `search_symbols(query, limit)` from Task 1。
- Produces: `GET /api/v1/symbols?query=<text>&limit=<1..8>` returning `list[SymbolLookupResponse]`。

- [ ] **Step 1: 写 API 成功、空结果和验证失败测试**

```python
def test_public_symbol_search_returns_app_candidates_without_confirming_them() -> None:
    app = create_app(
        symbol_lookup=FakeSymbolLookup(),
        symbol_search=lambda query, limit: [SymbolLookup("688027", "国盾量子", "17", "科创", None)],
    )
    response = TestClient(app).get("/api/v1/symbols", params={"query": "国盾", "limit": 8})
    assert response.status_code == 200
    assert response.json() == [{"symbol": "688027", "name": "国盾量子", "market": "17", "market_label": "科创"}]

@pytest.mark.parametrize("params", [{"query": "国"}, {"query": "科技", "limit": 9}])
def test_public_symbol_search_validates_query_and_limit(params) -> None:
    assert TestClient(create_app(symbol_search=lambda *_args: [])).get("/api/v1/symbols", params=params).status_code == 422
```

Also assert empty candidates return `200 []`, `DirectRequestError` returns `503`, and the verified symbol cache remains empty after fuzzy search.

- [ ] **Step 2: 运行 RED API 测试**

Run: `PYTHONPATH=. uv run pytest -q tests/test_public_api.py -k 'symbol_search'`

Expected: FAIL because `create_app` and the collection route do not accept symbol search.

- [ ] **Step 3: 实现请求模型和路由**

```python
class SymbolSearchResponse(BaseModel):
    symbol: str
    name: str
    market: str
    market_label: Optional[str]

@app.get("/api/v1/symbols", response_model=list[SymbolSearchResponse])
def search_public_symbols(query: str = Query(min_length=2, max_length=32), limit: int = Query(8, ge=1, le=8)):
    normalized = query.strip()
    if not 2 <= len(normalized) <= 32:
        raise HTTPException(status_code=422, detail="query must contain 2 to 32 characters")
    search = app.state.symbol_search
    if search is None:
        raise HTTPException(status_code=503, detail="symbol search temporarily unavailable")
    try:
        return [SymbolSearchResponse(...) for result in search(normalized, limit)]
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except DirectRequestError as error:
        raise HTTPException(status_code=503, detail="symbol search temporarily unavailable") from error
```

Add `symbol_search: Callable[[str, int], list[SymbolLookup]] | None` to `create_app()`, store it on `app.state`, and wire `parsed_value_source.search_symbols` in `create_production_app()`.

- [ ] **Step 4: 写并运行生产装配测试**

Extend `FakeParsedValueSource`/production factory assertions so `app.state.symbol_search("国盾", 8)` delegates to the core Frida source and never the fund account.

Run: `PYTHONPATH=. uv run pytest -q tests/test_public_api.py tests/test_deployment.py`

Expected: PASS.

- [ ] **Step 5: 提交 Task 2**

```bash
git add -- level2_service/api.py level2_service/main.py tests/test_public_api.py tests/test_deployment.py
git commit -m "feat: expose symbol suggestion API"
```

### Task 3: 可访问股票代码/名称录入框

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `api.searchSymbols(query, signal)` and existing `api.lookupSymbol(symbol, signal)`。
- Produces: 输入状态 `idle | suggesting | suggestions | no-results | suggestion-error | confirming | valid | invalid | unavailable`，最终提交仍只传六位 `symbol`。

- [ ] **Step 1: 写防抖和候选展示失败测试**

```tsx
it('debounces a name query and shows App-internal candidates', async () => {
  vi.useFakeTimers()
  vi.mocked(fetch).mockResolvedValueOnce(jsonResponse([
    { symbol: '688027', name: '国盾量子', market: '17', market_label: '科创' },
  ]))
  render(<App />)
  await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).type(screen.getByLabelText('股票代码或名称'), '国盾')
  await vi.advanceTimersByTimeAsync(299)
  expect(fetch).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1)
  expect(await screen.findByRole('option', { name: /国盾量子.*688027.*科创/ })).toBeInTheDocument()
})
```

- [ ] **Step 2: 写选择候选后精确确认和提交测试**

```tsx
it('exactly confirms a selected suggestion before submitting its six-digit code', async () => {
  vi.mocked(fetch)
    .mockResolvedValueOnce(jsonResponse([{ symbol: '688027', name: '国盾量子', market: '17', market_label: '科创' }]))
    .mockResolvedValueOnce(jsonResponse({ symbol: '688027', name: '国盾量子', market: '17' }))
    .mockResolvedValueOnce(jsonResponse({ ...task, symbol: '688027' }, 202))
  // type 国盾, choose option, wait for exact confirmation, submit
  expect(fetch).toHaveBeenNthCalledWith(2, '/api/v1/symbols/688027', expect.anything())
  expect(fetch).toHaveBeenNthCalledWith(3, '/api/v1/jobs', expect.objectContaining({ body: JSON.stringify({ symbol: '688027', include_long_capture: false }) }))
})
```

Add tests for ArrowUp/ArrowDown/Enter/Escape, no results, 503, stale request abortion, six-digit direct lookup, and IME composition suppression.

- [ ] **Step 3: 运行 RED 前端测试**

Run: `cd frontend && npm test -- --run src/App.test.tsx`

Expected: FAIL because the input strips non-digits and no combobox/search API exists.

- [ ] **Step 4: 实现 API 类型和候选请求**

```ts
export interface SymbolSuggestion extends SymbolLookup { market_label: string | null }

searchSymbols: (query: string, signal?: AbortSignal) => request<SymbolSuggestion[]>(
  (`/api/v1/symbols?query=${encodeURIComponent(query)}&limit=8`, { signal }),
```

- [ ] **Step 5: 实现 combobox 状态机**

Use a dedicated `SymbolEntry` component inside `App.tsx` with:

```tsx
<input
  role="combobox"
  aria-autocomplete="list"
  aria-expanded={suggestionsOpen}
  aria-controls="symbol-suggestion-list"
  aria-activedescendant={activeSuggestionId}
  onCompositionStart={() => setComposing(true)}
  onCompositionEnd={(event) => { setComposing(false); setEntry(event.currentTarget.value) }}
/>
```

The effect rules are exact: six digits call exact lookup immediately; non-numeric text of length 2–32 calls suggestions after 300ms; all other input clears requests and disables submit. Selecting an option sets the six-digit code and invokes exact lookup before marking the form valid.

- [ ] **Step 6: 实现下拉视觉和响应式行为**

Add one anchored suggestion surface under the input, with 44px minimum row height, highlighted keyboard row, name/code/market alignment, mobile-safe width, and visible loading/empty/error rows. Do not change the page's existing color system or introduce a second nested card layer.

- [ ] **Step 7: 运行 GREEN 前端测试和构建**

Run: `cd frontend && npm test -- --run src/App.test.tsx && npm run build`

Expected: PASS with the submitted body containing only the exact six-digit code.

- [ ] **Step 8: 提交 Task 3**

```bash
git add -- frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: add stock name suggestions"
```

### Task 4: 单股票页签删除确认

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: existing `StockTab[]`, `activePublicId`, `persistStockTabs()` and `persistActiveTab()`。
- Produces: `removeTab(publicId: string)` and an accessible confirmation dialog; no server API call。

- [ ] **Step 1: 写删除取消和确认失败测试**

```tsx
it('asks before deleting a browser-only stock tab', async () => {
  renderAppWithTwoStoredTabs()
  await user.click(screen.getByRole('button', { name: '删除 中国海油（600938）页签' }))
  expect(screen.getByRole('dialog', { name: '删除股票页签' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '取消' }))
  expect(screen.getByRole('tab', { name: /中国海油/ })).toBeInTheDocument()
})

it('deletes only local tab state and activates the right neighbor without refreshing it', async () => {
  renderAppWithThreeStoredTabs('middle')
  await user.click(screen.getByRole('button', { name: '删除 中间股票（600002）页签' }))
  await user.click(screen.getByRole('button', { name: '删除页签' }))
  expect(screen.queryByRole('tab', { name: /中间股票/ })).not.toBeInTheDocument()
  expect(screen.getByRole('tab', { name: /右侧股票/ })).toHaveAttribute('aria-selected', 'true')
  expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/retry'), expect.anything())
})
```

Also assert deleting a non-active tab preserves current content, deleting the final tab shows the empty state, and delete-button clicks do not call the tab selection handler.

- [ ] **Step 2: 运行 RED 删除测试**

Run: `cd frontend && npm test -- --run src/App.test.tsx -t 'delet'`

Expected: FAIL because tabs do not expose delete controls or dialog state.

- [ ] **Step 3: 重构页签 markup 并实现确认弹窗**

Render each item as a `.stock-tab-item` wrapper containing sibling controls:

```tsx
<div className="stock-tab-item">
  <button role="tab" className="stock-tab" onClick={() => onSelect(tab.public_id)}>...</button>
  <button
    type="button"
    className="stock-tab-delete"
    aria-label={`删除 ${tab.name}（${tab.symbol}）页签`}
    onClick={() => onRequestDelete(tab.public_id)}
  >×</button>
</div>
```

Keep dialog state in `App`, calculate the right-neighbor/left-neighbor replacement from the pre-delete array, persist both local storage keys, and never call `api.retryJob()` from deletion.

- [ ] **Step 4: 实现焦点和样式**

Use `role="dialog"`, `aria-modal="true"`, labelled title/body, Escape close, initial focus on “取消”, tab-cycle focus containment, and return focus to the originating delete button on cancel. Position the delete control at the tab's top-right without reducing the tab's touch target below 44px.

- [ ] **Step 5: 运行 GREEN 删除与完整前端测试**

Run: `cd frontend && npm test -- --run src/App.test.tsx && npm run build`

Expected: PASS; no delete test observes a `/api/v1/jobs/...` mutation.

- [ ] **Step 6: 提交 Task 4**

```bash
git add -- frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: add browser-only tab deletion"
```

### Task 5: 全量验证、浏览器验收与部署

**Files:**
- Modify: `README.md`
- Modify: `handoff.md`

**Interfaces:**
- Consumes: Tasks 1–4 completed behavior。
- Produces: deployed API/frontend with verified App-internal fuzzy search and browser-only tab deletion。

- [ ] **Step 1: 更新用户文档**

Add this behavior contract to both documents:

```markdown
- 采集输入框接受六位代码或至少两个字符的股票名称关键词；候选来自 core_metrics App 内部搜索，选择后仍需六位代码精确确认才能提交。
- 单个股票页签的删除只移除当前浏览器 localStorage 记录，不删除或取消服务端唯一任务。
```

- [ ] **Step 2: 运行完整自动验证**

Run in parallel:

```bash
PYTHONPATH=. uv run pytest -q
cd frontend && npm test -- --run
cd frontend && npm run build
git diff --check
```

Expected: all commands exit 0; known dependency deprecation warnings may remain unchanged.

- [ ] **Step 3: 桌面和移动浏览器验收**

At widths 1200×900 and 390×844 verify:

- entering `国盾` shows `国盾量子 · 688027 · 科创`;
- keyboard Enter selects it and exact confirmation enables submit;
- dropdown does not overflow the submit panel;
- delete control remains visible on compact tabs;
- confirm dialog fits mobile width and returns focus correctly;
- cancel keeps the tab, confirm removes only local storage.

- [ ] **Step 4: 标准部署**

```bash
docker --context orbstack compose --env-file .env --env-file deploy/macos.env -f deploy/compose.yml up -d --build
```

Preserve Redis, capture, Android emulator and login-state volumes.

- [ ] **Step 5: 部署后接口与真实 App 验证**

```bash
curl -fsS 'http://127.0.0.1:8001/api/v1/symbols?query=%E5%9B%BD%E7%9B%BE&limit=8'
curl -fsS 'http://127.0.0.1:8001/api/v1/symbols/688027'
docker --context orbstack inspect -f '{{.State.Health.Status}}' ths-level2-api-1
```

Expected: first response contains `国盾量子/688027/17/科创`, second returns the unique exact stock, and health is `healthy`.

- [ ] **Step 6: 验证页签删除不影响 Redis**

Record `SCARD ths:jobs:tasks`, delete one tab in the browser, and assert the Redis count is unchanged while local storage removes only that tab.

- [ ] **Step 7: 提交文档和验收修正**

```bash
git add -- README.md handoff.md
git commit -m "docs: describe symbol suggestions and tab deletion"
```
