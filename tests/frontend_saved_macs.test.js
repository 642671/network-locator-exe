"use strict";

const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "src", "frontend", "app.js"),
  "utf8"
);

const storage = new Map();
let idCounter = 0;

const context = {
  console,
  location: { pathname: "/" },
  navigator: {},
  window: {
    clearTimeout,
    setTimeout,
    crypto: {
      randomUUID: () => "test-id-" + (++idCounter)
    },
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key)
    },
    addEventListener: () => undefined
  },
  document: {
    addEventListener: () => undefined,
    readyState: "loading"
  }
};

vm.createContext(context);
vm.runInContext(source, context, { filename: "frontend/app.js" });

test("saved MAC entries keep the typed display and normalize the suffix", () => {
  const entry = context.sanitizeSavedMac({
    display: " a1:b2 ",
    note: "  客厅电视  "
  });
  assert.equal(entry.display, "a1:b2");
  assert.equal(entry.normalized, "A1B2");
  assert.equal(entry.note, "客厅电视");
  assert.equal(entry.lastSeenIp, "");
  assert.equal(entry.lastSeenAt, 0);
  assert.ok(entry.id);
});

test("saved MAC entries reject invalid suffixes and cap the note", () => {
  assert.equal(context.sanitizeSavedMac({ display: "ZZZZ" }), null);
  assert.equal(context.sanitizeSavedMac({ display: "A1" }), null);
  assert.equal(context.sanitizeSavedMac(null), null);

  const entry = context.sanitizeSavedMac({
    display: "A1B2",
    note: "x".repeat(150)
  });
  assert.equal(entry.note.length, context.SAVED_NOTE_LIMIT);
});

test("reading saved MACs drops duplicates, invalid entries, and excess items", () => {
  const items = [
    { display: "A1-B2", note: "first" },
    { display: "A1B2", note: "duplicate" },
    { display: "C3D4" },
    { display: "not-a-mac" }
  ];
  for (let index = 0; index < 10; index += 1) {
    items.push({ display: "AA" + String(index).padStart(2, "0") });
  }
  storage.set(
    context.SAVED_MACS_KEY,
    JSON.stringify({ version: 1, items })
  );

  const entries = context.readSavedMacs();
  assert.equal(entries.length, context.SAVED_MACS_LIMIT);
  assert.equal(entries[0].normalized, "A1B2");
  assert.equal(entries[0].note, "first");
  assert.equal(new Set(entries.map((entry) => entry.normalized)).size, entries.length);
});

test("persisting saved MACs writes a versioned payload and updates state", () => {
  const entry = context.sanitizeSavedMac({ display: "C3D4", note: "打印机" });
  assert.equal(context.persistSavedMacs([entry]), true);
  assert.equal(context.state.savedMacs.length, 1);

  const stored = JSON.parse(storage.get(context.SAVED_MACS_KEY));
  assert.equal(stored.version, 1);
  assert.equal(stored.items[0].normalized, "C3D4");
  assert.equal(stored.items[0].note, "打印机");
});

test("matching saved MACs returns every device with the same suffix", () => {
  const entry = context.sanitizeSavedMac({ display: "A1:B2" });
  const devices = [
    {
      interface: "eth1",
      scan_range: "10.0.0.0/24",
      ip: "10.0.0.5",
      mac: "02-42-AC-11-A1-B2",
      state: "REACHABLE"
    },
    {
      interface: "eth2",
      scan_range: "10.1.0.0/24",
      ip: "10.1.0.9",
      mac: "02-42-AC-22-A1-B2",
      state: "STALE"
    },
    {
      interface: "eth1",
      scan_range: "10.0.0.0/24",
      ip: "10.0.0.7",
      mac: "02-42-AC-11-99-99",
      state: "REACHABLE"
    }
  ];

  const matches = context.matchSavedMac(entry, devices);
  assert.equal(matches.length, 2);
  assert.deepEqual(matches.map((device) => device.ip), ["10.0.0.5", "10.1.0.9"]);
  assert.equal(context.matchSavedMac(entry, []).length, 0);
});

test("a cached full MAC breaks a suffix tie", () => {
  const entry = context.sanitizeSavedMac({ display: "A1B2" });
  entry.lastSeenFullMac = "0242AC22A1B2";
  const devices = [
    { ip: "10.0.0.5", mac: "02-42-AC-11-A1-B2" },
    { ip: "10.1.0.9", mac: "02-42-AC-22-A1-B2" }
  ];

  const matches = context.matchSavedMac(entry, devices);
  assert.equal(matches[0].ip, "10.1.0.9");
});

test("saved MAC status distinguishes online, recent, and missing devices", () => {
  assert.equal(context.savedMacStatus([{ state: "REACHABLE" }]).label, "在线");
  assert.equal(context.savedMacStatus([{ state: "LOCAL" }]).label, "在线");
  assert.equal(context.savedMacStatus([{ state: "STALE" }]).label, "近期");
  assert.equal(context.savedMacStatus([]).label, "离线");
});

test("merging a device scan fills last-seen fields for saved MACs", () => {
  const entry = context.sanitizeSavedMac({ display: "A1B2" });
  context.state.savedMacs = [entry];
  const devices = [{
    interface: "eth1",
    scan_range: "10.0.0.0/24",
    ip: "10.0.0.5",
    mac: "02-42-AC-11-A1-B2",
    state: "REACHABLE"
  }];

  assert.equal(context.mergeSavedMacLastSeen(devices), true);
  assert.equal(context.state.savedMacs[0].lastSeenIp, "10.0.0.5");
  assert.equal(context.state.savedMacs[0].lastSeenFullMac, "0242AC11A1B2");
  assert.equal(context.state.savedMacs[0].lastSeenRange, "10.0.0.0/24");
  assert.ok(context.state.savedMacs[0].lastSeenAt > 0);
  context.flushSavedMacsWrite();
});

test("the saved MAC limit and note limit stay at the agreed values", () => {
  assert.equal(context.SAVED_MACS_LIMIT, 8);
  assert.equal(context.SAVED_NOTE_LIMIT, 100);
  assert.equal(context.SAVED_MACS_KEY, "networkLocator.savedMacs.v1");
});
