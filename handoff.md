# 同花顺 Level2 项目交接文档

本文档面向接手维护、迁移和排障人员，说明当前项目迁移到另一台 Mac 时需要准备的内容，以及数据采集的实际执行逻辑。

## 1. 项目概况

项目采用单机、单 Android 设备架构：

- React 前端、FastAPI API、后台采集 Runner 打包在同一个 `api` 容器中。
- Redis 容器负责 FIFO 任务队列、任务元数据和状态事件。
- Android 33 ARM64 模拟器原生运行在 Apple Silicon Mac 上，不运行在 Docker 中。
- API 容器通过 `host.docker.internal:5037` 连接宿主机 ADB，通过 `host.docker.internal:27042` 连接模拟器中的 Frida Server。
- 同一时间只由一个 Runner 串行处理任务，避免多个任务同时控制同一台 Android 设备。
- FastAPI 同时提供前端和 API，默认地址为 `http://HOST:8000/`；管理页面为 `http://HOST:8000/#admin`。

项目不会保存同花顺账号或明文密码，也不会绕过登录、验证码、设备验证和 Level2 权限。模拟器中的同花顺必须由管理员正常登录，并且账号本身能够查看相应数据。

当前源码、前端构建和 Mac 容器部署已经有验证记录，但仓库 `README.md` 仍把“真实 APK 完整冒烟清单”作为正式支持的前置条件。在真实账号、真实权限、纯数据任务和长截图任务全部验收前，只能称为部署候选，不能对外宣称平台已经完整受支持。

### 当前交接风险

截至本文档编写时：

- 当前分支是 `feature/ths-level2-web`。
- 工作区存在大量尚未提交的修改和新增文件。
- 当前仓库没有配置可直接依赖的 Git 远程地址。

因此，迁移时不能只在新 Mac 上克隆某个旧提交。必须先将当前工作区提交到可信的私有仓库，或者完整复制当前项目目录。迁移前应再次执行 `git status --short`，确认真正需要交接的文件都已包含在迁移包中。

## 2. 迁移到另一台 Mac

### 2.1 新 Mac 的硬性要求

当前 `macos-avd` 配置只支持 Apple Silicon Mac：

- CPU 架构：`arm64`，不支持 Intel Mac。
- 至少 4 核 CPU。
- 至少 8 GiB 内存。
- 至少 30 GiB 可用磁盘空间；如果需要保留较多 Docker 镜像、AVD 和迁移备份，建议预留更多空间。
- Docker Desktop，且 `docker`、`docker compose` 命令可用。
- Python 3。
- Android SDK Command-line Tools，并确保以下命令已加入 `PATH`：
  - `adb`
  - `emulator`
  - `sdkmanager`
  - `avdmanager`
- 可供 Android SDK 工具使用的 Java/JDK。

先在新 Mac 检查基础环境：

```sh
uname -m
docker --version
docker compose version
python3 --version
command -v adb emulator sdkmanager avdmanager
```

`uname -m` 必须返回 `arm64`。

### 2.2 必须带走的内容

| 内容 | 作用 | 注意事项 |
| --- | --- | --- |
| 当前完整项目目录 | 包含尚未提交的实际源码和部署文件 | 不要只复制旧 Git 提交 |
| `.env` | 管理员密码 Argon2id 哈希和会话密钥 | 属于敏感文件，权限应保持为 `0600` |
| `deploy/macos.env` | Mac 的 ADB、Frida、端口配置 | 文件被 Git 忽略，不会随普通克隆出现 |
| 指定版本的同花顺 APK | 创建 AVD、安装应用 | SHA-256 必须完全匹配项目预检值 |
| `frida-server-16.7.19-android-arm64` | 与容器内 Python Frida 客户端通信 | 客户端和 Server 版本必须保持 `16.7.19` |
| Docker 数据卷备份 | 保留任务、截图、模板和管理员状态 | 是否全部恢复可按下节选择 |

项目要求的 APK SHA-256 为：

```text
2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e
```

不要把 APK、Frida Server 或 `.env` 提交到 Git，也不要通过公开网盘或未加密渠道传输。

### 2.3 Docker 数据分别保存在哪里

