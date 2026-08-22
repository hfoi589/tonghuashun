# 项目规则

## 市场数据采集

- 任务指标只能通过同花顺 App 内部接口 `FridaParsedValueSource.read_direct()` 获取。
- 严禁使用截图 OCR、UI 文本提取或长截图解析结果填写任何任务指标。
- 接口请求失败时，必须以原始接口错误码结束任务；不得打开股票搜索页作为降级路径。
- 接口只返回部分指标时，缺失指标必须保持为空并返回部分任务；不得使用 OCR 补值。
- 长截图开关只控制是否生成长截图，绝不能改变指标查询方式。
- OCR 只能用于长截图的非数据结构校验，例如确认必需的图表标题是否存在。

### 对外 API 调用契约

对外 API 有两个独立的市场数据调用：先确认股票代码和名称，再提交异步
数据任务。名称查询响应不是行情数据；生产环境提供 App 查询时，不得提交
未经确认的代码。

#### 1. 股票代码和名称查询

接口：

```http
GET /api/v1/symbols/{symbol}
```

调用时必须使用六位数字，例如：

```sh
curl -fsS "http://127.0.0.1:8000/api/v1/symbols/601872"
```

服务只接受已确认的股票和当前交易所基金代码前缀：

- `600/601/603/605/688/689` → 市场 `17`（上海）
- `000/001/002/003/300/301` → 市场 `33`（深圳）
- `920` → 市场 `151`（北京）
- `501/502/506/508/510/511/512/513/515/516/517/518/519/520/526/530/551/560/561/562/563/588/589`
  → 市场 `20`（沪基）
- `158/159/160/161/162/163/164/165/166/167/168/169/180`
  → 市场 `36`（深基）

生产实现通过 `FridaParsedValueSource.lookup_symbol()` 调用已运行的同花顺
App 内部精确搜索桥接。返回结果必须同时满足代码完全一致、市场一致、名称
非空，并且只能有一个精确结果。不得使用截图 OCR、UI 文本提取或模糊搜索
结果。同一代码同时返回市场 `19/35` 的债券和市场 `20/36` 的基金时，必须先
按预期基金市场过滤并忽略债券；预期市场内仍有多个精确结果时才算歧义。
成功结果可以从已确认股票缓存中复用。

成功响应（`200`）：

```json
{
  "symbol": "601872",
  "name": "招商轮船",
  "market": "17"
}
```

预期错误：

- `422`：代码格式错误或市场前缀不支持；不会调用 App 查询。
- `404`：没有返回精确匹配股票（`{"detail":"symbol not found"}`）。
- `409`：返回了多个精确匹配结果。
- `503`：App 查询或 Frida 桥接暂时不可用；稍后重试。

#### 2. 股票数据查询

没有同步返回八项指标的公开接口。必须先提交任务，再通过任务 ID 读取结果。

提交任务：

```http
POST /api/v1/jobs
Content-Type: application/json
```

纯数据请求示例：

```sh
curl -fsS -X POST "http://127.0.0.1:8000/api/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"601872","include_long_capture":false}'
```

请求体：

- `symbol`：必填六位股票代码。必须使用名称查询返回的精确代码；不得传入
  股票名称、模糊搜索文本、交易所后缀或未知市场前缀。
- `include_long_capture`：可选布尔值，默认 `true`。设为 `false` 时跳过
  App 页面导航、滚动、截图、拼接、OCR 和 PNG 存储，但绝不改变指标查询路径。

成功接受（`202`）会返回包含不透明 `public_id` 的 `TaskResponse`。
`202` 只表示任务已经进入 FIFO 队列，不表示数据已经准备好。生产 Runner
只能调用 `FridaParsedValueSource.read_direct(symbol)` 获取八项指标；该调用
把股票代码和已确认的市场代码发送给 App 内部请求桥接。不得降级到 `read()`、
股票页面导航、截图 OCR 或长截图解析。

查询任务状态：

```sh
curl -fsS "http://127.0.0.1:8000/api/v1/jobs/${PUBLIC_ID}"
```

也可以订阅状态事件，并在每次事件后获取完整任务：

