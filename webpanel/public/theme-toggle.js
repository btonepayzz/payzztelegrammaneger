(function () {
  function labelFor(theme) {
    return theme === "dark" ? "Aydınlık temaya geç" : "Karanlık temaya geç";
  }

  function iconFor(theme) {
    return theme === "dark" ? "☀️" : "🌙";
  }

  function syncButtons() {
    var t = document.documentElement.dataset.theme || "dark";
    document.querySelectorAll(".theme-toggle").forEach(function (btn) {
      btn.setAttribute("aria-label", labelFor(t));
      btn.setAttribute("title", labelFor(t));
      btn.textContent = iconFor(t);
    });
  }

  function toggle() {
    var cur = document.documentElement.dataset.theme || "dark";
    var next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("payzz-theme", next);
    } catch (e) {}
    syncButtons();
  }

  function bind() {
    syncButtons();
    document.querySelectorAll(".theme-toggle").forEach(function (btn) {
      btn.addEventListener("click", toggle);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
