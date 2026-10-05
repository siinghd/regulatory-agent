// Polls /r/{token}.json and re-renders the status, progress bar, steps and timeline.
// Plain DOM APIs and textContent only (no innerHTML), so nothing from the server is parsed as HTML.
(function () {
  "use strict";
  const root = document.querySelector(".progress");
  if (!root) return;
  // CSP forbids inline style attributes, so the server renders data-pct and we apply it here.
  const initialFill = document.getElementById("bar-fill");
  if (initialFill) initialFill.style.setProperty("--pct", (initialFill.dataset.pct || "0") + "%");
  if (root.dataset.terminal === "true") return;
  const token = root.dataset.token;
  let delay = 1500;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function render(v) {
    const status = document.getElementById("status-line");
    if (status) {
      let line = v.outcome || (v.step ? v.step + (v.total ? ` (${v.done || 0} of ${v.total})` : "") + "…" : "Working on it…");
      status.textContent = line;
    }
    const fill = document.getElementById("bar-fill");
    if (fill && v.total) fill.style.setProperty("--pct", Math.round(((v.done || 0) / v.total) * 100) + "%");

    const steps = document.getElementById("steps");
    if (steps && v.steps.length) {
      steps.replaceChildren(...v.steps.map(s => {
        const li = el("li", s.status);
        li.append(el("span", "dot"), document.createTextNode(s.label));
        return li;
      }));
    }
    const tl = document.getElementById("timeline");
    if (tl) {
      tl.replaceChildren(...v.timeline.map(e => {
        const li = el("li");
        const t = el("time", null, e.at.slice(11, 19) + " UTC");
        t.dateTime = e.at;
        li.append(t, document.createTextNode(" " + e.label));
        return li;
      }));
    }
  }

  async function tick() {
    try {
      const r = await fetch(`/r/${encodeURIComponent(token)}.json`, { cache: "no-store" });
      if (r.status === 404) return; // the request is gone: nothing more to show
      if (!r.ok) throw new Error(`HTTP ${r.status}`); // 502 during a deploy, 429...: keep trying
      const v = await r.json();
      render(v);
      if (v.terminal) return;
      delay = 1500;
    } catch (_) {
      delay = Math.min(delay * 2, 15000); // network hiccup or server error: back off, keep trying
    }
    setTimeout(tick, delay);
  }
  setTimeout(tick, delay);
})();
