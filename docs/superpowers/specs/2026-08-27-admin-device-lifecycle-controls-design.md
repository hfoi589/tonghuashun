# 管理员双设备生命周期与完整镜像部署设计

日期：2026-08-27
状态：已批准

## 1. 背景

管理员页面当前能够同时显示 `core_metrics` 和 `main_fund_flow` 两台 Android
模拟器的画面及 ADB、App、Frida 状态，也能在管理员取得设备锁后转发触摸和
按键输入。但是服务端只能通过宿主 ADB 访问已经运行的设备，不能从 OrbStack
Linux 容器内执行 macOS 的 Android Emulator 或 `launchctl`，因此目前没有可靠的
虚拟机启动、关闭入口。

用户明确授权两台设备都增加以下操作：

- 关闭虚拟机；
- 启动虚拟机并打开同花顺。

该授权只放开上述两个固定生命周期动作，不放开退出账号、切换账号、清数据、
克隆 AVD、安装或卸载 App、`force-stop`、自动搜索或其他 App 内导航。

用户随后要求把已验证 APK 封装进完整 Docker 镜像，并同时支持：

- 当前 Mac 保留双 AVD 和登录状态的一键重部署；
- 全新 Apple Silicon Mac 的交互式首次开通流程。

“首次开通”可以自动准备固定 AVD、APK、Frida 和服务，但账号登录、验证码、
设备验证、业务权限确认和 Android/第三方许可接受仍必须由用户人工完成。

## 2. 目标

- 在管理员页面的每张设备卡中提供独立的“关闭虚拟机”和
  “启动虚拟机并打开同花顺”按钮。
- 支持 `THS_CORE_33_ARM64 / emulator-5556` 和
  `THS_API_33_ARM64 / emulator-5554`。
- 操作必须经过现有管理员登录、CSRF 和当前会话设备锁。
- 启动过程恢复 ADB、Frida 和端口转发，并只打开同花顺入口 Activity。
- 关闭过程使用 Android Emulator 的正常关机协议，不使用 App `force-stop`。
- 控制服务不可接收任意命令、可执行文件、AVD 名、serial、端口或 shell 参数。
- 主机控制服务不可用时，只禁用生命周期按钮，不影响公开 Market、直连任务、
  Redis、管理登录或其他 API。
- Docker 镜像内置固定 APK、Frida Server 和部署辅助资产，构建时验证哈希。
- 提供一个幂等入口，自动识别已有 AVD 重部署、缺失 AVD 首次开通和混合状态。
- 已存在 AVD 永不被删除、替换、wipe 或自动重装 App；新创建 AVD 才安装镜像资产。
- 镜像默认只在本机或私有环境使用，不自动推送、导出或发布到公共 Registry。

## 3. 非目标

- 不自动登录同花顺，不输入账号密码，不处理验证码或设备验证。
- 不在启动后自动进入股票页、资金页或任何业务页面。
- 不改变 9528 和资金 HTTP 直连的会话模型。
- 不让浏览器直接连接 macOS 主机控制服务。
- 生命周期管理 API 不创建、复制、删除或重置 AVD；首次开通脚本只允许创建缺失的
  两个固定 AVD，绝不替换或重置已经存在的 AVD。
- 不允许调用方自定义 ADB 命令或 Emulator 参数。
- 不封装账号密码、Cookie、认证包、加密会话包、AVD 数据目录或已登录快照。
- 不把全新 Mac 宣称为无人值守可用；首次登录与验证始终是人工关口。

## 4. 已验证的环境事实

- API 容器通过 `ADB_SERVER_SOCKET=tcp:host.docker.internal:5037` 使用宿主 ADB。
- 容器内只有 Linux `adb`，不能执行 macOS Emulator 或 `launchctl`。
- 现有 `bootstrap-macos-dual-avd.sh` 已包含 AVD 启动、等待
  `sys.boot_completed=1`、核心设备显示校准、Frida 转发和打开同花顺的参考流程，
  但它还承担创建/安装等初始化职责，不能直接暴露给管理 API。
- 已用临时只读探测验证：OrbStack API 容器能够通过
  `host.docker.internal` 访问仅监听 macOS `127.0.0.1` 的 HTTP 服务。
