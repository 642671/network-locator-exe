# Network Locator V2.0 重构报告

- 目标仓库：`network-locator-exe/`
- 版本：`2.0.0`
- 日期：2026-11-24
- 范围：Windows 便携版；未修改 TOS 版及其他子仓库

## 1. 结论

V2.0 是对 Network Locator 的信息架构、页面、数据模型和扫描任务管理的完整重构，不是换肤。

- 保留并复用原有 Windows 网络能力：`GetAdaptersInfo`、PowerShell 回退、`arp.exe`、`ping.exe` 和本机回环 HTTP 服务。
- 原有 MAC 后缀查询继续可用，同时扩展为完整 MAC、6 位前缀、4 位后缀和批量查询。
- 新增 SQLite 设备档案与扫描历史。扫描任务由后台线程管理，不依赖页面生命周期。
- 七个页面共享一套 Professional 浅色设计系统与组件规范。
- 修复实机扫描发现的真实数据污染：广播 IP 和 `FF-FF-FF-FF-FF-FF` 曾进入设备档案。

## 2. V1 功能盘点

### 2.1 技术栈

| 层 | V1 实现 |
| --- | --- |
| 桌面窗口 | pywebview + WebView2，失败时回退 Edge/Chrome `--app` |
| 本地服务 | Python 标准库 `ThreadingHTTPServer`，仅监听 `127.0.0.1:18743` |
| 后端能力 | Python 3.10+ 标准库；Windows 网卡 API、ARP、ICMP |
| 前端 | 原生 HTML/CSS/JavaScript，无打包链、无前端框架 |
| 持久化 | WebView2 `localStorage` 中的界面偏好、常用 MAC、网络/设备缓存 |
| 打包 | PyInstaller 单文件 EXE |

### 2.2 功能清单

| 功能 | V1 行为 | 局限 |
| --- | --- | --- |
| 网卡与网段发现 | `win_network.read_windows_adapters` 读取 IPv4、掩码、网关和本机 MAC；PowerShell `Get-NetIPConfiguration` 回退 | 只服务扫描页，没有接口/网段档案 |
| 单/多 MAC 后缀定位 | 输入 4 位后缀，支持最多 8 个目标，多轮扫描直到命中或达到 10 轮 | 不支持完整 MAC、前缀或批量混合查询 |
| 扫描 | `core.ScanManager` 管理内存状态；UDP 预热 ARP、并发 ping、开始/停止和进度 | 任务随页面内存状态存在，无持久化历史 |
| 设备总表 | 筛选、排序、复制 IP/MAC、CSV 导出 | 无手动名称、备注、收藏、IP 历史和详情抽屉 |
| 常用 MAC | `localStorage` 保存后缀、别名、最近 IP | 不是设备档案，缓存可能过期或被误当作身份 |
| CSV | 前端 `csv.js` 生成；UTF-8 BOM 与公式注入转义 | 无设备批量导出、历史导出和可配置列 |
| 主题与布局 | `localStorage` 保存浅/深色与侧栏宽度 | 没有统一设计系统或七页信息架构 |
| 原生窗口 | WebView2 原生窗口，带 Edge/Chrome/默认浏览器回退 | 默认窗口尺寸偏小，V2 页面需要更宽工作区 |

### 2.3 V1 调用关系

```text
index.html + app.js
  ├─ GET  /api/networks            网卡与网段
  ├─ GET  /api/devices             ARP 邻居快照
  ├─ POST /api/devices/scan        单次设备探测
  ├─ POST /api/scan/start          按 MAC 后缀启动扫描
  ├─ GET  /api/scan/status         轮询扫描状态和结果
  ├─ POST /api/scan/stop           停止扫描
  └─ 前端 csv.js                   生成 CSV
```

后端核心路径：

- `win_network.discover_networks()` 读取适配器并构造 `NetworkTarget`。
- `win_network.read_neighbors()` 调用 `arp.exe -a -N <本机地址>` 并解析邻居表。
- `win_network.scan_hosts()` 使用 UDP 预热与并发 `ping.exe` 找响应主机。
- `core.ScanManager` 在内存中合并 ARP 与 ping 结果，维护 `job_id`、进度和状态。
- `server.NetworkLocatorHandler` 提供旧 API 和静态页面。