当前 Compose 项目名固定为 `ths-level2`，需要关注以下四个卷：

| Docker 卷 | 内容 | 是否建议迁移 |
| --- | --- | --- |
| `ths-level2_redis-data` | FIFO 队列、任务元数据、任务状态事件 | 希望保留最近任务和队列时迁移 |
| `ths-level2_capture-data` | 已生成的长截图 | 希望保留 24 小时有效期内截图时迁移 |
| `ths-level2_template-data` | 手工校准的 OpenCV 搜索/标签模板 | 有自定义模板时必须迁移 |
| `ths-level2_admin-data` | 管理员修改后的密码哈希、每日检查状态 | 希望保留当前管理密码时迁移 |

旧环境中可能仍存在 `ths-level2_caddy-data`、`ths-level2_caddy-config` 等历史卷。当前版本已经由 FastAPI 直接提供前端，不再使用 Caddy，这两个旧卷不需要迁移。

如果恢复 `admin-data`，其中的 `/data/admin/password.hash` 会优先于 `.env` 中的初始密码哈希。若希望在新 Mac 使用新管理员密码，不要恢复旧 `admin-data`，而应重新生成 `.env`。

### 2.4 旧 Mac 上的停机和备份

1. 在管理页面暂停队列，等待正在执行的任务结束。
2. 记录旧 Mac 的端口、模拟器序列号和 AVD 名称。
3. 停止 Compose 服务，但不要添加 `-v`：

```sh
docker compose --env-file deploy/macos.env -f deploy/compose.yml down
```

4. 备份四个当前数据卷：

```sh
mkdir -p migration-backup/volumes

for volume_name in redis-data capture-data template-data admin-data; do
  docker run --rm \
    -v "ths-level2_${volume_name}:/data:ro" \
    -v "$PWD/migration-backup/volumes:/backup" \
    alpine \
    tar -czf "/backup/${volume_name}.tgz" -C /data .
done
```

5. 单独、安全地保存 `.env` 和 `deploy/macos.env`：

```sh
cp -p .env migration-backup/.env
cp -p deploy/macos.env migration-backup/macos.env
chmod 600 migration-backup/.env
```

6. 为迁移文件生成校验值，并将整个备份目录通过加密磁盘、加密归档或可信的点对点通道发送到新 Mac：

```sh
shasum -a 256 migration-backup/volumes/*.tgz
```

不要执行 `docker compose down -v`，否则会删除上述数据卷。

### 2.5 Android 模拟器和登录状态怎么处理

推荐方案是在新 Mac 重新创建 AVD、重新安装 APK，并由管理员手工登录同花顺。这样最稳定，也能避免旧 AVD 中的绝对路径、快照、设备标识和 Android SDK 路径在新机器上失效。

当前默认 AVD 名为 `THS_API_33_ARM64`，使用：

- Android API 33
- `google_apis`
- `arm64-v8a`
- 默认设备序列号通常为 `emulator-5554`
- 1080×1920 截图尺寸是长截图拼接和 OCR 的前提

如果业务上必须尝试保留同花顺登录状态，可以在完全关闭模拟器后备份：

- `~/.android/avd/THS_API_33_ARM64.avd`
- `~/.android/avd/THS_API_33_ARM64.ini`

但直接复制 AVD 属于尽力迁移，不是当前项目保证支持的路径。即使复制成功，同花顺仍可能因为设备环境变化要求重新验证或重新登录。

### 2.6 新 Mac 上的恢复步骤

以下命令都应在新 Mac 的项目根目录执行。

#### 第一步：恢复源码和配置

将完整项目目录复制到新 Mac，然后恢复两个被 Git 忽略的配置文件：

```sh
cp /安全备份路径/.env .env
cp /安全备份路径/macos.env deploy/macos.env
chmod 600 .env
```

上述 `/安全备份路径` 以及下文的 `/绝对路径` 都是示例，请替换为新 Mac 上的真实绝对路径。

检查 `deploy/macos.env` 至少保持以下 Mac 配置：

```dotenv
ADB_SERIAL=emulator-5554
ADB_SERVER_SOCKET=tcp:host.docker.internal:5037
ADB_CONNECT=0
ADMIN_PASSWORD_FILE=/data/admin/password.hash
APP_PORT=8000
ADMIN_COOKIE_SECURE=0
FRIDA_SERVER_ENDPOINT=host.docker.internal:27042
```

