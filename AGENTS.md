# 项目规则

## 市场数据采集

- 任务指标只能通过已选择的同花顺 App 内部协议契约获取：默认
  `FridaParsedValueSource.read_direct()`，显式 `CORE_METRICS_TRANSPORT=direct`
  时使用已验证的 `Core9528Client`；公开行情源绝不能填写任务指标。
- 严禁使用截图 OCR、UI 文本提取或长截图解析结果填写任何任务指标。
- 接口请求失败时，必须以原始接口错误码结束任务；不得打开股票搜索页作为降级路径。
- 接口只返回部分指标时，缺失指标必须保持为空并返回部分任务；不得使用 OCR 补值。
- 长截图开关只控制是否生成长截图，绝不能改变指标查询方式。
- OCR 只能用于长截图的非数据结构校验，例如确认必需的图表标题是否存在。

### 对外 API 调用契约

对外 API 有两个独立的市场数据调用：先确认股票代码和名称，再提交异步
数据任务。名称查询响应不是行情数据；生产环境必须使用当前本地证券目录确认
代码和名称，不得提交未经确认的代码。

#### 1. 股票代码和名称查询

接口：

```http
GET /api/v1/symbols/{symbol}
```

调用时必须使用六位数字，例如：

```sh
curl -fsS "http://127.0.0.1:8001/api/v1/symbols/601872"
```

服务只接受已确认的股票和当前交易所基金代码前缀：

- `600/601/603/605/688/689` → 市场 `17`（上海）
- `000/001/002/003/300/301` → 市场 `33`（深圳）
- `920` → 市场 `151`（北京）
- `501/502/506/508/510/511/512/513/515/516/517/518/519/520/526/530/551/560/561/562/563/588/589`
  → 市场 `20`（沪基）
- `158/159/160/161/162/163/164/165/166/167/168/169/180`
  → 市场 `36`（深基）

生产实现通过 `/data/market/symbol-catalog.db` 的版本化 SQLite 证券目录查询。
目录每天从新浪公开 `hs_a`、`etf_hq_fund`、`lof_hq_fund` 分类同步，完整候选
版本校验通过后才原子切换；失败时保留旧版本，超过七天则返回 503。返回结果
必须同时满足六位代码完全一致、`market_code_for_symbol()` 市场一致和名称非空。
股票代码/名称联想、精确确认、任务提交二次确认和 market 自选添加均不得调用
App、Frida、截图 OCR 或 UI 文本。

成功响应（`200`）：

```json
{
  "symbol": "601872",
  "name": "招商轮船",
  "market": "17"
}
```

预期错误：

- `422`：代码格式错误或市场前缀不支持；不会查询证券目录。
- `404`：没有返回精确匹配股票（`{"detail":"symbol not found"}`）。
- `409`：返回了多个精确匹配结果。
- `503`：证券目录不存在、超过七天或刷新不可用；稍后重试。

#### 2. 股票数据查询

没有同步返回八项指标的公开接口。必须先提交任务，再通过任务 ID 读取结果。

提交任务：

```http
POST /api/v1/jobs
Content-Type: application/json
```

纯数据请求示例：

```sh
curl -fsS -X POST "http://127.0.0.1:8001/api/v1/jobs" \
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
默认调用 `FridaParsedValueSource.read_direct(symbol)` 获取八项指标；显式
启用 `CORE_METRICS_TRANSPORT=direct` 时才允许调用已验证的独立
`Core9528Client`。两条路径都必须把股票代码和已确认的市场代码用于同一 App
内部协议，不得降级到 `read()`、股票页面导航、截图 OCR 或长截图解析。

查询任务状态：

```sh
curl -fsS "http://127.0.0.1:8001/api/v1/jobs/${PUBLIC_ID}"
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

