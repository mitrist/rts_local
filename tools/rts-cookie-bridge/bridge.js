/* Shared logic for RTS Cookie Bridge (popup + background service worker). */
"use strict";

const RtsCookieBridge = (() => {
  const RTS_URLS = [
    "https://offers.rts-tender.ru/",
    "https://offers.rts-tender.ru/supplier/price-lists",
    "https://offers.rts-tender.ru/supplier/price-lists/positions",
    "https://offers.rts-tender.ru/customer/",
    "https://offers.rts-tender.ru/customer/offers",
    "https://platform.rts-tender.ru/",
    "https://platform.rts-tender.ru/api-gateway/",
    "https://app.rts-tender.ru/",
    "https://www.rts-tender.ru/",
    "https://rts-tender.ru/",
  ];
  const RTS_DOMAINS = [
    ".rts-tender.ru",
    "rts-tender.ru",
    "offers.rts-tender.ru",
    "platform.rts-tender.ru",
    "app.rts-tender.ru",
    "www.rts-tender.ru",
  ];
  const AUTH_COOKIE_NAMES = [".Auth.Sso", ".Auth.Rts.P", ".Auth.Rts.C"];
  const CUSTOMER_AUTH_SSO = ".Auth.Sso";
  const CUSTOMER_AUTH_RTS = [".Auth.Rts.C", ".Auth.Rts.P"];

  const AUTO_ALARM_NAME = "rts-cookie-bridge-auto";
  const AUTO_REFRESH_INTERVAL_SEC = 180;

  const PROFILE_HINTS = {
    IPSokolova:
      "ЛК поставщика. Откройте offers.rts-tender.ru под IPSokolova → «Сохранить cookie».",
    Nacpro: "ЛК поставщика. Откройте offers.rts-tender.ru под Nacpro → «Сохранить cookie».",
    customer_monitor:
      "ЛК заказчика (монитор ПП). Откройте offers.rts-tender.ru как заказчик → «Сохранить cookie».",
  };

  function normalizeApiUrl(raw) {
    const text = String(raw || "").trim();
    if (!text) return "";
    try {
      const url = new URL(text);
      if (url.protocol === "http:" && url.hostname === "rts.raytec-log.pro") {
        url.protocol = "https:";
        return url.toString();
      }
    } catch (_) {
      return text;
    }
    return text;
  }

  function normalizeCookieText(raw) {
    let text = String(raw || "").trim();
    if (!text) return "";
    if (text.toLowerCase().startsWith("cookie:")) {
      text = text.slice("cookie:".length).trim();
    }
    return text;
  }

  function cookieNameSet(cookie) {
    const names = new Set();
    for (const part of String(cookie || "").split(";")) {
      const idx = part.indexOf("=");
      if (idx <= 0) continue;
      names.add(part.slice(0, idx).trim().toLowerCase());
    }
    return names;
  }

  function cookieHasAuthMarkers(cookie, profile) {
    const names = cookieNameSet(cookie);
    if (!names.has(CUSTOMER_AUTH_SSO.toLowerCase())) return false;
    if (profile === "customer_monitor") {
      return CUSTOMER_AUTH_RTS.some((name) => names.has(name.toLowerCase()));
    }
    return names.has(".auth.rts.p");
  }

  function missingAuthMessage(profile, cookieNames) {
    const names = (cookieNames || []).join(", ") || "—";
    const role =
      profile === "customer_monitor"
        ? "заказчиком (.Auth.Rts.C)"
        : "поставщиком (.Auth.Rts.P)";
    const need =
      profile === "customer_monitor" ? ".Auth.Sso и .Auth.Rts.C" : ".Auth.Sso и .Auth.Rts.P";
    return (
      `Авто-сбор не нашёл ${need}.\n\n` +
      `1. Откройте вкладку offers.rts-tender.ru (вход по ЭЦП как ${role}).\n` +
      "2. Нажмите «Сохранить cookie» снова.\n\n" +
      `Найдены cookie: ${names}\n\n` +
      "Если не помогло — раскройте «Ручная вставка» и скопируйте Cookie из DevTools → Network."
    );
  }

  function cookieFromManual(raw) {
    const text = normalizeCookieText(raw);
    if (!text) return null;
    const pairs = text.split(";").map((p) => p.trim()).filter(Boolean);
    const cookies = [];
    for (const pair of pairs) {
      const idx = pair.indexOf("=");
      if (idx <= 0) continue;
      const name = pair.slice(0, idx).trim();
      const value = pair.slice(idx + 1).trim();
      if (name) cookies.push({ name, value });
    }
    if (!cookies.length) {
      throw new Error("Не удалось разобрать строку Cookie.");
    }
    return { cookie: text, cookies, source: "manual" };
  }

  function isRtsDomain(domain) {
    return String(domain || "").includes("rts-tender.ru");
  }

  async function addCookiesFromQuery(seen, query) {
    const items = await chrome.cookies.getAll(query);
    for (const c of items) {
      if (c.name && c.value != null && isRtsDomain(c.domain)) {
        seen.set(c.name, c.value);
      }
    }
  }

  async function collectCookiesAuto(profile, { includeActiveTab = true } = {}) {
    const seen = new Map();
    const urls = new Set(RTS_URLS);
    let activeTabUrl = "";

    if (includeActiveTab) {
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab && tab.url && tab.url.includes("rts-tender.ru")) {
          activeTabUrl = tab.url;
          urls.add(tab.url);
          urls.add(tab.url.split("/").slice(0, 3).join("/") + "/");
        }
      } catch (_) {
        /* popup closed or tabs unavailable */
      }
    }

    for (const url of urls) {
      await addCookiesFromQuery(seen, { url });
    }
    for (const domain of RTS_DOMAINS) {
      await addCookiesFromQuery(seen, { domain });
    }
    for (const name of AUTH_COOKIE_NAMES) {
      await addCookiesFromQuery(seen, { name });
    }

    if (!seen.size) {
      throw new Error(
        "Cookie РТС не найдены.\n\nОткройте offers.rts-tender.ru (вход по ЭЦП) и сохраните cookie снова."
      );
    }

    const cookies = [...seen.entries()].map(([name, value]) => ({ name, value }));
    const cookie = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
    return {
      cookie,
      cookies,
      source: "auto",
      names: [...seen.keys()],
      activeTabUrl,
    };
  }

  async function resolveCookiePayloadAuto(profile) {
    const payload = await collectCookiesAuto(profile, { includeActiveTab: false });
    if (!cookieHasAuthMarkers(payload.cookie, profile)) {
      throw new Error(missingAuthMessage(profile, payload.names));
    }
    return payload;
  }

  async function loadSyncSettings() {
    return chrome.storage.sync.get(["apiUrl", "apiUrl2", "apiKey", "profile"]);
  }

  async function saveSyncSettings({ apiUrl, apiUrl2, apiKey, profile }) {
    await chrome.storage.sync.set({ apiUrl, apiUrl2, apiKey, profile });
  }

  function collectApiUrls(settings) {
    const urls = [];
    for (const key of ["apiUrl", "apiUrl2"]) {
      const normalized = normalizeApiUrl(settings[key]);
      if (normalized && !urls.includes(normalized)) {
        urls.push(normalized);
      }
    }
    return urls;
  }

  async function getAutoRefreshState() {
    return chrome.storage.local.get([
      "autoRefreshEnabled",
      "autoRefreshLastAt",
      "autoRefreshLastOk",
      "autoRefreshLastError",
      "autoRefreshLastCount",
    ]);
  }

  async function setAutoRefreshState(patch) {
    await chrome.storage.local.set(patch);
  }

  async function scheduleAutoAlarm(delaySec = AUTO_REFRESH_INTERVAL_SEC) {
    const when = Date.now() + Math.max(5, delaySec) * 1000;
    await chrome.alarms.clear(AUTO_ALARM_NAME);
    await chrome.alarms.create(AUTO_ALARM_NAME, { when });
  }

  async function startAutoRefresh() {
    const settings = await loadSyncSettings();
    const apiUrls = collectApiUrls(settings);
    if (!apiUrls.length || !settings.apiKey || !settings.profile) {
      throw new Error("Укажите URL API (хотя бы один), API key и профиль перед авто-обновлением.");
    }
    await setAutoRefreshState({ autoRefreshEnabled: true });
    await scheduleAutoAlarm(AUTO_REFRESH_INTERVAL_SEC);
  }

  async function stopAutoRefresh() {
    await chrome.alarms.clear(AUTO_ALARM_NAME);
    await setAutoRefreshState({ autoRefreshEnabled: false });
  }

  async function restoreAutoRefresh() {
    const state = await getAutoRefreshState();
    if (state.autoRefreshEnabled) {
      await scheduleAutoAlarm(AUTO_REFRESH_INTERVAL_SEC);
    }
  }

  function formatProbeLine(data) {
    let probeLine = "probe: проверьте сессию";
    const probeOk = !!(data.probe && data.probe.ok);
    if (probeOk) {
      if (data.probe.channel === "offers") {
        probeLine = "probe OK (offers)";
      } else if (data.probe.channel === "customer_monitor") {
        probeLine = "probe OK (заказчик)";
      } else {
        probeLine = "probe OK";
      }
    } else if (data.probe && data.probe.skipped) {
      probeLine = "probe: пропущен";
    } else if (data.probe && data.probe.reason === "empty_page") {
      probeLine = "probe: неполная сессия — обновите страницу РТС";
    } else if (data.probe && data.probe.reason) {
      probeLine = `probe: ${data.probe.reason}`;
    }
    return { probeOk, probeLine };
  }

  async function postCookies({ apiUrl, apiKey, profile, payload, probe, source }) {
    const url = normalizeApiUrl(apiUrl);
    const r = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "api-key": apiKey,
      },
      body: JSON.stringify({
        cookie: payload.cookie,
        cookies: payload.cookies,
        profile,
        probe: !!probe,
        source,
      }),
    });
    const text = await r.text();
    let data = {};
    try {
      data = JSON.parse(text);
    } catch (_) {
      data = { message: text };
    }
    if (!r.ok) {
      let err = data.detail || data.message || `HTTP ${r.status}`;
      if (r.status === 405) {
        err += "\n\nИспользуйте https:// в URL API — редирект с http меняет POST на GET.";
      }
      const error = new Error(err);
      error.httpStatus = r.status;
      throw error;
    }
    return data;
  }

  async function sendCookiesToServer({
    apiUrl,
    apiKey,
    profile,
    payload,
    probe = true,
    source = "rts-cookie-bridge",
  }) {
    const data = await postCookies({ apiUrl, apiKey, profile, payload, probe, source });
    const { probeOk, probeLine } = formatProbeLine(data);
    const count = payload.cookies.length;
    const note =
      payload.source === "manual"
        ? `ручная вставка, ${count} cookie`
        : `авто-сбор, ${count} cookie`;
    return {
      ok: true,
      data,
      probeOk,
      probeLine,
      note,
      message: data.message || "Cookie сохранён.",
    };
  }

  async function sendCookiesToAllServers({
    apiUrls,
    apiKey,
    profile,
    payload,
    probe = true,
    source = "rts-cookie-bridge",
  }) {
    const targets = (apiUrls || []).filter(Boolean);
    if (!targets.length) {
      throw new Error("Укажите хотя бы один URL API.");
    }

    const results = [];
    for (const apiUrl of targets) {
      try {
        const result = await sendCookiesToServer({
          apiUrl,
          apiKey,
          profile,
          payload,
          probe,
          source,
        });
        results.push({ apiUrl, ok: true, ...result });
      } catch (e) {
        results.push({
          apiUrl,
          ok: false,
          error: String(e.message || e),
          probeOk: false,
        });
      }
    }

    const allOk = results.every((r) => r.ok);
    const probeOk = results.every((r) => r.ok && r.probeOk);
    const lines = results.map((r) => {
      if (!r.ok) {
        return `✗ ${r.apiUrl}\n  ${r.error}`;
      }
      return `✓ ${r.apiUrl}\n  ${r.message || "OK"} · ${r.probeLine || "probe: —"}`;
    });
    const note = results[0] && results[0].note ? results[0].note : "";
    return {
      ok: allOk,
      probeOk,
      results,
      note,
      message: allOk ? "Cookie сохранён на все сервисы." : "Ошибка на одном или нескольких сервисах.",
      statusText: lines.join("\n\n"),
    };
  }

  async function runAutoSave() {
    const settings = await loadSyncSettings();
    const state = await getAutoRefreshState();
    if (!state.autoRefreshEnabled) {
      return { skipped: true, reason: "disabled" };
    }

    const apiUrls = collectApiUrls(settings);
    const apiKey = String(settings.apiKey || "").trim();
    const profile = String(settings.profile || "").trim();
    if (!apiUrls.length || !apiKey || !profile) {
      await stopAutoRefresh();
      const err = "Авто-обновление остановлено: не заданы URL API, key или профиль.";
      await setAutoRefreshState({
        autoRefreshLastAt: new Date().toISOString(),
        autoRefreshLastOk: false,
        autoRefreshLastError: err,
      });
      return { ok: false, error: err };
    }

    try {
      const payload = await resolveCookiePayloadAuto(profile);
      const result = await sendCookiesToAllServers({
        apiUrls,
        apiKey,
        profile,
        payload,
        probe: false,
        source: "rts-cookie-bridge-auto",
      });
      await setAutoRefreshState({
        autoRefreshLastAt: new Date().toISOString(),
        autoRefreshLastOk: result.ok,
        autoRefreshLastError: result.ok ? "" : result.statusText,
        autoRefreshLastCount: payload.cookies.length,
      });
      await scheduleAutoAlarm(AUTO_REFRESH_INTERVAL_SEC);
      return result;
    } catch (e) {
      const err = String(e.message || e);
      await setAutoRefreshState({
        autoRefreshLastAt: new Date().toISOString(),
        autoRefreshLastOk: false,
        autoRefreshLastError: err,
      });
      await scheduleAutoAlarm(AUTO_REFRESH_INTERVAL_SEC);
      return { ok: false, error: err };
    }
  }

  function formatAutoStatus(state) {
    if (!state.autoRefreshEnabled) {
      return "Авто-обновление выключено.";
    }
    const lastAt = state.autoRefreshLastAt
      ? new Date(state.autoRefreshLastAt).toLocaleString("ru-RU")
      : "—";
    if (state.autoRefreshLastOk === true) {
      const count = state.autoRefreshLastCount ? `, ${state.autoRefreshLastCount} cookie` : "";
      return `Авто-обновление: каждые ${AUTO_REFRESH_INTERVAL_SEC} сек.\nПоследнее: ${lastAt} — OK${count}`;
    }
    if (state.autoRefreshLastOk === false && state.autoRefreshLastError) {
      return `Авто-обновление: каждые ${AUTO_REFRESH_INTERVAL_SEC} сек.\nПоследнее: ${lastAt} — ошибка:\n${state.autoRefreshLastError}`;
    }
    return `Авто-обновление: каждые ${AUTO_REFRESH_INTERVAL_SEC} сек.\nОжидание первого цикла…`;
  }

  return {
    AUTO_ALARM_NAME,
    AUTO_REFRESH_INTERVAL_SEC,
    PROFILE_HINTS,
    normalizeApiUrl,
    normalizeCookieText,
    collectApiUrls,
    cookieFromManual,
    cookieHasAuthMarkers,
    cookieNameSet,
    missingAuthMessage,
    collectCookiesAuto,
    resolveCookiePayloadAuto,
    loadSyncSettings,
    saveSyncSettings,
    getAutoRefreshState,
    startAutoRefresh,
    stopAutoRefresh,
    restoreAutoRefresh,
    runAutoSave,
    sendCookiesToServer,
    sendCookiesToAllServers,
    formatAutoStatus,
  };
})();

if (typeof globalThis !== "undefined") {
  globalThis.RtsCookieBridge = RtsCookieBridge;
}