如果新 AVD 的序列号不是 `emulator-5554`，同时修改后续命令和 `ADB_SERIAL`。

#### 第二步：验证 APK 并创建 AVD

```sh
python3 scripts/preflight.py --apk-only --apk /绝对路径/ths.apk
./scripts/bootstrap-macos-avd.sh /绝对路径/ths.apk
python3 scripts/preflight.py --profile macos-avd --apk /绝对路径/ths.apk
```

`bootstrap-macos-avd.sh` 会安装/检查 Android 33 ARM64 系统镜像、创建 AVD、启动模拟器并安装 APK。预检必须出现 `PREFLIGHT OK`。

采集和 OCR 坐标按 1080×1920 屏幕校准。创建 AVD 后必须检查实际输出：

```sh
adb -s emulator-5554 shell wm size
adb -s emulator-5554 shell wm density
```

如果截图尺寸不是 1080×1920，不能直接进入生产验收，应先按照已经验证的 AVD 参数完成分辨率、DPI 和模板校准。

#### 第三步：人工登录同花顺

在模拟器中打开同花顺，由管理员完成正常登录、验证码、设备验证和 Level2 权限确认。至少手工确认一次：

- App 能稳定运行五分钟。
- 能搜索并打开一只测试股票。
- 能看到大单净量、大单金额、散户数量和 MACDFS 对应页面数据。
- 页面可以滚动到底部图表。

项目不会自动处理这些登录或权限步骤。

#### 第四步：启动 Frida Server

```sh
adb -s emulator-5554 root
adb -s emulator-5554 push /绝对路径/frida-server-16.7.19-android-arm64 /data/local/tmp/ths-frida-server
adb -s emulator-5554 shell chmod 0755 /data/local/tmp/ths-frida-server
adb -s emulator-5554 shell '/data/local/tmp/ths-frida-server >/dev/null 2>&1 &'
adb -s emulator-5554 forward tcp:27042 tcp:27042
```

Frida Server 和端口转发在模拟器重启后可能需要重新执行。当前项目不会自动把 Frida Server 安装成永久开机服务。

不要运行 `adb -a`，也不要把 ADB 5037 或 Frida 27042 公开映射到局域网或互联网。

#### 第五步：恢复 Docker 数据卷

如果需要保留旧数据，在第一次启动 Compose 之前恢复：

```sh
for volume_name in redis-data capture-data template-data admin-data; do
  docker volume create "ths-level2_${volume_name}" >/dev/null
  docker run --rm \
    -v "ths-level2_${volume_name}:/restore" \
    -v "$PWD/migration-backup/volumes:/backup:ro" \
    alpine \
    sh -c "tar -xzf /backup/${volume_name}.tgz -C /restore"
done
```

如果只需要全新环境，可以跳过 `redis-data` 和 `capture-data`；如果存在手工校准模板，应至少恢复 `template-data`。

#### 第六步：构建并启动服务

```sh
docker compose --env-file deploy/macos.env -f deploy/compose.yml up -d --build
```

Mac 上必须使用 `deploy/macos.env`。如果直接使用默认 Compose 配置，API 会错误地等待 Linux 环境中的 `redroid:5555`。

### 2.7 迁移后的验收

先检查基础状态：

```sh
adb devices -l
adb -s emulator-5554 shell pidof com.hexin.plat.android
adb -s emulator-5554 forward --list
docker compose --env-file deploy/macos.env -f deploy/compose.yml ps
curl -fsS http://127.0.0.1:8000/openapi.json >/dev/null
```

验收标准：

- ADB 中的模拟器状态为 `device`。
- 同花顺进程正在运行。
- 27042 转发存在，Frida Server 版本为 `16.7.19`。
- `api` 和 `redis` 容器均为 healthy。
- `http://127.0.0.1:8000/` 能打开采集页面。
- `http://127.0.0.1:8000/#admin` 能登录并查看设备画面。
- 提交纯数据任务后，不切换 App 页面也能返回数据。
- 提交带长截图任务后，能返回八个字段和完整长截图。
- 新提交任务后，浏览器 URL 不被改写为 `?job=...`。