### 2.4 V1 已知问题

- 设备没有稳定身份。缓存中的 IP/MAC 只是展示数据，不能可靠表达“同一设备换 IP”。
- 扫描结果和常用 MAC 主要依赖 `localStorage`，关闭窗口、清理 WebView 或换配置后不可审计。
- 没有设备级名称、备注、收藏、收藏持久化、IP 历史和状态变化事件。
- 只有 MAC 后缀查询，无法定位完整 MAC 或厂商前缀。
- 没有多网段扫描、自定义 IP 范围、暂停/恢复和扫描历史。
- 没有网络分析页，DNS、设备分布、网关连通性和延迟/丢包缺少统一入口。

## 3. 重构影响分析

### 3.1 新增后端模块

| 文件 | 作用 |
| --- | --- |
| `src/network_locator/store.py` | SQLite Schema、设备身份、地址历史、扫描历史、事件、查找、设置和 CSV |
| `src/network_locator/scanner.py` | 持久化扫描任务、多网段/自定义范围、暂停/恢复/停止、自动扫描调度 |
| `src/network_locator/oui.py` | 内置离线 OUI 子集、厂商识别和本地管理 MAC 识别 |

### 3.2 扩展文件

| 文件 | 变化 |
| --- | --- |
| `src/network_locator/server.py` | 新增 `/api/v2/*`；保留旧 API 兼容路径 |
| `src/network_locator/win_network.py` | 扩展 DNS、网卡稳定 ID、真实 Ping 统计和无效 MAC 过滤 |
| `src/network_locator/core.py` | 增加网段稳定 ID、设备 MAC 合法性校验 |
| `src/frontend/index.html` | 替换为七页应用框架 |
| `src/frontend/styles-v2.css` | 统一 Professional 设计系统 |
| `src/frontend/v2-api.js` | V2 API 客户端 |
| `src/frontend/v2-utils.js` | 查询解析、过滤、排序、结果合并和范围校验 |
| `src/frontend/v2-app.js` | 七页渲染、抽屉、扫描轮询、批量选择、CSV 和设置 |

### 3.3 兼容边界

- 旧版 `app.js`、`styles.css` 和旧 API 保留，避免破坏旧测试与外部调用。
- `index.html` 只加载 V2 资源，静态资源使用版本查询参数避免缓存旧文件。
- 旧 V1 `localStorage` 不删除、不自动伪造成设备档案。
- TOS 应用与其他子仓库未修改。

## 4. V2 架构

### 4.1 应用骨架

- 左侧固定导航：210–230 px。
- 顶部工具栏：56–64 px。
- 主内容区：24 px 页面内边距。
- 右侧设备/历史详情抽屉：400–460 px。
- 表格行高 44–48 px，按钮高度 34–38 px，主色 `#2563EB`，背景 `#F5F7FA`。
- 窄窗口下侧栏变为可开合抽屉；主表格在自身容器内滚动，不产生页面横向溢出。

### 4.2 页面与调用

| 页面 | 主要 API | 关键行为 |
| --- | --- | --- |
| 设备总览 | `/api/v2/bootstrap` | 统计、接口/网段、最近设备、全局表格、立即扫描、CSV |
| 网络扫描 | `/api/v2/interfaces`、`/api/v2/scan/*` | 多网段、自定义范围、进度、暂停/恢复/停止、实时结果 |
| 设备管理 | `/api/v2/devices`、`PATCH /devices/<id>` | 筛选、搜索、排序、分页、批量、抽屉、名称/备注/收藏 |
| MAC 定位 | `POST /api/v2/lookup` | 完整/前缀/后缀/批量、格式识别、厂商、档案关联 |
| 网络分析 | `/api/v2/analysis`、`/analysis/ping` | 接口、IP/掩码/网关/DNS、网段、分布、真实 Ping |
| 扫描历史 | `/api/v2/history`、`/history/<id>` | 任务列表、统计、详情、历史 CSV |
| 系统设置 | `/api/v2/settings` | 扫描、CSV、自动扫描、主题、数据位置、关于 |

### 4.3 扫描生命周期

