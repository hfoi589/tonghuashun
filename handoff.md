# 同花顺 Level2 直连与公开行情交接

更新日期：2026-08-27

## 1. 当前结论

普通业务已经不要求虚拟机中的同花顺 App 持续运行：

- 股票代码/名称联想和精确确认：本地版本化 SQLite 证券目录。
- Market 基础报价、分时、五日/周/月 K：腾讯公开行情，新浪公开报价备用。
- 前复权日 K：同花顺公开 Web K 线，腾讯 qfq 备用，最后使用 stale 缓存。
- 原八项任务指标和三条 241 点 L2 曲线：服务端 9528 直连。
- 主力流向当日/3日/5日：服务端资金 HTTP 直连。

App 仅保留以下用途：

- 管理员人工登录、验证码和设备验证；
- 私有直连会话材料过期后的人工续签；
- 用户显式请求 `include_long_capture=true` 时由核心设备生成长截图；
- 管理员主动打开设备控制页面进行维护。

任务指标仍严禁使用公开行情、UI 文本、OCR 或截图补值。

## 2. 生产运行基线

- 服务：`http://127.0.0.1:8001/`
- 管理页：`http://127.0.0.1:8001/#admin`
- Market：`http://127.0.0.1:8001/market`
- Compose 项目：`ths-level2`
- Docker context：`orbstack`
- API 和 React：`ths-level2-api-1`
- Redis：`ths-level2-redis-1`
- Git 远端：`https://github.com/hfoi589/tonghuashun.git`
- 发布目标分支：`main`（开发来源：`codex/symbol-search-tab-delete`）

标准部署命令：

```sh
docker --context orbstack compose \
  --env-file .env \
  --env-file deploy/macos.env \
  -f deploy/compose.yml up -d --build
```

禁止使用 `desktop-linux`，禁止执行 `down -v`，禁止删除 Redis、market、admin、
session 或 capture 卷。

## 3. 设备角色与保护边界

| 角色 | AVD / ADB | Frida | 当前责任 |
| --- | --- | --- | --- |
| `core_metrics` | `THS_CORE_33_ARM64 / emulator-5556` | `host.docker.internal:27043` | 人工会话续签、核心协议材料、显式长截图、管理员维护 |
| `main_fund_flow` | `THS_API_33_ARM64 / emulator-5554` | `host.docker.internal:27042` | 人工资金会话续签；普通资金请求由服务端 HTTP 直连 |

`emulator-5554` 仍是受保护资金账号：

- 禁止退出、切号、克隆、重装、卸载、清数据或 `force-stop`。
- 禁止自动导航、搜索和长截图。
- 任何 ADB 命令必须显式 `-s emulator-5554`，并且只能用于用户明确授权的维护。

本轮实现、部署和验收没有向 `emulator-5554` 发送操作命令。

## 4. 核心 9528 直连

### 协议能力

`Core9528TemplateProtocol` 已实现：

- 认证帧读取和业务请求分段；
- HSSL 外层帧校验；
- Snappy、`gov`、`cv3`、HXLONG 解码；
- 主报价、换手率、涨跌幅；
- 大单净量 `33007`；
- 大单金额 `33015`；
- 散户数量 `216`；
- MACDFS 参数捕获与计算；
- 三条 241 点 L2 分时曲线。

未知加密、压缩、半帧、残帧、身份漂移和错误长度继续 fail closed。

### 一次性预认证池

- 池目标容量为 1。
- 预认证连接只允许使用一次，业务完成后永久关闭，绝不归池。
- 业务读取期间不并行发起下一次认证；连接关闭后才异步补池。
- 会话 fingerprint 变化、管理员 refresh、超过 25 秒、EOF、超时、解码失败和
  服务关闭都会销毁旧连接。
- 同步认证和会话失效之间使用 generation 校验；客户端读取 session、prewarm、
  invalidate、close 共用生命周期锁。
- 原错误码保持不变，不产生 warm-pool 专用公共错误码。

### Runner 唤醒

API 入队、公开 retry、管理员 retry/resume 和队列 resume 会即时唤醒 Runner。
Redis 仍是持久 FIFO 权威，`RUNNER_POLL_INTERVAL_SECONDS` 只作为外部 Redis
入队的兜底。

## 5. 股票目录

目录文件：`/data/market/symbol-catalog.db`

公开来源：

- `https://money.finance.sina.com.cn/.../Market_Center.getHQNodeStockCount`
- `hs_a`
- `etf_hq_fund`
- `lof_hq_fund`

刷新流程：

1. 读取每个分类总数；
2. 固定每页 100 条分页，防止新浪静默把大页截成 100 条；
3. 过滤项目允许的代码前缀并校验 `sh/sz/bj` 与市场代码；
4. 同名重复去重，冲突名称拒绝；
5. 首版至少 5000 条，后续不得缩小到上一版的 90% 以下；
6. 生成 SHA-256 checksum；
7. 完整写入新版本后原子切换 active 指针。

启动时目录不存在或超过 18 小时会后台刷新；每天 16:20 Asia/Shanghai 再刷新。
刷新失败保留旧版，超过 7 天后 lookup/search 返回固定 503。

2026-08-27 真实同步结果：

