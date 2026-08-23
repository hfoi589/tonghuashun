# 同花顺 Level2 双账号采集交接

更新日期：2026-08-23

本文记录当前已经实现并在真实 App 上验收的双设备架构。后续维护以
`AGENTS.md` 的采集红线为最高约束：所有任务数值只能来自同花顺 App 内部接口，
不得使用 UI 文本、截图或 OCR 补值。

## 1. 当前运行基线

- 服务地址：`http://127.0.0.1:8001/`
- 管理页面：`http://127.0.0.1:8001/#admin`
- Compose 项目：`ths-level2`
- API：FastAPI、Runner 和 React 前端位于同一个 `api` 容器。
- Redis：FIFO 队列、任务、SSE 状态事件和已确认股票缓存。
- Android：两台 API 33 ARM64 AVD 原生运行在 Apple Silicon Mac，不在 Docker 中。
- 同花顺 App：11.59.03。
- Frida Server/Python 客户端：16.7.19。
- Caddy 已移除；本机部署继续使用 HTTP 8001。

当前 Git 远端为 `https://github.com/hfoi589/tonghuashun.git`，默认分支为
`main`。`.env`、`deploy/macos.env`、APK、Frida Server、Docker 卷和 AVD 数据
都不在 Git 中，迁移时必须单独处理。

## 2. 两台设备的固定职责

| 角色 | AVD / ADB | Frida | 责任 |
| --- | --- | --- | --- |
| `core_metrics` | `THS_CORE_33_ARM64 / emulator-5556` | `host.docker.internal:27043` | 股票名称确认、原八项指标、自动页面导航、长截图 |
| `main_fund_flow` | `THS_API_33_ARM64 / emulator-5554` | `host.docker.internal:27042` | 主力流向当日/3日/5日接口 |

### 资金设备保护规则

`emulator-5554` 保存当前资金账号登录状态，是受保护设备：

- 禁止退出或切换账号。
- 禁止克隆 AVD 数据目录。
- 禁止安装、卸载、重装、清数据或 `force-stop` 同花顺。
- 禁止任务自动点击、搜索、切换页面或生成长截图。
- 设备停止时只能从原 AVD 数据目录重新启动。
- 所有 ADB 命令必须显式带 `-s emulator-5554`，并先确认操作确实属于资金接口维护。

股票页面导航和长截图只能操作 `emulator-5556`。第二账号的登录、验证码、
设备验证和大单权限由管理员人工完成，项目不接收或保存账号密码。

## 3. 数据采集架构

公开 API 仍采用“先确认代码和名称，再提交异步任务”的两步契约：

```sh
curl -fsS http://127.0.0.1:8001/api/v1/symbols/601872

curl -fsS -X POST http://127.0.0.1:8001/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"601872","include_long_capture":false}'

curl -fsS http://127.0.0.1:8001/api/v1/jobs/PUBLIC_ID
```

Runner 对外仍只有一个 `read_direct(symbol)` 数据入口，内部并行连接两台 App：

1. `core_metrics` 使用 App 内部精确搜索桥接确认股票，并查询报价和四个技术指标。
2. `main_fund_flow` 创建独立资金 `QueryClient`，在同一客户端内严格顺序执行
   `win_size=1`、`3`、`5`。
3. 聚合器按字段白名单合并结果，两个来源不能互相覆盖。
4. 两台 Frida 使用独立 endpoint、会话和锁；初次连接设备的 Frida 枚举操作有
   模块级短锁，实际数据请求仍并行。
5. `-2147483648` 等无权限哨兵在格式化前变为 `null`。

原八项必填字段为：

- `stock_name`
- `current_price`
- `change_percent`
- `turnover_rate`
- `large_order_net`
- `large_order_amount`
- `retail_count`
- `macdfs`

资金增强数据位于 `values.main_fund_flow`，周期为 `today`、`three_day`、
`five_day`。每个周期保存独立动态单位 `万元` 或 `亿元`，以及：

- `main_net_inflow`：直接使用 `charge_main_capital`。
- `main_visible_inflow`：`charge_main_listed_capital`。
- `main_hidden_inflow`：`charge_main_grey_capital`。
- `retail_inflow`：App 模型定义的 `-main_net_inflow`。

所有成功值的 `value_sources` 都是 `INTERFACE`；缺失值及其来源保持 `null`。
主力净流入不能用已经四舍五入的明盘、暗盘重新相加。

