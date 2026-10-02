"use strict";

(() => {
  const themeButton = document.getElementById("theme-button");
  themeButton.hidden = false;
  themeButton.addEventListener("click", () => {
    const useLightTheme = document.documentElement.dataset.theme !== "silk";
    document.documentElement.dataset.theme = useLightTheme ? "silk" : "luxury";
    themeButton.setAttribute("aria-pressed", String(useLightTheme));
    themeButton.setAttribute("aria-label", `Switch to ${useLightTheme ? "dark" : "light"} theme`);
  });

  const revealTarget = () => {
    const fragment = window.location.hash.slice(1);
    if (!/^task-n\d+$/.test(fragment)) return;
    const task = document.getElementById(fragment);
    if (task) {
      task.open = true;
      task.scrollIntoView({ block: "start" });
    }
  };
  window.addEventListener("hashchange", revealTarget);
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='#task-n']");
    if (!link) return;
    const task = document.getElementById(link.getAttribute("href").slice(1));
    if (task) task.open = true;
  });
  revealTarget();

  const graph = document.getElementById("plan-graph");
  if (graph) {
    document.getElementById("graph-controls").hidden = false;
    const originalWidth = Number(graph.getAttribute("width"));
    const originalHeight = Number(graph.getAttribute("height"));
    let zoom = 1;
    const updateZoom = (nextZoom) => {
      zoom = Math.max(0.25, Math.min(2, nextZoom));
      graph.setAttribute("width", String(originalWidth * zoom));
      graph.setAttribute("height", String(originalHeight * zoom));
      document.getElementById("zoom-level").textContent = `${Math.round(zoom * 100)}%`;
      document.getElementById("zoom-out").disabled = zoom <= 0.25;
      document.getElementById("zoom-in").disabled = zoom >= 2;
    };
    document.getElementById("zoom-in").addEventListener("click", () => updateZoom(zoom + 0.25));
    document.getElementById("zoom-out").addEventListener("click", () => updateZoom(zoom - 0.25));
    document.getElementById("zoom-reset").addEventListener("click", () => updateZoom(1));
  }

  // Print expanded task/report contents, then restore the reader's chosen state.
  let closedBeforePrint = [];
  window.addEventListener("beforeprint", () => {
    closedBeforePrint = Array.from(document.querySelectorAll("details:not([open])"));
    closedBeforePrint.forEach((detail) => { detail.open = true; });
  });
  window.addEventListener("afterprint", () => {
    closedBeforePrint.forEach((detail) => { detail.open = false; });
    closedBeforePrint = [];
  });
})();
