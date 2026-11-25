# Privacy Policy — Network Locator (Windows)

Last updated: 2026-11-24
Applies to: Network Locator for Windows 2.0.0 (`network-locator.exe`)

This policy describes what the Windows build of **Network Locator** does with information. The application is a portable single-file executable that runs on the user's own computer. The publisher does not operate an account service for the app and does not receive a copy of anything a local installation creates.

## Who provides the application

The executable is published by the independent publisher **qishiyi** through the public repository [github.com/642671/network-locator-exe](https://github.com/642671/network-locator-exe). It is not a TerraMaster official application and is not distributed through a store. Questions about this policy can be opened as issues on that repository.

## 1. What the application accesses

- **Local adapter and routing information** — adapter names, IPv4 addresses, prefix lengths, gateways, DNS servers and the local MAC address, read through `GetAdaptersInfo` and `Get-DnsClientServerAddress` (with a PowerShell `Get-NetIPConfiguration` fallback). Used only to list the networks you can scan and to populate the network analysis page.
- **Neighbor table entries** — IP and MAC pairs read with `arp -a -N`. Used to show devices that are already known to the system.
- **Scan results** — devices found by sending ICMP echo requests (`ping`) and ARP-triggering UDP packets to the subnets you select. Results are stored in the local device inventory and scan history described below, and can be exported to a CSV file you choose.
- **Device profiles** — display names, notes, favorites, IP address history, vendor labels and device status events derived from scans or entered by you. These are stored only in the local SQLite database.
- **Your configuration** — the local port and an optional browser path in `config.json`, plus scan, CSV, theme and storage preferences in the SQLite database.

The application does not ask for an account, a name, an email address, or any other personal identifier.

## 2. Where the data is stored

All data stays on the computer where the application runs:

| Path | Contents |
| --- | --- |
| `%LOCALAPPDATA%\NetworkLocator\data\network-locator.db` | SQLite database: device profiles, IP history, adapter/network snapshots, scan tasks and results, device events and settings |
| `%LOCALAPPDATA%\NetworkLocator\config.json` | local port and optional fallback browser path |
| `%LOCALAPPDATA%\NetworkLocator\logs\app.log` | run log: version, window mode, local URL, service start/stop, errors |
| `%LOCALAPPDATA%\NetworkLocator\webview\` | WebView2 profile used by the native window: theme cache and browser storage retained from earlier versions |
| `%LOCALAPPDATA%\NetworkLocator\browser-profile\` | Edge/Chrome profile, used only when the app falls back to a browser app window |

The SQLite database is the primary data store and remains on the same computer. It can contain IP addresses, MAC addresses, adapter names, vendor labels, the names and notes you enter, and scan history. The log is append-only and is **not** rotated or deleted automatically. It can contain adapter names, local IP addresses, MAC addresses and error details; it does not contain file contents or personal identifiers. You can delete the whole `NetworkLocator` folder at any time.

## 3. Is anything uploaded or shared?

No. The application code contains no telemetry, analytics, crash reporting, account sync, or update check, and it makes no requests to the publisher or to any third party.

- The UI is served only on the loopback interface (`http://127.0.0.1:18743/` by default) and is not reachable from the LAN.
- Scanning sends ICMP echo requests and ARP-triggering UDP packets **only to the subnets you select**, and only from your own machine.
- Device inventory, scan history and settings stay in the local SQLite database; they are not uploaded or synchronized.
- CSV files are created only when you request an export and are saved to the location your browser chooses.
- Nothing is written to the Windows registry, to a system service, or to a startup entry.

One thing outside the app's control: the native window uses the Microsoft Edge **WebView2 runtime** installed on the system. Like any browser engine, that runtime may contact Microsoft for its own component updates, certificate revocation or SmartScreen lookups under Microsoft's terms. The Network Locator app itself does not send data through it; if you delete `%LOCALAPPDATA%\NetworkLocator\webview`, only local profile data is removed.

## 4. Third-party components

| Component | Version | License | Role |
| --- | --- | --- | --- |
| Python | 3.13 | PSF License | bundled interpreter |
| pywebview | 6.2.1 | BSD-3-Clause | native window host |
| pythonnet | 3.2.1 | MIT | .NET bridge used by pywebview |
| Bottle | as bundled by pywebview | MIT | pywebview's optional local HTTP server (unused for the app UI) |
| PyInstaller bootloader | 6.22.3 | GPL-2.0 with bootloader exception | single-file packaging |
| Microsoft Edge WebView2 Runtime | system-installed | Microsoft terms | rendering engine for the native window |

Pillow is used only while building the icon and is not part of the shipped executable. Source for every application component is in this repository; the dependency set is pinned in `requirements-build.txt`.

## 5. Permissions used

| Capability | Purpose |
| --- | --- |
| Read local network adapter and route information | discover the networks you can scan |
| Read the IPv4 DNS server list | show interface details on the network analysis page |
| Send ICMP echo and UDP packets on the local network | detect devices in the subnets you select |
| Read the local ARP/neighbor table | list devices already known to the system |
| Read and write `%LOCALAPPDATA%\NetworkLocator` | store the device database, scan history, settings, configuration, logs and window profile |
| Listen on `127.0.0.1` | serve the local UI to the app window |

The application does not request administrator rights and does not modify network configuration. Windows Firewall may ask about the executable the first time it sends probes; only outbound local-network traffic is needed.

## 6. Uninstall and deletion

Deleting `network-locator.exe` and the `%LOCALAPPDATA%\NetworkLocator` folder removes everything the application created: the SQLite device database and scan history, configuration, device notes and favorites, theme choice, logs, and both window profiles. CSV files you exported are normal user files outside this folder and are not deleted automatically. No system service, Unix socket, registry key, scheduled task, or startup entry is created, so nothing else needs to be cleaned up.

The shared Microsoft Edge WebView2 runtime is installed system-wide and is not removed by uninstalling this app.

## 7. Children

The application is not directed at children under 13 and does not knowingly collect information from them.

## 8. Changes

This policy is versioned with the application. Changes are published in this repository and noted in [CHANGELOG.md](CHANGELOG.md).

## 9. Contact

Publisher: qishiyi
Contact: open an issue at [github.com/642671/network-locator-exe/issues](https://github.com/642671/network-locator-exe/issues)