### 状态规则

- 原八项接口明确失败：`FAILED`，保留原始接口错误码。
- 原八项接口有效响应但必填值缺失：`PARTIAL / VALUE_RECOGNITION_FAILED`。
- 资金接口明确失败：保留原八项数据，任务为 `PARTIAL`，错误写入
  `source_errors.main_fund_flow`。
- 资金接口有效响应但个别值缺失：缺失项为 `null`；原八项完整时仍可
  `COMPLETED`。
- 不使用页面、OCR 或截图作为任何失败接口的降级路径。

`include_long_capture=false` 不做页面导航、滚动、截图、拼接或 OCR。
`include_long_capture=true` 只额外让核心设备生成长截图；接口数据路径不变。
OCR 仅能检查长图是否出现“净量”或基金页面的“大单占比”等结构标题。

## 4. 双设备管理页面

- `GET /api/admin/devices` 返回两台设备的 ADB、App 和 Frida 健康状态，不返回账号信息。
- `WS /api/admin/devices/core_metrics` 控制和预览核心设备。
- `WS /api/admin/devices/main_fund_flow` 控制和预览资金设备。
- 旧 `WS /api/admin/device` 兼容映射到 `core_metrics`。
- 桌面端双列、窄屏上下排列，两路画面保持同时在线并各自约 2 FPS。
- 点击、滑动和键盘输入只发送到产生事件的设备，不广播。
- 资金面板固定显示“当前账号，禁止退出”。
- 两个画面共享一个管理员锁；获取锁会暂停任务队列，释放锁后需显式恢复队列。
- 一台设备离线只影响自己的画面和健康状态。

## 5. 本 Mac 启动与恢复

### 5.1 本机配置

`deploy/macos.env` 应至少包含：

```dotenv
CORE_ADB_SERIAL=emulator-5556
CORE_FRIDA_SERVER_ENDPOINT=host.docker.internal:27043
FUND_ADB_SERIAL=emulator-5554
FUND_FRIDA_SERVER_ENDPOINT=host.docker.internal:27042
ADB_SERIAL=
ADB_SERVER_SOCKET=tcp:host.docker.internal:5037
ADB_CONNECT=0
ADMIN_PASSWORD_FILE=/data/admin/password.hash
APP_PORT=8001
ADMIN_COOKIE_SECURE=0
FRIDA_SERVER_ENDPOINT=
```

四个双账号变量必须一起设置。旧 `ADB_SERIAL` 和
`FRIDA_SERVER_ENDPOINT` 只用于单设备兼容模式。

### 5.2 一键启动双 AVD 和桥接

```sh
./scripts/bootstrap-macos-dual-avd.sh \
  /绝对路径/ths.apk \
  /绝对路径/frida-server-16.7.19-android-arm64
```

脚本会：

- 仅从原数据目录启动已存在的资金 AVD，不修改其 App 或数据。
- 缺失时创建干净的 `THS_CORE_33_ARM64`，并只在核心设备缺 App 时安装 APK。
- 首次创建/安装后暂停，等待管理员人工登录和确认大单权限。
- 把核心设备校准为 `1080x1920 / 480 dpi`。
- 启动两台设备的 Frida，建立 `27042→27042`、`27043→27042` 转发。
- 通过 launchctl 运行 bridge watcher，在模拟器/Frida 重启后恢复 root Frida 和转发。

只重新校准核心显示可执行：

```sh
./scripts/configure-macos-core-display.sh emulator-5556 /opt/homebrew/bin/adb
```

长截图代码按 `1080x1920`、固定顶部 215 px 和底部 154 px 标定。核心设备
若回到物理 `320x640 / 160 dpi`，导航虽然可能成功，但首张截图会报
`device screenshot must be 1080x1920` 并表现为 `NAVIGATION_FAILED`。

### 5.3 启动 Web/API/Redis

```sh
docker compose --env-file deploy/macos.env -f deploy/compose.yml config --quiet
docker compose --env-file deploy/macos.env -f deploy/compose.yml up -d --build api redis
```

不得加入 Caddy，不得使用 `down -v`。重建必须保留：

- `ths-level2_redis-data`
- `ths-level2_capture-data`
- `ths-level2_template-data`
- `ths-level2_admin-data`

Android 登录状态保存在宿主机 AVD 数据目录，不在 Docker 卷中。Docker Hub
偶发 TLS 超时时不要删除现有镜像或容器；先保留运行服务，网络恢复后再重建。

