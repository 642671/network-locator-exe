(function (root) {
  "use strict";

  var directory = location.pathname.slice(0, location.pathname.lastIndexOf("/") + 1).replace(/\/$/, "");
  var API_ROOT = directory + "/api/v2";

  function request(path, options) {
    var settings = options || {};
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timeout = window.setTimeout(function () {
      if (controller) {
        controller.abort();
      }
    }, settings.timeoutMs || 20000);
    var headers = {};
    if (settings.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    return fetch(API_ROOT + path, {
      method: settings.method || (settings.body === undefined ? "GET" : "POST"),
      credentials: "include",
      cache: "no-store",
      headers: headers,
      body: settings.body === undefined ? undefined : JSON.stringify(settings.body),
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      return response.text().then(function (text) {
        var payload = null;
        try {
          payload = text ? JSON.parse(text) : null;
        } catch (error) {
          payload = null;
        }
        if (!response.ok || !payload || payload.ok === false) {
          var message = payload && payload.error ? payload.error : "HTTP " + response.status;
          var failure = new Error(message);
          failure.status = response.status;
          throw failure;
        }
        return payload;
      });
    }).catch(function (error) {
      if (error.name === "AbortError") {
        throw new Error("请求超时，请稍后重试");
      }
      throw error;
    }).finally(function () {
      window.clearTimeout(timeout);
    });
  }

  function asQuery(ids) {
    if (!ids || !ids.length) {
      return "";
    }
    return "?ids=" + encodeURIComponent(ids.join(","));
  }

  root.LocatorApi = {
    root: API_ROOT,
    bootstrap: function () {
      return request("/bootstrap", { timeoutMs: 30000 });
    },
    interfaces: function (refresh) {
      return request("/interfaces" + (refresh ? "?refresh=1" : ""), { timeoutMs: 30000 });
    },
    devices: function () {
      return request("/devices");
    },
    device: function (deviceId) {
      return request("/devices/" + encodeURIComponent(deviceId));
    },
    updateDevice: function (deviceId, payload) {
      return request("/devices/" + encodeURIComponent(deviceId), {
        method: "PATCH",
        body: payload
      });
    },
    startScan: function (networkIds, customRanges) {
      return request("/scan/start", {
        body: {
          network_ids: networkIds,
          custom_ranges: customRanges || []
        },
        timeoutMs: 30000
      });
    },
    scanStatus: function () {
      return request("/scan/status");
    },
    pauseScan: function () {
      return request("/scan/pause", { body: {} });
    },
    resumeScan: function () {
      return request("/scan/resume", { body: {} });
    },
    stopScan: function () {
      return request("/scan/stop", { body: {} });
    },
    history: function () {
      return request("/history");
    },
    historyTask: function (taskId) {
      return request("/history/" + encodeURIComponent(taskId));
    },
    lookup: function (query) {
      return request("/lookup", { body: { query: query }, timeoutMs: 30000 });
    },
    analysis: function () {
      return request("/analysis", { timeoutMs: 30000 });
    },
    ping: function (address, count) {
      return request("/analysis/ping", {
        body: { address: address, count: count },
        timeoutMs: 30000
      });
    },
    settings: function () {
      return request("/settings");
    },
    updateSettings: function (payload) {
      return request("/settings", { method: "PATCH", body: payload });
    },
    exportDevicesUrl: function (ids) {
      return API_ROOT + "/export/devices.csv" + asQuery(ids);
    },
    exportHistoryUrl: function (taskId) {
      return API_ROOT + "/export/history/" + encodeURIComponent(taskId) + ".csv";
    }
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