```text
GET /api/v1/jobs/{public_id}/events
GET /api/v1/jobs/{public_id}
```

终态结果位于 `values`：

| 字段 | 含义和格式 |
| --- | --- |
| `stock_name` | App 返回的股票名称 |
| `current_price` | 当前股价，保留两位小数 |
| `change_percent` | 当前涨跌幅，保留两位小数并带 `%` |
| `turnover_rate` | 换手率，保留两位小数并带 `%` |
| `large_order_net` | 最新大单净量，保留两位小数 |
| `large_order_amount` | 最新大单金额，换算为 `万`，保留一位小数 |
| `retail_count` | 最新散户数量指标，保留两位小数 |
| `macdfs` | 最新 MACDFS 点，保留三位小数，正数带 `+` |

`value_sources` 中，已返回指标必须为 `INTERFACE`，缺失指标必须为 `null`。
缺失字段保持 `null`，绝不能从 OCR 或截图中补齐。八项指标完整时任务为
`COMPLETED`；任一指标缺失时任务为 `PARTIAL`，错误码为
`VALUE_RECOGNITION_FAILED`。App 内部直接请求失败时，任务为 `FAILED`，并保留
原始接口错误码，例如 `DIRECT_APP_OFFLINE`、`DIRECT_REQUEST_TIMEOUT` 或
`DIRECT_MANAGER_UNAVAILABLE`；不得替换成 UI 降级路径。

提交和状态查询的预期错误：

- `422`：股票代码格式错误或市场前缀不支持。
- `404`：名称查询没有找到精确股票，或任务 ID 已不存在。
- `409`：名称查询结果有歧义。
- `429`：全局待处理队列已满。
- `503`：生产环境在入队前无法使用 App 股票查询。

当 `include_long_capture` 为 `true` 时，长截图生成后可能返回
`/api/v1/jobs/{public_id}/capture`。OCR 仍只能用于长截图结构校验，例如确认
必需的图表标题存在；它绝不能作为 `values` 中任何字段的数据来源。

## 本地部署

- 在本 Mac 上必须使用 `deploy/macos.env` 部署，HTTP 使用 8000 端口，不得重新引入 Caddy。
- 重建 API 服务时必须保留 Redis 数据卷以及 Android 模拟器数据和登录状态。

---

# Project rules

## Market data collection

- Task values must be obtained only through the Tonghuashun App-internal interface exposed by `FridaParsedValueSource.read_direct()`.
- Never use screenshot OCR, UI text extraction, or values parsed from a long screenshot to fill any task metric.
- If the interface request fails, fail the task with the original interface error code. Do not open the stock search page as a fallback.
- If the interface returns only some metrics, keep missing metrics empty and return a partial task. Do not fill them with OCR.
- The long-capture switch controls only whether a long screenshot is generated. It must never change how metric values are queried.
- OCR may be used only for non-data structural validation of a long screenshot, such as checking that a required chart heading is present.

### Public API call contract

The public API has two separate market-data calls. First confirm the stock code
and name; then submit an asynchronous data task. Do not treat the name lookup
response as market data, and do not submit a task with an unverified code when
the production App lookup is available.

#### 1. Stock code and name lookup

Endpoint:

```http
GET /api/v1/symbols/{symbol}
```

Call it with exactly six digits, for example:

```sh
curl -fsS "http://127.0.0.1:8000/api/v1/symbols/601872"
```

The service accepts only confirmed stock and current exchange-fund prefixes:

- `600/601/603/605/688/689` → market `17` (Shanghai)
- `000/001/002/003/300/301` → market `33` (Shenzhen)
- `920` → market `151` (Beijing)
- `501/502/506/508/510/511/512/513/515/516/517/518/519/520/526/530/551/560/561/562/563/588/589`
  → market `20` (Shanghai funds)
- `158/159/160/161/162/163/164/165/166/167/168/169/180`
  → market `36` (Shenzhen funds)