1. API 启动扫描前先把范围写入 `scan_tasks`。
2. `InventoryScanner` 在独立线程中读取设置、发现接口、选择网段并执行范围。
3. 普通网段按 `scan_range` 分组，多网卡同网段只扫描一次；自定义范围单独执行。
4. 进度、警告、当前网卡/网段和状态写入内存状态与数据库。
5. 前端轮询 `/api/v2/scan/status`，切换页面不停止任务。
6. 完成、停止、失败和中断分别记录；启动时旧 `running` 任务会标记为 `interrupted`。
7. 全量网段扫描才执行“未出现设备转离线”；部分网段和自定义范围不误判其他设备离线。

### 4.4 真实数据边界

- 扫描只使用 ICMP 与 ARP 的真实响应，不使用静态假数据。
- `STALE` 只表示 ARP 缓存存在但本轮未收到 ping，不伪装成在线。
- 无效 MAC 不当成“无 MAC 设备”继续建档：扫描层、存储层和数据库迁移均会拒绝。
- IPv4 网络地址和广播地址不会从普通网段 ARP 结果进入设备档案。

## 5. 数据模型与迁移

### 5.1 实体

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `devices` | `device_id`、`identity_key`、`primary_mac`、名称、备注、收藏、当前 IP、状态 | 设备档案主表 |
| `device_addresses` | `device_id`、IP、接口、MAC、首次/最后发现、当前标记 | IP 历史 |
| `network_interfaces` | 稳定接口 ID、别名、本机 IP、掩码、网关、DNS、虚拟标记 | 网卡快照 |
| `network_segments` | 网段 ID、接口、网络、广播、扫描范围、主机数 | 可扫描网段 |
| `scan_tasks` | `task_id`、触发方式、范围、配置、进度、统计、状态、警告 | 扫描任务 |
| `scan_results` | 任务、设备、IP/MAC、状态、厂商、新增/IP 变化标记 | 单次结果观察 |
| `device_events` | 设备、任务、事件类型、详情、时间 | 发现、IP 变化、上线等事件 |
| `settings` | 键、JSON 值、更新时间 | 本机设置 |

### 5.2 身份规则

- `device_id` 是唯一身份，不使用 IP。
- 有 MAC 时优先通过 `mac:<MAC>` 查找已有档案。
- 同一 MAC 换 IP 时更新 `current_ip`，新增或刷新地址记录，并记录 `ip_changed`。
- 同一 MAC 在同一轮扫描中对应多个 IP 时识别为冲突，不盲目合并，保留多个档案。
- 无 MAC 时才建立 `ip:<IP>:<随机后缀>` 临时档案；后续观察到 MAC 时可补全。
- 随机/虚拟 MAC 单独标记，不因本地管理位而丢弃。
- 手动名称、备注、收藏只由设备编辑 API 修改，扫描更新不会覆盖。

### 5.3 状态规则

| 状态 | 判定 |
| --- | --- |
| 在线 | 本轮扫描命中且状态为 `LOCAL` 或 `REACHABLE` |
| 未知 | 仅 ARP 缓存、部分范围未覆盖、或仍在离线阈值内 |
| 离线 | 全量扫描确认未出现，或最后发现时间超过离线阈值 |

### 5.4 迁移策略

1. V1 的 `localStorage` 原地保留在 WebView2 配置目录，不删除。
2. V2 首次启动创建 SQLite 数据库，不把旧 IP 缓存或常用后缀直接导入为设备。
3. 第一次真实扫描后，由 MAC/IP/网卡观察重建设备档案。
4. 数据结构用 `schema_meta.schema_version` 管理，当前为 `2`。
5. Schema 2 迁移会删除旧的广播/组播无效 MAC 设备，并保留合法设备与手动字段。
6. 旧常用 MAC 仍可通过 MAC 定位页输入查询；设备备注在详情抽屉重新维护。

## 6. 测试报告

### 6.1 测试环境

- Windows 10/11 本机
- Python 3.14.6（测试用解释器）
- Node.js 22.22.2
- Microsoft Edge headless + CDP（真实页面与真实扫描验证）
- 网络环境：`10.18.15.0/24`

### 6.2 自动化测试结果

后端：

```powershell
python -m unittest discover -s tests -v
```

结果：`Ran 51 tests ... OK`。

前端：