### 5.4 快速健康检查

```sh
/opt/homebrew/bin/adb devices -l
/opt/homebrew/bin/adb -s emulator-5556 shell pidof com.hexin.plat.android
/opt/homebrew/bin/adb -s emulator-5554 shell pidof com.hexin.plat.android
/opt/homebrew/bin/adb -s emulator-5556 shell pidof ths-frida-server
/opt/homebrew/bin/adb -s emulator-5554 shell pidof ths-frida-server
/opt/homebrew/bin/adb -s emulator-5556 shell wm size
/opt/homebrew/bin/adb -s emulator-5556 shell wm density
/opt/homebrew/bin/adb forward --list
curl -fsS http://127.0.0.1:8001/openapi.json >/dev/null
```

应看到核心 `27043→27042`、资金 `27042→27042`，核心显示覆盖为
`1080x1920 / 480`。不要为了健康检查停止资金 App。

## 6. 真实验收记录

2026-08-23 的当前 Mac 验收结果：

- 两台 AVD、两台 App、两台 Frida Server 和两个转发同时在线。
- `600938` 纯数据任务完成，原八项和三周期资金数据均通过接口返回。
- 杀掉核心 Frida 后，bridge watcher 能恢复 root Frida 和 `27043` 转发，随后
  `600938` 再次完成。
- `160723` 数据任务完成：原八项完整，三个资金周期各四项均返回
  `0.00 万元`。
- `160723` 长截图任务
  `B6zOMpOpqQkvhPLxor_jgSZshBwdVbi9` 为 `COMPLETED`，长图为
  `1080x4868` PNG，HTTP 下载返回 200。
- 资金 App 在修复和验收期间未退出、未重装、未清数据，进程保持运行。
- 后端：`PYTHONPATH=. uv run pytest -q` → `258 passed`。
- 前端：`npm test` → `40 passed`；`npm run build` 成功。

旧任务 `hW6lnm_Ge5FLGiF8c_L_KGSOi6F68iyS` 的
`NAVIGATION_FAILED` 根因是新核心 AVD 没有显示覆盖，实际截图为
`320x640`。历史失败记录不会被修改，替代验收任务已经成功。

## 7. 排障顺序

### `DIRECT_*` 或某个 `source_errors` 非空

1. 根据 `source_errors.core_metrics` / `main_fund_flow` 判断设备角色。
2. 检查对应 ADB、App PID、Frida PID 和端口转发。
3. 保留 App 内部回调的原始错误码；不得打开股票页或用 OCR 补值。
4. 资金错误只处理资金 Frida/网络，禁止退出资金账号。

### `NAVIGATION_FAILED`

1. 只观察/操作 `emulator-5556`。
2. 检查 `wm size` 和 `wm density`，必要时运行显示校准脚本。
3. 确认核心账号能搜索目标代码，股票页可以滚动到底部。
4. 检查第一帧是否为可读 `1080x1920` PNG、滚动是否产生偏移、拼接图是否有
   必需结构标题。
5. 不要把导航错误误诊为市场数据接口失败，也不要操作资金页面。

### 管理页面单路离线

1. 调用 `GET /api/admin/devices` 区分 ADB、App 或 Frida。
2. 检查该角色 WebSocket；不要关闭另一条正常连接。
3. 管理员锁释放后，显式恢复任务队列。

## 8. 安全与迁移注意事项

- `.env` 包含管理员哈希和会话密钥，权限保持 `0600`。
- 不要提交 APK、Frida Server、账号信息、AVD 数据或未脱敏日志。
- 不要运行 `adb -a`，不要把 5037、27042、27043 或 Redis 6379 暴露到公网。
- 当前 HTTP 只适用于可信本地网络；对外发布必须使用可信 HTTPS 代理并设置
  `ADMIN_COOKIE_SECURE=1`。
- 长截图保留 24 小时，任务元数据保留 7 天；浏览器历史位于当前 Origin 的
  `localStorage`，不会随 Redis 卷迁移。
- 迁移另一台 Mac 时推荐新建核心 AVD 并人工登录；资金账号是否迁移由管理员
  单独决定，不能未经确认复制其 AVD 数据。
- 下线旧环境前，先验收两台 App、两个接口来源、双设备管理画面、纯数据任务和
  长截图任务，并确认 Docker 卷已备份。