可用接口做最小验证：

```sh
curl -sS http://127.0.0.1:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"601872","include_long_capture":false}'
```

确认纯数据模式后，再在网页提交一次勾选“生成整页长截图”的任务。长截图真实验收不能只看 HTTP 200，必须打开图片，确认从股票页顶部覆盖到底部图表，并且包含“大单净量”区域。

### 2.8 浏览器历史不会随 Docker 卷自动迁移

前端只在当前浏览器、当前 Origin 的 `localStorage` 中保存最多 50 个任务 ID，键名为：

```text
ths_level2_job_history
```

因此，即使恢复了 Redis，换 Mac、换浏览器、换主机名或换端口后，采集历史列表也可能为空。服务器数据没有丢失，只是新浏览器没有对应任务 ID。

如果需要保留列表，可在旧浏览器开发者工具中读取：

```js
localStorage.getItem('ths_level2_job_history')
```

然后在新浏览器的相同 Origin 下恢复该值。任务元数据超过 7 天后仍会被服务器清理，已经不存在的任务 ID 会在前端恢复时自动移除。

## 3. 本项目数据采集逻辑

### 3.1 总体流程

```text
浏览器提交股票代码
        ↓
POST /api/v1/jobs
        ↓
Redis 保存任务并加入 FIFO 队列
        ↓
单 Runner 将任务从 QUEUED 原子切换到 RUNNING
        ↓
执行每日人工门禁检查
        ↓
根据 include_long_capture 选择采集路径
        ├─ false：Frida 直接调用 App 内部数据管理器
        └─ true ：ADB/uiautomator2 操作页面 + Frida 读值 + 必要时 OCR
        ↓
结果和状态写回 Redis，长截图写入 capture-data
        ↓
浏览器通过 SSE 收到状态变化，再 GET 最新结果
```

### 3.2 任务提交和排队

前端调用：

```http
POST /api/v1/jobs
Content-Type: application/json

{
  "symbol": "601872",
  "include_long_capture": true
}
```

规则如下：

- `symbol` 会去除首尾空格并转为大写。
- 允许 1～16 位字母、数字、`.`、`_`、`-`。
- 纯数据模式要求传入可确认市场的六位 A 股代码。
- 每个任务生成随机公开 ID，初始状态为 `QUEUED`。
- Redis 以 FIFO 顺序保存待处理任务，最多允许 200 个处于 `QUEUED`、`RUNNING` 或 `WAITING_ADMIN` 的任务；超过后 API 返回 429。
- Runner 只有一个，通过 Redis Lua 脚本原子领取任务，领取后状态变为 `RUNNING`。

任务状态变化会写入 Redis Stream。浏览器订阅 `/api/v1/jobs/{id}/events` 的 SSE 状态流；收到变化后再读取 `/api/v1/jobs/{id}` 获取完整结果。

### 3.3 每日人工门禁检查

Runner 每次处理任务前，会查看 `/data/admin/daily-check.json`。如果北京时间当天还没有通过检查，系统会读取当前 Android 页面文本，检查是否出现：

- 登录
- 验证码
- 设备验证
- 人机验证
- 暂无权限
- 开通

如果出现上述内容：

1. 当前任务变为 `WAITING_ADMIN`。
2. 整个队列自动暂停。
3. Runner 状态变为 `NEEDS_ADMIN`。
4. 管理员需要进入 `/#admin`，取得设备控制权并手工处理。
5. 释放设备锁后，队列不会自动恢复；需要显式恢复等待任务并恢复队列。

管理员取得设备锁时也会自动暂停新任务领取，避免人工操作和自动采集同时控制设备。

### 3.4 纯数据模式：不生成长截图

条件：

```json
{"include_long_capture": false}
```

这条路径不会执行股票搜索、页面切换、滚动、截图、拼接或 OCR。实际流程为：

1. 根据股票代码前缀确定同花顺内部市场代码：
   - `600/601/603/605/688/689` → `17`，上海市场。
   - `000/001/002/003/300/301` → `33`，深圳市场。
   - `920` → `151`，北京市场。
