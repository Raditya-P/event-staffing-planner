/* Guided welcome: one idea per screen, Back/Next, arrow keys, and the browser's back button. */
(function () {
  "use strict";
  const panes = Array.from(document.querySelectorAll("[data-step]"));
  const steps = Array.from(document.querySelectorAll("#steps .step"));
  const back = document.getElementById("back");
  const next = document.getElementById("next");
  const counter = document.getElementById("counter");
  const LAST = panes.length - 1;
  let current = 0;

  function show(i, push) {
    current = Math.max(0, Math.min(LAST, i));
    panes.forEach((p, k) => { p.hidden = k !== current; });
    steps.forEach((s, k) => s.classList.toggle("step-primary", k <= current));
    back.disabled = current === 0;
    counter.textContent = `${current + 1} of ${panes.length}`;
    next.hidden = current === LAST;
    next.textContent = current === 0 ? "Start the walkthrough" : current === LAST - 1 ? "Almost done" : "Next";
    if (push) history.pushState({ step: current }, "", `#step-${current + 1}`);
    const heading = panes[current].querySelector("h1, h2");
    if (heading && push) { heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }
  }

  back.addEventListener("click", () => show(current - 1, true));
  next.addEventListener("click", () => show(current + 1, true));
  steps.forEach((s, k) => { s.style.cursor = "pointer"; s.addEventListener("click", () => show(k, true)); });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select")) return;
    if (e.key === "ArrowRight") show(current + 1, true);
    if (e.key === "ArrowLeft") show(current - 1, true);
  });
  window.addEventListener("popstate", () => {
    const m = location.hash.match(/step-(\d+)/);
    show(m ? Number(m[1]) - 1 : 0, false);
  });
  const m = location.hash.match(/step-(\d+)/);
  show(m ? Number(m[1]) - 1 : 0, false);

  // Real numbers and this server's addresses.
  fetch("/api/me").then((r) => r.json()).then((me) => {
    document.getElementById("mcp-url").value = me.mcp_url;
    if (me.auth_mode !== "oidc" || me.signed_in) {
      document.querySelectorAll("[data-signin]").forEach((a) => { a.href = "/"; a.textContent = a.classList.contains("btn-lg") ? "Open my dashboard" : "Open dashboard"; });
    }
  }).catch(() => {});
  fetch("/api/facts").then((r) => r.json()).then((f) => {
    if (!f.available) return;
    const words = ["zero", "one", "two", "three", "four", "five", "six"];
    const gates = document.querySelector('[data-fact="gates"]');
    if (gates && f.gates) gates.textContent = words[f.gates] || String(f.gates);
    const days = document.querySelector('[data-fact="history_days"]');
    if (days && f.history_days) days.textContent = String(f.history_days);
    const cov = document.querySelector("[data-fact-coverage]");
    if (cov && f.coverage != null) {
      cov.textContent = `On past days it hadn't seen, the forecast's likely ranges contained ${Math.round(f.coverage * 100)}% of what really happened. The aim is ${Math.round((f.target || 0.8) * 100)}%, so its sense of its own uncertainty is about right.`;
    }
  }).catch(() => {});

  const copy = document.getElementById("copy");
  copy.addEventListener("click", async () => {
    const value = document.getElementById("mcp-url").value;
    try { await navigator.clipboard.writeText(value); copy.textContent = "Copied"; } catch (e) { document.getElementById("mcp-url").select(); }
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
})();
