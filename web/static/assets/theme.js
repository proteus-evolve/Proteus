// Explicit theme choice, remembered per browser; absent key = follow the OS.
(() => {
  const KEY = "proteus_theme";
  const sysDark = () => matchMedia("(prefers-color-scheme: dark)").matches;
  const btn = document.getElementById("theme-btn");
  function paint() {
    const set = localStorage.getItem(KEY);
    if (set) document.documentElement.setAttribute("data-theme", set);
    else document.documentElement.removeAttribute("data-theme");
    if (btn) btn.textContent = (set ? set === "dark" : sysDark()) ? "light" : "dark";
  }
  btn?.addEventListener("click", () => {
    const dark = localStorage.getItem(KEY)
      ? localStorage.getItem(KEY) === "dark" : sysDark();
    localStorage.setItem(KEY, dark ? "light" : "dark");
    paint();
  });
  paint();
})();
