/* Senkron: FOUC önlemek için stylesheet'ten önce yükleyin. */
(function () {
  try {
    var k = "payzz-theme";
    var t = localStorage.getItem(k);
    if (t !== "light" && t !== "dark") {
      t =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
    }
    document.documentElement.dataset.theme = t;
  } catch (e) {
    document.documentElement.dataset.theme = "dark";
  }
})();
