# Changelog

## 2.0.2 — 2026-11-27

修复 V2 与 1.3.x / 另一个 V2 实例同时运行时端口冲突和 WebView2 窗口退化的问题，并重做设备总览顶部的扫描提示。

### 动态端口

- 本地服务默认端口从固定 `18743` 改为 `0`，首次启动由系统分配空闲端口，并从 `server.server_address` 读回实际端口
- 实际端口记录到 `%LOCALAPPDATA%\NetworkLocator\config.json` 的 `v2_port`，与 1.3.x 的 `v1_port` 互不覆盖
- 重启时优先复用上次端口；端口被占用时自动回退到系统分配的随机端口，且不覆盖已保存的首选端口
- `--port` 与 `NETWORK_LOCATOR_PORT` 仍然优先，显式指定时使用指定端口且不静默回退
- 日志打印实际监听端口和两个 profile 目录名

### 实例隔离

- 启动时检测共享 WebView2 目录（`webview\EBWebView\lockfile`）和浏览器目录（`browser-profile\lockfile`）是否被其他实例占用
- 被占用时自动改用 `webview-<port>` / `browser-profile-<port>`，两个实例的原生窗口可以同时正常显示
- 新增每个 profile 的本地锁文件，避免两个实例在启动瞬间同时抢占共享目录
- 原生窗口状态和结果文件按实例区分，避免并发实例互相覆盖启动结果
- 与 1.3.10 同时运行时各自使用不同端口和 WebView2 目录

### 设备总览提示

- 设备总览顶部的 5 条全宽黄色提示条改为一条紧凑的 info 折叠条
- 默认一行显示「扫描提示：N 个网卡/地址被跳过」，点击展开显示每条警告原文
- 展开状态在页面刷新后保留；提示信息不再截断（原来最多显示 5 条）
- 使用现有设计系统的浅色 info 小卡片样式，不再大面积使用黄色

### 测试与构建

- 新增端口配置（`v2_port` 与 `v1_port` 隔离）和实例 profile 隔离的单元测试
- 前端测试、Python 测试和 `build.ps1` 构建通过

## 2.0.1 — 2026-11-25

修复 Windows 启动失败：WebView2 原生窗口未能加载时，应用会直接退出且没有任何提示。

### 根因

- 本机 WebView2 控制器初始化会失败（E_ABORT / E_UNEXPECTED），pywebview 内部报错时 pythonnet 转换 .NET 异常会直接让进程崩溃（0xe0434352）
- 旧版把“窗口在加载完成前关闭”当成启动成功，于是进程以 0 退出、既没有窗口也不回退
- 旧的 CI 自测只校验打包内容，从不启动窗口或本地服务，因此测不出上述问题

### 修复

- 原生窗口改为独立子进程运行：即使 pythonnet/CLR 崩溃或弹框，也只影响子进程，主进程按超时回退到 Edge/Chrome 应用窗口
- 原生窗口只有在页面真正加载完成后才算启动成功；窗口在加载完成前关闭会判定为失败
- 原生窗口启动失败时自动回退到 Edge/Chrome 应用窗口，不再静默退出
- 启动超时从 15 秒放宽到 30 秒，失败后自动备份并重建 WebView2 缓存目录
- 下次启动检测到上次未成功加载时，先重建 WebView2 缓存再打开原生窗口
- 启动前检查 WebView2 缓存目录是否为可写目录，异常路径直接走回退
- 所有退出路径统一使用 `os._exit` 并且不依赖 `sys.stdout`，避免无控制台时退出阶段抛异常并弹出“Unhandled exception in script”
- 退出时跳过 pythonnet/CLR 清理阶段，避免 WebView2 初始化失败后触发 CLR 崩溃（0xe0434352）

### 测试与构建

- 新增 `--smoke-test`：启动本地服务 → 校验首页和 `/api/networks` 返回 HTTP 200 → 干净退出
- CI 构建新增启动冒烟测试，并在 Release 中附带 `smoke.json`
- 新增启动器单元测试，覆盖“窗口关闭但页面未加载必须判定为失败”的回归场景

## 2.0.0 — 2026-11-25

完整产品重构：从单一 MAC 后缀查询器升级为局域网设备发现与管理桌面应用。

### 信息架构与界面

- 全新应用主框架：左侧固定导航、顶部工具栏、主内容区和右侧详情抽屉
- 七个页面：设备总览、网络扫描、设备管理、MAC 定位、网络分析、扫描历史、系统设置
- 统一 Professional 浅色设计系统，主色 `#2563EB`、背景 `#F5F7FA`
- 统一按钮、输入框、表格、状态标签、卡片、空状态、加载状态、错误状态和抽屉
- 支持窗口缩放；窄窗口下导航切换为抽屉式菜单，页面无横向溢出
- 保留浅色/深色主题切换，并提供完整的键盘焦点与 ARIA 状态

### 数据模型与持久化

