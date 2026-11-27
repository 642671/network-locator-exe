# Changelog

## 1.3.8 — 2026-11-27

WebUI 对齐 TOS `network-locator` 1.3.8（V1 界面）。

### 变更

- 移植 TOS 1.3.8 的标题栏调整：栏高 56px → 41px（40px 内容 + 1px 底线），左右内边距 16px / 168px，深色主题补底线颜色
- 品牌图标 22px → 20px、标题字号 15px → 14px、「服务在线」徽标内边距压缩为 2px 8px，适配 41px 栏高
- 全窗口圆角由 14/18px 统一为 8px
- 版本号更新为 1.3.8

### 说明

- 1.3.8 前端与 1.3.7 相比只有 `styles.css` 变化，`app.js`、`index.html`、`csv.js` 无需改动
- 保留 Windows 版适配：`__APP_VERSION__` 占位符、WebView2 原生窗口、本地服务提示文案与同源 API 路径

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