```powershell
node --test tests\frontend_v2_utils.test.js tests\frontend_csv.test.js tests\frontend_search_state.test.js tests\frontend_saved_macs.test.js
```

结果：`tests 35 / pass 35 / fail 0`。

语法检查：

```powershell
node --check src\frontend\v2-utils.js
node --check src\frontend\v2-api.js
node --check src\frontend\v2-app.js
node --check src\frontend\app.js
node --check src\frontend\csv.js
```

结果：全部通过。

### 6.3 功能验证矩阵

| 测试项 | 验证方式 | 结果 |
| --- | --- | --- |
| 七页切换 | Edge/CDP 点击导航，检查激活视图和标题 | 7/7 通过 |
| 主色与布局 | 读取计算样式和页面 `scrollWidth` | `#2563EB`，横向溢出 0 |
| 窗口缩放 | 1440、900、760、480 px 检查导航与溢出 | 全部无页面横向溢出 |
| 多网卡识别 | 网卡发现单元测试 + 实机接口页面 | 通过 |
| 多网段扫描 | `test_multi_segment_scan_covers_every_selected_network` | 通过 |
| 扫描中断 | 暂停、恢复、停止状态测试 | 通过 |
| 重复扫描/去重 | 重复观察、相同 MAC 多 IP 测试 | 通过 |
| IP 变化 | 同一 MAC 更换 IP 后检查地址历史和事件 | 通过 |
| 广播/组播过滤 | ARP 解析、扫描器、Store、Schema 迁移测试 | 通过 |
| MAC 格式校验 | 前端 4/6/12 位识别与非法输入测试 | 通过 |
| 收藏/备注持久化 | 实机抽屉保存 + 重新加载；单元测试保护手动字段 | 通过 |
| 在线/离线/未知 | 最新扫描、部分范围和阈值测试 | 通过 |
| CSV 导出 | 设备、批量、历史导出；BOM 和公式转义 | 通过 |
| 扫描历史 | 任务列表、详情和结果持久化 | 通过 |
| 大量设备加载 | 实机扫描发现 125 台并渲染 | 通过 |
| 真实扫描 | `10.18.15.0/24` 完成，125 个结果 | 通过 |
| 切页后任务继续 | 扫描中切到设置页，再回到状态轮询 | 通过 |
| MAC 查询 | 实机完整/后缀查询并关联档案 | 通过 |
| 真实 Ping | `127.0.0.1` 延迟与丢包统计 | 通过 |
| 设置保存 | 超时改为 1100 ms 后通过 API 复核 | 通过 |
| 浏览器控制台 | 收集 `console` error 与未处理异常 | 0 异常、0 错误 |

### 6.4 实机扫描结果

- 选择网段：`10.18.15.0/24`
- 完成状态：`completed`
- 发现结果：125 台
- 扫描中切换页面后任务继续并最终完成
- 设备抽屉可打开，名称/备注保存成功
- MAC 查询返回 1 个档案匹配
- `127.0.0.1` Ping 返回 2/2 成功、0% 丢包

浏览器验证产物（本机临时目录）：

- `%TEMP%\nl-v2-cdp-report.json`
- `%TEMP%\nl-v2-drawer.png`
- `%TEMP%\nl-v2-settings.png`

### 6.5 当前代码服务冒烟

使用 `launcher.py --serve-only --port 0` 启动当前工作区代码，结果：

- `/health`：`app=network-locator`，`version=2.0.0`
- 首页：HTTP 200，包含七个视图，`__APP_VERSION__` 已替换
- `/api/v2/bootstrap`：扫描状态 `idle`，返回 2 个网段
- `/api/v2/analysis`：返回 4 个网络接口

### 6.6 Windows EXE 构建与发布

2026-11-25 执行完整构建并发布：

- `build.ps1`：通过，包含 51 项后端测试、35 项前端测试、Python/JavaScript 语法检查、PyInstaller 打包、EXE 自检和 HTTP 冒烟测试
- 本地产物：`release\network-locator.exe`，SHA256 `ab8812bd60798ffe1cd2597413161890885d3d59ba2fe61bcb75e02630f39cae`
- EXE 自检：`status=ok`，版本 `2.0.0`，WebView2 `158.0.4370.33`
- GitHub Actions `Build Windows EXE`：成功，运行 ID `45524694922`
- GitHub Release `v2.0.0`：已发布 EXE、SHA256 和自检报告；远程 EXE SHA256 `e8e1acbc253dc6f8438c2bb83835a7c90a630c1f7023ecb7de35136af4e94cf1`，与远程 `.sha256` 文件一致
- 本地与云端分别使用 Python 3.14.6 和 3.13 构建，因此 EXE 哈希不同，但均通过自检