- 7574 条；
- 完整刷新约 30.7 秒；
- `601872 / 300750 / 920002 / 510300 / 159919` 五类样本均命中；
- checksum 前缀：`2ef0e96eb7e2`。

## 6. Market 公开数据平面

### 基础快照

- 轻量自选报价：`https://qt.gtimg.cn/q=<sh|sz|bj><symbol>`。
- 详情和当日分时：腾讯 `minute/query`，沪深/基金为四列累计量额，北交所为
  三列累计量；统一换算为分钟增量和股。
- 腾讯失败时使用新浪 `hq.sinajs.cn` 基础报价；没有分时时明确标记能力缺失。
- 股票价格通常两位，沪深基金三位。

### K 线

- 日 K：同花顺公开 qfq 年线主源，腾讯 qfq 备用。
- 五日/周/月：腾讯公开 qfq。
- 日 K 两个公开源都失败时使用 stale 缓存；无缓存返回空页和
  `KLINE_SOURCES_UNAVAILABLE`。
- 不存在 App K 线 fallback。

### 可选 L2 增强

只有 `CORE_METRICS_TRANSPORT=direct`、`FUND_FLOW_TRANSPORT=direct` 且
`MARKET_DIRECT_ENRICHMENT=1` 时启用。每股票 15 秒缓存，只合并：

- 大单净量/金额；
- 散户数量；
- MACDFS；
- 三周期资金流；
- 对应 L2 分时曲线。

它不得覆盖公开名称、价格、OHLC、涨跌幅、换手率、成交量、成交额或公开分时。
增强失败只更新 `source_errors`，公开快照仍返回。

### 推送和前端

- Broker 按 client+symbol 保留最新事件，不同股票不再互相覆盖。
- WebSocket 断开后按 1/2/4/8/15 秒退避重连、重新订阅，并用 HTTP 刷新当前股票。
- L2 无有效数据时隐藏增强卡片和资金区。
- 前端不再出现“正在读取 App 接口”“App 内部 K 线”等误导文案。

## 7. 当前非秘密配置

`deploy/macos.env` 当前包含：

```dotenv
CORE_ADB_SERIAL=emulator-5556
CORE_FRIDA_SERVER_ENDPOINT=host.docker.internal:27043
FUND_ADB_SERIAL=emulator-5554
FUND_FRIDA_SERVER_ENDPOINT=host.docker.internal:27042
CORE_METRICS_TRANSPORT=direct
FUND_FLOW_TRANSPORT=direct
SYMBOL_CATALOG_PATH=/data/market/symbol-catalog.db
SYMBOL_CATALOG_MAX_AGE_SECONDS=604800
SYMBOL_CATALOG_REFRESH_HOUR=16
SYMBOL_CATALOG_REFRESH_MINUTE=20
PUBLIC_MARKET_TIMEOUT_SECONDS=8
MARKET_DIRECT_ENRICHMENT=1
MARKET_DIRECT_ENRICHMENT_TTL_SECONDS=15
CORE_WARM_CONNECTION_MAX_IDLE_SECONDS=25
```

`.env` 继续只保存管理员秘密和 `THS_SESSION_ENCRYPTION_KEY`，不得提交。

## 8. 2026-08-27 验收证据

本地测试：

- Python：511 passed；
- 前端：82 passed；
- `npm run build` 成功；
- `git diff --check` 成功。

部署：

- 标准 OrbStack 全量构建成功；
- API 和 Redis healthy；
- 证券目录 active version 1 / 7574 条。

公开 market 五类实测：

| 代码 | 市场 | 价格精度 | 分时点 | 日/周/月 |
| --- | --- | ---: | ---: | --- |
| 601872 | 沪A | 2 | 242 | 5/5/5 |
| 300750 | 深A | 2 | 242 | 5/5/5 |
| 920002 | 北交 | 2 | 242 | 1/1/1（腾讯该标的历史限制，日 K 主路径仍优先同花顺公开源） |
| 510300 | 沪基 | 3 | 242 | 5/5/5 |
| 159919 | 深基 | 3 | 242 | 5/5/5 |

纯数据任务：

| 代码 | 状态 | 端到端 | 八项 | L2 曲线 | 资金周期 |
| --- | --- | ---: | ---: | --- | ---: |
| 601872 | COMPLETED | 0.660s | 8/8 | 3 × 241 | 3 |
| 300750 | COMPLETED | 0.980s | 8/8 | 3 × 241 | 3 |
| 000001 | COMPLETED | 0.570s | 8/8 | 3 × 241 | 3 |

三任务同时进入 FIFO 的最后完成时间约 1.94 秒，均为 COMPLETED。

## 9. 常用检查

```sh
curl -fsS http://127.0.0.1:8001/openapi.json >/dev/null
curl -fsS http://127.0.0.1:8001/api/v1/symbols/601872
curl -fsS 'http://127.0.0.1:8001/api/v1/symbols?query=5103&limit=8'

docker --context orbstack compose \
  --env-file .env \
  --env-file deploy/macos.env \
  -f deploy/compose.yml ps
```

目录异常先检查 `/data/market/symbol-catalog.db` 和 API 日志；公开行情异常检查
腾讯/新浪网络与 stale 缓存。只有 `DIRECT_SESSION_*` 或核心协议错误才需要管理员
人工打开 App 续签。不要把公开源故障误诊为 App 故障，也不要操作资金设备。