- 新增本机 SQLite 数据库 `%LOCALAPPDATA%\NetworkLocator\data\network-locator.db`
- 新增 `devices`、`device_addresses`、`network_interfaces`、`network_segments`、`scan_tasks`、`scan_results`、`device_events`、`settings` 表
- 设备使用 `device_id` 作为唯一标识，不以 IP 作为身份；优先按 MAC 合并档案
- 同一 MAC 换 IP 时更新当前地址并保留历史地址，同时记录 `ip_changed` 事件
- 同一 MAC 在同一轮扫描中出现多个 IP 时按冲突保留多个档案，避免误合并
- 手动名称、备注和收藏状态不会被后续扫描覆盖
- Schema 升级到 2，并清理旧数据库中由广播/组播 ARP 记录产生的无效设备

### 扫描与设备管理

- 扫描任务独立于页面生命周期运行，切换页面不会中断任务
- 支持多网卡、多网段选择、自定义 CIDR/IP 范围、实时结果、进度、暂停、恢复和停止
- 支持部分网段扫描，不会把未覆盖网段的设备误判为离线
- 增加广播地址、全零 MAC、全 FF MAC 和组播 MAC 的扫描层与持久化层双重过滤
- 设备管理支持全部/在线/离线/收藏筛选、搜索、排序、分页、批量选择和批量导出
- 设备详情抽屉支持名称、备注、收藏维护，并展示 IP 历史和设备事件
- 新增 MAC 完整地址、6 位前缀、4 位后缀和批量查询，自动识别格式并关联设备档案
- 新增网络接口、网段、DNS、设备分布、网关连通性和指定 IP Ping 延迟/丢包统计
- 新增扫描历史任务、范围、发现/新增/离线/IP 变化统计、任务详情和结果导出
- 新增设置页：扫描超时、并发、默认网卡、离线阈值、自动扫描、CSV、主题和数据位置

### 兼容与安全

- 保留旧版 `/api/scan/*`、`/api/devices` 和前端 CSV 测试兼容路径
- 保留 V1 WebView2 本地存储，不把旧 IP 缓存或常用后缀伪造成设备
- CSV 继续使用 UTF-8 BOM、可配置表头和备注列，并对公式注入单元格做转义
- 本地服务仍只监听 `127.0.0.1`，不开放局域网端口
- 扫描失败、停止和中断分别记录真实状态，不伪装成完成

### 测试与构建

- 后端单元测试扩展到 51 项，前端 Node 测试 35 项
- 构建脚本加入 V2 Python 文件、JavaScript 语法检查、Node 测试和 V2 静态资源自检
- README 更新为 V2 运行、数据、迁移、测试和打包说明

## 1.1.0 — 2026-11-24

窗口从浏览器应用窗口改为 WebView2 原生窗口。

### 变更

- 使用 pywebview + WebView2（`edgechromium`）打开原生窗口，标题为「网络定位 / Network Locator」，无浏览器地址栏与标签页
- 窗口与任务栏使用应用自己的图标（`assets/network-locator.ico`）
- 本地 HTTP 服务保持不变：仍为 `127.0.0.1:18743`，页面通过同源 URL 加载
- WebView2 存储目录改为 `%LOCALAPPDATA%\NetworkLocator\webview`，常用 MAC 与主题在原生窗口下同样保留
- 启动器进程设置 AppUserModelID，任务栏按应用归组
- `--selftest` 增加 pywebview 模块、应用图标与 WebView2 运行时报告
- 构建流水线显式收集 `webview.platforms.edgechromium`，并在自检中校验 pywebview 已打进 EXE

### 回退与安全网

- 找不到 WebView2 运行时、或原生窗口启动异常时，自动回退到 Edge/Chrome `--app` 应用窗口
- Edge/Chrome 也不可用时，使用默认浏览器打开，并弹出提示说明关闭提示即退出
- 原生窗口 15 秒内没有完成页面加载会被关闭并回退，避免停留在空白窗口
- 新增 `NETWORK_LOCATOR_GUI=auto|native|app-window|browser`，便于排查环境问题
- 新增 `PRIVACY.md`，补齐数据存放路径、上传说明、第三方组件、权限与卸载删除范围

## 1.0.0 — 2026-11-24

首个 Windows 单文件版本，功能对齐 TOS `network-locator` 1.3.7。

### 新增

- Windows 网卡发现：`GetAdaptersInfo`，并带有 PowerShell `Get-NetIPConfiguration` 回退
- 虚拟网卡过滤：Hyper-V、WSL、VMware、VirtualBox、Docker、Tailscale 等
- ARP 邻居表读取：`arp.exe -a -N`，兼容中英文 Windows 输出
- 并发 ping 扫描、UDP ARP 预热、进度与开始/停止状态机
- 本地 HTTP 服务 `127.0.0.1:18743`，复用 TOS 版 WebUI
- Edge/Chrome `--app` 应用窗口，回退默认浏览器
- 设备总表、筛选、排序、复制、CSV 导出、主题切换、工具提示、常用 MAC
- 日志与配置写入 `%LOCALAPPDATA%\NetworkLocator`
- `--selftest`、`--serve-only`、`--ready-file`、`--duration` 自动化验证入口

### 适配说明

- TOS 的 Unix socket 与 iframe 代理改为同源本地 HTTP
- `ip neigh` 的状态改为：ping 响应为 `REACHABLE`，仅 ARP 缓存为 `STALE`，本机为 `LOCAL`
- WebUI 中平台会话/代理相关提示改为本地服务提示
