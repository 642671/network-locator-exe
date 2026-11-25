# 网络定位 Network Locator V2.0（Windows）

Professional 局域网设备发现与管理桌面应用。单文件 `network-locator.exe` 使用 WebView2 原生窗口，本地 Python 服务只监听 `127.0.0.1`，不向局域网开放端口。

V2 不是换肤：设备档案、扫描历史、MAC 定位、网络分析和设置均迁移到本机 SQLite 持久化模型，扫描任务独立于页面生命周期运行。

## 页面与能力

| 页面 | 主要能力 |
| --- | --- |
| 设备总览 | 设备统计、当前网卡与网段、最近发现、全局搜索/状态筛选/排序、复制 IP/MAC、立即扫描、CSV 导出 |
| 网络扫描 | 多网卡识别、多网段选择、自定义 CIDR/IP 范围、进度、暂停/恢复/停止、实时结果、异常提示、本轮导出 |
| 设备管理 | 全部/在线/离线/收藏筛选、名称/备注/收藏维护、批量选择、批量导出、详情抽屉、IP 历史与设备事件 |
| MAC 定位 | 完整 MAC、6 位前缀、4 位后缀、批量查询、格式自动识别、本地 OUI 厂商识别、关联设备档案 |
| 网络分析 | IP、掩码、网关、DNS、网段、设备分布、网关连通性检测、指定 IPv4 Ping、延迟与丢包统计 |
| 扫描历史 | 任务列表、范围、发现/新增/离线/IP 变化、任务详情、历史结果 CSV |
| 系统设置 | 超时、并发、默认网卡、离线阈值、自动扫描、CSV 配置、主题、数据位置与关于信息 |

## 技术栈

- 后端：Python 3.10+ 标准库；Windows 网卡 API、`arp.exe`、并发 `ping.exe`
- 持久化：SQLite（WAL），无第三方 Python 依赖
- 前端：原生 HTML/CSS/JavaScript，无打包链、无前端运行时
- 桌面窗口：pywebview + Microsoft Edge WebView2，失败时回退 Edge/Chrome 应用窗口
- 本地 API：`http://127.0.0.1:<port>/api/v2/*`

## 数据模型

| 实体 | 作用 |
| --- | --- |
| `devices` | 设备档案；`device_id` 为唯一标识，保存手动名称、备注、收藏、当前 IP/MAC、厂商和状态 |
| `device_addresses` | 设备 IP 地址历史，同一设备更换 IP 时追加/更新记录 |
| `network_interfaces` | 网卡快照：索引、别名、本机 IP、掩码、网关、DNS、虚拟网卡标记 |
| `network_segments` | 可扫描网段，与网卡关联 |
| `scan_tasks` | 扫描任务、范围、配置、进度、警告、统计结果 |
| `scan_results` | 单次扫描中的设备/IP/MAC/网卡/状态观察 |
| `device_events` | 新发现、IP 变化、恢复在线、转离线等状态事件 |
| `settings` | 本机键值配置 |

身份判定规则：

- 设备不以 IP 作为唯一标识。优先使用 MAC 关联已有档案。
- 同一 MAC 换 IP 时更新 `current_ip` 并保留旧地址与 `ip_changed` 事件。
- 同一 MAC 在同一轮扫描中出现多个 IP 时按冲突处理，保留多个档案，避免错误合并。
- 无 MAC 时才使用 IP + 随机 UUID 建立临时档案。
- 用户手动设置的名称、备注、收藏只由 `PATCH /api/v2/devices/<id>` 修改，扫描不会覆盖。

状态规则：

- `在线`：本轮扫描命中且状态为 `LOCAL` 或 `REACHABLE`。
- `离线`：全量扫描确认未出现，或已超过设置的离线阈值。
- `未知`：仅有 ARP 缓存、被部分扫描跳过、或尚未达到离线阈值。
- 部分网段和自定义范围扫描不会把未覆盖网段的设备误判为离线。

## 运行

发布包直接运行 `network-locator.exe`。源码运行：

```powershell
cd network-locator-exe
python launcher.py
```