- `ths_android_V11_59_03.apk` 已存在于项目历史，大小 214088292 bytes，SHA-256
  为 `2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e`，
  包含 `arm64-v8a` 和 `armeabi-v7a`；当前 `.dockerignore` 的 `*.apk` 仍将其排除
  在镜像构建上下文之外。
- 官方 `frida-server-16.7.19-android-arm64.xz` 大小 15972776 bytes，SHA-256
  为 `36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581`；
  解压后大小 53702368 bytes，SHA-256 为
  `4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a`。
- 当前 API 镜像约 597 MB；加入 APK 和解压后的 Frida Server 后预计约
  800–850 MB。

## 5. 方案比较

### 方案 A：macOS 回环生命周期服务（采用）

在 macOS 上以 LaunchAgent 运行一个最小 HTTP 服务，只监听 `127.0.0.1`，使用
独立随机 Token，并只接受固定的角色和动作。FastAPI 作为认证代理调用该服务。

优点：

- 能从管理员页面实时触发操作；
- 浏览器不接触主机 Token；
- 角色、AVD 和命令可在主机服务中强制白名单；
- 操作状态可以异步查询，浏览器刷新后仍可恢复显示；
- macOS 重启后由 LaunchAgent 自动恢复服务。

代价：新增一个受控主机服务和安装步骤。

### 方案 B：共享目录命令队列

API 写入 bind mount 目录，macOS watcher 读取命令文件并写回结果。

优点是不监听端口；缺点是文件队列、原子性、过期命令、结果回收和权限管理更
复杂，启动进度反馈也更迟钝。因此不采用。

### 方案 C：浏览器直接调用主机服务

实现量较少，但会把主机控制 Token 或等价能力暴露给浏览器，并绕过现有管理会话
和 CSRF 边界。因此禁止采用。

## 6. 总体架构

```text
管理员浏览器
  │  管理会话 + CSRF
  ▼
FastAPI /api/admin/devices/{role}/actions
  │  固定 role/action + Bearer Token
  ▼
macOS 127.0.0.1:18765 生命周期服务
  │  固定白名单映射
  ├─ core_metrics     → THS_CORE_33_ARM64 / emulator-5556 / 5556 / 27043
  └─ main_fund_flow   → THS_API_33_ARM64 / emulator-5554 / 5554 / 27042
       │
       ├─ Android Emulator / launchctl
       ├─ 宿主 adb
       └─ Frida server 与端口转发
```

FastAPI 和浏览器均不得提交 AVD 名、serial、端口、Activity 或 shell 参数。主机
服务从本机权限为 `0600` 的配置文件读取固定映射和 Token。

## 7. macOS 生命周期服务

### 7.1 进程与安装

- 新增 `scripts/macos-device-lifecycle.py`，只使用 Python 标准库。
- 新增 `scripts/install-macos-device-lifecycle.sh`，安装并加载
  `~/Library/LaunchAgents/com.ths.device-lifecycle.plist`。
- 安装脚本把 lifecycle 服务和 bridge watcher 复制到
  `~/.local/lib/ths-device-lifecycle/`；LaunchAgent 只引用该稳定安装目录，不引用
  仓库 checkout 或临时 Codex worktree。
- Token 和本机路径写入 `~/.config/ths-device-lifecycle.env`，权限固定为 `0600`；
  plist 不包含 Token。
- 服务只监听 `127.0.0.1:18765`。
- 日志只能包含角色、动作、安全状态和固定错误码，不记录 Token、完整命令输出、
  账号信息、Cookie、协议材料或文件内容。

### 7.2 主机 API

请求必须带 `Authorization: Bearer <token>`。

```http
GET /v1/devices
POST /v1/devices/{role}/actions
GET /v1/operations/{operation_id}
```

动作请求体只有两个合法值：

```json
{"action":"start_and_launch_app"}
{"action":"shutdown"}
```

响应只包含：

- `operation_id`；
- `role`；
- `action`；
- `state`；
- `error_code`；
- `updated_at`。

不返回 serial、AVD 名、端口、命令或 stderr。

### 7.3 生命周期状态

每个角色独立维护：

- `UNCONFIGURED`：主机配置缺失或角色映射无效；
- `UNKNOWN`：暂时不能可靠判断；
- `STOPPED`：宿主确认 Emulator 进程不存在；
- `STARTING`：已接受启动，尚未完成 Android boot；
- `RUNNING`：Android 已 boot complete；
- `STOPPING`：已发送正常关闭命令，正在等待退出；
- `ERROR`：最近一次操作失败。