2. FastAPI 容器中的 Frida 客户端通过 `host.docker.internal:27042` 连接模拟器内的 Frida Server。
3. Frida 附加到已运行的 `com.hexin.plat.android` 进程。
4. 注入脚本调用同花顺 App 自身的曲线/指标管理对象，由 App 自己执行正常的认证、签名和行情请求。
5. 等待 App 回调并读取已经解析好的报价与指标对象。
6. 校验回调中的股票代码与任务代码一致，避免读取到缓存中的其他股票。
7. 格式化八个结果字段并写回 Redis。

未知市场前缀不会猜测，而是直接返回 `UNSUPPORTED_MARKET`。App 未运行、Frida 不可用、内部管理器缺失或请求超时，也会以明确错误码结束任务，不会自动切换到 UI 截图路径。

### 3.5 长截图模式：页面操作、拼接和取值

条件：

```json
{"include_long_capture": true}
```

此模式默认开启，流程分为页面导航、长截图生成和数值读取三部分。

#### 页面导航

1. 通过 ADB 启动同花顺 `com.hexin.plat.android/.LogoEmptyActivity`。
2. 最多按返回键八次回到 App 首页。
3. 优先通过 uiautomator2 的资源 ID 点击首页搜索入口。
4. 资源 ID 不可用时，尝试点击“搜索”文本；仍失败时才使用 `template-data` 中的 OpenCV 模板。
5. 输入标准化后的股票代码。
6. 等待搜索结果中出现完全相同的代码。
7. 要求完全匹配的结果只有一个，避免点错股票。
8. 打开股票页面，并等待标题控件出现。

股票页面导航最多重试三次。

#### 长截图生成

1. 从当前股票页面截取一张 1080×1920 PNG。
2. 使用固定滑动手势向下滚动图表区域。
3. 每次滑动后等待页面稳定，再截下一张图。
4. 对比前后截图内容，计算真实纵向偏移。
5. 当页面不再滚动时结束，最多采集六帧。
6. 拼接时顶部固定栏和底部固定栏只保留一次；中间图表区域按计算出的偏移覆盖拼接。
7. 用中文 OCR 检查最终图片是否包含“净量”。校验失败时重新打开股票并再采集一次。
8. 两次都缺少“大单净量”区域时，任务以 `NAVIGATION_FAILED` 结束。
9. 成功图片保存到：

```text
/data/captures/{task_id}/LONG.png
```

对应宿主数据位于 `ths-level2_capture-data` 卷。

#### 数值读取和 OCR 降级

长截图生成后，系统优先使用 Frida 扫描当前 App 已解析的对象，读取与任务股票代码匹配的数据：

| 返回字段 | App 数据来源/映射 |
| --- | --- |
| 股票名称 | Quote 扩展字段 `55` |
| 当前股价 | Quote 字段 `10` |
| 当前涨跌幅 | 字段 `34315`；缺失时按现价和昨收计算 |
| 换手率 | 字段 `34312` |
| 大单净量 | `techid 7031`，数据字段 `33007` |
| 大单金额 | `techid 7032`，数据字段 `33015`，结果除以 10000 后显示为“万” |
| 散户数量 | `techid 7034`，数据字段 `216` |
| MACDFS | `techid 7051`，数据字段 `36883`，读取最新点 |

OCR 不是主数据源。只有 Frida 缺少以下某个图表指标时，才会从已截取的图表帧中裁剪对应区域并用 Tesseract 补值：

- 大单净量
- 大单金额
- 散户数量

股票名称、股价、涨跌幅、换手率和 MACDFS 没有 OCR 兜底。只要八个字段中有任意字段为空，任务会标记为 `PARTIAL`，错误码为 `VALUE_RECOGNITION_FAILED`；长截图仍然可以是可用状态。

### 3.6 结果状态和常见错误码

