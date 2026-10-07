# 查询日志页面实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Admin 页面增加查询日志，记录查询者 IP、设备类型、浏览器标识及股票查询/任务结果，并支持分页筛选与 90 天清理。

**Architecture:** 复用现有 SQLite 业务库，新增独立 request_logs 表。FastAPI 中间件只捕获查询与任务相关路由，从请求上下文和已确认证券目录提取固定字段；Admin 通过受保护 API 分页读取。前端在现有 AdminPage 中增加日志页签和筛选表格。

**Tech Stack:** FastAPI、SQLite、Pydantic、React、TypeScript、Vitest。

**Spec:** 本轮对话中已确认的日志设计。

## Global Constraints

- 不记录 Token、Cookie、协议帧、完整请求体或敏感认证信息。
- 股票名称只使用本地证券目录确认结果。
- 日志默认保留 90 天。
- 仅 Admin 鉴权后可查询日志。
- 不改变现有市场数据来源和任务指标契约。

### Task 1: SQLite 日志存储与脱敏字段

**Files:**
- Create: `level2_service/request_logs.py`
- Modify: `level2_service/main.py`
- Test: `tests/test_request_logs.py`

**Interfaces:**
- `RequestLogStore.record(entry: RequestLogEntry) -> None`
- `RequestLogStore.list(limit: int, offset: int, filters: LogFilters) -> tuple[list[RequestLog], int]`
- `RequestLogStore.purge_before(cutoff: datetime) -> int`

- [ ] 写测试：记录 IP、User-Agent、设备类型、股票字段、状态和错误码；禁止持久化请求体与认证头。
- [ ] 运行测试确认因模块/方法不存在而失败。
- [ ] 实现 SQLite 表、索引、固定字段校验和分页查询。
- [ ] 增加 90 天清理方法。
- [ ] 运行测试确认通过。

### Task 2: 查询请求采集与任务关联

**Files:**
- Modify: `level2_service/api.py`
- Modify: `level2_service/main.py`
- Test: `tests/test_request_logging_api.py`

**Interfaces:**
- 中间件仅覆盖 `/api/v1/symbols`, `/api/v1/jobs`, `/api/v1/market` 查询路径。
- 记录 `request_id`、路由、HTTP 状态、耗时、客户端 IP、设备类型、User-Agent、symbol、stock_name、public_id、task_status、error_code。

- [ ] 写测试：股票精确查询和任务提交/状态请求各生成一条日志；代理头只在现有可信代理配置下解析。
- [ ] 运行测试确认失败。
- [ ] 实现中间件和响应后字段补全，错误只保存固定错误码。
- [ ] 确保未确认名称时 stock_name 为空，不从输入文本猜测。
- [ ] 运行测试确认通过。

### Task 3: Admin 日志查询 API

**Files:**
- Modify: `level2_service/api.py`
- Test: `tests/test_admin_logs_api.py`

**Interfaces:**
- `GET /api/admin/logs?limit=&offset=&from=&to=&symbol=&status=&ip=`
- 返回 `{items, total, limit, offset}`。
- 未登录返回 401；非法分页参数返回 422。

- [ ] 写认证、分页、时间/股票/状态/IP 筛选测试。
- [ ] 运行测试确认失败。
- [ ] 实现 admin 依赖保护和稳定响应模型。
- [ ] 运行测试确认通过。

### Task 4: Admin 日志页面

**Files:**
- Modify: `frontend/src/AdminPage.tsx`
- Modify: `frontend/src/api.ts`
- Test: `frontend/src/AdminPage.test.tsx`

**Interfaces:**
- 新增 `adminApi.logs(params)` 类型接口。
- 页面显示时间、IP、设备、浏览器、股票代码/名称、路径、任务状态、错误码、耗时。
- 支持分页、筛选、刷新、空状态和加载/错误状态。

- [ ] 写 Vitest 失败测试覆盖日志页签、筛选、分页和空状态。
- [ ] 运行测试确认失败。
- [ ] 实现 API 类型和页面组件，保持移动端不横向滚动。
- [ ] 运行测试确认通过。

### Task 5: 定时清理与运行验收

**Files:**
- Modify: `level2_service/main.py`
- Modify: `README.md`
- Test: `tests/test_request_logs.py`, `tests/test_deployment.py`

- [ ] 写清理任务和启动配置测试。
- [ ] 实现启动时及周期性清理 90 天前日志，清理失败只记录固定内部错误。
- [ ] 增加部署/运维说明和日志字段边界。
- [ ] 运行后端全量测试、前端构建与日志相关 Vitest。
- [ ] 记录测试结果和已知环境限制。
