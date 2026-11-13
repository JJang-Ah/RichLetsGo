// Dashboard behaviour. Data values are only ever written with textContent (M5-01 출력 규칙).
"use strict";

function refreshOverview() {
  fetch("/api/overview", { credentials: "same-origin", cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data) return;
      if (Array.isArray(data.attention)) renderAttention(data.attention);
      renderBand(data.tone, data.pause_reasons);
      for (const el of document.querySelectorAll("[data-field]")) {
        const key = el.getAttribute("data-field");
        if (key === "trader") el.textContent = "Trader " + data.trader;
        else if (Object.prototype.hasOwnProperty.call(data, key)) el.textContent = data[key];
      }
    })
    .catch(() => {});
}

// Keeps the status band's colour and reasons in step with the state word (PR #68 P2).
function renderBand(tone, reasons) {
  const band = document.getElementById("band");
  if (band && ["ok", "warn", "halt"].includes(tone)) {
    band.classList.remove("tone-ok", "tone-warn", "tone-halt");
    band.classList.add("tone-" + tone);
  }
  const why = document.getElementById("state-why");
  if (why && Array.isArray(reasons)) {
    why.textContent = reasons.length ? "이유: " + reasons.map(String).join(", ") : "";
    why.hidden = reasons.length === 0;
  }
}

// Rebuilds the "먼저 확인할 것" list so it never shows a stale all-clear (PR #65 P2).
// An unchanged list is left alone, so keyboard focus and the live region stay put (P3).
function renderAttention(items) {
  const box = document.getElementById("attention");
  if (!box) return;
  const safe = (href) => (href.startsWith("/") || href.startsWith("#") ? href : "/");
  const wanted = items.map((i) => [safe(String(i.href || "")), String(i.text || "")]);
  const shown = [...box.querySelectorAll("ul a")].map((a) => [
    a.getAttribute("href"),
    a.lastChild ? a.lastChild.textContent : "",
  ]);
  const clear = box.querySelector(".all-clear") !== null;
  const same = items.length === 0 ? clear : JSON.stringify(wanted) === JSON.stringify(shown);
  if (same) return;
  for (const old of box.querySelectorAll("ul, .all-clear")) old.remove();
  const mark = (symbol) => {
    const span = document.createElement("span");
    span.className = "mark";
    span.setAttribute("aria-hidden", "true");
    span.textContent = symbol;
    return span;
  };
  if (items.length === 0) {
    const p = document.createElement("p");
    p.className = "all-clear";
    p.append(mark("✓"), document.createTextNode("확인할 항목이 없습니다."));
    box.append(p);
  } else {
    const ul = document.createElement("ul");
    ul.className = "plain";
    for (const item of items) {
      const href = String(item.href || "");
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.setAttribute("href", safe(href));
      a.append(mark("!"), document.createTextNode(String(item.text || "")));
      li.append(a);
      ul.append(li);
    }
    box.append(ul);
  }
  box.classList.toggle("has-items", items.length > 0);
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
