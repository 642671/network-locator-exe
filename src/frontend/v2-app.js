(function () {
  "use strict";

  var U = window.LocatorUtils;
  var API = window.LocatorApi;
  if (!U || !API) {
    return;
  }

  var VIEW_META = {
    dashboard: ["设备总览", "局域网设备档案与运行状态"],
    discovery: ["网络扫描", "选择真实网卡网段或自定义 IP 范围"],
    inventory: ["设备管理", "持久化设备档案与手动维护信息"],
    lookup: ["MAC 定位", "完整 MAC、前缀与后缀批量查询"],
    analysis: ["网络分析", "接口、网段与真实连通性探测"],
    history: ["扫描历史", "扫描结果、变化与可追溯记录"],
    settings: ["系统设置", "扫描默认值、数据与外观配置"]
  };

  var state = {
    bootstrapped: false,
    connected: false,
    currentView: "dashboard",
    devices: [],
    interfaces: [],
    segments: [],
    warnings: [],
    history: [],
    settings: {},
    scan: {},
    analysis: null,
    appVersion: "2.0.0",
    selectedSegments: new Set(),
    segmentSelectionInitialized: false,
    dashboardQuery: "",
    dashboardStatus: "all",
    dashboardSortKey: "last_seen_at",
    dashboardSortDirection: "descending",
    inventoryQuery: "",
    inventoryStatus: "all",
    inventoryFavorite: false,
    inventorySortKey: "last_seen_at",
    inventorySortDirection: "descending",
    inventoryPage: 1,
    inventoryPageSize: 25,
    inventorySelected: new Set(),
    inventoryPageIds: [],
    lookupPayload: null,
    lookupError: "",
    pingResult: null,
    pingLoading: false,
    scanPollBusy: false,
    scanWasRunning: false,
    finalizedTaskId: "",
    drawerReturnFocus: null
  };

  var scanTimer = null;
  var toastSerial = 0;

  function byId(id) {
    return document.getElementById(id);
  }

  function setHtml(id, html) {
    var node = byId(id);
    if (node) {
      node.innerHTML = html;
    }
  }

  function setText(id, text) {
    var node = byId(id);
    if (node) {
      node.textContent = text;
    }
  }

  function icon(name, className) {
    return '<svg class="icon ' + (className || "") + '" aria-hidden="true"><use href="#' + name + '"></use></svg>';
  }

  function esc(value) {
    return U.escapeHtml(value);
  }

  function attr(value) {
    return U.escapeHtml(value === undefined || value === null ? "" : value);
  }

  function loadingMarkup(label) {
    return '<div class="loading-state"><span class="spinner" aria-hidden="true"></span><span>' + esc(label || "正在加载") + "</span></div>";
  }

  function emptyMarkup(title, detail, iconName) {
    return '<div class="empty-state"><span class="empty-icon">' + icon(iconName || "i-info") + "</span><h3>" + esc(title) + "</h3><p>" + esc(detail || "") + "</p></div>";
  }

  function errorMarkup(message, retryAction) {
    return '<div class="error-state">' + icon("i-alert") + "<p>" + esc(message || "数据读取失败") + "</p>" +
      (retryAction ? '<button class="btn btn-secondary btn-sm" type="button" data-action="' + attr(retryAction) + '">重试</button>' : "") +
      "</div>";
  }

  function showToast(message, tone) {
    var region = byId("toastRegion");
    if (!region) {
      return;
    }
    var serial = ++toastSerial;
    var toneName = tone || "info";
    var iconName = toneName === "success" ? "i-check" : (toneName === "error" || toneName === "warning" ? "i-alert" : "i-info");
    var node = document.createElement("div");
    node.className = "toast " + toneName;
    node.dataset.toastId = String(serial);
    node.innerHTML = icon(iconName) + "<span>" + esc(message) + "</span>";
    region.appendChild(node);
    window.setTimeout(function () {
      if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    }, toneName === "error" ? 5200 : 3200);
  }

  function reportError(error, context) {
    var message = error && error.message ? error.message : String(error || "未知错误");
    showToast(context ? context + "：" + message : message, "error");
    if (window.console && console.error) {
      console.error(context || "Network Locator", error);
    }
  }

  function setConnection(ok, message) {
    state.connected = Boolean(ok);
    var card = byId("backendStatus");
    var dot = card ? card.querySelector(".connection-dot") : null;
    if (dot) {
      dot.className = "connection-dot " + (ok ? "is-ok" : "is-error");
    }
    setText("backendStatusText", ok ? "服务正常" : "服务异常");
    var alert = byId("connectionAlert");
    if (alert) {
      alert.hidden = Boolean(ok);
    }
    if (!ok) {
      setText("connectionAlertText", message || "无法连接本地服务，请检查应用是否仍在运行。");
    }
  }

  function applyTheme(theme, persistBackend) {
    var selected = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", selected);
    try {
      window.localStorage.setItem("networkLocator.theme.v2", selected);
    } catch (error) {
      /* localStorage can be unavailable in hardened WebView profiles. */
    }
    state.settings.theme = selected;
    var select = byId("settingTheme");
    if (select) {
      select.value = selected;
    }
    if (persistBackend) {
      API.updateSettings({ theme: selected }).then(function (payload) {
        state.settings = payload.settings || state.settings;
      }).catch(function (error) {
        reportError(error, "主题设置保存失败");
      });
    }
  }

  function initialize() {
    bindEvents();
    renderInitialLoading();
    applyTheme(state.settings.theme || readStoredTheme(), false);
    startScanPolling();
    loadBootstrap({ silent: true });
  }

  function readStoredTheme() {
    try {
      return window.localStorage.getItem("networkLocator.theme.v2") || "light";
    } catch (error) {
      return "light";
    }
  }

  function renderInitialLoading() {
    setHtml("dashboardStats", loadingMarkup("正在读取设备统计"));
    setHtml("dashboardNetwork", loadingMarkup("正在读取网卡信息"));
    setHtml("recentDevices", loadingMarkup("正在读取最近设备"));
    setHtml("dashboardTable", loadingMarkup("正在读取设备档案"));
    setHtml("segmentOptions", loadingMarkup("正在读取网卡网段"));
    setHtml("discoveryResults", emptyMarkup("尚未开始扫描", "扫描结果仅来自真实的 ICMP 与 ARP 数据。", "i-radar"));
    setHtml("inventoryTable", loadingMarkup("正在读取设备档案"));
    setHtml("analysisInterfaces", loadingMarkup("正在读取网络接口"));
    setHtml("analysisDistribution", loadingMarkup("正在统计设备分布"));
    setHtml("historyTable", loadingMarkup("正在读取扫描历史"));
  }

  function bindEvents() {
    document.addEventListener("click", function (event) {
      handleDocumentClick(event).catch(function (error) {
        reportError(error, "操作失败");
      });
    });
    document.addEventListener("submit", function (event) {
      handleSubmit(event).catch(function (error) {
        reportError(error, "提交失败");
      });
    });
    document.addEventListener("input", handleInput);
    document.addEventListener("change", function (event) {
      handleChange(event).catch(function (error) {
        reportError(error, "设置更新失败");
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        if (byId("detailDrawer") && byId("detailDrawer").classList.contains("is-open")) {
          closeDrawer();
        }
        document.body.classList.remove("sidebar-open");
      }
    });
    var backdrop = byId("drawerBackdrop");
    if (backdrop) {
      backdrop.addEventListener("click", closeDrawer);
    }
  }

  async function loadBootstrap(options) {
    var settings = options || {};
    if (!state.bootstrapped) {
      setConnection(false, "正在连接本地服务…");
      var dot = byId("backendStatus") ? byId("backendStatus").querySelector(".connection-dot") : null;
      if (dot) {
        dot.className = "connection-dot is-waiting";
      }
      setText("backendStatusText", "正在连接");
    }
    try {
      var payload = await API.bootstrap();
      applyBootstrap(payload);
      state.bootstrapped = true;
      setConnection(true);
      renderAll();
      if (settings.notify) {
        showToast("设备、网卡与扫描历史已刷新", "success");
      }
      return payload;
    } catch (error) {
      setConnection(false, error && error.message ? error.message : "本地服务连接失败");
      if (!state.bootstrapped) {
        setHtml("dashboardStats", errorMarkup(error.message, "refresh"));
        setHtml("dashboardNetwork", errorMarkup(error.message, "refresh"));
        setHtml("recentDevices", errorMarkup(error.message, "refresh"));
        setHtml("dashboardTable", errorMarkup(error.message, "refresh"));
        setHtml("inventoryTable", errorMarkup(error.message, "refresh"));
        setHtml("historyTable", errorMarkup(error.message, "refresh"));
        setHtml("segmentOptions", errorMarkup(error.message, "refresh"));
        setHtml("analysisInterfaces", errorMarkup(error.message, "refresh"));
        setHtml("analysisDistribution", errorMarkup(error.message, "refresh"));
      }
      if (settings.notify) {
        reportError(error, "刷新失败");
      }
      return null;
    }
  }

  function applyBootstrap(payload) {
    state.appVersion = payload.version || state.appVersion;
    state.devices = Array.isArray(payload.devices) ? payload.devices : [];
    state.interfaces = Array.isArray(payload.interfaces) ? payload.interfaces : [];
    state.segments = Array.isArray(payload.segments) ? payload.segments : [];
    state.warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
    state.history = Array.isArray(payload.history) ? payload.history : [];
    state.settings = payload.settings || state.settings || {};
    state.scan = payload.scan || {};
    state.analysis = {
      interfaces: state.interfaces,
      segments: state.segments,
      distribution: buildDistribution(state.devices),
      warnings: state.warnings
    };
    applyTheme(state.settings.theme || readStoredTheme(), false);
    initializeSegments();
    pruneInventorySelection();
    if (!state.scan.running && state.scan.task_id) {
      state.finalizedTaskId = state.scan.task_id;
    }
    if (state.scan.running) {
      state.scanWasRunning = true;
    }
  }

  function initializeSegments() {
    var available = state.segments.map(segmentId).filter(Boolean);
    if (!state.segmentSelectionInitialized) {
      var preferred = String(state.settings.default_interface_id || "");
      var defaultIds = preferred ? state.segments.filter(function (segment) {
        return String(segment.interface_id || "") === preferred;
      }).map(segmentId).filter(Boolean) : [];
      state.selectedSegments = new Set(defaultIds.length ? defaultIds : available);
      state.segmentSelectionInitialized = true;
      return;
    }
    var availableSet = new Set(available);
    state.selectedSegments.forEach(function (value) {
      if (!availableSet.has(value)) {
        state.selectedSegments.delete(value);
      }
    });
  }

  function pruneInventorySelection() {
    var ids = new Set(state.devices.map(function (device) { return String(device.device_id || ""); }));
    state.inventorySelected.forEach(function (value) {
      if (!ids.has(value)) {
        state.inventorySelected.delete(value);
      }
    });
  }

  function segmentId(segment) {
    return String(segment.network_id || segment.segment_id || segment.scan_range || "");
  }

  function buildDistribution(devices) {
    var counts = {};
    (devices || []).forEach(function (device) {
      var key = String(device.current_scan_range || "未知网段");
      counts[key] = (counts[key] || 0) + 1;
    });
    return Object.keys(counts).map(function (key) {
      return { segment: key, count: counts[key] };
    }).sort(function (left, right) {
      return right.count - left.count || String(left.segment).localeCompare(String(right.segment), "zh-CN");
    });
  }

  function renderAll() {
    renderGlobalScan();
    renderDashboard();
    renderDiscovery();
    renderInventory();
    renderLookup();
    renderAnalysis();
    renderHistory();
    renderSettings();
  }

  function renderGlobalScan() {
    var pill = byId("globalScanPill");
    var text = byId("globalScanText");
    if (!pill || !text) {
      return;
    }
    var meta = U.scanStatusMeta(state.scan);
    pill.classList.remove("is-idle", "is-running", "is-success", "is-warning", "is-danger");
    var toneClass = meta.tone === "primary" ? "is-running" : (meta.tone === "success" ? "is-success" : (meta.tone === "warning" ? "is-warning" : (meta.tone === "danger" ? "is-danger" : "is-idle")));
    pill.classList.add(toneClass);
    if (state.scan.running) {
      text.textContent = "扫描 " + percentOf(state.scan) + "%";
    } else {
      text.textContent = meta.label;
    }
    pill.title = meta.detail;
  }

  function percentOf(scan) {
    var total = Number(scan && scan.total_hosts) || 0;
    var done = Number(scan && scan.probed_hosts) || 0;
    if (scan && scan.status === "completed") {
      return 100;
    }
    if (total <= 0) {
      return Math.max(0, Math.min(100, Number(scan && scan.progress_percent) || 0));
    }
    return Math.max(0, Math.min(100, Math.round(done * 100 / total)));
  }

  function renderDashboard() {
    var summary = U.summarizeDevices(state.devices);
    var stats = [
      { icon: "i-monitor", tone: "", value: summary.total, label: "设备总数", meta: summary.unknown + " 台状态未知" },
      { icon: "i-wifi", tone: "success", value: summary.online, label: "在线设备", meta: "本轮扫描确认" },
      { icon: "i-clock", tone: "warning", value: summary.offline, label: "离线设备", meta: "超过离线阈值或未出现" },
      { icon: "i-star", tone: "info", value: summary.favorites, label: "收藏设备", meta: "手动标记的重点设备" }
    ];
    setHtml("dashboardStats", stats.map(function (item) {
      return '<article class="stat-card"><span class="stat-icon ' + item.tone + '">' + icon(item.icon) + '</span><span class="stat-copy"><span class="stat-value">' + item.value + '</span><span class="stat-label">' + esc(item.label) + '</span><span class="stat-meta">' + esc(item.meta) + "</span></span></article>";
    }).join(""));

    var warnings = byId("dashboardWarnings");
    if (warnings) {
      if (state.warnings.length) {
        warnings.hidden = false;
        warnings.innerHTML = state.warnings.slice(0, 5).map(function (warning) {
          return '<div class="alert-item">' + icon("i-alert") + "<span>" + esc(warning) + "</span></div>";
        }).join("");
      } else {
        warnings.hidden = true;
        warnings.innerHTML = "";
      }
    }

    renderDashboardNetwork();
    renderRecentDevices();
    renderDashboardTable();
  }

  function renderDashboardNetwork() {
    var container = byId("dashboardNetwork");
    if (!container) {
      return;
    }
    if (!state.interfaces.length) {
      container.innerHTML = emptyMarkup("未发现可用 IPv4 网卡", "请确认网卡已连接网络并启用 IPv4。", "i-wifi");
      return;
    }
    container.innerHTML = state.interfaces.slice(0, 4).map(function (item) {
      var tags = '<span class="network-badge ' + (item.is_virtual ? "virtual" : "good") + '">' + (item.is_virtual ? "虚拟网卡" : "物理网卡") + "</span>";
      if (!item.is_up) {
        tags += '<span class="network-badge virtual">未启用</span>';
      }
      return '<div class="network-item"><div class="network-main"><div class="network-title"><strong>' + esc(item.alias || item.interface_id || "网卡") + "</strong>" + tags + '</div><div class="network-meta">' + esc(item.description || "IPv4 接口") + " · 网关 " + esc(item.gateway || "未配置") + " · DNS " + esc((item.dns_servers || []).join(", ") || "自动") + '</div></div><div class="network-values"><code>' + esc(item.local_ip || "—") + "</code><span>" + esc(item.scan_range || "") + "</span></div></div>";
    }).join("");
  }

  function renderRecentDevices() {
    var container = byId("recentDevices");
    if (!container) {
      return;
    }
    var recent = state.devices.slice().sort(function (left, right) {
      return new Date(right.last_seen_at || 0).getTime() - new Date(left.last_seen_at || 0).getTime();
    }).slice(0, 5);
    if (!recent.length) {
      container.innerHTML = emptyMarkup("尚无设备档案", "运行一次网络扫描后，这里会显示最近发现的设备。", "i-inventory");
      return;
    }
    container.innerHTML = recent.map(function (device) {
      var badge = deviceStatusBadge(device.status);
      return '<button class="recent-item" type="button" data-action="open-device" data-device-id="' + attr(device.device_id) + '"><span class="recent-main"><span class="status-dot ' + U.statusMeta(device.status).dot + '"></span><span><span class="recent-name">' + esc(device.effective_name || device.current_ip || "未命名设备") + '</span><span class="recent-meta"><code>' + esc(device.current_ip || "—") + "</code><span>" + esc(device.vendor || "厂商未知") + "</span>" + badge + '</span></span></span><span class="recent-time">' + esc(U.formatRelative(device.last_seen_at)) + "</span></button>";
    }).join("");
  }

  function deviceStatusBadge(status) {
    var meta = U.statusMeta(status);
    return '<span class="status-badge ' + meta.className + '"><span class="status-dot ' + meta.dot + '"></span>' + meta.label + "</span>";
  }

  function dashboardSortControl(key, label) {
    var active = state.dashboardSortKey === key;
    var arrow = active ? (state.dashboardSortDirection === "ascending" ? "i-arrow-up" : "i-arrow-down") : "";
    return '<button class="sort-button" type="button" data-action="dashboard-sort" data-key="' + attr(key) + '">' + esc(label) + (arrow ? icon(arrow) : "") + "</button>";
  }

  function deviceTableMarkup(devices, options) {
    var opts = options || {};
    var selectable = Boolean(opts.selectable);
    var selected = opts.selected || new Set();
    var sortControl = opts.sortControl || function (key, label) { return esc(label); };
    var currentIds = devices.map(function (device) { return String(device.device_id || ""); });
    var allSelected = currentIds.length > 0 && currentIds.every(function (id) { return selected.has(id); });
    var header = '<tr>' + (selectable ? '<th class="cell-checkbox"><input class="segment-check" type="checkbox" data-inventory-select-all aria-label="选择当前页全部设备"' + (allSelected ? " checked" : "") + "></th>" : "") +
      "<th>" + sortControl("name", "设备") + "</th><th>" + sortControl("status", "状态") + "</th><th>" + sortControl("ip", "IP 地址") + "</th><th>" + sortControl("mac", "MAC 地址") + "</th><th>" + sortControl("vendor", "厂商") + "</th><th>" + sortControl("last_seen_at", "最近发现") + '</th><th class="cell-actions">操作</th></tr>';
    var rows = devices.map(function (device) {
      var id = String(device.device_id || "");
      var isSelected = selected.has(id);
      var name = device.effective_name || device.current_ip || "未命名设备";
      var favorite = device.favorite ? icon("i-star", "favorite-mark") : "";
      var copyIp = device.current_ip ? '<button class="icon-action" type="button" data-action="copy-text" data-copy="' + attr(device.current_ip) + '" aria-label="复制 IP" title="复制 IP">' + icon("i-copy") + "</button>" : "";
      var copyMac = device.primary_mac ? '<button class="icon-action" type="button" data-action="copy-text" data-copy="' + attr(device.primary_mac) + '" aria-label="复制 MAC" title="复制 MAC">' + icon("i-copy") + "</button>" : "";
      return '<tr data-device-open="' + attr(id) + '"' + (isSelected ? ' class="is-selected"' : "") + ">" +
        (selectable ? '<td class="cell-checkbox"><input class="segment-check" type="checkbox" data-inventory-select="' + attr(id) + '" aria-label="选择 ' + attr(name) + '"' + (isSelected ? " checked" : "") + "></td>" : "") +
        '<td><button class="cell-main cell-button" type="button" data-action="open-device" data-device-id="' + attr(id) + '">' + esc(name) + "</button><span class=\"cell-sub\">" + favorite + (device.is_random_mac ? " 随机 MAC" : "") + "</span></td>" +
        "<td>" + deviceStatusBadge(device.status) + "</td>" +
        '<td><span class="ip-mono">' + esc(device.current_ip || "—") + "</span></td>" +
        '<td><span class="mac-mono">' + esc(device.primary_mac || "—") + "</span></td>" +
        "<td>" + esc(device.vendor || "未识别") + "</td>" +
        '<td><span class="text-small">' + esc(U.formatRelative(device.last_seen_at)) + '</span><span class="cell-sub" title="' + attr(U.formatDate(device.last_seen_at)) + '">' + esc(device.current_scan_range || device.current_interface || "—") + "</span></td>" +
        '<td class="cell-actions"><span class="row-actions">' + copyIp + copyMac + '<button class="icon-action" type="button" data-action="open-device" data-device-id="' + attr(id) + '" aria-label="查看详情" title="查看详情">' + icon("i-chevron-right") + "</button></span></td></tr>";
    }).join("");
    return '<table class="data-table"><thead>' + header + "</thead><tbody>" + rows + "</tbody></table>";
  }

  function renderDashboardTable() {
    var container = byId("dashboardTable");
    if (!container) {
      return;
    }
    var filtered = U.filterDevices(state.devices, state.dashboardQuery, { status: state.dashboardStatus });
    var sorted = U.sortDevices(filtered, state.dashboardSortKey, state.dashboardSortDirection);
    var visible = sorted.slice(0, 50);
    setText("dashboardTableSummary", filtered.length ? "显示 " + visible.length + " / " + filtered.length + " 台设备" : "0 台设备");
    var search = byId("dashboardSearch");
    if (search && search.value !== state.dashboardQuery) {
      search.value = state.dashboardQuery;
    }
    var status = byId("dashboardStatus");
    if (status && status.value !== state.dashboardStatus) {
      status.value = state.dashboardStatus;
    }
    if (!visible.length) {
      container.innerHTML = emptyMarkup(state.devices.length ? "没有符合条件的设备" : "尚无设备档案", state.devices.length ? "调整搜索关键字或状态筛选。" : "运行网络扫描后设备会自动写入档案。", "i-inventory");
      return;
    }
    container.innerHTML = deviceTableMarkup(visible, { sortControl: dashboardSortControl });
  }

  function renderDiscovery() {
    renderSegmentOptions();
    renderScanConfigHint();
    updateRangeHint(false);
    renderScanPanel();
  }

  function renderSegmentOptions() {
    var container = byId("segmentOptions");
    if (!container) {
      return;
    }
    if (!state.segments.length) {
      container.innerHTML = emptyMarkup("未发现可扫描网段", "可在右侧输入自定义 CIDR、IP 范围或单个 IP。", "i-network");
      return;
    }
    var interfaces = {};
    state.interfaces.forEach(function (item) {
      interfaces[String(item.interface_id || "")] = item;
    });
    container.innerHTML = state.segments.map(function (segment) {
      var id = segmentId(segment);
      var iface = interfaces[String(segment.interface_id || "")] || {};
      var label = segment.interface || segment.alias || iface.alias || "未知网卡";
      var checked = state.selectedSegments.has(id);
      var tags = '<span class="tag">' + esc(segment.scan_range || "未知网段") + "</span>";
      if (iface.is_virtual) {
        tags += '<span class="tag">虚拟</span>';
      }
      return '<label class="segment-option"><input class="segment-check" type="checkbox" data-segment-id="' + attr(id) + '"' + (checked ? " checked" : "") + '><span class="segment-body"><span class="segment-title">' + esc(label) + tags + '</span><span class="segment-meta"><code>' + esc(segment.local_ip || iface.local_ip || "—") + "</code><span>" + (Number(segment.host_count) || 0) + " 台主机</span><span>网关 " + esc(segment.gateway || iface.gateway || "无") + "</span></span></span></label>";
    }).join("");
  }

  function selectedHostCount() {
    return state.segments.reduce(function (total, segment) {
      return total + (state.selectedSegments.has(segmentId(segment)) ? (Number(segment.host_count) || 0) : 0);
    }, 0);
  }

  function renderScanConfigHint() {
    var count = state.selectedSegments.size;
    setText("scanConfigHint", "超时 " + (state.settings.scan_timeout_ms || 900) + " ms · 并发 " + (state.settings.scan_concurrency || 64) + " · 已选 " + count + " 个网段 / " + selectedHostCount() + " 台主机");
  }

  function updateRangeHint(showErrorOnly) {
    var input = byId("customRanges");
    var hint = byId("rangeHint");
    if (!input || !hint) {
      return;
    }
    var value = input.value.trim();
    if (!value) {
      hint.textContent = "支持 CIDR、起止 IP 和单个 IP；每行一个。";
      hint.classList.remove("is-error");
      return;
    }
    var result = U.validateCustomRanges(value);
    if (!result.valid) {
      hint.textContent = result.error;
      hint.classList.add("is-error");
      return;
    }
    hint.textContent = result.count + " 个自定义范围 · " + result.total + " 台主机";
    hint.classList.remove("is-error");
    if (!showErrorOnly) {
      hint.title = "自定义范围会与所选网卡网段一起扫描";
    }
  }

  function renderScanPanel() {
    var status = byId("scanStatusPanel");
    if (!status) {
      return;
    }
    var meta = U.scanStatusMeta(state.scan);
    var tone = meta.tone === "primary" ? "is-running" : (meta.tone === "success" ? "is-success" : (meta.tone === "warning" ? "is-warning" : (meta.tone === "danger" ? "is-danger" : "")));
    var orbIcon = state.scan.running ? "i-radar" : (state.scan.status === "completed" ? "i-check" : (state.scan.error ? "i-alert" : "i-clock"));
    var percent = percentOf(state.scan);
    var totalHosts = Number(state.scan.total_hosts) || 0;
    var probed = Number(state.scan.probed_hosts) || 0;
    var summaryCounts = state.scan.summary || {};
    status.innerHTML = '<div class="scan-status ' + tone + '"><span class="status-orb">' + icon(orbIcon) + '</span><span class="scan-status-copy"><strong>' + esc(meta.label) + "</strong><span>" + esc(meta.detail) + "</span></span></div>" +
      '<div class="scan-summary-grid"><div class="scan-summary-item"><span>发现设备</span><strong>' + (Number(summaryCounts.found_devices) || (state.scan.results || []).length) + '</strong></div><div class="scan-summary-item"><span>新增设备</span><strong>' + (Number(summaryCounts.new_devices) || 0) + '</strong></div><div class="scan-summary-item"><span>IP 变更</span><strong>' + (Number(summaryCounts.ip_changes) || 0) + "</strong></div></div>";

    if (state.scan.running) {
      setText("scanProgressLabel", state.scan.paused ? "扫描已暂停" : "正在扫描");
    } else if (state.scan.status === "completed") {
      setText("scanProgressLabel", "扫描完成");
    } else if (state.scan.status === "stopped") {
      setText("scanProgressLabel", "扫描已停止");
    } else if (state.scan.status === "error" || state.scan.status === "interrupted") {
      setText("scanProgressLabel", "扫描失败");
    } else {
      setText("scanProgressLabel", "等待开始");
    }
    setText("scanProgressValue", percent + "%");
    var bar = byId("scanProgressBar");
    if (bar) {
      bar.style.width = percent + "%";
    }
    setText("scanHostCounts", probed + " / " + totalHosts + " 台主机");
    setText("scanCurrentRange", state.scan.current_range || state.scan.current_interface || (state.selectedSegments.size ? "已选择 " + state.selectedSegments.size + " 个网段" : "未选择扫描范围"));

    var warnings = byId("scanWarnings");
    var scanWarnings = Array.isArray(state.scan.warnings) ? state.scan.warnings : [];
    if (warnings) {
      if (state.scan.error) {
        scanWarnings = [state.scan.error].concat(scanWarnings);
      }
      warnings.hidden = scanWarnings.length === 0;
      warnings.innerHTML = scanWarnings.slice(0, 6).map(function (warning) {
        return '<div class="alert-item">' + icon("i-alert") + "<span>" + esc(warning) + "</span></div>";
      }).join("");
    }

    var pause = byId("pauseScanButton");
    var resume = byId("resumeScanButton");
    var stop = byId("stopScanButton");
    var exportButton = byId("exportScanButton");
    var start = document.querySelector('[data-action="start-scan"]');
    if (pause) { pause.disabled = !state.scan.running || state.scan.paused; }
    if (resume) { resume.disabled = !state.scan.running || !state.scan.paused; }
    if (stop) { stop.disabled = !state.scan.running; }
    if (start) { start.disabled = Boolean(state.scan.running); }
    if (exportButton) { exportButton.disabled = !state.scan.task_id; }

    renderScanResults();
  }

  function scanStateBadge(value) {
    var lookup = {
      LOCAL: ["本机在线", "is-success", "success"],
      REACHABLE: ["可达", "is-online", "success"],
      DELAY: ["延迟", "is-unknown", "unknown"],
      PROBE: ["探测中", "is-running", "unknown"],
      STALE: ["ARP 缓存", "is-neutral", "unknown"],
      OFFLINE: ["离线", "is-offline", "danger"]
    };
    var found = lookup[String(value || "").toUpperCase()] || [String(value || "未知"), "is-neutral", "unknown"];
    return '<span class="status-badge ' + found[1] + '"><span class="status-dot ' + found[2] + '"></span>' + esc(found[0]) + "</span>";
  }

  function renderScanResults() {
    var container = byId("discoveryResults");
    if (!container) {
      return;
    }
    var results = Array.isArray(state.scan.results) ? state.scan.results.slice() : [];
    results.sort(function (left, right) {
      return U.compareIp(left.ip, right.ip) || String(left.mac || "").localeCompare(String(right.mac || ""));
    });
    setText("scanResultCount", String(results.length));
    if (!results.length) {
      container.innerHTML = emptyMarkup(state.scan.running ? "正在等待扫描结果" : "尚无本轮结果", state.scan.running ? "真实探测结果会实时出现在这里。" : "开始扫描后，ICMP 与 ARP 命中的设备会显示在这里。", "i-radar");
      return;
    }
    var rows = results.map(function (item, index) {
      var mac = item.mac || "";
      return "<tr><td>" + (index + 1) + '</td><td><span class="ip-mono">' + esc(item.ip || "—") + '</span></td><td><span class="mac-mono">' + esc(mac || "未获取") + "</span></td><td>" + scanStateBadge(item.state) + "</td><td>" + esc(item.interface || "—") + "</td><td>" + esc(item.scan_range || "—") + '</td><td><span class="row-actions"><button class="icon-action" type="button" data-action="copy-text" data-copy="' + attr(item.ip) + '" title="复制 IP" aria-label="复制 IP">' + icon("i-copy") + "</button>" + (mac ? '<button class="icon-action" type="button" data-action="copy-text" data-copy="' + attr(mac) + '" title="复制 MAC" aria-label="复制 MAC">' + icon("i-copy") + "</button>" : "") + "</span></td></tr>";
    }).join("");
    container.innerHTML = '<table class="data-table"><thead><tr><th>#</th><th>IP 地址</th><th>MAC 地址</th><th>状态</th><th>网卡</th><th>扫描范围</th><th class="cell-actions">复制</th></tr></thead><tbody>' + rows + "</tbody></table>";
  }

  function filteredInventory() {
    var filtered = U.filterDevices(state.devices, state.inventoryQuery, {
      status: state.inventoryStatus,
      favorite: state.inventoryFavorite
    });
    return U.sortDevices(filtered, state.inventorySortKey, state.inventorySortDirection);
  }

  function renderInventory() {
    var container = byId("inventoryTable");
    if (!container) {
      return;
    }
    var search = byId("inventorySearch");
    if (search && search.value !== state.inventoryQuery) {
      search.value = state.inventoryQuery;
    }
    var sortSelect = byId("inventorySort");
    var sortValue = state.inventorySortKey + ":" + state.inventorySortDirection;
    if (sortSelect && sortSelect.value !== sortValue) {
      sortSelect.value = sortValue;
    }
    var pageSize = byId("inventoryPageSize");
    if (pageSize) {
      pageSize.value = String(state.inventoryPageSize);
    }
    var tabs = byId("inventoryFilterTabs");
    if (tabs) {
      Array.prototype.forEach.call(tabs.querySelectorAll(".filter-tab"), function (tab) {
        var isFavorite = tab.dataset.status === "favorite";
        var active = isFavorite ? state.inventoryFavorite : (!state.inventoryFavorite && tab.dataset.status === state.inventoryStatus);
        tab.classList.toggle("is-active", active);
        tab.setAttribute("aria-selected", active ? "true" : "false");
      });
    }
    var filtered = filteredInventory();
    var pageCount = Math.max(1, Math.ceil(filtered.length / state.inventoryPageSize));
    state.inventoryPage = Math.max(1, Math.min(pageCount, state.inventoryPage));
    var start = (state.inventoryPage - 1) * state.inventoryPageSize;
    var visible = filtered.slice(start, start + state.inventoryPageSize);
    state.inventoryPageIds = visible.map(function (device) { return String(device.device_id || ""); });
    setText("inventoryPaginationLabel", filtered.length ? (start + 1) + "–" + (start + visible.length) + " / " + filtered.length + " 台设备" : "0 台设备");
    renderPagination(pageCount);
    renderBulkBar();
    if (!visible.length) {
      container.innerHTML = emptyMarkup(state.devices.length ? "没有符合条件的设备" : "尚无设备档案", state.devices.length ? "调整筛选条件或清空搜索关键字。" : "运行网络扫描后设备会自动写入档案。", "i-inventory");
      return;
    }
    container.innerHTML = deviceTableMarkup(visible, { selectable: true, selected: state.inventorySelected });
  }

  function renderPagination(pageCount) {
    var container = byId("inventoryPagination");
    if (!container) {
      return;
    }
    if (pageCount <= 1) {
      container.innerHTML = "";
      return;
    }
    var pages = [];
    var first = Math.max(1, state.inventoryPage - 2);
    var last = Math.min(pageCount, state.inventoryPage + 2);
    for (var page = first; page <= last; page += 1) {
      pages.push(page);
    }
    var html = '<button class="page-button" type="button" data-action="inventory-page" data-page="' + (state.inventoryPage - 1) + '" aria-label="上一页"' + (state.inventoryPage <= 1 ? " disabled" : "") + ">" + icon("i-chevron-left") + "</button>";
    if (pages[0] > 1) {
      html += '<button class="page-button" type="button" data-action="inventory-page" data-page="1">1</button><span class="muted">…</span>';
    }
    pages.forEach(function (page) {
      html += '<button class="page-button' + (page === state.inventoryPage ? " is-active" : "") + '" type="button" data-action="inventory-page" data-page="' + page + '">' + page + "</button>";
    });
    if (pages[pages.length - 1] < pageCount) {
      html += '<span class="muted">…</span><button class="page-button" type="button" data-action="inventory-page" data-page="' + pageCount + '">' + pageCount + "</button>";
    }
    html += '<button class="page-button" type="button" data-action="inventory-page" data-page="' + (state.inventoryPage + 1) + '" aria-label="下一页"' + (state.inventoryPage >= pageCount ? " disabled" : "") + ">" + icon("i-chevron-right") + "</button>";
    container.innerHTML = html;
  }

  function renderBulkBar() {
    var bar = byId("bulkBar");
    if (!bar) {
      return;
    }
    var count = state.inventorySelected.size;
    bar.hidden = count === 0;
    setText("bulkCount", String(count));
  }

  function renderLookup() {
    var container = byId("lookupResults");
    if (!container) {
      return;
    }
    if (state.lookupError) {
      container.innerHTML = '<section class="card lookup-input-card"><div class="lookup-error">' + esc(state.lookupError) + "</div></section>";
      return;
    }
    if (!state.lookupPayload) {
      return;
    }
    var queries = Array.isArray(state.lookupPayload.queries) ? state.lookupPayload.queries : [];
    if (!queries.length) {
      container.innerHTML = emptyMarkup("没有查询结果", "请输入至少一个 MAC 地址、前缀或后缀。", "i-search");
      return;
    }
    var labels = { full: "完整 MAC", prefix: "MAC 前缀", suffix: "MAC 后缀", invalid: "格式无效" };
    container.innerHTML = queries.map(function (query) {
      var matches = Array.isArray(query.matches) ? query.matches : [];
      var head = '<div class="lookup-query-head"><div class="lookup-query-title"><code>' + esc(query.query) + '</code><span class="tag">' + esc(labels[query.kind] || query.kind || "MAC") + '</span><span class="lookup-query-meta"><span>' + esc(query.vendor || "厂商未识别") + "</span>" + (query.is_random ? "<span>本地/随机 MAC</span>" : "") + "</span></div><span class=\"status-badge is-neutral\">" + matches.length + " 个匹配</span></div>";
      var body;
      if (query.error) {
        body = '<div class="lookup-error">' + esc(query.error) + "</div>";
      } else if (!matches.length) {
        body = '<div class="lookup-empty">未在持久化设备档案中找到匹配项。</div>';
      } else {
        body = '<div class="lookup-match-list">' + matches.map(function (device) {
          return '<button class="lookup-match" type="button" data-action="open-device" data-device-id="' + attr(device.device_id) + '"><span><span class="cell-main">' + esc(device.effective_name || device.current_ip || "未命名设备") + '</span><span class="cell-sub"><span class="ip-mono">' + esc(device.current_ip || "—") + '</span> · <span class="mac-mono">' + esc(device.primary_mac || "—") + "</span></span></span><span class=\"row-actions\">" + deviceStatusBadge(device.status) + icon("i-chevron-right") + "</span></button>";
        }).join("") + "</div>";
      }
      return '<section class="card lookup-query-card">' + head + body + "</section>";
    }).join("");
  }

  function prefixToMask(prefix) {
    var value = Number(prefix);
    if (!Number.isInteger(value) || value < 0 || value > 32) {
      return "—";
    }
    var mask = value === 0 ? 0 : (0xFFFFFFFF << (32 - value)) >>> 0;
    return [(mask >>> 24) & 255, (mask >>> 16) & 255, (mask >>> 8) & 255, mask & 255].join(".");
  }

  function renderAnalysis() {
    var container = byId("analysisInterfaces");
    if (!container) {
      return;
    }
    var analysis = state.analysis || { interfaces: state.interfaces, segments: state.segments, distribution: buildDistribution(state.devices), warnings: state.warnings };
    var interfaces = Array.isArray(analysis.interfaces) ? analysis.interfaces : [];
    var warnings = Array.isArray(analysis.warnings) ? analysis.warnings : [];
    if (!interfaces.length) {
      container.innerHTML = (warnings.length ? warnings.slice(0, 3).map(function (warning) {
        return '<div class="alert-item">' + icon("i-alert") + "<span>" + esc(warning) + "</span></div>";
      }).join("") : "") + emptyMarkup("未发现可用网络接口", "请检查网卡状态与 IPv4 配置。", "i-network");
      return;
    }
    container.innerHTML = interfaces.map(function (item) {
      var tags = '<span class="tag">' + (item.is_virtual ? "虚拟" : "物理") + "</span><span class=\"tag\">" + (item.is_up === false ? "未启用" : "已启用") + "</span>";
      return '<article class="interface-card"><div class="interface-head"><div><strong>' + esc(item.alias || "网卡") + '</strong><span>' + esc(item.description || item.interface_id || "") + '</span></div><div class="interface-tags">' + tags + '</div></div><div class="interface-grid"><div class="info-pair"><span>本机 IP</span><code>' + esc(item.local_ip || "—") + '</code></div><div class="info-pair"><span>子网掩码</span><code>' + esc(prefixToMask(item.prefix_length)) + '</code></div><div class="info-pair"><span>网关</span><code>' + esc(item.gateway || "未配置") + '</code></div><div class="info-pair"><span>网段</span><code>' + esc(item.scan_range || item.network || "—") + '</code></div><div class="info-pair"><span>可扫描主机</span><strong>' + (Number(item.host_count) || 0) + '</strong></div><div class="info-pair"><span>本机 MAC</span><code>' + esc(item.local_mac || "—") + '</code></div></div><div class="dns-list">' + ((item.dns_servers || []).length ? item.dns_servers.map(function (server) { return "<code>DNS " + esc(server) + "</code>"; }).join("") : '<span class="text-small muted">未读取到 DNS 服务器</span>') + '</div>' + (item.gateway ? '<div class="interface-actions"><button class="text-button" type="button" data-action="ping-gateway" data-address="' + attr(item.gateway) + '">' + icon("i-activity") + "检测网关连通性</button></div>" : "") + "</article>";
    }).join("");
    renderDistribution();
  }

  function renderDistribution() {
    var container = byId("analysisDistribution");
    if (!container) {
      return;
    }
    var analysis = state.analysis || {};
    var rows = Array.isArray(analysis.distribution) ? analysis.distribution.slice() : buildDistribution(state.devices);
    if (!rows.length) {
      container.innerHTML = emptyMarkup("尚无设备分布数据", "完成扫描后可查看各网段设备数量。", "i-activity");
      return;
    }
    rows.sort(function (left, right) { return Number(right.count) - Number(left.count); });
    var max = Math.max.apply(Math, rows.map(function (row) { return Number(row.count) || 0; }).concat([1]));
    container.innerHTML = rows.map(function (row) {
      var width = Math.max(3, Math.round((Number(row.count) || 0) * 100 / max));
      return '<div class="distribution-row"><div class="distribution-head"><code>' + esc(row.segment || "未知网段") + "</code><strong>" + (Number(row.count) || 0) + ' 台</strong></div><div class="distribution-bar"><span style="width:' + width + '%"></span></div></div>';
    }).join("");
    renderPingResult();
  }

  function renderPingResult() {
    var container = byId("pingResults");
    if (!container) {
      return;
    }
    if (state.pingLoading) {
      container.innerHTML = loadingMarkup("正在发送 ICMP 探测");
      return;
    }
    var result = state.pingResult;
    if (!result) {
      container.innerHTML = '<div class="inline-empty">输入地址开始检测，或从上方网卡卡片选择网关。</div>';
      return;
    }
    if (result.error && !result.received) {
      container.innerHTML = '<div class="ping-error">' + esc(result.error) + "</div>" + renderPingMetrics(result);
      return;
    }
    container.innerHTML = renderPingMetrics(result) + '<div class="sample-list">' + (result.samples || []).map(function (sample) {
      var tone = sample.success ? "success" : "failure";
      var text = sample.success ? (sample.latency_ms === null ? "成功" : sample.latency_ms + " ms") : "丢失";
      return '<span class="sample-item ' + tone + '">#' + (Number(sample.sequence) || 0) + " " + esc(text) + "</span>";
    }).join("") + "</div>" + (result.error ? '<div class="ping-error" style="margin-top:8px">' + esc(result.error) + "</div>" : "");
  }

  function renderPingMetrics(result) {
    var received = Number(result.received) || 0;
    var loss = Number(result.packet_loss_percent) || 0;
    return '<div class="ping-summary"><div class="ping-metric ' + (received ? "is-good" : "is-bad") + '"><span>目标</span><strong>' + esc(result.address || "—") + '</strong></div><div class="ping-metric ' + (received === Number(result.sent) ? "is-good" : "is-bad") + '"><span>接收 / 发送</span><strong>' + received + " / " + (Number(result.sent) || 0) + '</strong></div><div class="ping-metric ' + (loss === 0 ? "is-good" : "is-bad") + '"><span>丢包</span><strong>' + loss + '%</strong></div><div class="ping-metric"><span>平均延迟</span><strong>' + (result.avg_ms === null || result.avg_ms === undefined ? "—" : esc(result.avg_ms) + " ms") + '</strong></div><div class="ping-metric"><span>最小 / 最大</span><strong>' + (result.min_ms === null || result.min_ms === undefined ? "—" : esc(result.min_ms) + " / " + esc(result.max_ms) + " ms") + "</strong></div></div>";
  }

  function historyStatusMeta(status) {
    var value = String(status || "idle");
    if (value === "completed") { return { label: "完成", tone: "is-success" }; }
    if (value === "stopped") { return { label: "已停止", tone: "is-warning" }; }
    if (value === "error" || value === "interrupted") { return { label: "失败", tone: "is-danger" }; }
    if (value === "paused") { return { label: "已暂停", tone: "is-warning" }; }
    if (value === "running" || value === "scanning" || value === "discovering") { return { label: "运行中", tone: "is-running" }; }
    return { label: value, tone: "is-neutral" };
  }

  function historyScope(task) {
    var custom = Array.isArray(task.custom_ranges) ? task.custom_ranges : [];
    var networks = Array.isArray(task.network_ids) ? task.network_ids : [];
    if (custom.length && networks.length) {
      return custom.join("、") + " + " + networks.length + " 个网段";
    }
    if (custom.length) {
      return "自定义：" + custom.join("、");
    }
    if (networks.length) {
      return networks.join("、");
    }
    return "全部可用网段";
  }

  function renderHistory() {
    var container = byId("historyTable");
    if (!container) {
      return;
    }
    if (!state.history.length) {
      container.innerHTML = emptyMarkup("尚无扫描历史", "每次扫描完成、停止或失败后都会保存记录。", "i-history");
      return;
    }
    var rows = state.history.map(function (task) {
      var meta = historyStatusMeta(task.status);
      return '<tr><td><button class="cell-main cell-button" type="button" data-action="open-history" data-task-id="' + attr(task.task_id) + '">' + esc(U.formatDate(task.started_at)) + '</button><span class="cell-sub">' + esc(task.trigger === "auto" ? "自动扫描" : "手动扫描") + "</span></td><td><span class=\"status-badge " + meta.tone + '">' + esc(meta.label) + "</span></td><td><span class=\"cell-main\">" + esc(historyScope(task)) + '</span><span class="cell-sub">' + (Number(task.total_hosts) || 0) + " 台主机</span></td><td>" + (Number(task.found_devices) || 0) + "</td><td>" + (Number(task.new_devices) || 0) + "</td><td>" + (Number(task.offline_devices) || 0) + "</td><td>" + (Number(task.ip_changes) || 0) + '</td><td class="cell-actions"><span class="row-actions"><button class="icon-action" type="button" data-action="open-history" data-task-id="' + attr(task.task_id) + '" title="查看详情" aria-label="查看详情">' + icon("i-chevron-right") + '</button><button class="icon-action" type="button" data-action="export-history" data-task-id="' + attr(task.task_id) + '" title="导出 CSV" aria-label="导出 CSV">' + icon("i-download") + "</button></span></td></tr>";
    }).join("");
    container.innerHTML = '<table class="data-table"><thead><tr><th>扫描时间</th><th>状态</th><th>扫描范围</th><th>发现</th><th>新增</th><th>离线</th><th>IP 变化</th><th class="cell-actions">操作</th></tr></thead><tbody>' + rows + "</tbody></table>";
  }

  function renderSettings() {
    var settings = state.settings || {};
    var fields = {
      settingTimeout: settings.scan_timeout_ms,
      settingConcurrency: settings.scan_concurrency,
      settingOffline: settings.offline_after_minutes,
      settingAutoInterval: settings.auto_scan_interval_minutes,
      settingDataDir: settings.data_dir || ""
    };
    Object.keys(fields).forEach(function (id) {
      var field = byId(id);
      if (field && document.activeElement !== field) {
        field.value = fields[id] === undefined ? "" : String(fields[id]);
      }
    });
    var checks = {
      settingAutoScan: settings.auto_scan_enabled,
      settingCsvBom: settings.csv_bom,
      settingCsvHeader: settings.csv_include_header,
      settingCsvNotes: settings.csv_include_notes
    };
    Object.keys(checks).forEach(function (id) {
      var field = byId(id);
      if (field) {
        field.checked = Boolean(checks[id]);
      }
    });
    var theme = byId("settingTheme");
    if (theme) {
      theme.value = settings.theme === "dark" ? "dark" : "light";
    }
    var select = byId("settingInterface");
    if (select) {
      var current = String(settings.default_interface_id || "");
      var options = ['<option value="">自动选择全部可用网段</option>'];
      var found = false;
      state.interfaces.forEach(function (item) {
        var id = String(item.interface_id || "");
        if (id === current) {
          found = true;
        }
        options.push('<option value="' + attr(id) + '">' + esc(item.alias || id) + (item.is_virtual ? "（虚拟）" : "") + "</option>");
      });
      if (current && !found) {
        options.push('<option value="' + attr(current) + '">已保存的网卡（当前不可用）</option>');
      }
      select.innerHTML = options.join("");
      select.value = current;
    }
    setText("aboutVersion", state.appVersion || "2.0.0");
    setText("aboutSchema", "Schema " + (settings.schema_version || 2));
    var dataDir = byId("settingDataDir");
    if (dataDir) {
      dataDir.title = settings.database_path || settings.data_dir || "";
    }
    setText("sidebarVersion", "v" + (state.appVersion || "2.0.0"));
  }

  function navigate(view) {
    var name = VIEW_META[view] ? view : "dashboard";
    state.currentView = name;
    document.body.classList.remove("sidebar-open");
    Array.prototype.forEach.call(document.querySelectorAll(".view"), function (panel) {
      panel.classList.toggle("is-active", panel.dataset.viewPanel === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (item) {
      item.classList.toggle("is-active", item.dataset.view === name);
    });
    var meta = VIEW_META[name];
    setText("topPageTitle", meta[0]);
    setText("topPageSubtitle", meta[1]);
    document.title = meta[0] + " · 网络定位";
    var main = byId("mainContent");
    if (main) {
      main.scrollTop = 0;
    }
    if (name === "analysis") {
      renderAnalysis();
      refreshAnalysis(false).catch(function (error) {
        reportError(error, "网络分析读取失败");
      });
    } else if (name === "settings") {
      renderSettings();
    } else if (name === "history") {
      renderHistory();
    } else if (name === "inventory") {
      renderInventory();
    } else if (name === "discovery") {
      renderDiscovery();
    } else if (name === "lookup") {
      renderLookup();
    } else {
      renderDashboard();
    }
  }

  async function refreshInterfaces() {
    var payload = await API.interfaces(true);
    state.interfaces = Array.isArray(payload.interfaces) ? payload.interfaces : [];
    state.segments = Array.isArray(payload.segments) ? payload.segments : [];
    state.warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
    state.analysis = {
      interfaces: state.interfaces,
      segments: state.segments,
      distribution: buildDistribution(state.devices),
      warnings: state.warnings
    };
    initializeSegments();
    renderDashboard();
    renderDiscovery();
    renderAnalysis();
    renderSettings();
    showToast("网卡和网段信息已刷新", "success");
  }

  async function refreshAnalysis(notify) {
    var payload = await API.analysis();
    state.interfaces = Array.isArray(payload.interfaces) ? payload.interfaces : state.interfaces;
    state.segments = Array.isArray(payload.segments) ? payload.segments : state.segments;
    state.analysis = {
      interfaces: state.interfaces,
      segments: state.segments,
      distribution: Array.isArray(payload.distribution) ? payload.distribution : buildDistribution(state.devices),
      warnings: Array.isArray(payload.warnings) ? payload.warnings : []
    };
    renderAnalysis();
    if (notify) {
      showToast("网络接口与设备分布已刷新", "success");
    }
  }

  function startScanPolling() {
    if (scanTimer) {
      return;
    }
    pollScan();
    scanTimer = window.setInterval(pollScan, 1600);
  }

  async function pollScan() {
    if (state.scanPollBusy) {
      return;
    }
    state.scanPollBusy = true;
    try {
      var payload = await API.scanStatus();
      var previous = state.scan || {};
      var wasRunning = Boolean(previous.running) || state.scanWasRunning;
      state.scan = payload.scan || {};
      if (state.scan.running) {
        state.scanWasRunning = true;
      }
      renderGlobalScan();
      if (state.bootstrapped) {
        renderScanPanel();
      }
      if (!state.scan.running && state.scan.task_id && state.scan.task_id !== state.finalizedTaskId) {
        state.finalizedTaskId = state.scan.task_id;
        state.scanWasRunning = false;
        if (wasRunning) {
          var meta = U.scanStatusMeta(state.scan);
          showToast(meta.label + (state.scan.summary ? "：发现 " + (Number(state.scan.summary.found_devices) || 0) + " 台设备" : ""), state.scan.status === "completed" ? "success" : (state.scan.status === "error" ? "error" : "warning"));
          await loadBootstrap({ silent: true });
        }
      }
    } catch (error) {
      if (state.bootstrapped) {
        setConnection(false, error && error.message ? error.message : "扫描状态读取失败");
      }
    } finally {
      state.scanPollBusy = false;
    }
  }

  function customRangeTokens() {
    var input = byId("customRanges");
    if (!input) {
      return [];
    }
    return input.value.split(/[\r\n,;，；]+/).map(function (item) { return item.trim(); }).filter(Boolean);
  }

  async function startScan() {
    if (state.scan.running) {
      showToast("已有扫描任务正在运行", "warning");
      return;
    }
    var ids = Array.from(state.selectedSegments);
    var tokens = customRangeTokens();
    var validation = U.validateCustomRanges(tokens.join("\n"));
    if (!validation.valid) {
      updateRangeHint(true);
      showToast(validation.error, "error");
      return;
    }
    if (!ids.length && !tokens.length) {
      showToast("请选择至少一个网段或输入自定义范围", "warning");
      return;
    }
    var button = document.querySelector('[data-action="start-scan"]');
    if (button) {
      button.disabled = true;
    }
    try {
      var payload = await API.startScan(ids, tokens);
      state.scan = payload.scan || state.scan;
      state.scanWasRunning = true;
      state.finalizedTaskId = "";
      renderGlobalScan();
      renderScanPanel();
      showToast("扫描任务已启动，可切换到其他页面", "success");
    } catch (error) {
      if (button) {
        button.disabled = false;
      }
      throw error;
    }
  }

  async function changeScan(action) {
    var payload;
    if (action === "pause") {
      payload = await API.pauseScan();
    } else if (action === "resume") {
      payload = await API.resumeScan();
    } else {
      payload = await API.stopScan();
    }
    state.scan = payload.scan || state.scan;
    renderGlobalScan();
    renderScanPanel();
    showToast(payload.changed ? (action === "pause" ? "扫描已暂停" : action === "resume" ? "扫描已恢复" : "正在停止扫描") : "当前扫描状态无需变更", payload.changed ? "success" : "warning");
  }

  async function openDevice(deviceId) {
    openDrawer("设备详情", "正在读取设备档案", loadingMarkup("正在读取设备档案"));
    try {
      var payload = await API.device(deviceId);
      renderDeviceDrawer(payload.device);
    } catch (error) {
      setHtml("drawerBody", errorMarkup(error.message));
      throw error;
    }
  }

  function renderDeviceDrawer(device) {
    if (!device) {
      setHtml("drawerBody", errorMarkup("设备不存在或已被删除"));
      return;
    }
    setText("drawerTitle", device.effective_name || device.current_ip || "设备档案");
    setText("drawerKicker", "设备详情");
    var ip = device.current_ip || "";
    var mac = device.primary_mac || "";
    var randomNote = device.is_random_mac ? '<span class="tag">本地/随机 MAC</span>' : "";
    var addresses = Array.isArray(device.addresses) ? device.addresses : [];
    var events = Array.isArray(device.events) ? device.events : [];
    var recent = Array.isArray(device.recent_results) ? device.recent_results : [];
    var html = '<div class="device-hero"><span class="device-avatar">' + icon("i-monitor") + '</span><span class="device-hero-copy"><strong>' + esc(device.effective_name || "未命名设备") + '</strong><span>' + deviceStatusBadge(device.status) + " " + randomNote + '</span></span><span class="device-hero-actions">' + (ip ? '<button class="icon-action" type="button" data-action="copy-text" data-copy="' + attr(ip) + '" title="复制 IP" aria-label="复制 IP">' + icon("i-copy") + "</button>" : "") + (mac ? '<button class="icon-action" type="button" data-action="copy-text" data-copy="' + attr(mac) + '" title="复制 MAC" aria-label="复制 MAC">' + icon("i-copy") + "</button>" : "") + "</span></div>" +
      '<div class="detail-grid"><div class="info-pair"><span>当前 IP</span><code>' + esc(ip || "—") + '</code></div><div class="info-pair"><span>MAC 地址</span><code>' + esc(mac || "未获取") + '</code></div><div class="info-pair"><span>厂商</span><strong>' + esc(device.vendor || "未识别") + '</strong></div><div class="info-pair"><span>当前网卡</span><strong>' + esc(device.current_interface || "—") + '</strong></div><div class="info-pair"><span>首次发现</span><strong>' + esc(U.formatDate(device.first_seen_at)) + '</strong></div><div class="info-pair"><span>最近发现</span><strong>' + esc(U.formatDate(device.last_seen_at)) + '</strong></div></div>' +
      '<section class="detail-section"><h3>设备档案</h3><form class="detail-form" id="deviceEditForm" data-device-id="' + attr(device.device_id) + '"><label class="form-field"><span>设备名称</span><input id="drawerDisplayName" type="text" maxlength="80" value="' + attr(device.display_name || "") + '" placeholder="留空则使用自动名称"></label><label class="form-field"><span>备注</span><textarea id="drawerNote" rows="3" maxlength="500" placeholder="位置、用途或维护说明">' + esc(device.note || "") + '</textarea></label><label class="toggle-row"><input id="drawerFavorite" type="checkbox"' + (device.favorite ? " checked" : "") + '><span class="toggle-ui"></span><span><strong>收藏此设备</strong><small>收藏状态不会被扫描结果覆盖。</small></span></label><p class="field-hint" id="drawerFormMessage"></p><div class="detail-save-row"><button class="btn btn-primary btn-sm" type="submit">' + icon("i-check") + "保存设备档案</button></div></form></section>" +
      '<section class="detail-section"><h3>IP 历史 <span class="count-badge">' + addresses.length + '</span></h3>' + (addresses.length ? '<div class="detail-table-wrap"><table class="detail-table"><thead><tr><th>IP</th><th>网卡</th><th>最近发现</th><th>状态</th></tr></thead><tbody>' + addresses.map(function (address) {
        return "<tr><td><code>" + esc(address.ip || "—") + "</code></td><td>" + esc(address.interface || "—") + "<br><span class=\"muted\">" + esc(address.scan_range || "") + "</span></td><td>" + esc(U.formatDate(address.last_seen_at)) + "</td><td>" + (address.is_current ? "当前" : "历史") + "</td></tr>";
      }).join("") + "</tbody></table></div>" : '<div class="drawer-empty">尚无地址记录。</div>') + "</section>" +
      '<section class="detail-section"><h3>设备事件</h3>' + (events.length ? '<div class="event-list">' + events.slice(0, 40).map(function (event) {
        var detail = event.detail || {};
        var tone = event.event_type === "offline" ? "is-danger" : (event.event_type === "came_online" || event.event_type === "discovered" ? "is-success" : "");
        return '<div class="event-item"><span class="event-dot ' + tone + '"></span><span class="event-copy"><strong>' + esc(eventLabel(event.event_type)) + '</strong><span>' + esc(eventDetailText(event)) + " · " + esc(U.formatDate(event.created_at)) + "</span></span></div>";
      }).join("") + "</div>" : '<div class="drawer-empty">尚无状态变化事件。</div>') + "</section>" +
      '<section class="detail-section"><h3>最近扫描命中</h3>' + (recent.length ? '<div class="detail-table-wrap"><table class="detail-table"><thead><tr><th>时间</th><th>IP</th><th>扫描范围</th></tr></thead><tbody>' + recent.slice(0, 20).map(function (row) {
        return '<tr><td>' + esc(U.formatDate(row.scan_started_at || row.seen_at)) + "</td><td><code>" + esc(row.ip || "—") + "</code></td><td>" + esc(row.scan_range || "—") + "</td></tr>";
      }).join("") + "</tbody></table></div>" : '<div class="drawer-empty">尚无扫描命中记录。</div>') + "</section>";
    setHtml("drawerBody", html);
  }

  function eventLabel(type) {
    return {
      discovered: "新设备发现",
      ip_changed: "IP 地址变化",
      came_online: "设备恢复在线",
      offline: "设备转为离线"
    }[type] || String(type || "设备事件");
  }

  function eventDetailText(event) {
    var detail = event.detail || {};
    if (event.event_type === "ip_changed") {
      return String(detail.old_ip || "—") + " → " + String(detail.new_ip || "—");
    }
    if (event.event_type === "offline") {
      return "最后 IP " + String(detail.last_ip || "—");
    }
    return String(detail.ip || detail.state || "—");
  }

  async function openHistory(taskId) {
    openDrawer("扫描历史", "正在读取扫描详情", loadingMarkup("正在读取扫描详情"));
    try {
      var payload = await API.historyTask(taskId);
      renderHistoryDrawer(payload.task);
    } catch (error) {
      setHtml("drawerBody", errorMarkup(error.message));
      throw error;
    }
  }

  function renderHistoryDrawer(task) {
    if (!task) {
      setHtml("drawerBody", errorMarkup("扫描记录不存在"));
      return;
    }
    var meta = historyStatusMeta(task.status);
    setText("drawerKicker", "扫描任务");
    setText("drawerTitle", U.formatDate(task.started_at));
    var results = Array.isArray(task.results) ? task.results : [];
    var warnings = Array.isArray(task.warnings) ? task.warnings : [];
    var summary = '<div class="scan-summary-grid"><div class="scan-summary-item"><span>发现设备</span><strong>' + (Number(task.found_devices) || results.length) + '</strong></div><div class="scan-summary-item"><span>新增设备</span><strong>' + (Number(task.new_devices) || 0) + '</strong></div><div class="scan-summary-item"><span>离线设备</span><strong>' + (Number(task.offline_devices) || 0) + '</strong></div><div class="scan-summary-item"><span>IP 变化</span><strong>' + (Number(task.ip_changes) || 0) + '</strong></div><div class="scan-summary-item"><span>主机进度</span><strong>' + (Number(task.probed_hosts) || 0) + " / " + (Number(task.total_hosts) || 0) + '</strong></div><div class="scan-summary-item"><span>触发方式</span><strong>' + esc(task.trigger === "auto" ? "自动" : "手动") + "</strong></div></div>";
    var table = results.length ? '<div class="detail-table-wrap"><table class="detail-table"><thead><tr><th>IP</th><th>MAC</th><th>设备</th><th>状态</th><th>变化</th></tr></thead><tbody>' + results.slice().sort(function (left, right) { return U.compareIp(left.ip, right.ip); }).map(function (row) {
      return "<tr><td><code>" + esc(row.ip || "—") + "</code></td><td><code>" + esc(row.mac || "—") + "</code></td><td>" + esc(row.display_name || "—") + "</td><td>" + scanStateBadge(row.state) + "</td><td>" + (row.is_new ? '<span class="tag">新增</span> ' : "") + (row.ip_changed ? '<span class="tag">IP 变化</span>' : "") + "</td></tr>";
    }).join("") + "</tbody></table></div>" : '<div class="drawer-empty">本次扫描没有命中设备。</div>';
    var html = '<div class="device-hero"><span class="device-avatar">' + icon("i-history") + '</span><span class="device-hero-copy"><strong><span class="status-badge ' + meta.tone + '">' + esc(meta.label) + '</span></strong><span>' + esc(historyScope(task)) + '</span></span></div>' + summary +
      '<section class="detail-section"><h3>任务信息</h3><div class="detail-grid"><div class="info-pair"><span>开始时间</span><strong>' + esc(U.formatDate(task.started_at)) + '</strong></div><div class="info-pair"><span>结束时间</span><strong>' + esc(U.formatDate(task.finished_at)) + '</strong></div><div class="info-pair"><span>扫描范围</span><strong>' + esc(historyScope(task)) + '</strong></div><div class="info-pair"><span>进度</span><strong>' + (Number(task.progress_percent) || (task.status === "completed" ? 100 : 0)) + '%</strong></div></div></section>' +
      (task.error ? '<div class="detail-section"><div class="alert-item is-error">' + icon("i-alert") + "<span>" + esc(task.error) + "</span></div></div>" : "") +
      (warnings.length ? '<section class="detail-section"><h3>警告</h3><div class="alert-stack">' + warnings.map(function (warning) { return '<div class="alert-item">' + icon("i-alert") + "<span>" + esc(warning) + "</span></div>"; }).join("") + "</div></section>" : "") +
      '<section class="detail-section"><h3>扫描结果 <span class="count-badge">' + results.length + '</span></h3>' + table + '</section>' +
      '<div class="drawer-actions"><button class="btn btn-secondary btn-sm" type="button" data-action="export-history" data-task-id="' + attr(task.task_id) + '">' + icon("i-download") + "导出本次 CSV</button></div>";
    setHtml("drawerBody", html);
  }

  function openDrawer(kicker, title, bodyHtml) {
    var drawer = byId("detailDrawer");
    var backdrop = byId("drawerBackdrop");
    if (!drawer || !backdrop) {
      return;
    }
    state.drawerReturnFocus = document.activeElement;
    setText("drawerKicker", kicker);
    setText("drawerTitle", title);
    setHtml("drawerBody", bodyHtml);
    backdrop.hidden = false;
    drawer.setAttribute("aria-hidden", "false");
    document.body.classList.add("drawer-open");
    window.requestAnimationFrame(function () {
      backdrop.classList.add("is-open");
      drawer.classList.add("is-open");
    });
    var close = drawer.querySelector('[data-action="close-drawer"]');
    if (close) {
      window.setTimeout(function () { close.focus(); }, 20);
    }
  }

  function closeDrawer() {
    var drawer = byId("detailDrawer");
    var backdrop = byId("drawerBackdrop");
    if (!drawer || !backdrop) {
      return;
    }
    drawer.classList.remove("is-open");
    backdrop.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
    document.body.classList.remove("drawer-open");
    window.setTimeout(function () {
      if (!drawer.classList.contains("is-open")) {
        backdrop.hidden = true;
      }
    }, 190);
    if (state.drawerReturnFocus && typeof state.drawerReturnFocus.focus === "function") {
      state.drawerReturnFocus.focus();
    }
  }

  function downloadUrl(url) {
    var anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  async function copyText(value) {
    var text = String(value || "");
    if (!text) {
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        var textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        textarea.remove();
      }
      showToast("已复制：" + text, "success");
    } catch (error) {
      showToast("复制失败，请手动选择文本", "error");
    }
  }

  function parseSort(value) {
    var parts = String(value || "").split(":");
    return {
      key: parts[0] || "last_seen_at",
      direction: parts[1] === "ascending" ? "ascending" : "descending"
    };
  }

  function currentInventoryFilteredIds() {
    return filteredInventory().map(function (device) { return String(device.device_id || ""); });
  }

  async function handleDocumentClick(event) {
    var actionNode = event.target.closest("[data-action]");
    if (actionNode) {
      var action = actionNode.dataset.action;
      switch (action) {
        case "navigate":
          navigate(actionNode.dataset.view || "dashboard");
          break;
        case "refresh":
          await loadBootstrap({ notify: true });
          break;
        case "refresh-interfaces":
          await refreshInterfaces();
          break;
        case "refresh-analysis":
          await refreshAnalysis(true);
          break;
        case "export-all":
          downloadUrl(API.exportDevicesUrl([]));
          showToast("正在准备全部设备 CSV", "success");
          break;
        case "quick-scan":
          navigate("discovery");
          if (state.scan.running) {
            showToast("扫描任务正在运行", "warning");
          } else {
            if (!state.selectedSegments.size) {
              state.selectedSegments = new Set(state.segments.map(segmentId).filter(Boolean));
              renderSegmentOptions();
              renderScanConfigHint();
            }
            await startScan();
          }
          break;
        case "export-filtered": {
          var filteredIds = currentInventoryFilteredIds();
          if (!filteredIds.length) {
            showToast("当前筛选没有可导出的设备", "warning");
          } else {
            downloadUrl(API.exportDevicesUrl(filteredIds));
            showToast("正在导出当前列表", "success");
          }
          break;
        }
        case "export-selected":
          if (!state.inventorySelected.size) {
            showToast("请先选择要导出的设备", "warning");
          } else {
            downloadUrl(API.exportDevicesUrl(Array.from(state.inventorySelected)));
            showToast("正在导出所选设备", "success");
          }
          break;
        case "export-scan":
          if (!state.scan.task_id) {
            showToast("本轮扫描尚无任务记录", "warning");
          } else {
            downloadUrl(API.exportHistoryUrl(state.scan.task_id));
          }
          break;
        case "export-history":
          if (actionNode.dataset.taskId) {
            downloadUrl(API.exportHistoryUrl(actionNode.dataset.taskId));
          }
          break;
        case "toggle-theme": {
          var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
          applyTheme(next, true);
          showToast(next === "dark" ? "已切换为深色主题" : "已切换为浅色主题", "success");
          break;
        }
        case "toggle-sidebar":
          document.body.classList.toggle("sidebar-open");
          break;
        case "start-scan":
          await startScan();
          break;
        case "pause-scan":
          await changeScan("pause");
          break;
        case "resume-scan":
          await changeScan("resume");
          break;
        case "stop-scan":
          await changeScan("stop");
          break;
        case "select-all-segments":
          state.selectedSegments = new Set(state.segments.map(segmentId).filter(Boolean));
          renderSegmentOptions();
          renderScanConfigHint();
          showToast("已选择全部 " + state.selectedSegments.size + " 个网段", "success");
          break;
        case "inventory-status":
          state.inventoryStatus = actionNode.dataset.status || "all";
          state.inventoryFavorite = false;
          state.inventoryPage = 1;
          state.inventorySelected.clear();
          renderInventory();
          break;
        case "inventory-favorite":
          state.inventoryStatus = "all";
          state.inventoryFavorite = true;
          state.inventoryPage = 1;
          state.inventorySelected.clear();
          renderInventory();
          break;
        case "inventory-page":
          state.inventoryPage = Math.max(1, Number(actionNode.dataset.page) || 1);
          renderInventory();
          break;
        case "clear-selection":
          state.inventorySelected.clear();
          renderInventory();
          break;
        case "dashboard-sort": {
          var key = actionNode.dataset.key || "last_seen_at";
          if (state.dashboardSortKey === key) {
            state.dashboardSortDirection = state.dashboardSortDirection === "ascending" ? "descending" : "ascending";
          } else {
            state.dashboardSortKey = key;
            state.dashboardSortDirection = key === "last_seen_at" ? "descending" : "ascending";
          }
          renderDashboardTable();
          break;
        }
        case "open-device":
          await openDevice(actionNode.dataset.deviceId);
          break;
        case "open-history":
          await openHistory(actionNode.dataset.taskId);
          break;
        case "close-drawer":
          closeDrawer();
          break;
        case "clear-lookup":
          state.lookupPayload = null;
          state.lookupError = "";
          var lookupInput = byId("lookupInput");
          if (lookupInput) { lookupInput.value = ""; }
          updateLookupHint();
          byId("lookupResults").innerHTML = emptyMarkup("等待 MAC 查询", "查询结果会关联到已持久化的设备档案。", "i-search");
          break;
        case "copy-text":
          await copyText(actionNode.dataset.copy);
          break;
        case "ping-gateway": {
          var address = actionNode.dataset.address || "";
          navigate("analysis");
          var input = byId("pingAddress");
          if (input) { input.value = address; }
          if (address) { await runPing(address, 4); }
          break;
        }
        default:
          break;
      }
      return;
    }
    var row = event.target.closest("tr[data-device-open]");
    if (row && !event.target.closest("button, a, input, select, textarea")) {
      await openDevice(row.dataset.deviceOpen);
      return;
    }
    if (!event.target.closest("#sidebar") && !event.target.closest('[data-action="toggle-sidebar"]')) {
      document.body.classList.remove("sidebar-open");
    }
  }

  function handleInput(event) {
    var target = event.target;
    if (target.id === "dashboardSearch") {
      state.dashboardQuery = target.value;
      renderDashboardTable();
    } else if (target.id === "dashboardStatus") {
      state.dashboardStatus = target.value;
      renderDashboardTable();
    } else if (target.id === "inventorySearch") {
      state.inventoryQuery = target.value;
      state.inventoryPage = 1;
      state.inventorySelected.clear();
      renderInventory();
    } else if (target.id === "customRanges") {
      updateRangeHint(false);
    } else if (target.id === "lookupInput") {
      updateLookupHint();
    }
  }

  async function handleChange(event) {
    var target = event.target;
    if (target.id === "inventorySort") {
      var parsed = parseSort(target.value);
      state.inventorySortKey = parsed.key;
      state.inventorySortDirection = parsed.direction;
      state.inventoryPage = 1;
      renderInventory();
      return;
    }
    if (target.id === "inventoryPageSize") {
      state.inventoryPageSize = Math.max(1, Number(target.value) || 25);
      state.inventoryPage = 1;
      renderInventory();
      return;
    }
    if (target.id === "settingTheme") {
      applyTheme(target.value, false);
      return;
    }
    if (target.dataset.segmentId) {
      if (target.checked) {
        state.selectedSegments.add(target.dataset.segmentId);
      } else {
        state.selectedSegments.delete(target.dataset.segmentId);
      }
      renderScanConfigHint();
      return;
    }
    if (target.matches("[data-inventory-select]")) {
      var deviceId = target.dataset.inventorySelect;
      if (target.checked) {
        state.inventorySelected.add(deviceId);
      } else {
        state.inventorySelected.delete(deviceId);
      }
      renderInventory();
      return;
    }
    if (target.matches("[data-inventory-select-all]")) {
      state.inventoryPageIds.forEach(function (id) {
        if (target.checked) {
          state.inventorySelected.add(id);
        } else {
          state.inventorySelected.delete(id);
        }
      });
      renderInventory();
    }
  }

  function updateLookupHint() {
    var input = byId("lookupInput");
    var hint = byId("lookupHint");
    if (!input || !hint) {
      return;
    }
    var tokens = U.parseBatchMacs(input.value);
    if (!tokens.length) {
      hint.textContent = "一行一个，也支持逗号分隔；单次最多 64 个。";
      hint.classList.remove("is-error");
      return;
    }
    var invalid = tokens.filter(function (item) { return item.kind === "invalid"; }).length;
    hint.textContent = tokens.length + " 个查询" + (invalid ? " · " + invalid + " 个格式无效" : " · 格式有效") + (tokens.length > 64 ? " · 超过 64 个上限" : "");
    hint.classList.toggle("is-error", invalid > 0 || tokens.length > 64);
  }

  async function runPing(address, count) {
    state.pingLoading = true;
    state.pingResult = null;
    renderPingResult();
    try {
      var payload = await API.ping(address, count);
      state.pingResult = payload.result;
    } catch (error) {
      state.pingResult = { address: address, sent: count, received: 0, packet_loss_percent: 100, avg_ms: null, min_ms: null, max_ms: null, samples: [], error: error.message };
    } finally {
      state.pingLoading = false;
      renderPingResult();
    }
  }

  async function handleSubmit(event) {
    var form = event.target;
    if (form.id === "lookupForm") {
      event.preventDefault();
      var input = byId("lookupInput");
      var value = input ? input.value : "";
      var tokens = U.parseBatchMacs(value);
      if (!tokens.length) {
        state.lookupError = "请输入至少一个 MAC 地址、前缀或后缀";
        renderLookup();
        return;
      }
      if (tokens.length > 64) {
        state.lookupError = "一次最多查询 64 个 MAC";
        renderLookup();
        return;
      }
      var button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = true; }
      state.lookupError = "";
      setHtml("lookupResults", loadingMarkup("正在查询设备档案"));
      try {
        state.lookupPayload = await API.lookup(value);
        renderLookup();
      } catch (error) {
        state.lookupError = error.message;
        renderLookup();
        throw error;
      } finally {
        if (button) { button.disabled = false; }
      }
      return;
    }
    if (form.id === "pingForm") {
      event.preventDefault();
      var addressInput = byId("pingAddress");
      var countInput = byId("pingCount");
      var address = addressInput ? addressInput.value.trim() : "";
      if (U.ipv4Value(address) === null) {
        state.pingLoading = false;
        state.pingResult = { address: address, sent: 0, received: 0, packet_loss_percent: 100, samples: [], error: "请输入有效的 IPv4 地址" };
        renderPingResult();
        return;
      }
      var count = countInput ? Number(countInput.value) || 4 : 4;
      await runPing(address, count);
      return;
    }
    if (form.id === "settingsForm") {
      event.preventDefault();
      await saveSettings(form);
      return;
    }
    if (form.id === "deviceEditForm") {
      event.preventDefault();
      await saveDeviceForm(form);
    }
  }

  async function saveSettings(form) {
    var status = byId("settingsStatus");
    var button = form.querySelector('button[type="submit"]');
    if (status) {
      status.textContent = "正在保存…";
      status.className = "settings-status";
    }
    if (button) { button.disabled = true; }
    var payload = {
      scan_timeout_ms: Number(byId("settingTimeout").value) || 900,
      scan_concurrency: Number(byId("settingConcurrency").value) || 64,
      default_interface_id: byId("settingInterface").value || "",
      offline_after_minutes: Number(byId("settingOffline").value) || 30,
      auto_scan_interval_minutes: Number(byId("settingAutoInterval").value) || 30,
      auto_scan_enabled: byId("settingAutoScan").checked,
      csv_bom: byId("settingCsvBom").checked,
      csv_include_header: byId("settingCsvHeader").checked,
      csv_include_notes: byId("settingCsvNotes").checked,
      theme: byId("settingTheme").value
    };
    try {
      var response = await API.updateSettings(payload);
      state.settings = response.settings || payload;
      applyTheme(state.settings.theme || payload.theme, false);
      renderSettings();
      renderScanConfigHint();
      if (status) {
        status.textContent = "设置已保存到本机";
        status.className = "settings-status is-success";
      }
      showToast("设置已保存", "success");
    } catch (error) {
      if (status) {
        status.textContent = error.message;
        status.className = "settings-status is-error";
      }
      throw error;
    } finally {
      if (button) { button.disabled = false; }
    }
  }

  async function saveDeviceForm(form) {
    var deviceId = form.dataset.deviceId || "";
    var name = byId("drawerDisplayName");
    var note = byId("drawerNote");
    var favorite = byId("drawerFavorite");
    var message = byId("drawerFormMessage");
    var button = form.querySelector('button[type="submit"]');
    if (message) {
      message.textContent = "正在保存…";
      message.classList.remove("is-error");
    }
    if (button) { button.disabled = true; }
    try {
      var response = await API.updateDevice(deviceId, {
        display_name: name ? name.value : "",
        note: note ? note.value : "",
        favorite: favorite ? favorite.checked : false
      });
      var updated = response.device;
      state.devices = state.devices.map(function (device) {
        return String(device.device_id) === String(deviceId) ? Object.assign({}, device, updated) : device;
      });
      renderDashboard();
      renderInventory();
      renderDeviceDrawer(updated);
      showToast("设备档案已保存", "success");
    } catch (error) {
      if (message) {
        message.textContent = error.message;
        message.classList.add("is-error");
      }
      throw error;
    } finally {
      if (button) { button.disabled = false; }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
}());