`values.intraday_series` 是当前交易日的 App 分时曲线，包含
`large_order_net`、`large_order_amount` 和 `retail_count`。每条曲线返回
独立的 `unit` 和按时间排序的 `points`；点的 `time` 为 `HH:mm`，`value` 为
格式化字符串或 `null`。大单金额统一换算为 `万`。曲线只能来自
`core_metrics` 的 `read_direct()` App 内部接口，禁止使用 UI、OCR 或截图补值。
`value_sources.intraday_series` 对有有效点的曲线为 `INTERFACE`，否则为
`null`；曲线缺失不改变原八项指标的任务完成状态。

前端结果区的数据展示顺序固定为：原八项指标卡片（`MACDFS` 最后）、
`main_fund_flow` 主力流向、`intraday_series` App 内部曲线。主力流向在前端
统一换算为 `亿元` 并四舍五入保留两位小数，但 API 仍保留 App 返回的动态单位
和原始格式化值。主力流向和 App 内部曲线都只保留各自最外层容器边框，内部
表格/曲线不得再套卡片边框。资金表和 SVG 曲线必须在手机宽度内完整缩放，
不得依赖横向滚动查看右侧内容。

`values.main_fund_flow` 是可选增强，按 `today`、`three_day`、`five_day`
三个周期返回。每个周期包含独立的动态 `unit`（`万元` 或 `亿元`），以及
`main_net_inflow`、`main_visible_inflow`、`main_hidden_inflow`、
`retail_inflow` 四个保留两位小数的字符串或 `null`。主力净流入必须直接采用
`charge_main_capital`；散户流入按 App 模型定义为主力净流入的相反数。成功的
资金值来源为 `INTERFACE`，缺失值来源为 `null`。

`value_sources` 中，已返回指标必须为 `INTERFACE`，缺失指标必须为 `null`。
缺失字段保持 `null`，绝不能从 OCR 或截图中补齐。八项指标完整时任务为
`COMPLETED`；任一指标缺失时任务为 `PARTIAL`，错误码为
`VALUE_RECOGNITION_FAILED`。App 内部直接请求失败时，任务为 `FAILED`，并保留
原始接口错误码，例如 `DIRECT_APP_OFFLINE`、`DIRECT_REQUEST_TIMEOUT` 或
`DIRECT_MANAGER_UNAVAILABLE`；不得替换成 UI 降级路径。

双账号模式下，`source_errors` 固定包含 `core_metrics` 和
`main_fund_flow`。原八项接口失败时任务为 `FAILED`；资金接口明确失败时保留
原八项结果并返回 `PARTIAL`，原始资金错误写入
`source_errors.main_fund_flow`。资金接口有效响应但个别字段缺失时，资金字段
保持 `null`，不影响原八项完整任务的 `COMPLETED` 状态。

提交和状态查询的预期错误：

- `422`：股票代码格式错误或市场前缀不支持。
- `404`：名称查询没有找到精确股票，或任务 ID 已不存在。
- `409`：名称查询结果有歧义。
- `429`：全局待处理队列已满。
- `503`：生产环境在入队前无法使用有效证券目录。

当 `include_long_capture` 为 `true` 时，长截图生成后可能返回
`/api/v1/jobs/{public_id}/capture`。OCR 仍只能用于长截图结构校验，例如确认
必需的图表标题存在；它绝不能作为 `values` 中任何字段的数据来源。

### Market 公开行情契约

- Market 基础报价、当日分时和五日/周/月 K 线使用腾讯公开接口；腾讯基础报价
  失败时可使用新浪公开报价。
- 前复权日 K 优先使用同花顺公开 Web K 线，失败时使用腾讯公开 qfq 日 K，
  再失败只能返回已验证 stale 缓存或 `KLINE_SOURCES_UNAVAILABLE`，不得回退 App。
- 公开行情统一校验代码、名称、价格精度、交易时段、OHLC、成交量和成交额；
  股票通常两位价格，沪深基金通常三位。
- 9528 和资金 HTTP 只可作为 market 的可选 L2 增强，拥有大单、散户、MACDFS
  和资金流字段；增强失败不得使公开基础行情不可用，也不得覆盖公开报价。
- Market WebSocket 必须按股票保留最新事件并支持自动重连及 HTTP 快照降级。