| 状态/错误码 | 含义 | 处理方式 |
| --- | --- | --- |
| `QUEUED` | 已进入 FIFO 队列 | 等待单 Runner 领取 |
| `RUNNING` | 正在采集 | 不要同时人工控制设备 |
| `WAITING_ADMIN` | 登录、验证或权限需要人工处理 | 管理页面接管设备，处理后恢复任务和队列 |
| `COMPLETED` | 八个字段完整；如果请求截图，截图也已生成 | 正常完成 |
| `PARTIAL` / `VALUE_RECOGNITION_FAILED` | 截图可能已生成，但部分字段缺失 | 检查 Frida、App 页面和 OCR 校准 |
| `UNSUPPORTED_MARKET` | 纯数据任务的市场前缀未确认 | 使用支持的六位 A 股代码 |
| `DIRECT_APP_OFFLINE` | 同花顺进程未运行 | 启动 App 并保持登录 |
| `DIRECT_REQUEST_TIMEOUT` | App 内部数据请求超时 | 检查网络、App 状态和 Frida |
| `NAVIGATION_FAILED` | 搜索、页面、滚动、拼接或长图校验失败 | 管理页面观察设备，检查 App UI 是否变化 |
| `DEVICE_OFFLINE` | ADB 设备不可用 | 检查 AVD、ADB Server 和 `ADB_SERIAL` |
| `CAPTURE_STORAGE_FAILED` | 长截图写入卷失败 | 检查卷挂载、空间和权限 |
| `EXPIRED` | 截图已超过 24 小时 | 重新采集 |

失败任务可以从管理页面重试。`WAITING_ADMIN` 任务需要由管理员处理后重新入队。

### 3.7 数据保留和前端历史

- 长截图文件和下载链接保留 24 小时。
- 后台每 60 秒执行一次清理。
- 截图过期后文件会从 `capture-data` 删除，任务状态变为 `EXPIRED`。
- 任务元数据从创建时间起保留 7 天，之后从 Redis 删除。
- 前端在浏览器 `localStorage` 中最多保存 50 个任务 ID，新任务排在最前。
- 刷新页面后，前端逐个调用任务查询接口恢复记录。
- 已不存在的 404 任务会自动从本机历史中移除。
- 对尚未结束的任务，前端继续订阅 SSE 状态更新。
- 长截图在前端默认折叠，只有用户展开时才加载图片。
- 提交任务不会改写当前页面 URL；旧的 `?job=` 单任务链接仍保留兼容读取能力。

## 4. 日常运行注意事项

1. 模拟器、ADB Server、Frida Server、Docker API 和 Redis 必须同时可用。
2. 模拟器重启后，优先检查 Frida Server 和 27042 端口转发。
3. Mac 部署始终使用：

   ```sh
   docker compose --env-file deploy/macos.env -f deploy/compose.yml up -d api
   ```

4. 管理员接管设备会暂停队列；释放锁后还需要显式恢复队列。
5. 不要把 5037、27042、6379 暴露到公网。
6. 当前默认使用 HTTP，管理员密码、Cookie、设备画面和输入事件不会被传输层加密，只适合可信局域网。需要公网访问时，应在可信 HTTPS 反向代理后使用，并把 `ADMIN_COOKIE_SECURE` 改为 `1`。
7. 升级和重建容器时保留 Docker 卷，不要使用 `down -v`。
8. 构建成功、容器启动或 HTTP 200 都不等于采集可用；必须用真实 APK、真实登录状态、纯数据任务和长截图任务完成验收。

## 5. 交接完成清单

- [ ] 当前未提交源码已完整提交或复制。
- [ ] `.env` 已安全迁移并保持 `0600`。
- [ ] `deploy/macos.env` 已迁移并核对序列号、端口和 Frida 地址。
- [ ] APK SHA-256 预检通过。
- [ ] Frida Server 文件版本为 `16.7.19`。
- [ ] 需要保留的四个 Docker 卷已备份并验证校验值。
- [ ] 新 Mac 为 Apple Silicon，资源和工具预检通过。
- [ ] AVD 已创建，同花顺已由管理员正常登录。
- [ ] Frida Server 已启动，27042 转发存在。
- [ ] API 和 Redis 容器 healthy。
- [ ] 管理页面可以登录并接管设备。
- [ ] 纯数据任务完成且没有切换 App 页面。
- [ ] 长截图任务返回八个字段和完整图片。
- [ ] 长截图包含“大单净量”区域并覆盖到底部图表。
- [ ] 新任务不会改变当前网页 URL。
- [ ] 旧 Mac 保留到新环境连续稳定运行并验收完成后再下线。