The production implementation calls the already running Tonghuashun App's
internal exact-search bridge through `FridaParsedValueSource.lookup_symbol()`.
It must find exactly one result whose stock code, market, and non-empty name
all match the request. It must not use screenshot OCR, UI text extraction, or
a fuzzy result. When one code has both a market `19/35` bond and a market
`20/36` fund, filter by the expected fund market and ignore the bond before
checking uniqueness. Multiple exact results inside the expected market remain
ambiguous. Successful results may be reused from the verified symbol cache.

Success (`200`):

```json
{
  "symbol": "601872",
  "name": "招商轮船",
  "market": "17"
}
```

Expected errors:

- `422`: malformed code or unsupported market prefix; the App lookup is not called.
- `404`: no exact matching stock was returned (`{"detail":"symbol not found"}`).
- `409`: more than one exact matching result was returned.
- `503`: the App lookup or Frida bridge is temporarily unavailable; retry later.

#### 2. Stock data query

There is no synchronous public endpoint that returns the eight metrics in one
response. Submit a job, then read its result by the opaque task ID.

Submit:

```http
POST /api/v1/jobs
Content-Type: application/json
```

Example data-only request:

```sh
curl -fsS -X POST "http://127.0.0.1:8000/api/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"601872","include_long_capture":false}'
```

Request body:

- `symbol`: required six-digit stock code. Use the exact code returned by the
  name lookup; do not pass a stock name, fuzzy search text, exchange suffix,
  or an unknown prefix.
- `include_long_capture`: optional boolean, default `true`. `false` skips App
  page navigation, scrolling, screenshots, stitching, OCR, and PNG storage.
  It never changes the metric query path.

Accepted (`202`) returns a `TaskResponse` containing an opaque `public_id`.
`202` means only that the task entered the FIFO queue; it does not mean the
data is ready. In production, the task's eight values are obtained only by
calling `FridaParsedValueSource.read_direct(symbol)`, which sends the symbol
and its confirmed market code to the App-internal request bridge. It must not
fall back to `read()`, stock-page navigation, screenshot OCR, or long-screenshot
parsing.

Read the task status:

```sh
curl -fsS "http://127.0.0.1:8000/api/v1/jobs/${PUBLIC_ID}"
```

Or subscribe to status events and fetch the full task after each event:

```text
GET /api/v1/jobs/{public_id}/events
GET /api/v1/jobs/{public_id}
```

The terminal result is in `values`:

| Field | Meaning and formatting |
| --- | --- |
| `stock_name` | App quote name |
| `current_price` | Current price, two decimal places |
| `change_percent` | Change percentage, two decimal places and `%` |
| `turnover_rate` | Turnover rate, two decimal places and `%` |
| `large_order_net` | Latest large-order net value, two decimal places |
| `large_order_amount` | Latest large-order amount, converted to `万`, one decimal place |
| `retail_count` | Latest retail-count indicator, two decimal places |
| `macdfs` | Latest MACDFS point, three decimal places; positive values include `+` |

`value_sources` must be `INTERFACE` for a returned metric and `null` for a
missing metric. A missing field remains `null`; it is never filled from OCR or
the screenshot. A complete result is `COMPLETED`; any missing field is
`PARTIAL` with `error_code` `VALUE_RECOGNITION_FAILED`. If the direct App
request fails, the task is `FAILED` and keeps the original interface error code
(for example `DIRECT_APP_OFFLINE`, `DIRECT_REQUEST_TIMEOUT`, or
`DIRECT_MANAGER_UNAVAILABLE`); do not replace it with a UI fallback.

Expected submission/status errors:

- `422`: malformed or unsupported stock code.
- `404`: name lookup found no exact stock, or the task ID no longer exists.
- `409`: name lookup is ambiguous.
- `429`: the global pending queue is full.
- `503`: the production App lookup is unavailable before queueing.

When `include_long_capture` is `true`, the returned `long_capture` may expose
`/api/v1/jobs/{public_id}/capture` after the image is ready. OCR remains limited
to structural validation of that image, such as confirming that the required
chart heading is present; it is never a source for any field in `values`.

## Local deployment

- On this Mac, deploy with `deploy/macos.env`, keep HTTP on port 8000, and do not reintroduce Caddy.
- Preserve the Redis volume and Android emulator data/login state when rebuilding the API service.
