/* polycopy dashboard M6 — JS minimal client.
   - Theme toggle persistant via localStorage (clé 'polycopy.theme').
   - Lucide createIcons() après chaque htmx:afterSwap (sinon icônes manquantes).
   - Fetch /api/version (footer).
   Aucune dépendance, aucun token, aucune session. */
(function () {
  "use strict";

  var THEME_KEY = "polycopy.theme";

  function safeGetItem(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function safeSetItem(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  function applyStoredTheme() {
    var stored = safeGetItem(THEME_KEY);
    if (stored === "dark" || stored === "light") {
      document.documentElement.setAttribute("data-theme", stored);
    }
  }

  function toggleTheme() {
    var current = document.documentElement.getAttribute("data-theme") || "dark";
    var next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    safeSetItem(THEME_KEY, next);
    if (window._pnlChart) {
      // Forcer un refresh des couleurs Chart.js liées aux variables CSS.
      try { window._pnlChart.update(); } catch (e) { /* noop */ }
    }
  }

  function initLucide() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      try { window.lucide.createIcons(); } catch (e) { /* noop */ }
    }
  }

  function fetchVersion() {
    try {
      fetch("/api/version", { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.version) return;
          document.querySelectorAll("[data-app-version]").forEach(function (el) {
            el.textContent = "v" + d.version;
          });
        })
        .catch(function () { /* offline ok */ });
    } catch (e) { /* noop */ }
  }

  function onReady() {
    applyStoredTheme();
    initLucide();
    fetchVersion();
    document.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-theme-toggle]");
      if (btn) toggleTheme();
    });
    document.body.addEventListener("htmx:afterSwap", initLucide);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onReady);
  } else {
    onReady();
  }
})();
