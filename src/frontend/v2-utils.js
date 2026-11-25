(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.LocatorUtils = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var MAC_COMPACT_PATTERN = /^[0-9A-F]{4,12}$/;
  var STATUS_RANK = { online: 0, unknown: 1, offline: 2 };

  function compactMac(value) {
    return String(value || "").replace(/[-:\s]/g, "").toUpperCase();
  }

  function normalizeMac(value) {
    var compact = compactMac(value);
    if (!/^[0-9A-F]{12}$/.test(compact)) {
      return "";
    }
    return compact.match(/.{2}/g).join("-");
  }

  function classifyMacQuery(value) {
    var display = String(value || "").trim();
    var compact = compactMac(display);
    if (!MAC_COMPACT_PATTERN.test(compact)) {
      return { kind: "invalid", compact: compact, label: "格式无效", display: display };
    }
    if (compact.length === 4) {
      return { kind: "suffix", compact: compact, label: "MAC 后缀", display: display };
    }
    if (compact.length === 6) {
      return { kind: "prefix", compact: compact, label: "MAC 前缀", display: display };
    }
    if (compact.length === 12) {
      return { kind: "full", compact: compact, label: "完整 MAC", display: display };
    }
    return { kind: "invalid", compact: compact, label: "格式无效", display: display };
  }

  function parseBatchMacs(value) {
    var source = Array.isArray(value) ? value : String(value || "").split(/[\s,;，；]+/);
    var seen = {};
    var items = [];
    source.forEach(function (part) {
      var text = String(part || "").trim();
      if (!text) {
        return;
      }
      var parsed = classifyMacQuery(text);
      var key = parsed.compact + "|" + parsed.kind;
      if (seen[key]) {
        return;
      }
      seen[key] = true;
      items.push(parsed);
    });
    return items;
  }

  function escapeHtml(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function ipv4Value(value) {
    var parts = String(value || "").split(".");
    if (parts.length !== 4) {
      return null;
    }
    var total = 0;
    for (var index = 0; index < 4; index += 1) {
      if (!/^\d{1,3}$/.test(parts[index])) {
        return null;
      }
      var octet = Number(parts[index]);
      if (octet > 255) {
        return null;
      }
      total = total * 256 + octet;
    }
    return total;
  }

  function compareText(left, right) {
    return String(left || "").localeCompare(String(right || ""), "zh-CN", {
      numeric: true,
      sensitivity: "base"
    });
  }

  function compareIp(left, right) {
    var leftValue = ipv4Value(left);
    var rightValue = ipv4Value(right);
    if (leftValue !== null && rightValue !== null) {
      return leftValue - rightValue;
    }
    if (leftValue !== null) {
      return -1;
    }
    if (rightValue !== null) {
      return 1;
    }
    return compareText(left, right);
  }

  function formatDate(value) {
    if (!value) {
      return "—";
    }
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return date.toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function formatRelative(value, nowValue) {
    if (!value) {
      return "从未";
    }
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    var now = nowValue instanceof Date ? nowValue : new Date(nowValue || Date.now());
    var seconds = Math.max(0, Math.floor((now.getTime() - date.getTime()) / 1000));
    if (seconds < 45) {
      return "刚刚";
    }
    if (seconds < 3600) {
      return Math.floor(seconds / 60) + " 分钟前";
    }
    if (seconds < 86400) {
      return Math.floor(seconds / 3600) + " 小时前";
    }
    if (seconds < 604800) {
      return Math.floor(seconds / 86400) + " 天前";
    }
    return formatDate(value);
  }

  function statusMeta(status) {
    if (status === "online") {
      return { label: "在线", className: "is-online", dot: "success" };
    }
    if (status === "offline") {
      return { label: "离线", className: "is-offline", dot: "danger" };
    }
    return { label: "未知", className: "is-unknown", dot: "unknown" };
  }

  function deviceSearchText(device) {
    var item = device || {};
    return [
      item.effective_name,
      item.display_name,
      item.current_ip,
      item.primary_mac,
      compactMac(item.primary_mac),
      item.vendor,
      item.note,
      item.current_interface,
      item.current_scan_range,
      item.status_label,
      item.status
    ].join(" ").toLowerCase();
  }

  function filterDevices(devices, query, options) {
    var settings = options || {};
    var tokens = String(query || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
    return (Array.isArray(devices) ? devices : []).filter(function (device) {
      if (settings.status && settings.status !== "all" && device.status !== settings.status) {
        return false;
      }
      if (settings.favorite && !device.favorite) {
        return false;
      }
      if (!tokens.length) {
        return true;
      }
      var text = deviceSearchText(device);
      return tokens.every(function (token) {
        if (text.indexOf(token) >= 0) {
          return true;
        }
        var compact = compactMac(token);
        return compact !== token && text.indexOf(compact.toLowerCase()) >= 0;
      });
    });
  }

  function sortDevices(devices, key, direction) {
    var rows = (Array.isArray(devices) ? devices : []).slice();
    var factor = direction === "ascending" ? 1 : -1;
    rows.sort(function (left, right) {
      var comparison = 0;
      if (key === "name") {
        comparison = compareText(left.effective_name, right.effective_name);
      } else if (key === "status") {
        var leftRank = STATUS_RANK[left.status] === undefined ? 9 : STATUS_RANK[left.status];
        var rightRank = STATUS_RANK[right.status] === undefined ? 9 : STATUS_RANK[right.status];
        comparison = leftRank - rightRank;
      } else if (key === "ip") {
        comparison = compareIp(left.current_ip, right.current_ip);
      } else if (key === "mac") {
        comparison = compareText(compactMac(left.primary_mac), compactMac(right.primary_mac));
      } else if (key === "vendor") {
        comparison = compareText(left.vendor, right.vendor);
      } else if (key === "last_seen_at") {
        comparison = new Date(left.last_seen_at || 0).getTime() - new Date(right.last_seen_at || 0).getTime();
      } else if (key === "first_seen_at") {
        comparison = new Date(left.first_seen_at || 0).getTime() - new Date(right.first_seen_at || 0).getTime();
      } else {
        comparison = compareText(left.effective_name, right.effective_name);
      }
      if (comparison === 0) {
        comparison = compareIp(left.current_ip, right.current_ip);
      }
      return comparison * factor;
    });
    return rows;
  }

  function scanResultKey(item) {
    return String(item.device_id || "") + "|" + String(item.mac || "no-mac") + "|" + String(item.ip || "");
  }

  function mergeScanResults(current, incoming) {
    var map = {};
    (Array.isArray(current) ? current : []).concat(Array.isArray(incoming) ? incoming : []).forEach(function (item) {
      if (!item || !item.ip) {
        return;
      }
      var key = scanResultKey(item);
      var existing = map[key];
      var rank = { LOCAL: 5, REACHABLE: 4, DELAY: 3, PROBE: 2, STALE: 1 };
      if (!existing || (rank[item.state] || 0) >= (rank[existing.state] || 0)) {
        map[key] = item;
      }
    });
    return Object.keys(map).map(function (key) {
      return map[key];
    }).sort(function (left, right) {
      return compareIp(left.ip, right.ip) || compareText(left.mac, right.mac);
    });
  }

  function summarizeDevices(devices) {
    var summary = { total: 0, online: 0, offline: 0, unknown: 0, favorites: 0 };
    (Array.isArray(devices) ? devices : []).forEach(function (device) {
      summary.total += 1;
      if (device.status === "online") {
        summary.online += 1;
      } else if (device.status === "offline") {
        summary.offline += 1;
      } else {
        summary.unknown += 1;
      }
      if (device.favorite) {
        summary.favorites += 1;
      }
    });
    return summary;
  }

  function scanStatusMeta(scan) {
    var value = scan || {};
    var status = String(value.status || "idle");
    if (status === "completed") {
      return { label: "扫描完成", tone: "success", detail: "设备档案已更新" };
    }
    if (status === "stopped") {
      return { label: "扫描已停止", tone: "warning", detail: "已保存完成部分的结果" };
    }
    if (status === "error" || status === "interrupted") {
      return { label: "扫描失败", tone: "danger", detail: value.error || "请检查网络配置" };
    }
    if (status === "paused") {
      return { label: "扫描已暂停", tone: "warning", detail: value.current_range || "等待继续" };
    }
    if (value.running) {
      return {
        label: "正在扫描",
        tone: "primary",
        detail: (value.current_interface || "准备扫描") + (value.current_range ? " · " + value.current_range : "")
      };
    }
    return { label: "待机", tone: "neutral", detail: "尚未开始本轮扫描" };
  }

  function validateCustomRanges(value) {
    var tokens = String(value || "").split(/[\r\n,;，；]+/).map(function (item) {
      return item.trim();
    }).filter(Boolean);
    var total = 0;
    for (var index = 0; index < tokens.length; index += 1) {
      var token = tokens[index];
      var count = 0;
      if (token.indexOf("/") >= 0) {
        var parts = token.split("/");
        if (parts.length !== 2 || ipv4Value(parts[0]) === null || !/^\d{1,2}$/.test(parts[1])) {
          return { valid: false, error: "网段格式无效：" + token, total: 0 };
        }
        var prefix = Number(parts[1]);
        if (prefix < 0 || prefix > 32) {
          return { valid: false, error: "网段前缀无效：" + token, total: 0 };
        }
        count = prefix >= 31 ? Math.pow(2, 32 - prefix) : Math.pow(2, 32 - prefix) - 2;
      } else if (token.indexOf("-") >= 0) {
        var range = token.split("-");
        var start = ipv4Value(range[0]);
        var end = ipv4Value(range[1]);
        if (range.length !== 2 || start === null || end === null || start > end) {
          return { valid: false, error: "IP 范围格式无效：" + token, total: 0 };
        }
        count = end - start + 1;
      } else if (ipv4Value(token) !== null) {
        count = 1;
      } else {
        return { valid: false, error: "IP 地址格式无效：" + token, total: 0 };
      }
      if (count > 1022) {
        return { valid: false, error: "单个范围最多 1022 台主机：" + token, total: total };
      }
      total += count;
    }
    if (total > 4096) {
      return { valid: false, error: "自定义范围总计最多 4096 台主机", total: total };
    }
    return { valid: true, error: "", total: total, count: tokens.length };
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, Number(value) || 0));
  }

  return {
    compactMac: compactMac,
    normalizeMac: normalizeMac,
    classifyMacQuery: classifyMacQuery,
    parseBatchMacs: parseBatchMacs,
    escapeHtml: escapeHtml,
    ipv4Value: ipv4Value,
    compareIp: compareIp,
    formatDate: formatDate,
    formatRelative: formatRelative,
    statusMeta: statusMeta,
    filterDevices: filterDevices,
    sortDevices: sortDevices,
    mergeScanResults: mergeScanResults,
    summarizeDevices: summarizeDevices,
    scanStatusMeta: scanStatusMeta,
    validateCustomRanges: validateCustomRanges,
    clamp: clamp
  };
});
