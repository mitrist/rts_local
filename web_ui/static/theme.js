(function () {
  var KEY = "rtsTheme";

  function isLight() {
    return document.documentElement.getAttribute("data-theme") === "light";
  }

  function applyTheme(theme) {
    if (theme === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function syncToggle(btn) {
    if (!btn) return;
    var light = isLight();
    btn.setAttribute("aria-pressed", light ? "true" : "false");
    btn.setAttribute(
      "aria-label",
      light ? "Переключить на тёмную тему" : "Переключить на светлую тему"
    );
    var label = btn.querySelector(".theme-toggle-label");
    if (label) {
      label.textContent = light ? "Тёмная тема" : "Светлая тема";
    }
    var icon = btn.querySelector(".theme-toggle-icon");
    if (icon) {
      icon.textContent = light ? "☀" : "☾";
    }
  }

  window.rtsInitThemeToggle = function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn || btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", function () {
      var next = isLight() ? "dark" : "light";
      applyTheme(next);
      localStorage.setItem(KEY, next);
      syncToggle(btn);
    });
    syncToggle(btn);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", window.rtsInitThemeToggle);
  } else {
    window.rtsInitThemeToggle();
  }
})();