## 本地部署

- 当前 Mac 双设备角色固定如下：
  - `core_metrics`：`THS_CORE_33_ARM64 / emulator-5556 / 27043`，用于人工会话续签、原八项协议材料、显式长截图请求和必要的管理员维护；股票名称和 market 基础行情不再依赖它。
  - `main_fund_flow`：`THS_API_33_ARM64 / emulator-5554 / 27042`，只用于资金 1/3/5 日接口。
- `emulator-5554` 是受保护的当前资金账号。禁止退出登录、切换账号、克隆 AVD、重装/卸载 App、清数据、`force-stop` 或自动页面导航。
- 自动任务导航和长截图只能操作 `emulator-5556`。核心设备必须保持 `wm size 1080x1920` 和 `wm density 480`；使用 `scripts/configure-macos-core-display.sh` 校准。
- 在本 Mac 上必须使用 OrbStack Docker context 和 `deploy/macos.env` 部署，标准命令为
  `docker --context orbstack compose --env-file .env --env-file deploy/macos.env -f deploy/compose.yml up -d --build`。
  根目录 `.env` 提供管理密钥，`deploy/macos.env` 提供本 Mac 的设备配置；两者必须同时加载。
  禁止使用 Docker Desktop 的 `desktop-linux` context。HTTP 使用 8001 端口，不得重新引入 Caddy。
- 双账号变量 `CORE_ADB_SERIAL`、`CORE_FRIDA_SERVER_ENDPOINT`、`FUND_ADB_SERIAL`、`FUND_FRIDA_SERVER_ENDPOINT` 必须四项齐全；旧单设备变量仅用于兼容模式。
- 重建 API 服务时必须保留 Redis 数据卷以及 Android 模拟器数据和登录状态。
- 管理页面同时显示两台设备；两个 WebSocket 为 `/api/admin/devices/core_metrics` 和 `/api/admin/devices/main_fund_flow`，旧 `/api/admin/device` 只映射到 `core_metrics`。

## Direct transport exception

When `CORE_METRICS_TRANSPORT` or `FUND_FLOW_TRANSPORT` is explicitly set to
`direct` or `shadow`, the corresponding server-side client may use only a
previously verified App-internal wire contract and an encrypted session bundle
captured after normal human login. It must preserve the same field ownership,
formatting, missing-value, and original-error rules as `read_direct()`; it may
never use OCR, UI text, or a guessed public endpoint. The raw cookie, User-Agent,
auth packet, and protocol keys must not appear in logs, task records, or public
responses. `frida` remains the default transport and the only allowed fallback
when a direct client is not selected.

---

# Project rules

## Market data collection

- Task values must use the selected verified Tonghuashun App-internal contract:
  `FridaParsedValueSource.read_direct()` by default, or `Core9528Client` when
  `CORE_METRICS_TRANSPORT=direct`; public quote sources never fill task values.
- Never use screenshot OCR, UI text extraction, or values parsed from a long screenshot to fill any task metric.
- If the interface request fails, fail the task with the original interface error code. Do not open the stock search page as a fallback.
- If the interface returns only some metrics, keep missing metrics empty and return a partial task. Do not fill them with OCR.
- The long-capture switch controls only whether a long screenshot is generated. It must never change how metric values are queried.
- OCR may be used only for non-data structural validation of a long screenshot, such as checking that a required chart heading is present.

### Public API call contract

The public API has two separate market-data calls. First confirm the stock code
and name; then submit an asynchronous data task. Do not treat the name lookup
response as market data, and do not submit a task with an unverified code when
the production catalog is available.

#### 1. Stock code and name lookup

Endpoint:

```http
GET /api/v1/symbols/{symbol}
```

Call it with exactly six digits, for example:

```sh
curl -fsS "http://127.0.0.1:8001/api/v1/symbols/601872"
```

The service accepts only confirmed stock and current exchange-fund prefixes:

