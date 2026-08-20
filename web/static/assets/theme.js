// Explicit theme choice, remembered per browser; absent key = the light campaign default.
(() => {
  const KEY = "proteus_theme";
  const btn = document.getElementById("theme-btn");
  function paint() {
    const set = localStorage.getItem(KEY);
    if (set) document.documentElement.setAttribute("data-theme", set);
    else document.documentElement.setAttribute("data-theme", "light");
    if (btn) btn.textContent = (set || "light") === "dark" ? "light" : "dark";
  }
  btn?.addEventListener("click", () => {
    const dark = (localStorage.getItem(KEY) || "light") === "dark";
    localStorage.setItem(KEY, dark ? "light" : "dark");
    paint();
  });
  paint();
})();