只启动本地服务、不打开窗口：

```powershell
python launcher.py --serve-only --ready-file ready.json --port 0 --duration 10
```

环境变量：

| 变量 | 用途 |
| --- | --- |
| `NETWORK_LOCATOR_PORT` | 覆盖本地端口 |
| `NETWORK_LOCATOR_DATA_DIR` | 覆盖 SQLite 数据目录 |
| `NETWORK_LOCATOR_GUI` | `auto` / `native` / `app-window` / `browser` |
| `NETWORK_LOCATOR_BROWSER` | 指定回退浏览器路径 |

## 数据与日志

| 路径 | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\NetworkLocator\data\network-locator.db` | 设备档案、地址历史、扫描历史、事件和设置 |
| `%LOCALAPPDATA%\NetworkLocator\config.json` | 本地端口与回退浏览器 |
| `%LOCALAPPDATA%\NetworkLocator\logs\app.log` | 运行日志 |
| `%LOCALAPPDATA%\NetworkLocator\webview\` | WebView2 本机配置 |

删除 `network-locator.exe` 和 `%LOCALAPPDATA%\NetworkLocator` 即可完成卸载。应用不写注册表启动项、系统服务或计划任务。

## V1 到 V2 数据迁移

V1 仅有浏览器 `localStorage` 中的网段/设备缓存、常用 MAC 别名和界面偏好，没有可靠的设备档案。V2 不使用这些易失缓存作为设备身份来源，因此采用安全迁移策略：

1. 不删除 V1 的 WebView2 本机存储，旧数据仍保留在 `%LOCALAPPDATA%\NetworkLocator\webview`。
2. 首次 V2 启动创建新的 SQLite 数据库，不把 IP 缓存或常用后缀直接伪造成设备。
3. 完成首次真实扫描后，设备由 MAC/IP/网卡观察重建档案。
4. 旧“常用 MAC”后缀可继续在 MAC 定位页查询；设备备注可在详情抽屉重新维护。

该策略避免把随机 MAC、MAC 冲突、旧 IP 缓存或虚拟网卡错误合并到新档案中。完整说明见 [V2_REFACTOR.md](V2_REFACTOR.md)。

## 测试

```powershell
python -m unittest discover -s tests -v
node --test tests\frontend_csv.test.js tests\frontend_search_state.test.js tests\frontend_saved_macs.test.js tests\frontend_v2_utils.test.js
```

后端测试覆盖设备身份、IP 变化、重复观察去重、MAC 冲突、手动字段保护、部分扫描离线判定、多网段扫描、扫描暂停/恢复/停止、设置、CSV、查找和 HTTP 契约。前端 Node 测试覆盖 MAC 格式、过滤排序、实时结果合并、自定义范围校验和旧版 CSV 兼容。

## 构建

需要 Windows + Python 3.12/3.13：

```powershell
python -m pip install -r requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

构建流程：

1. 生成应用图标
2. Python 单元测试与 `py_compile`
3. 前端 JavaScript 语法检查和 Node 测试
4. PyInstaller 单文件打包
5. EXE 自检（网卡发现、V2 WebUI 资源、pywebview、图标）
6. `--serve-only` 冒烟测试（健康检查、首页、版本占位符）
7. 输出 `release\network-locator.exe` 和 SHA256

## 已知限制

- 仅支持 Windows 10/11；依赖系统 `ping.exe`、`arp.exe` 和 IPv4。
- Windows 无法像 Linux 一样把 ICMP 绑定到指定网卡；多网卡路由最终由系统路由表决定。
- 防火墙阻止 ICMP 时，ARP 缓存中的设备仍会显示为“ARP 缓存/未知”，不会伪装成在线。
- 单个自定义范围最多 1022 台主机，总计最多 4096 台；默认网段安全上限为 1022 台。
- MAC 厂商库为内置离线 OUI 子集，未命中时显示“未知厂商”。
- 旧 V1 `localStorage` 别名不会自动合并到 SQLite，详见迁移策略。

## 许可证

MIT，见 [LICENSE](LICENSE)。