- `600/601/603/605/688/689` → market `17` (Shanghai)
- `000/001/002/003/300/301` → market `33` (Shenzhen)
- `920` → market `151` (Beijing)
- `501/502/506/508/510/511/512/513/515/516/517/518/519/520/526/530/551/560/561/562/563/588/589`
  → market `20` (Shanghai funds)
- `158/159/160/161/162/163/164/165/166/167/168/169/180`
  → market `36` (Shenzhen funds)

Production lookup uses the versioned SQLite catalog at
`/data/market/symbol-catalog.db`. It is refreshed daily from Sina's public
`hs_a`, `etf_hq_fund`, and `lof_hq_fund` categories and activates a candidate
version only after complete validation. Failed refreshes keep the previous
version; a catalog older than seven days returns 503. Suggestions, exact
confirmation, submission re-confirmation, and market watchlist additions must
not call the App, Frida, screenshots, OCR, or UI text.

Success (`200`):

```json
{
  "symbol": "601872",
  "name": "招商轮船",
  "market": "17"
}
```

Expected errors:

- `422`: malformed code or unsupported market prefix; the catalog is not queried.
- `404`: no exact matching stock was returned (`{"detail":"symbol not found"}`).
- `409`: more than one exact matching result was returned.
- `503`: the local catalog is unavailable, stale, or cannot be refreshed.

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
curl -fsS -X POST "http://127.0.0.1:8001/api/v1/jobs" \
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
data is ready. Production defaults to `FridaParsedValueSource.read_direct(symbol)`;
with explicit `CORE_METRICS_TRANSPORT=direct`, it may call the verified
standalone `Core9528Client` instead. Both paths must use the confirmed symbol
and market code under the same App-internal contract and must not fall back to
`read()`, stock-page navigation, screenshot OCR, or long-screenshot parsing.

Read the task status:

