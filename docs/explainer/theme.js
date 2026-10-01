"use strict";

const themeButton = document.getElementById("theme-button");
themeButton.hidden = false;
themeButton.addEventListener("click", () => {
  const useLightTheme = document.documentElement.dataset.theme !== "silk";
  document.documentElement.dataset.theme = useLightTheme ? "silk" : "luxury";
  themeButton.setAttribute("aria-pressed", String(useLightTheme));
  themeButton.setAttribute("aria-label", `Switch to ${useLightTheme ? "dark" : "light"} theme`);
});