同一角色同一时间只允许一个操作。不同角色可以并行，但管理员页面默认按顺序操作，
降低宿主资源峰值。

### 7.4 启动流程

`start_and_launch_app` 必须幂等：

1. 获取该角色的进程内互斥锁；
2. 若设备已经 `device + boot_completed=1`，跳过 Emulator 启动；
3. 验证固定 AVD 已存在，缺失时失败，绝不创建替代 AVD；
4. 用固定参数通过 `launchctl submit` 启动对应 AVD；
5. 等待固定 serial 进入 `device`；
6. 等待 `sys.boot_completed=1`；
7. `core_metrics` 执行既有 1080×1920 / 480 dpi 校准；
8. 仅对当前角色恢复 root Frida、固定端口转发和 watcher；
9. 执行固定 Activity：
   `com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity`；
10. 验证 App 进程已运行，状态变为 `RUNNING`。

对 `main_fund_flow`，第 9 步只允许打开入口 Activity，不允许任何后续点击、滑动、
搜索或页面导航。

### 7.5 关闭流程

`shutdown` 必须幂等：

1. 获取该角色互斥锁；
2. 若已确认 `STOPPED`，直接返回成功；
3. 对固定 serial 执行 `adb -s <serial> emu kill`；
4. 等待 serial 从宿主 Emulator/ADB 状态中消失；
5. 状态变为 `STOPPED`。

禁止使用：

- `am force-stop`；
- `reboot -p`；
- `pm clear`；
- `install`、`uninstall`；
- AVD 创建、复制、删除或 wipe-data；
- 账号退出、切换或任何凭据输入。

## 8. FastAPI 集成

### 8.1 配置

新增：

```text
THS_DEVICE_LIFECYCLE_URL=http://host.docker.internal:18765
THS_DEVICE_LIFECYCLE_TOKEN=<独立随机值>
THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS=5
```

URL 或 Token 缺失时，服务正常启动，设备生命周期状态为 `UNCONFIGURED`，按钮禁用。

### 8.2 服务边界

新增 `level2_service/device_lifecycle.py`：

- `DeviceLifecycleClient`；
- 安全响应模型；
- 固定动作枚举；
- HTTP 超时与状态查询；
- 错误码白名单和异常脱敏。

FastAPI 不执行 Emulator 命令，也不解析或转发主机命令输出。

### 8.3 管理 API

扩展：

```http
GET /api/admin/devices
POST /api/admin/devices/{role}/actions
```

动作请求仍只有：

```json
{"action":"start_and_launch_app"}
{"action":"shutdown"}
```

动作接口必须：

- 验证管理员会话；
- 验证 CSRF；
- 验证当前会话已取得设备锁；
- 验证队列处于暂停状态；
- 拒绝存在仍在执行的设备任务；
- 验证角色只能是 `core_metrics` 或 `main_fund_flow`；
- 将固定 role/action 转发给主机服务；
- 只返回安全状态和错误码。

取得设备锁会继续沿用现有行为：暂停 Runner 领取新任务；释放锁不会自动恢复队列。

### 8.4 错误码

- `DEVICE_LIFECYCLE_UNAVAILABLE`：主机服务未配置或不可达；
- `DEVICE_LIFECYCLE_LOCK_REQUIRED`：当前会话未取得设备锁；
- `DEVICE_LIFECYCLE_BUSY`：Runner 或设备任务仍在执行；
- `DEVICE_ACTION_IN_PROGRESS`：同角色已有操作；
- `DEVICE_AVD_NOT_FOUND`：固定 AVD 不存在；
- `DEVICE_BOOT_TIMEOUT`：Android 未在时限内完成启动；
- `DEVICE_APP_LAUNCH_FAILED`：同花顺未成功启动；
- `DEVICE_SHUTDOWN_FAILED`：Emulator 未正常退出；
- `DEVICE_LIFECYCLE_FAILED`：其他已脱敏失败。

任何主机异常、命令输出或 Token 都不得进入 HTTP detail、日志或任务记录。

## 9. 管理员前端

### 9.1 设备卡

沿用当前双列、窄屏单列的设备卡，不重新设计管理台。每张卡新增“虚拟机控制”行：

