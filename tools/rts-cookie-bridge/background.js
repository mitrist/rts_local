"use strict";

importScripts("bridge.js");

const Bridge = globalThis.RtsCookieBridge;

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== Bridge.AUTO_ALARM_NAME) return;
  Bridge.runAutoSave().catch((err) => {
    console.error("RTS Cookie Bridge auto-save failed:", err);
  });
});

chrome.runtime.onStartup.addListener(() => {
  Bridge.restoreAutoRefresh().catch((err) => {
    console.error("RTS Cookie Bridge restore failed:", err);
  });
});

chrome.runtime.onInstalled.addListener(() => {
  Bridge.restoreAutoRefresh().catch((err) => {
    console.error("RTS Cookie Bridge restore failed:", err);
  });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const type = message && message.type;
  if (type === "startAutoRefresh") {
    Bridge.startAutoRefresh()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: String(err.message || err) }));
    return true;
  }
  if (type === "stopAutoRefresh") {
    Bridge.stopAutoRefresh()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: String(err.message || err) }));
    return true;
  }
  if (type === "getAutoStatus") {
    Bridge.getAutoRefreshState()
      .then((state) => sendResponse({ ok: true, state, text: Bridge.formatAutoStatus(state) }))
      .catch((err) => sendResponse({ ok: false, error: String(err.message || err) }));
    return true;
  }
  return false;
});
