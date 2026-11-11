// Dashboard behaviour. Data values are only ever written with textContent (M5-01 출력 규칙).
"use strict";

function refreshOverview() {
  fetch("/api/overview", { credentials: "same-origin", cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data) return;
      for (const el of document.querySelectorAll("[data-field]")) {
        const key = el.getAttribute("data-field");
        if (key === "trader") el.textContent = "Trader " + data.trader;
        else if (Object.prototype.hasOwnProperty.call(data, key)) el.textContent = data[key];
      }
    })
    .catch(() => {});
}

function bindChart(figure) {
  const points = (figure.getAttribute("data-points") || "")
    .split(";")
    .map((row) => row.split("|"))
    .filter((p) => p.length === 4)
    .map(([x, y, t, v]) => ({ x: Number(x), y: Number(y), t, v }));
  if (points.length < 2) return;
  const svg = figure.querySelector("svg");
  const cross = svg.querySelector(".crosshair");
  const marker = svg.querySelector(".marker");
  const tip = figure.querySelector(".tooltip");
  const box = svg.viewBox.baseVal;

  function nearest(vx) {
    let lo = 0, hi = points.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (points[mid].x < vx) lo = mid; else hi = mid;
    }
    return vx - points[lo].x < points[hi].x - vx ? points[lo] : points[hi];
  }

  function show(evt) {
    const rect = svg.getBoundingClientRect();
    const vx = ((evt.clientX - rect.left) / rect.width) * box.width;
    const p = nearest(vx);
    cross.setAttribute("x1", p.x); cross.setAttribute("x2", p.x);
    marker.setAttribute("cx", p.x); marker.setAttribute("cy", p.y);
    cross.removeAttribute("hidden"); marker.removeAttribute("hidden");
    tip.textContent = p.t + "  ·  " + p.v;
    tip.hidden = false;
    const px = (p.x / box.width) * rect.width;
    const py = (p.y / box.height) * rect.height;
    const left = Math.min(Math.max(px + 12, 0), rect.width - tip.offsetWidth);
    tip.style.left = left + "px";
    tip.style.top = Math.max(py - 40, 0) + figure.querySelector("figcaption").offsetHeight + "px";
  }

  function hide() {
    cross.setAttribute("hidden", ""); marker.setAttribute("hidden", "");
    tip.hidden = true;
  }

  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
  window.addEventListener("resize", hide);
}

document.addEventListener("DOMContentLoaded", () => {
  for (const fig of document.querySelectorAll("figure.chart[data-points]")) bindChart(fig);
  const tab = document.querySelector('.tabs [aria-current="page"]');
  if (tab) tab.parentElement.scrollLeft = tab.offsetLeft - 12;  // keep the current tab visible on narrow screens
  if (document.querySelector("[data-field]")) setInterval(refreshOverview, 10000);
});