- 主按钮：`启动虚拟机并打开同花顺`；
- 危险次按钮：`关闭虚拟机`；
- 生命周期状态：`未配置 / 待检测 / 已关闭 / 启动中 / 运行中 / 关闭中 / 异常`；
- 卡内 `aria-live="polite"` 进度文案；
- 卡内 `role="alert"` 失败文案。

按钮状态：

- 未取得设备锁：两个按钮禁用，并提示“请先接管设备”；
- `STARTING` 或 `STOPPING`：两个按钮禁用；
- `STOPPED`：启动按钮启用，关闭按钮禁用；
- `RUNNING` 且 App 离线：启动按钮保持启用，用于只打开同花顺；
- `RUNNING` 且 App 在线：启动按钮禁用，关闭按钮启用；
- `UNCONFIGURED`：两个按钮禁用；
- `ERROR` 或 `UNKNOWN`：允许刷新状态，不凭 WebSocket 离线直接认定 VM 已关闭。

### 9.2 确认对话框

两个动作都使用可访问的自定义 `alertdialog`，不使用 `window.confirm`：

- `aria-modal="true"`；
- 标题和描述通过 `aria-labelledby` / `aria-describedby` 关联；
- 打开时默认聚焦“取消”；
- Escape 关闭；
- Tab 焦点陷阱；
- 操作提交后禁用对话框按钮；
- 完成后焦点返回原按钮。

关闭确认：

> 关闭“{设备名称}”虚拟机？这会中断该设备画面以及需要设备的会话刷新或长截图，
> 但不会退出账号、清除数据或影响另一台设备。

启动确认：

> 启动“{设备名称}”虚拟机并打开同花顺？启动完成后只打开同花顺首页；如遇验证码、
> 登录或设备验证，请在设备画面中人工处理。

资金设备额外显示：

> 资金账号受保护：该操作不会切号、清数据、重装 App 或执行页面导航。

### 9.3 状态更新

- 管理页面登录后每 2 秒轮询设备生命周期状态；
- 操作进入终态后恢复现有 15 秒普通健康刷新；
- 设备画面 WebSocket 继续只负责帧、输入和 ADB/App/Frida 状态；
- 生命周期函数状态不能由 WebSocket 连接状态推断。

## 10. 安全与并发

- 两个角色均由用户明确授权启动和正常关闭。
- 资金设备的其他保护规则保持不变。
- 生命周期操作必须持有管理员设备锁，避免 Runner 与管理员同时操作设备。
- 主机服务按角色加锁；同角色重复动作返回冲突，不执行第二次命令。
- 启动与关闭均幂等。
- 操作过程中不自动恢复任务队列；管理员需交还控制并显式恢复队列。
- Token 与管理员密码、会话加密密钥分离。
- API、日志、前端状态和异常只使用固定错误码。
- 主机服务拒绝非回环监听配置、未知角色、未知动作和额外请求字段。

## 11. 测试策略

### 11.1 主机服务单元测试

使用 fake command runner，不操作真实 AVD：

- 两角色固定映射；
- start/shutdown 幂等；
- boot 等待与超时；
- App 启动验证；
- 同角色互斥；
- Token 校验；
- 响应和日志脱敏；
- 断言源码/命令中不存在 `force-stop`、`pm clear`、install/uninstall、wipe-data；
- 断言请求不能覆盖 AVD、serial、端口或命令参数。

### 11.2 FastAPI 测试

- 未登录返回 401；
- 缺少或错误 CSRF 返回 403；
- 未持设备锁返回 409；
- Runner 忙返回 409；
- 两个合法角色均可提交两个动作；
- 未知角色或动作被拒绝；
- 同角色操作中返回 409；
- 主机服务不可用返回 503；
- 主机超时和固定错误码映射；
- detail、日志和设备响应不泄漏 Token、serial、命令或主机异常文本。

### 11.3 前端测试

- 两张设备卡都出现两个按钮；
- 未接管、未配置、启动中和关闭中状态正确禁用；
- core 与 fund 请求使用正确 role；
- 确认取消不发请求；
- 确认后发送 CSRF 和固定 action；
- 进度、成功和错误反馈位于对应设备卡；
- 对话框焦点、Escape、Tab 陷阱和焦点恢复；
- 窄屏按钮完整显示且不横向滚动；
- 资金设备保护说明始终存在。

