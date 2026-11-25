"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const utils = require("../src/frontend/v2-utils.js");

function device(overrides) {
  return Object.assign({
    device_id: "d1",
    effective_name: "财务打印机",
    display_name: "",
    current_ip: "192.0.2.10",
    primary_mac: "02-42-AC-11-00-10",
    vendor: "本地管理地址（随机/虚拟）",
    note: "二楼",
    current_interface: "Ethernet",
    current_scan_range: "192.0.2.0/24",
    status: "online",
    status_label: "在线",
    favorite: false,
    last_seen_at: "2026-11-24T00:00:00Z",
    first_seen_at: "2026-11-23T00:00:00Z"
  }, overrides || {});
}

test("MAC queries auto-detect full, prefix, and suffix forms", () => {
  assert.equal(utils.classifyMacQuery("02-42-AC-11-00-10").kind, "full");
  assert.equal(utils.classifyMacQuery("02:42:AC").kind, "prefix");
  assert.equal(utils.classifyMacQuery("AC-11").kind, "suffix");
  assert.equal(utils.classifyMacQuery("ZZZZ").kind, "invalid");
  assert.deepEqual(utils.parseBatchMacs("0010, 02-42-AC\n0010"), [
    { kind: "suffix", compact: "0010", label: "MAC 后缀", display: "0010" },
    { kind: "prefix", compact: "0242AC", label: "MAC 前缀", display: "02-42-AC" }
  ]);
});
test("device filters search manual fields, status, MAC variants, and favorites", () => {
  const rows = [
    device({ favorite: true }),
    device({
      device_id: "d2",
      effective_name: "客厅电视",
      current_ip: "192.0.2.100",
      primary_mac: "AA-BB-CC-DD-EE-01",
      status: "offline",
      status_label: "离线",
      note: "客厅"
    })
  ];
  assert.deepEqual(utils.filterDevices(rows, "00:10").map((item) => item.device_id), ["d1"]);
  assert.deepEqual(utils.filterDevices(rows, "客厅").map((item) => item.device_id), ["d2"]);
  assert.deepEqual(utils.filterDevices(rows, "", { status: "offline" }).map((item) => item.device_id), ["d2"]);
  assert.deepEqual(utils.filterDevices(rows, "", { favorite: true }).map((item) => item.device_id), ["d1"]);
});

test("device sorting uses numeric IPv4 and status order", () => {
  const rows = [
    device({ device_id: "a", current_ip: "192.0.2.100", status: "offline" }),
    device({ device_id: "b", current_ip: "192.0.2.9", status: "online" }),
    device({ device_id: "c", current_ip: "192.0.2.20", status: "unknown" })
  ];
  assert.deepEqual(utils.sortDevices(rows, "ip", "ascending").map((item) => item.device_id), ["b", "c", "a"]);
  assert.deepEqual(utils.sortDevices(rows, "status", "ascending").map((item) => item.device_id), ["b", "c", "a"]);
  assert.deepEqual(rows.map((item) => item.device_id), ["a", "b", "c"]);
});

test("realtime scan results merge duplicate device/IP observations", () => {
  const current = [
    { device_id: "d1", ip: "192.0.2.10", mac: "02-42-AC-11-00-10", state: "STALE" }
  ];
  const incoming = [
    { device_id: "d1", ip: "192.0.2.10", mac: "02-42-AC-11-00-10", state: "REACHABLE" },
    { device_id: "d2", ip: "192.0.2.11", mac: "02-42-AC-11-00-11", state: "LOCAL" }
  ];
  const merged = utils.mergeScanResults(current, incoming);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].state, "REACHABLE");
  assert.equal(merged[1].state, "LOCAL");
});

test("summary and custom range validation remain deterministic", () => {
  const summary = utils.summarizeDevices([
    device({ favorite: true }),
    device({ status: "offline" }),
    device({ status: "unknown" })
  ]);
  assert.deepEqual(summary, { total: 3, online: 1, offline: 1, unknown: 1, favorites: 1 });
  assert.equal(utils.validateCustomRanges("192.0.2.0/30\n198.51.100.10-198.51.100.12").total, 5);
  assert.equal(utils.validateCustomRanges("10.0.0.0/8").valid, false);
  assert.equal(utils.validateCustomRanges("not-an-ip").valid, false);
});
