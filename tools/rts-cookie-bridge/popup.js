"use strict";

const Bridge = globalThis.RtsCookieBridge;

function setStatus(text, ok) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = ok === true ? "ok" : ok === false ? "err" : "";
}

function setAutoStatus(text) {
  const el = document.getElementById("autoStatus");
  if (el) el.textContent = text;
}

function manualCookieText() {
  const details = document.getElementById("manualCookieDetails");
  if (details && !details.open) return "";
  return document.getElementById("cookieManual").value.trim();
}

async function resolveCookiePayload(profile) {
  const manual = manualCookieText();
  if (manual) {
    const payload = Bridge.cookieFromManual(manual);
    if (!Bridge.cookieHasAuthMarkers(payload.cookie, profile)) {
      throw new Error(
        Bridge.missingAuthMessage(profile, [...Bridge.cookieNameSet(payload.cookie)])
      );
    }
    return payload;
  }

  const payload = await Bridge.collectCookiesAuto(profile);
  if (!Bridge.cookieHasAuthMarkers(payload.cookie, profile)) {
    throw new Error(Bridge.missingAuthMessage(profile, payload.names));
  }
  return payload;
}

function updateProfileHint() {
  const profile = document.getElementById("profile").value.trim();
  document.getElementById("profileHint").textContent = Bridge.PROFILE_HINTS[profile] || "";
}

async function refreshAutoStatus() {
  const resp = await chrome.runtime.sendMessage({ type: "getAutoStatus" });
  if (resp && resp.ok) {
    setAutoStatus(resp.text);
    const checkbox = document.getElementById("autoRefresh");
    if (checkbox) checkbox.checked = !!resp.state.autoRefreshEnabled;
  }
}

async function loadSettings() {
  const data = await Bridge.loadSyncSettings();
  if (data.apiUrl) document.getElementById("apiUrl").value = data.apiUrl;
  if (data.apiUrl2) document.getElementById("apiUrl2").value = data.apiUrl2;
  if (data.apiKey) document.getElementById("apiKey").value = data.apiKey;
  if (data.profile) document.getElementById("profile").value = data.profile;
  updateProfileHint();
  await refreshAutoStatus();
}

async function saveSettings() {
  const apiUrl = document.getElementById("apiUrl").value.trim();
  const apiUrl2 = document.getElementById("apiUrl2").value.trim();
  const apiKey = document.getElementById("apiKey").value.trim();
  const profile = document.getElementById("profile").value.trim();
  await Bridge.saveSyncSettings({ apiUrl, apiUrl2, apiKey, profile });
}

async function setAutoRefreshEnabled(enabled) {
  const resp = await chrome.runtime.sendMessage({
    type: enabled ? "startAutoRefresh" : "stopAutoRefresh",
  });
  if (!resp || !resp.ok) {
    throw new Error((resp && resp.error) || "Не удалось изменить авто-обновление.");
  }
  await refreshAutoStatus();
}

async function sendCookies() {
  let apiUrl = Bridge.normalizeApiUrl(document.getElementById("apiUrl").value);
  let apiUrl2 = Bridge.normalizeApiUrl(document.getElementById("apiUrl2").value);
  const apiKey = document.getElementById("apiKey").value.trim();
  const profile = document.getElementById("profile").value.trim();
  const autoRefreshWanted = document.getElementById("autoRefresh").checked;
  const isManual = !!manualCookieText();

  const apiUrls = Bridge.collectApiUrls({ apiUrl, apiUrl2 });
  if (!apiUrls.length || !apiKey) {
    setStatus("Укажите хотя бы один URL API и API key.", false);
    return;
  }
  if (apiUrl !== document.getElementById("apiUrl").value.trim()) {
    document.getElementById("apiUrl").value = apiUrl;
  }
  if (apiUrl2 !== document.getElementById("apiUrl2").value.trim()) {
    document.getElementById("apiUrl2").value = apiUrl2;
  }
  if (!profile) {
    setStatus("Выберите профиль.", false);
    return;
  }
  await saveSettings();
  setStatus("Сбор cookie из браузера…", null);
  try {
    const payload = await resolveCookiePayload(profile);
    setStatus(
      `${payload.source === "manual" ? "ручная вставка" : "авто-сбор"}, ${payload.cookies.length} cookie\n` +
        `Отправка на ${apiUrls.length} сервис(а)…\nНе закрывайте popup — идёт проверка сессии.`,
      null
    );

    const result = await Bridge.sendCookiesToAllServers({
      apiUrls,
      apiKey,
      profile,
      payload,
      probe: true,
      source: payload.source === "manual" ? "rts-cookie-bridge-manual" : "rts-cookie-bridge",
    });

    let statusText = `${result.message}\n${result.note}\n\n${result.statusText}`;

    if (autoRefreshWanted && !isManual && result.probeOk) {
      await setAutoRefreshEnabled(true);
      statusText +=
        `\n\nАвто-обновление включено (каждые ${Bridge.AUTO_REFRESH_INTERVAL_SEC} сек).` +
        "\nPopup можно закрыть — cookie будут отправляться в фоне.";
      await refreshAutoStatus();
    } else if (autoRefreshWanted && isManual) {
      statusText += "\n\nАвто-обновление не включено: использована ручная вставка cookie.";
    } else if (autoRefreshWanted && !result.probeOk) {
      statusText += "\n\nАвто-обновление не включено: probe не прошёл на всех сервисах.";
      await setAutoRefreshEnabled(false);
    } else {
      await setAutoRefreshEnabled(false);
    }

    setStatus(statusText, result.ok && result.probeOk);
  } catch (e) {
    let msg = String(e.message || e);
    if (msg === "Failed to fetch") {
      msg =
        "Failed to fetch — запрос не дошёл до сервера.\n\n" +
        "• Не закрывайте popup до ответа\n" +
        "• URL: https://rts.raytec-log.pro/api/rts/cookie/save\n" +
        "• Проверьте api-key и блокировщики (AdBlock, VPN)";
    }
    setStatus(msg, false);
  }
}

async function onAutoRefreshToggle() {
  const enabled = document.getElementById("autoRefresh").checked;
  try {
    await saveSettings();
    await setAutoRefreshEnabled(enabled);
    if (enabled) {
      setAutoStatus(
        `Авто-обновление включено (каждые ${Bridge.AUTO_REFRESH_INTERVAL_SEC} сек).\n` +
          "Убедитесь, что сессия РТС активна в браузере."
      );
    }
  } catch (e) {
    document.getElementById("autoRefresh").checked = !enabled;
    setAutoStatus(String(e.message || e));
  }
}

document.getElementById("sendBtn").addEventListener("click", sendCookies);
document.getElementById("profile").addEventListener("change", updateProfileHint);
document.getElementById("autoRefresh").addEventListener("change", onAutoRefreshToggle);
loadSettings();