## 12. 部署与真实验收

部署顺序：

1. 生成独立生命周期 Token；
2. 安装并加载 macOS LaunchAgent；
3. 从 API 容器验证主机 `/v1/devices` 可达；
4. 配置 Compose URL、Token 和超时；
5. 使用标准 OrbStack 命令重建 API；
6. 登录管理员页面并取得设备锁。

真实验收按顺序执行，禁止同时启停两台设备：

1. 对 `core_metrics` 点击关闭，确认状态变为 `STOPPED`；
2. 在 core 关闭期间提交 `include_long_capture=false` 的直连任务，确认仍为完整结果；
3. 点击启动，确认 AVD boot、显示校准、Frida 转发和同花顺入口页恢复；
4. 确认核心账号登录状态未丢失；
5. 对 `main_fund_flow` 重复正常关闭和启动；
6. 确认资金账号仍保持原登录账号，没有退出、切号或清数据；
7. 再提交完整直连任务，确认八项指标、三条曲线和三周期资金流；
8. 扫描 API、主机服务日志和管理响应，确认无敏感信息；
9. 确认 Redis、market、admin、session 和 capture 卷均未删除。

## 13. 回滚

- 将 `THS_DEVICE_LIFECYCLE_URL` 和 Token 从 Compose 环境移除，按钮自动进入
  `UNCONFIGURED`；
- 卸载 `com.ths.device-lifecycle` LaunchAgent；
- 不删除 AVD、登录数据、会话包或 Docker 数据卷；
- 公开 Market 和直连采集继续运行；
- 必要时按现有人工方式启动 AVD。

## 14. 完整 Docker 镜像资产

### 14.1 镜像内容

镜像固定包含：

- `/opt/ths/assets/ths.apk`；
- `/opt/ths/assets/ths-frida-server`；
- `/opt/ths/assets/manifest.json`；
- lifecycle、bridge、显示校准和首次开通所需的只读脚本副本。

`manifest.json` 只记录版本、文件大小、APK SHA-256、APK 支持 ABI、Frida 版本和
Frida SHA-256，不包含路径、账号或秘密。

Dockerfile 必须：

- 只对白名单文件 `ths_android_V11_59_03.apk` 解除 `.dockerignore`；
- 使用 `COPY --chmod=0444` 复制 APK；
- 在独立资产 stage 从固定官方 URL 下载 Frida `16.7.19`；
- 在构建时校验两个固定 SHA-256，任何不一致都终止构建；
- 解压 Frida 后设置 `0555`，资产目录不可写；
- 使用 OCI label 记录资产版本和摘要；
- 不复制 `.env`、`deploy/macos.env`、session、AVD、capture、Redis 或日志。

镜像构建参数不得覆盖 APK/Frida URL、摘要或文件名。升级必须修改受审查的源码常量、
manifest 和测试，不能通过部署环境临时替换。

### 14.2 分发边界

默认只构建本机镜像 `ths-level2-api:local`。一键脚本不得执行 `docker push`、
`docker save` 或创建公共发布。若未来需要共享 Registry，必须单独取得 APK 分发授权
并建立私有仓库访问控制、镜像签名和删除策略。

## 15. 一键部署模式

统一入口：

```bash
scripts/deploy-macos-one-click.sh --mode auto
```

脚本只支持 Apple Silicon macOS，并始终使用显式 OrbStack context。

### 15.1 公共前置检查

所有模式先验证：

- Apple Silicon、OrbStack、Docker、Java 17、Android command-line tools；
- `adb`、`emulator`、`sdkmanager`、`avdmanager` 和可用磁盘；
- Android 33 ARM64 system image许可已由用户接受；
- APK/Frida 镜像资产摘要与 manifest 一致；
- `.env` 权限为 `0600`，并包含管理员秘密、会话加密密钥和 lifecycle Token；
- `deploy/macos.env` 的双角色配置完整；
- 不存在正在使用设备的任务。