## 7. 已知问题与限制

1. Windows 不能像 Linux 一样把 ICMP 探测严格绑定到指定网卡；多网卡同网段时最终路径仍由系统路由表决定。
2. 主机存在但防火墙阻止 ICMP 时，只能依赖 ARP 缓存。此时设备显示为未知/陈旧，不显示为在线。
3. 仅支持 IPv4；IPv6 邻居发现、IPv6 扫描和 IPv6 设备档案未实现。
4. MAC 厂商库是内置离线 OUI 子集，未命中显示“未知厂商”，不会联网补全。
5. 自动扫描依赖应用进程常驻；应用关闭后不会创建计划任务或后台服务。
6. 单网段安全上限为 1022 台主机；单个自定义范围最多 1022 台，总计最多 4096 台。
7. 旧 V1 常用 MAC/别名不会自动转换为 V2 设备档案，避免把过期缓存当成真实设备。
8. EXE 未做代码签名，首次从网络下载运行时 Windows SmartScreen 可能提示未知发布者。
9. 实机扫描的历史统计可能包含旧版本已经写入的广播记录计数；Schema 2 会清除无效设备档案，但不会重写历史任务的汇总数字。

## 8. 启动与打包

### 8.1 源码运行

```powershell
cd network-locator-exe
python launcher.py
```

仅启动本地服务，不打开窗口：

```powershell
python launcher.py --serve-only --ready-file ready.json --port 0 --duration 10
```

常用环境变量：

| 变量 | 作用 |
| --- | --- |
| `NETWORK_LOCATOR_PORT` | 覆盖本地端口 |
| `NETWORK_LOCATOR_DATA_DIR` | 覆盖 SQLite 数据目录 |
| `NETWORK_LOCATOR_GUI` | `auto` / `native` / `app-window` / `browser` |
| `NETWORK_LOCATOR_BROWSER` | 指定回退浏览器路径 |

### 8.2 构建依赖

```powershell
python -m pip install -r requirements-build.txt
```

CI 使用 Python 3.13、Windows x64、PyInstaller 6.22、pywebview 6.2.1。构建脚本会：

1. 生成应用图标。
2. 运行 Python 单元测试和 `py_compile`。
3. 运行前端 JavaScript 语法检查与 Node 测试。
4. 用 PyInstaller 生成单文件 EXE。
5. 运行 EXE `--selftest`，检查 V2 资源、pywebview 和图标。
6. 运行 `--serve-only` 健康检查、首页和版本占位符检查。
7. 输出 `release\network-locator.exe` 与 `.sha256`。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

最终验证不要使用 `-SkipTests`。构建会重建 `build/`、`dist/` 和 `release/`。

### 8.3 数据位置

| 路径 | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\NetworkLocator\data\network-locator.db` | 设备档案、地址历史、扫描历史、事件和设置 |
| `%LOCALAPPDATA%\NetworkLocator\config.json` | 端口与回退浏览器 |
| `%LOCALAPPDATA%\NetworkLocator\logs\app.log` | 运行日志 |
| `%LOCALAPPDATA%\NetworkLocator\webview\` | WebView2 配置与 V1 保留数据 |

## 9. 交付清单

- 完整七页 UI 与统一设计组件。
- 可持久化设备档案、地址历史、扫描任务、扫描结果、设备事件和设置。
- 多网卡/多网段/自定义范围扫描，暂停、恢复、停止和后台持续运行。
- MAC 完整/前缀/后缀/批量定位、厂商识别和设备关联。
- 网络分析、真实 Ping、扫描历史和 CSV 导出。
- 从 V1 到 V2 的安全迁移策略与 Schema 2 数据清理。
- 51 项 Python 测试、35 项 Node 测试和真实浏览器/真实网络验证记录。
- 已知问题清单与本报告。
