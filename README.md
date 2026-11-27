# 网络定位 Network Locator（Windows）

单文件 Windows 应用：在当前电脑所在的物理局域网中，按 MAC 地址后四位查找设备。

- 与 TOS 版 [`network-locator`](https://github.com/642671/network-locator) 1.3.7 使用同一套 WebUI 与匹配逻辑
- 单文件 `network-locator.exe`，无需安装 Python 或浏览器扩展
- 只在本机回环地址 `127.0.0.1` 的自动分配端口上提供页面，不对局域网开放端口；多个实例可同时运行
- 使用 WebView2（pywebview）打开原生应用窗口，没有浏览器地址栏和标签页，界面与 TOS 版一致

## 功能

| 能力 | Windows 实现 |
| --- | --- |
| 网卡与网段发现 | `GetAdaptersInfo`（iphlpapi），失败时回退到 PowerShell `Get-NetIPConfiguration` |
| 物理网卡过滤 | 跳过 Hyper-V、WSL、VMware、Tailscale、Docker 等虚拟网卡；若机器上只有虚拟网卡，则将其作为可用网段 |
| 邻居表读取 | `arp.exe -a -N <本机地址>`，兼容中英文 Windows 输出 |
| MAC 归一化 | `AA-BB`、`AA:BB`、`AABB` 三种输入；统一输出 `AA-BB-CC-DD-EE-FF` |
| 扫描 | 选定网段、UDP 预热 ARP、并发 `ping` 探测、开始/停止状态机与进度 |
| 设备总表 | 筛选、排序、复制 IP/MAC、CSV 导出、深浅色主题、工具提示 |
| 常用 MAC | 最多保存 100 个常用后缀与备注，结果自动关联上次发现的 IP |
| 个人设备视图 | 左侧「个人设备」入口只显示已保存 MAC 匹配到的设备 |
| 持续搜索 | 多轮扫描，找到目标后自动结束；最多 10 轮无结果后提示 |

扫描结果的“状态”含义：

- `本机 LOCAL`：当前电脑的网卡地址
- `可达 REACHABLE`：本轮 ping 有响应
- `陈旧 STALE`：仅存在于 ARP 缓存，本轮没有响应
- Windows 没有 Linux 的 `DELAY` / `PROBE` 邻居状态，因此不会出现这两类值

## 下载与运行

1. 从 [Releases](https://github.com/642671/network-locator-exe/releases) 下载 `network-locator.exe`
2. 双击运行，应用会启动本地服务并打开 WebView2 原生窗口
3. 关闭应用窗口即退出；本地服务随进程一起停止

窗口打开顺序（自动回退，不会白屏）：

1. WebView2 原生窗口（需要 Microsoft Edge WebView2 运行时；Windows 10/11 通常已内置）
2. 若运行时缺失或原生窗口启动失败：Microsoft Edge / Google Chrome 的 `--app` 应用窗口
3. 若两者都不可用：系统默认浏览器，并提示关闭提示框即退出

没有 WebView2 运行的机器可以从 [Microsoft WebView2](https://developer.microsoft.com/microsoft-edge/webview2/) 安装 Evergreen Runtime 后重新打开应用。

首次运行如果 Windows 防火墙询问，请允许该程序访问网络（仅需要出站 ICMP/ARP）。

卸载：删除 `network-locator.exe`，再删除 `%LOCALAPPDATA%\NetworkLocator` 即可，不会写系统服务或注册表启动项。

## 数据与日志

| 路径 | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\NetworkLocator\config.json` | 上次使用的端口（`v1_port`）与浏览器选择 |
| `%LOCALAPPDATA%\NetworkLocator\logs\app.log` | 运行日志 |
| `%LOCALAPPDATA%\NetworkLocator\webview\` | WebView2 原生窗口的本地存储（保留常用 MAC、主题与缓存） |
| `%LOCALAPPDATA%\NetworkLocator\webview-<port>\` | 第二个同时运行的实例使用的独立 WebView2 存储 |
| `%LOCALAPPDATA%\NetworkLocator\browser-profile\` | 回退到 Edge/Chrome 应用窗口时使用的浏览器配置 |

默认端口由系统自动分配并记录到 `config.json` 的 `v1_port`（不会覆盖 2.x 的 `port` 设置）：下次启动优先复用该端口，以保持 WebView2 本地存储（常用 MAC、主题）的页面来源不变；如果端口已被另一个实例占用，则自动改用系统分配的端口。`NETWORK_LOCATOR_PORT` 或命令行 `--port` 可强制指定端口；设置 `NETWORK_LOCATOR_BROWSER` 可指定回退用的浏览器可执行文件。

设置 `NETWORK_LOCATOR_GUI` 可强制指定窗口模式，便于排查环境问题：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 原生窗口 → Edge/Chrome 应用窗口 → 默认浏览器 |
| `native` | 只用 WebView2 原生窗口，失败则报错退出 |
| `app-window` | 跳过原生窗口，直接使用 Edge/Chrome 应用窗口 |
| `browser` | 直接使用默认浏览器 |

## 与 TOS 版的差异

TOS 版是 Deb 应用，运行在 NAS 上，通过 systemd、Unix socket 和平台 iframe 代理访问。Windows 版做了等价替换：

| TOS 版 | Windows 版 |
| --- | --- |
| systemd 服务 `network-locator` | 单进程 EXE，关闭窗口即退出 |
| `/var/api/network-locator.sock` | `http://127.0.0.1:<自动分配的端口>/` |
| 平台 iframe 代理与 TOS 会话 | 同源本地页面，无需平台鉴权 |
| `ip addr` / `ip route` | `GetAdaptersInfo` + PowerShell 回退 |
| `ip neigh` | `arp -a` |
| `ping -I <网卡>` | `ping -n 1 -w <毫秒>` |
| TOS 桌面图标 | 直接运行 EXE |

Windows 上不再需要“登录会话”“平台代理”这类概念，因此页面中的相关提示改成了本地服务提示。

## 隐私

完整说明见 [PRIVACY.md](PRIVACY.md)（随仓库公开发布）。要点：

- 不收集账号、遥测或崩溃上报；没有任何向开发者或第三方的上传行为，也没有更新检查
- 页面只监听 `127.0.0.1`；扫描只向你在界面上选择的网段发送 ICMP 探测包和触发 ARP 解析的 UDP 包
- 配置、常用 MAC、主题与日志都写在 `%LOCALAPPDATA%\NetworkLocator`，日志不会自动轮转或删除
- 卸载时删除 `network-locator.exe` 和该目录即可，不写系统服务、注册表或启动项
- 原生窗口依赖系统自带的 Microsoft Edge WebView2 运行时，该运行时按其自身条款可能联网更新组件

## 构建

需要 Windows + Python 3.12 或更高版本：

```powershell
python -m pip install -r requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

构建流程会执行：

1. 生成图标（Pillow）
2. Python 单元测试（匹配、ARP 解析、扫描状态、HTTP 契约）
3. `node --check` 与前端 Node 测试（需要 Node.js）
4. PyInstaller 单文件打包
5. EXE 自检 `--selftest`（网卡发现 + WebUI 资源 + pywebview 模块 + 图标完整性）
6. EXE `--serve-only` 冒烟测试（`/health`、首页与版本占位符替换）
7. 输出 `release\network-locator.exe` 与 `.sha256`

CI 使用 GitHub Actions `windows-latest` 构建；推送 `v*` 标签会自动创建 Release。

## 命令行

```text
network-locator.exe                    正常启动
network-locator.exe --selftest --result-file selftest.json
network-locator.exe --serve-only --ready-file ready.json --port 0 --duration 10
network-locator.exe --version
```

## 已知限制

- 仅支持 Windows 10/11；依赖系统的 `ping.exe` 与 `arp.exe`
- 原生窗口依赖 Microsoft Edge WebView2 运行时；缺失时会回退到 Edge/Chrome 应用窗口
- 回退的应用窗口需要 Microsoft Edge 或 Google Chrome；两者都没有时会使用默认浏览器
- 目标主机或本机防火墙禁止 ICMP 时，ping 结果会缺失，但 ARP 缓存中的设备仍会显示为“陈旧”
- 扫描范围沿用 TOS 版的安全上限：单个网段最多 1022 台主机
- 多个实例同时运行时，后启动的实例会自动改用系统分配的端口和独立的 WebView2 存储目录；各实例的浏览器本地数据（常用 MAC、主题）按端口来源分别保存

## 许可证

MIT，见 [LICENSE](LICENSE)。