磁盘前置检查按模式执行：已有 AVD `existing`（existing mode）模式要求项目文件系统、Android
`~/.android/avd` 文件系统以及 OrbStack 外部数据文件系统各至少有 8 GiB 可用空间；
`provision`（provisioning mode）模式在这三个文件系统上各要求至少 30 GiB。OrbStack 数据文件系统只读解析
自 `~/.orbstack/vmconfig.json` 的绝对 `data_dir`，缺失、格式错误、相对路径或路径不存在
时返回固定错误，不接受环境变量覆盖。所有三项检查必须在镜像构建、journal 写入或
AVD 创建之前完成。

缺少 OrbStack、Java、Android 工具或未接受许可时，脚本以固定错误码退出并打印人工
安装/许可命令；不自动安装 Homebrew、OrbStack，不自动接受许可。

### 15.2 已有双 AVD 重部署

当两个固定 AVD 都存在时：

1. 绝不创建、删除、复制或重置 AVD；
2. 构建包含资产的本机镜像；
3. 校验两台设备已安装同花顺，读取已安装 base APK 的 SHA-256；
4. 若摘要不等于镜像 APK，返回 `INSTALLED_APK_MISMATCH`，不自动覆盖安装；
5. 安装/更新稳定 host lifecycle 和 bridge LaunchAgent；
6. 通过 lifecycle 服务启动已关闭的 AVD 并只打开同花顺入口页；
7. 使用标准 OrbStack Compose 命令重建 API/Redis，保留全部卷；
8. 验证 API、Redis、双角色设备、直连会话和数据任务。

该模式不执行 `adb install`、`install -r` 或任何 App 数据变更，因此保留两个账号的
登录状态。

### 15.3 全新或部分缺失 AVD 首次开通

当一个或两个固定 AVD 不存在时，`auto` 进入交互式 provisioning：

1. 记录每个角色在开始时是否已经存在；
2. 仅使用固定 Android 33 ARM64 system image 创建缺失角色；
3. 已存在角色继续执行摘要校验，绝不重装或重置；
4. 启动新创建 AVD，等待 `boot_completed=1`；
5. 通过一次性资产容器，仅对新创建角色安装 `/opt/ths/assets/ths.apk`；
6. 仅对新创建角色推送固定 Frida Server，并恢复固定端口转发；
7. 校准 core 显示，打开两台设备的同花顺入口页；
8. 启动 Web/API/Redis，使管理员页面可访问；
9. 对每个新角色暂停并要求用户人工登录、处理验证码/设备验证和确认权限；
10. 管理页面同时显示 core/fund 加密会话状态和刷新按钮；用户完成登录后分别刷新；
11. 两个会话均为 READY 后，运行完整直连验收。

若新建 AVD 安装或登录未完成，系统保持可恢复状态并返回
`FIRST_TIME_LOGIN_REQUIRED`，下次执行从现有状态继续，不删除已创建 AVD。

### 15.4 密钥与配置初始化

`.env` 不存在时，一键脚本调用增强后的 `scripts/setup-admin.sh`，交互式读取管理员
密码，并一次生成：

- `ADMIN_PASSWORD_HASH`；
- `ADMIN_SESSION_SECRET`；
- `THS_SESSION_ENCRYPTION_KEY`；
- `THS_DEVICE_LIFECYCLE_TOKEN`。

所有秘密只写入 `0600` 的 `.env`，不打印值。已有 `.env` 只补缺失的新键，绝不
重置管理员密码、会话加密密钥或 lifecycle Token。

## 16. 扩展测试与验收

自动测试新增：

- Dockerfile/.dockerignore 精确白名单和构建摘要失败测试；
- 资产 manifest、APK ABI、Frida 版本及禁止秘密文件测试；
- 一键脚本 `auto` 的已有、全新、部分缺失、版本不一致和恢复路径；
- 断言 existing 模式永不出现 install/clear/reset；
- 断言 provisioning 只向本次新建角色安装资产；
- 断言任何脚本都不 push/save 镜像、不自动接受许可；
- 管理页面同时刷新 core/fund 会话。

真实验收除第 12 节外还包括：

1. 在当前 Mac 运行 `--mode auto`，确认双 AVD ID、数据目录和登录状态未变化；
2. 确认镜像中的 APK/Frida 摘要与 manifest 一致；
3. 在隔离的临时配置/模拟命令环境验证首次开通脚本的全路径；
4. 真实新 Mac 首次开通仅在具备目标设备和人工登录条件时执行；没有该环境时不得
   宣称完成真实新 Mac 验收，只能报告自动测试和当前 Mac 结果。
