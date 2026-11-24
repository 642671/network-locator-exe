# Changelog

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