```sh
curl -fsS "http://127.0.0.1:8001/api/v1/jobs/${PUBLIC_ID}"
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

`values.intraday_series` contains the current trading day's App intraday curves
for `large_order_net`, `large_order_amount`, and `retail_count`. Each curve has
its own `unit` and time-ordered `points`; point `time` is `HH:mm`, while `value`
is a formatted string or `null`. Large-order amount uses `万`. Curves must come
only from the `core_metrics` App-internal `read_direct()` request and must never
be filled from UI text, OCR, or screenshots. A curve's
`value_sources.intraday_series` entry is `INTERFACE` when it has at least one
valid point and otherwise `null`. Missing curves do not affect completion based
on the eight required scalar values.

The frontend result order is fixed: the eight scalar cards (`MACDFS` last),
`main_fund_flow`, then the `intraday_series` App curves. The frontend converts
all fund-flow values to `亿元` and rounds them to two decimal places, while the
API keeps the App's dynamic units and original formatted values. Fund flow and
App curves retain only their outer section border; tables and individual curves
must not add nested card borders. Both the fund table and SVG curves must fit a
mobile viewport without horizontal scrolling to reveal right-side content.

`values.main_fund_flow` is an optional enhancement with `today`,
`three_day`, and `five_day` periods. Each period carries its own dynamic
`unit` (`万元` or `亿元`) plus `main_net_inflow`, `main_visible_inflow`,
`main_hidden_inflow`, and `retail_inflow`, each a two-decimal string or
`null`. Use `charge_main_capital` directly for main net inflow; retail inflow
is the App model's negation of that value. Successful fund values have source
`INTERFACE`; missing values have source `null`.

`value_sources` must be `INTERFACE` for a returned metric and `null` for a
missing metric. A missing field remains `null`; it is never filled from OCR or
the screenshot. A complete result is `COMPLETED`; any missing field is
`PARTIAL` with `error_code` `VALUE_RECOGNITION_FAILED`. If the direct App
request fails, the task is `FAILED` and keeps the original interface error code
(for example `DIRECT_APP_OFFLINE`, `DIRECT_REQUEST_TIMEOUT`, or
`DIRECT_MANAGER_UNAVAILABLE`); do not replace it with a UI fallback.

In dual-account mode, `source_errors` always contains `core_metrics` and
`main_fund_flow`. A core-interface failure makes the task `FAILED`. An explicit
fund-interface failure preserves the core values and returns `PARTIAL`, with
the original code in `source_errors.main_fund_flow`. Missing fields in an
otherwise valid fund response remain `null` and do not prevent `COMPLETED`
when all eight required core values are present.

Expected submission/status errors:

- `422`: malformed or unsupported stock code.
- `404`: name lookup found no exact stock, or the task ID no longer exists.
- `409`: name lookup is ambiguous.
- `429`: the global pending queue is full.
- `503`: the production symbol catalog is unavailable or stale before queueing.

When `include_long_capture` is `true`, the returned `long_capture` may expose
`/api/v1/jobs/{public_id}/capture` after the image is ready. OCR remains limited
to structural validation of that image, such as confirming that the required
chart heading is present; it is never a source for any field in `values`.

### Public market contract

- Tencent public endpoints own basic quotes, current-day intraday data, and
  five-day/weekly/monthly series; Sina public quotes are the basic fallback.
- Front-adjusted daily K-line uses the public Tonghuashun web feed, then
  Tencent qfq, then validated stale cache. It never falls back to App.
- Public data validates identity, precision, sessions, OHLC, volume, and amount.
- 9528/fund HTTP may provide optional L2 enrichment only. Enrichment failure
  cannot fail or overwrite the public basic snapshot.
- Market WebSocket delivery keeps the latest event per symbol and reconnects
  with HTTP snapshot fallback.

## Local deployment

- The current Mac has two fixed device roles:
  - `core_metrics`: `THS_CORE_33_ARM64 / emulator-5556 / 27043` for human session renewal, core protocol material, explicit long captures, and administrator maintenance. Symbol lookup and basic market data no longer depend on it.
  - `main_fund_flow`: `THS_API_33_ARM64 / emulator-5554 / 27042` only for 1/3/5-day fund-flow interface requests.
- `emulator-5554` holds the protected current fund account. Never log it out, switch accounts, clone the AVD, reinstall/uninstall or clear the App, `force-stop` it, or navigate it automatically.
- Automated navigation and long captures may operate only on `emulator-5556`. Keep the core device at `wm size 1080x1920` and `wm density 480`; use `scripts/configure-macos-core-display.sh` to calibrate it.
- On this Mac, deploy with the OrbStack Docker context and `deploy/macos.env`.
  The canonical command is
  `docker --context orbstack compose --env-file .env --env-file deploy/macos.env -f deploy/compose.yml up -d --build`.
  The root `.env` provides the administrator secrets, while `deploy/macos.env`
  provides this Mac's device configuration; load both files.
  Do not use Docker Desktop's `desktop-linux` context. Keep HTTP on port 8001,
  and do not reintroduce Caddy.
- `CORE_ADB_SERIAL`, `CORE_FRIDA_SERVER_ENDPOINT`, `FUND_ADB_SERIAL`, and `FUND_FRIDA_SERVER_ENDPOINT` must be provided together; legacy single-device variables are compatibility-only.
- Preserve the Redis volume and Android emulator data/login state when rebuilding the API service.
- The admin page shows both devices concurrently. Its WebSockets are `/api/admin/devices/core_metrics` and `/api/admin/devices/main_fund_flow`; legacy `/api/admin/device` maps only to `core_metrics`.

## Direct transport exception

When `CORE_METRICS_TRANSPORT` or `FUND_FLOW_TRANSPORT` is explicitly set to
`direct` or `shadow`, the corresponding server-side client may use only a
previously verified App-internal wire contract and an encrypted session bundle
captured after normal human login. It must preserve the same field ownership,
formatting, missing-value, and original-error rules as `read_direct()`; it may
never use OCR, UI text, or a guessed public endpoint. The raw cookie, User-Agent,
auth packet, and protocol keys must not appear in logs, task records, or public
responses. `frida` remains the default transport and the only allowed fallback
when a direct client is not selected.
