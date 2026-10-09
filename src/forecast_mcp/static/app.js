/* The signed-in app: one idea per page, a guided tour, and short explanations on demand.
   Reads and writes the same store as Claude's tools and polls one revision number per event,
   so changes made in Claude show up here within a couple of seconds.
   Every text that comes from data is inserted with textContent. */
(function () {
  "use strict";
  const { h, fmt, fmt1, eur, pct } = window.FC;
  const $ = (id) => document.getElementById(id);
  const page = $("page");
  const S = { me: null, events: [], eventId: null, event: null, scenarioId: null, scenario: null, revision: null,
              gate: null, lastSync: 0, dirty: false, showWhy: false };

  /* ---------------- small helpers ---------------- */

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: fine */ } },
  };

  async function api(path, opts) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) { location.href = "/welcome"; throw new Error("Please sign in."); }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
    return data;
  }
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

  function toast(text, kind) {
    const el = h("div", { class: `alert alert-soft shadow ${kind === "error" ? "alert-error" : "alert-success"}`, role: kind === "error" ? "alert" : "status" }, $("toasts"));
    h("span", {}, el, text);
    setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
  }
  async function act(fn, okText) {
    try { await fn(); if (okText) toast(okText); await reload(); } catch (e) { toast(e.message, "error"); }
  }

  const ICONS = {
    home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
    chart: "M4 19h16M5 15l4-5 4 3 6-7",
    plans: "M12 3v18M5 7h14M7 7l-3 7a3 3 0 0 0 6 0zM17 7l-3 7a3 3 0 0 0 6 0z",
    whatif: "M6 3v12M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6M18 9c0 5-12 3-12 6",
    inbox: "M4 13l3-8h10l3 8v6H4zM4 13h5l1 2h4l1-2h5",
    history: "M3 12a9 9 0 1 0 3-6.7M3 4v4h4M12 7v5l3 2",
    learn: "M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2zM4 19a2 2 0 0 1 2-2h13",
    claude: "M4 5h16v11H8l-4 4z",
    info: "M12 8h.01M11 12h1v5h1M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18",
    check: "M5 12l5 5L20 7",
    plus: "M12 5v14M5 12h14",
  };
  function icon(name, cls) {
    const s = FC.svg("svg", { viewBox: "0 0 24 24", class: cls || "size-5", fill: "none", stroke: "currentColor",
      "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
    FC.svg("path", { d: ICONS[name] }, s);
    return s;
  }
  function btn(parent, text, cls, onClick, iconName) {
    const b = h("button", { class: `btn ${cls || ""}`.trim(), type: "button" }, parent);
    if (iconName) b.appendChild(icon(iconName, "size-4"));
    b.appendChild(document.createTextNode(text));
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }
  function link(parent, text, href, cls) {
    return h("a", { class: cls || "link", href }, parent, text);
  }
  const longDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const isRunning = () => { const r = S.scenario && S.scenario.run; return !!(r && (r.status === "queued" || r.status === "running")); };
  const official = () => S.event && S.event.scenarios.find((s) => s.kind === "official" && s.status === "active");
  const thinGates = (fc) => (fc ? fc.series.filter((s) => s.history && s.history.thin) : []);

  /* ---------------- explanations (the "What am I looking at?" button) ---------------- */

  const EXPLAIN = {
    home: { title: "Overview", tour: 1, body: [
      "This page summarizes the selected plan: expected attendance, staffing cost and average waiting time.",
      "Use the menu to open each topic in detail." ] },
    forecast: { title: "Reading the forecast", tour: 2, body: [
      "The line shows the median forecast of arrivals per hour at the selected entrance.",
      "The shaded band is the 80% range: on about 8 days out of 10, actual arrivals fall inside it. A wider band indicates greater uncertainty." ] },
    uncertainty: { title: "Sources of uncertainty", tour: 3, body: [
      "Blue shows day-to-day variation. It cannot be reduced, so it is covered by a small staffing buffer.",
      "Orange shows uncertainty caused by limited historical data. Information you provide about the situation can reduce it." ] },
    plans: { title: "Choosing a staffing plan", tour: 4, body: [
      "Each point is a staffing plan. Plans further right use more staff and cost more; lower plans have shorter queues.",
      "The line above each point shows the P90 wait, which is exceeded on 1 day in 10. The tool marks a balanced plan; the final choice is yours." ] },
    whatifs: { title: "Scenarios", tour: 5, body: [
      "A scenario is a copy of the official plan for testing changes, such as an entrance closure or a staff shortage. Plans are recalculated within seconds.",
      "The official plan changes only when you adopt a scenario." ] },
    inbox: { title: "Inbox", tour: 6, body: [
      "When you mention roadworks, another event or a schedule change to Claude, it records a proposed change with its interpretation.",
      "Proposed changes wait here. Each takes effect only after you confirm it, and is then added to a scenario." ] },
    history: { title: "Activity log", tour: 6, body: [
      "Every proposal, confirmation, change and decision is recorded with its author, time and channel (website or Claude)." ] },
    claude: { title: "Claude integration", tour: 7, body: [
      "Add Event Staffing Planner to Claude as a connector, then ask questions in plain language. Claude displays the same charts in the conversation." ] },
  };
  function explain(key) {
    const e = EXPLAIN[key] || EXPLAIN.home;
    $("explain-title").textContent = e.title;
    const body = $("explain-body");
    body.textContent = "";
    for (const p of e.body) h("p", {}, body, p);
    $("explain-more").href = `#/tour/${e.tour}`;
    $("explain").showModal();
  }
  $("explain-more").addEventListener("click", () => $("explain").close());

  let confirmAction = null;
  function confirmBox(title, text, yesText, action) {
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = text;
    $("confirm-yes").textContent = yesText;
    confirmAction = action;
    $("confirm").showModal();
  }
  $("confirm-yes").addEventListener("click", async () => {
    $("confirm").close();
    if (confirmAction) await confirmAction();
  });

  function header(parent, title, subtitle, helpKey) {
    const row = h("div", { class: "flex flex-wrap items-start justify-between gap-3" }, parent);
    const left = h("div", {}, row);
    h("h1", { class: "text-2xl font-bold sm:text-3xl" }, left, title);
    if (subtitle) h("p", { class: "mt-1 text-base-content/75" }, left, subtitle);
    if (helpKey) btn(row, "About this page", "btn-sm btn-soft", () => explain(helpKey), "info");
    return row;
  }
  function card(parent, cls) {
    const c = h("div", { class: `card bg-base-100 shadow-sm ${cls || ""}`.trim() }, parent);
    return h("div", { class: "card-body gap-3" }, c);
  }
  function waiting(parent, text) {
    const body = card(parent);
    const row = h("div", { class: "flex items-center gap-3" }, body);
    h("span", { class: "loading loading-dots loading-md" }, row);
    h("span", {}, row, text || "Calculating. This takes a few seconds.");
  }

  /* ---------------- explanation layer: one button per chart, steps in order ----------------
     Why, how sure and what would change it (question-driven explanation, Liao et al. 2020), each shown on
     demand and one step at a time (progressive disclosure, Springer and Whittaker 2020). The format of the
     "why" step (chart, text or both) follows Szymanski et al. (2021). Format and wording are the viewer's
     own preferences, kept in this browser. Every sentence is computed from the forecast and the plans. */

  const PREFS = { format: store.get("fm-format") || "hybrid", wording: store.get("fm-wording") || "technical" };
  FC.setWording(PREFS.wording);
  const WORDS = {
    technical: { median: "median", range: "80% range", p90: "P90 wait", p90Means: "exceeded on 1 day in 10",
                 vol: "day-to-day variation", hist: "limited history", histPart: "limited-history component" },
    plain: { median: "most likely number", range: "likely range", p90: "busy-day wait", p90Means: "only 1 day in 10 is worse",
             vol: "normal ups and downs", hist: "little past data", histPart: "part caused by little past data" },
  };
  const wd = () => WORDS[PREFS.wording];
  const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  const para = (parent, text, cls) => h("p", cls ? { class: cls } : {}, parent, text);
  let EX = null;

  function openExplainer(title, steps) {
    EX = { title, steps, i: 0 };
    $("explainer").showModal();  // open first, so charts can measure the width they draw into
    drawExplainer();
  }
  function drawExplainer() {
    const step = EX.steps[EX.i];
    $("ex-title").textContent = EX.title;
    const ul = $("ex-steps");
    ul.textContent = "";
    EX.steps.forEach((s, k) => {
      const li = h("li", { class: `step ${k <= EX.i ? "step-primary" : ""}`.trim() }, ul);
      const b = h("button", { type: "button", class: "px-1 leading-tight", "aria-current": k === EX.i ? "step" : "false" }, li, s.title);
      b.addEventListener("click", () => { EX.i = k; drawExplainer(); });
    });
    const prefs = $("ex-prefs");
    prefs.textContent = "";
    if (step.formats) segmented(prefs, "Format", "format", [["visual", "Chart"], ["textual", "Text"], ["hybrid", "Both"]]);
    segmented(prefs, "Wording", "wording", [["plain", "Plain"], ["technical", "Technical"]]);
    const body = $("ex-body");
    body.textContent = "";
    step.render(body);
    $("ex-back").disabled = EX.i === 0;
    $("ex-count").textContent = `${EX.i + 1} of ${EX.steps.length}`;
    $("ex-next").textContent = EX.i === EX.steps.length - 1 ? "Done" : "Next";
  }
  function segmented(parent, label, key, options) {
    const wrap = h("div", { class: "flex items-center gap-2" }, parent);
    h("span", { class: "text-base-content/70" }, wrap, label);
    const group = h("div", { class: "join", role: "group", "aria-label": label }, wrap);
    for (const [value, text] of options) {
      const on = PREFS[key] === value;
      const b = h("button", { type: "button", class: `btn btn-xs join-item ${on ? "btn-primary" : ""}`.trim(), "aria-pressed": String(on) }, group, text);
      b.addEventListener("click", () => {
        PREFS[key] = value;
        store.set(`fm-${key}`, value);
        if (key === "wording") { FC.setWording(value); S.dirty = true; }  // the page's own charts redraw when the layer closes
        drawExplainer();
      });
    }
  }
  $("ex-back").addEventListener("click", () => { if (EX.i > 0) { EX.i -= 1; drawExplainer(); } });
  $("ex-next").addEventListener("click", () => { if (EX.i < EX.steps.length - 1) { EX.i += 1; drawExplainer(); } else $("explainer").close(); });

  function byFormat(body, drawChart, writeText) {
    if (PREFS.format !== "textual") drawChart(h("div", {}, body));
    if (PREFS.format !== "visual") writeText(h("div", { class: "space-y-2" }, body));
  }
  function navLink(parent, text, href) {
    const a = link(parent, text, href, "btn btn-sm");
    a.addEventListener("click", () => $("explainer").close());
    return a;
  }
  function backtestSentence(fc) {
    const c = pct(fc.backtest.coverage_p10_p90);
    return PREFS.wording === "plain"
      ? `Tested on past days it had not seen, the model's likely range held the actual number ${c} of the time, across all entrances (the aim is 8 times in 10).`
      : `Backtest across all entrances: on days held out from training, ${c} of actual arrivals fell within the 80% range (target: 80%).`;
  }
  function askClaude(body, prompt) {
    para(body, "Claude can explain this in conversation and answer follow-up questions. With the Event Staffing Planner connector, it works from the same forecast and plans.");
    const area = h("textarea", { class: "textarea w-full bg-base-200 text-sm", rows: 3, readonly: "", "aria-label": "Question for Claude" }, body);
    area.value = prompt;
    const row = h("div", { class: "flex flex-wrap gap-2" }, body);
    h("a", { class: "btn btn-primary btn-sm", href: `https://claude.ai/new?q=${encodeURIComponent(prompt)}`, target: "_blank", rel: "noopener" }, row, "Open in Claude");
    const copy = btn(row, "Copy question", "btn-sm", async () => {
      try { await navigator.clipboard.writeText(prompt); copy.textContent = "Copied"; } catch (e) { area.select(); }
      setTimeout(() => { copy.textContent = "Copy question"; }, 1500);
    });
    navLink(row, "Set up the connector", "#/claude");
  }

  function explainForecast(fc, series) {
    const pts = series.points;
    const name = series.gate_name.replace(/\s*\(.*\)$/, "");
    const peak = pts.reduce((a, b) => (b.p50 > a.p50 ? b : a));
    const widest = [...pts].sort((a, b) => (b.p90 - b.p10) - (a.p90 - a.p10)).slice(0, 3).sort((a, b) => a.hour.localeCompare(b.hour));
    const share = pts.reduce((t, p) => t + p.share_missing_history, 0) / pts.length;
    const mostHist = pts.reduce((a, b) => (b.share_missing_history > a.share_missing_history ? b : a));
    const dt = series.day_total;
    openExplainer(`Explain the forecast: ${name}`, [
      { title: "Why", formats: true, render: (b) => byFormat(b,
        (box) => FC.renderGate(box, fc, series.gate_id, { strip: true, legend: true, height: 190 }),
        (t) => {
          const w = wd();
          para(t, `Across the day, ${pct(1 - share)} of the uncertainty at this entrance comes from ${w.vol} and ${pct(share)} from ${w.hist}. The hours with the widest ${w.range}:`);
          const ul = h("ul", { class: "list-disc space-y-1 pl-5" }, t);
          for (const p of widest) h("li", {}, ul, `${p.hour}: ${fmt(p.p10)} to ${fmt(p.p90)} guests; ${pct(p.share_missing_history)} of this uncertainty comes from ${w.hist}.`);
          para(t, share >= 0.35
            ? `${cap(w.hist)} is a large part here, so the forecast at this entrance is less certain than its pattern alone suggests.`
            : `${cap(w.vol)} dominates. It cannot be reduced, so staffing plans keep a small buffer for it.`);
        }) },
      { title: "How sure", render: (b) => {
        const w = wd();
        para(b, `At the peak hour, ${peak.hour}, the ${w.median} is ${fmt(peak.p50)} guests, with a ${w.range} of ${fmt(peak.p10)} to ${fmt(peak.p90)}.`);
        para(b, `For the whole day at this entrance: about ${fmt(dt.p50)} guests (${w.range}: ${fmt(dt.p10)} to ${fmt(dt.p90)}).`);
        para(b, PREFS.wording === "plain"
          ? "On about 8 days out of 10, the actual number falls inside the likely range."
          : "The 80% range is expected to contain actual arrivals on about 8 days out of 10.");
        if (fc.backtest) para(b, backtestSentence(fc));
        if (series.history) para(b, `This entrance has ${series.history.days} days of history${series.history.thin ? ", which is flagged as limited" : ""}.`);
      } },
      { title: "What would change it", render: (b) => {
        const w = wd();
        const narrow = mostHist.sd_total > 0 ? 1 - mostHist.sd_volatility / mostHist.sd_total : 0;
        para(b, narrow >= 0.05
          ? `The ${w.histPart} shrinks as more days of data accumulate. Without it, the ${w.range} at ${mostHist.hour} would be about ${pct(narrow)} narrower, leaving only ${w.vol}.`
          : `Without the ${w.histPart}, the ${w.range} would be less than 5% narrower at every hour, because ${w.vol} dominates at this entrance.`);
        para(b, "The forecast also depends on these inputs. A change in a scenario, such as a show time change or a competing event, updates them:");
        const ul = h("ul", { class: "list-disc space-y-1 pl-5 text-sm" }, b);
        for (const a of (fc.assumptions || []).slice(0, 6)) h("li", {}, ul, a.text);
        navLink(h("div", { class: "flex flex-wrap gap-2" }, b), "Open scenarios", "#/whatifs");
      } },
      { title: "Ask Claude", render: (b) => askClaude(b,
        `Using Event Staffing Planner, explain the ${name} forecast for ${S.event.name} on ${S.event.date}: why it is uncertain, how sure it is, and what would change it.`) },
    ]);
  }

  const STRESS_CACHE = {};
  let stressFactor = 1.1;
  function stressStep(body, scenarioId, sel) {
    para(body, "How this plan copes if more or fewer guests arrive than forecast. The staffing stays fixed; nothing is re-optimized.");
    const box = h("div", {}, body);
    const text = h("div", { class: "space-y-2" }, body);
    const key = `${scenarioId}:${sel.id}:${S.revision}`;
    const draw = (data) => {
      FC.renderStress(box, data, { selected: stressFactor, onSelect: (f) => { stressFactor = f; drawExplainer(); } });
      const p = data.points.find((q) => Math.abs(q.factor - stressFactor) < 1e-9) || data.points[0];
      const level = Math.abs(stressFactor - 1) < 1e-9 ? "as forecast"
        : `${Math.round(Math.abs(stressFactor - 1) * 100)}% ${stressFactor > 1 ? "above" : "below"} the forecast`;
      text.textContent = "";
      para(text, `With arrivals ${level}, the average wait is ${fmt1(p.expected_wait)} minutes per guest and the ${wd().p90} is ${fmt1(p.wait_p90)} minutes. The day's average wait exceeds ${fmt(data.wait_threshold_min)} minutes on ${pct(p.prob_wait_over_threshold)} of sampled days.`);
      para(text, "Select a point on the chart to change the arrival level.", "text-sm text-base-content/70");
    };
    if (STRESS_CACHE[key]) { draw(STRESS_CACHE[key]); return; }
    h("span", { class: "loading loading-dots loading-md" }, box);
    api(`/api/scenarios/${scenarioId}/stress?solution_id=${encodeURIComponent(sel.id)}`)
      .then((data) => { STRESS_CACHE[key] = data; if (box.isConnected) draw(data); })
      .catch((e) => {
        box.textContent = "";
        const al = h("div", { role: "alert", class: "alert alert-warning alert-soft" }, box);
        h("span", {}, al, e.message);
      });
  }

  function explainPlan(st) {
    const result = st.result, sel = st.selected_solution, fc = st.forecast, sc = st.scenario;
    const sols = result.solutions;
    const idx = sols.findIndex((p) => p.id === sel.id);
    const lanes = result.hours.map((_, i) => result.gates.reduce((t, g) => t + sel.schedule[g][i], 0));
    const hi = lanes.indexOf(Math.max(...lanes)), lo = lanes.indexOf(Math.min(...lanes));
    const arrivals = (i) => { const r = fc && fc.totals.hourly.find((x) => x.hour === result.hours[i]); return r ? r.p50 : null; };
    const what = { cheapest: "the lowest-cost plan the optimizer found",
                   shortest_wait: "the plan with the shortest waits the optimizer found",
                   balanced: "the balanced plan. It sits at the bend of the trade-off curve, where adding staff starts to buy smaller reductions in waiting time" }[sel.label]
      || "a plan you selected from the trade-off";
    const scope = sc.kind === "official" ? "the official plan" : `the plan in “${sc.name}”`;
    openExplainer(`Explain ${scope}`, [
      { title: "Why", formats: true, render: (b) => byFormat(b,
        (box) => FC.renderSchedule(box, sel, result, S.event.gates, { title: false }),
        (t) => {
          para(t, `This is ${what}.`);
          const a1 = arrivals(hi), a0 = arrivals(lo);
          para(t, `It opens ${lanes[hi]} lanes at ${result.hours[hi]}${a1 != null ? `, when about ${fmt(a1)} guests arrive across all entrances` : ""}, and ${lanes[lo]} lanes at ${result.hours[lo]}${a0 != null ? `, when about ${fmt(a0)} arrive` : ""}.`);
          para(t, `Waiting is longest for guests arriving around ${sel.peak_hour}: ${fmt1(sel.peak_hour_wait)} minutes on average.`);
          for (const e of result.constraints_applied || []) para(t, `Shaped by a change in this scenario: ${e.effect}`, "text-sm text-base-content/75");
        }) },
      { title: "How sure", render: (b) => {
        const w = wd();
        para(b, `Average wait: ${fmt1(sel.expected_wait)} minutes per guest. ${cap(w.p90)}: ${fmt1(sel.wait_p90)} minutes (${w.p90Means}).`);
        para(b, `On ${pct(sel.prob_wait_over_threshold)} of sampled days, the day's average wait exceeds ${fmt(result.wait_threshold_min)} minutes.`);
        para(b, `These figures come from ${fmt(result.algorithm.check_samples)} days sampled from the forecast, so they include its uncertainty.`);
        if (fc && fc.backtest) para(b, backtestSentence(fc));
      } },
      { title: "What would change it", render: (b) => {
        const prev = sols[idx - 1], next = sols[idx + 1], first = sols[0], last = sols[sols.length - 1];
        if (prev) para(b, `The next cheaper plan (${prev.staff_hours} staff-hours) saves ${eur(sel.staff_cost - prev.staff_cost)} and raises the average wait by ${fmt1(prev.expected_wait - sel.expected_wait)} minutes.`);
        if (next) para(b, `The next plan with shorter waits (${next.staff_hours} staff-hours) costs ${eur(next.staff_cost - sel.staff_cost)} more and cuts the average wait by ${fmt1(sel.expected_wait - next.expected_wait)} minutes.`);
        if (idx > 1) para(b, `The lowest-cost plan costs ${eur(first.staff_cost)}, with an average wait of ${fmt1(first.expected_wait)} minutes.`);
        if (idx < sols.length - 2) para(b, `The shortest-wait plan costs ${eur(last.staff_cost)}, with an average wait of ${fmt1(last.expected_wait)} minutes.`);
        para(b, "Changes such as an entrance closure or a staff limit alter the available plans. Test them in a scenario.", "text-sm text-base-content/75");
        navLink(h("div", { class: "flex flex-wrap gap-2" }, b), "Open scenarios", "#/whatifs");
      } },
      { title: "Stress test", render: (b) => stressStep(b, sc.id, sel) },
      { title: "Ask Claude", render: (b) => askClaude(b,
        `Using Event Staffing Planner, explain ${scope} for ${S.event.name}: why it staffs each entrance as it does, how sure its waiting times are, and what a cheaper or faster plan would change.`) },
    ]);
  }

  /* ---------------- navigation ---------------- */

  const NAV = [
    ["home", "Overview", "home"], ["forecast", "Forecast", "chart"], ["plans", "Staffing plans", "plans"],
    ["whatifs", "Scenarios", "whatif"], ["inbox", "Inbox", "inbox"], ["history", "Activity log", "history"],
    ["learn", "Guide", "learn"], ["claude", "Claude integration", "claude"],
  ];
  function renderNav(active) {
    const nav = $("nav");
    nav.textContent = "";
    for (const [key, label, ic] of NAV) {
      const li = h("li", {}, nav);
      const a = h("a", { href: `#/${key}`, class: key === active ? "menu-active" : "" }, li);
      a.appendChild(icon(ic));
      a.appendChild(document.createTextNode(label));
      if (key === "inbox" && S.event && S.event.pending.length) {
        h("span", { class: "badge badge-sm badge-secondary", "aria-label": `${S.event.pending.length} waiting` }, a, String(S.event.pending.length));
      }
    }
  }

  function route() {
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    return { name: parts[0] || "home", arg: parts[1] };
  }
  const PAGES = { home: pageHome, forecast: pageForecast, plans: pagePlans, whatifs: pageWhatIfs, inbox: pageInbox,
                  history: pageHistory, learn: pageLearn, tour: pageTour, claude: pageClaude, about: pageAbout };

  function busy() {
    return !!document.querySelector("dialog[open]") || !!(document.activeElement && document.activeElement.closest("#page input, #page textarea, #page select"));
  }
  function render(force) {
    if (!S.event || !S.scenario) return;
    if (!force && busy()) { S.dirty = true; return; }
    S.dirty = false;
    const r = route();
    if (!PAGES[r.name]) { location.hash = "#/home"; return; }
    renderNav(r.name === "tour" ? "learn" : r.name === "about" ? "learn" : r.name);
    const scroll = window.scrollY;
    page.textContent = "";
    const wrap = h("div", { class: "space-y-6" }, page);
    PAGES[r.name](wrap, r.arg);
    if (force === "nav") { wrap.classList.add("page-enter"); window.scrollTo(0, 0); page.focus({ preventScroll: true }); }
    else window.scrollTo(0, scroll);
    $("run-status").hidden = !isRunning();
  }
  window.addEventListener("hashchange", () => { $("nav-drawer").checked = false; render("nav"); });
  for (const d of document.querySelectorAll("dialog")) d.addEventListener("close", () => { if (S.dirty) render(true); });

  /* ---------------- loading data ---------------- */

  async function loadMe() {
    S.me = await api("/api/me");
    $("who").textContent = S.me.name ? `Signed in as ${S.me.name}` : "Signed in";
    $("signout-item").hidden = S.me.auth_mode !== "oidc";
    $("reset-item").hidden = !S.me.can_reset;
  }

  async function loadEvents() {
    S.events = await api("/api/events");
    const sel = $("event-select");
    sel.textContent = "";
    for (const ev of S.events) h("option", { value: ev.id }, sel, `${ev.name} · ${new Date(ev.date + "T12:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" })}`);
    const saved = store.get("fm-event");
    S.eventId = S.events.some((e) => e.id === saved) ? saved : (S.events[0] && S.events[0].id);
    sel.value = S.eventId;
  }

  async function reload() {
    if (!S.eventId) return;
    S.event = await api(`/api/events/${S.eventId}`);
    S.revision = S.event.revision;
    const active = S.event.scenarios.filter((s) => s.status === "active");
    const savedScenario = store.get(`fm-scenario-${S.eventId}`);
    if (!S.scenarioId || !active.some((s) => s.id === S.scenarioId)) {
      S.scenarioId = active.some((s) => s.id === savedScenario) ? savedScenario : official().id;
    }
    S.scenario = await api(`/api/scenarios/${S.scenarioId}`);
    const ssel = $("scenario-select");
    ssel.textContent = "";
    for (const sc of active) h("option", { value: sc.id }, ssel, sc.kind === "official" ? "Official plan" : `Scenario: ${sc.name.replace(/^What-if:\s*/, "")}`);
    ssel.value = S.scenarioId;
    synced();
    render(false);
  }
  function setScenario(id) {
    S.scenarioId = id;
    store.set(`fm-scenario-${S.eventId}`, id);
    return reload();
  }

  $("event-select").addEventListener("change", (e) => {
    S.eventId = e.target.value; S.scenarioId = null; S.gate = null;
    store.set("fm-event", S.eventId);
    reload();
  });
  $("scenario-select").addEventListener("change", (e) => setScenario(e.target.value));

  function synced() {
    S.lastSync = Date.now();
    const el = $("sync");
    el.querySelector(".status").className = "status status-success";
    el.lastElementChild.textContent = "Up to date";
  }
  async function poll() {
    if (!document.hidden && S.eventId) {
      try {
        const { revision } = await api(`/api/events/${S.eventId}/revision`);
        if (revision !== S.revision) await reload();
        else if (isRunning()) { S.scenario = await api(`/api/scenarios/${S.scenarioId}`); render(false); synced(); }
        else synced();
      } catch (e) {
        const el = $("sync");
        el.querySelector(".status").className = "status status-error";
        el.lastElementChild.textContent = "Reconnecting…";
      }
    }
    setTimeout(poll, 2000);
  }

  /* ---------------- pages ---------------- */

  function pageHome(root) {
    const ev = S.event, st = S.scenario, sel = st.selected_solution, fc = st.forecast;
    header(root, ev.name, `${longDate(ev.date)} · ${ev.venue}`, "home");

    if (!store.get("fm-tour-done")) {
      const c = h("div", { class: "card bg-primary text-primary-content" }, root);
      const b = h("div", { class: "card-body flex-col gap-3 sm:flex-row sm:items-center" }, c);
      const t = h("div", { class: "flex-1" }, b);
      h("h2", { class: "card-title" }, t, "Take the guided tour");
      h("p", { class: "mt-1" }, t, "Seven short steps explain the charts and the planning workflow.");
      const a = h("div", { class: "card-actions" }, b);
      link(a, "Start tour", "#/tour/1", "btn");
      btn(a, "Dismiss", "btn-ghost text-primary-content hover:bg-primary-content/10", () => { store.set("fm-tour-done", "1"); render(true); });
    }

    if (ev.pending.length) {
      const al = h("div", { role: "alert", class: "alert alert-info alert-soft alert-vertical sm:alert-horizontal" }, root);
      al.appendChild(icon("inbox"));
      h("span", {}, al, `${ev.pending.length} proposed change${ev.pending.length > 1 ? "s" : ""} from Claude await${ev.pending.length > 1 ? "" : "s"} your review.`);
      link(al, "Open inbox", "#/inbox", "btn btn-sm");
    }
    if (st.scenario.kind === "what_if") {
      const al = h("div", { role: "status", class: "alert alert-soft alert-vertical sm:alert-horizontal" }, root);
      h("span", {}, al, `You are viewing the scenario “${st.scenario.name}”. The official plan is unchanged.`);
      btn(al, "View official plan", "btn-sm", () => setScenario(official().id));
    }

    if (!sel || !fc) { waiting(root); return; }
    const stats = h("div", { class: "stats stats-vertical w-full bg-base-100 shadow-sm sm:stats-horizontal" }, root);
    const stat = (title, value, desc) => {
      const s = h("div", { class: "stat" }, stats);
      h("div", { class: "stat-title" }, s, title);
      h("div", { class: "stat-value text-3xl" }, s, value);
      h("div", { class: "stat-desc" }, s, desc);
    };
    const t = fc.totals.day;
    stat("Expected guests", fmt(t.p50), `80% range: ${fmt(t.p10)} to ${fmt(t.p90)}`);
    stat("Staff cost", eur(sel.staff_cost), `${sel.staff_hours} staff-hours`);
    stat("Average wait", `${fmt1(sel.expected_wait)} min`, `under ${fmt1(sel.wait_p90)} min on 9 days in 10`);

    h("h2", { class: "text-lg font-bold" }, root, "Next steps");
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-3" }, root);
    const tile = (href, ic, title, text) => {
      const a = h("a", { href, class: "card bg-base-100 shadow-sm transition hover:shadow-md" }, grid);
      const b = h("div", { class: "card-body" }, a);
      const ih = h("div", { class: "flex size-10 items-center justify-center rounded-full bg-secondary text-secondary-content" }, b);
      ih.appendChild(icon(ic));
      h("h3", { class: "card-title text-base" }, b, title);
      h("p", { class: "text-sm text-base-content/75" }, b, text);
    };
    tile("#/forecast", "chart", "View the forecast", "Expected arrivals per entrance, with their uncertainty.");
    tile("#/plans", "plans", "Compare staffing plans", "Trade staffing cost against guest waiting time.");
    tile("#/whatifs", "whatif", "Test a scenario", "Model an entrance closure or a nearby event without changing the official plan.");
  }

  function gateSentence(series) {
    const peak = series.points.reduce((a, b) => (b.p50 > a.p50 ? b : a));
    const share = series.points.reduce((s, p) => s + p.share_missing_history, 0) / series.points.length;
    const busiest = `Peak hour: ${peak.hour}, with about ${fmt(peak.p50)} arrivals (80% range: ${fmt(peak.p10)} to ${fmt(peak.p90)}).`;
    const why = share >= 0.35 ? "A large share of the uncertainty comes from limited history." : "Most of the uncertainty is day-to-day variation.";
    return `${busiest} Expected total for the day: about ${fmt(series.day_total.p50)}. ${why}`;
  }

  function pageForecast(root) {
    header(root, "Arrival forecast", "Expected arrivals per entrance and hour.", "forecast");
    const fc = S.scenario.forecast;
    if (!fc) { waiting(root); return; }
    if (!S.gate || (S.gate !== "all" && !fc.series.some((s) => s.gate_id === S.gate))) S.gate = fc.series[0].gate_id;

    const tabs = h("div", { role: "tablist", class: "tabs tabs-box w-fit flex-wrap" }, root);
    for (const s of fc.series) {
      const t = h("button", { role: "tab", type: "button", class: `tab ${S.gate === s.gate_id ? "tab-active" : ""}`.trim(), "aria-selected": String(S.gate === s.gate_id) }, tabs, s.gate_name.replace(/\s*\(.*\)$/, ""));
      t.addEventListener("click", () => { S.gate = s.gate_id; render(true); });
    }
    const all = h("button", { role: "tab", type: "button", class: `tab ${S.gate === "all" ? "tab-active" : ""}`.trim(), "aria-selected": String(S.gate === "all") }, tabs, "All entrances");
    all.addEventListener("click", () => { S.gate = "all"; render(true); });

    const body = card(root);
    if (S.gate === "all") {
      h("p", { class: "text-base-content/80" }, body, `Expected total: about ${fmt(fc.totals.day.p50)} guests (80% range: ${fmt(fc.totals.day.p10)} to ${fmt(fc.totals.day.p90)}). All panels share one scale for comparison.`);
      const box = h("div", {}, body);
      FC.renderForecast(box, fc, { title: false, table: false, notes: false, legend: true });
    } else {
      const series = fc.series.find((s) => s.gate_id === S.gate);
      const titleRow = h("div", { class: "flex flex-wrap items-center justify-between gap-2" }, body);
      h("h2", { class: "card-title" }, titleRow, series.gate_name);
      btn(titleRow, "Explain this forecast", "btn-sm btn-primary", () => explainForecast(fc, series), "info");
      h("p", { class: "text-base-content/85" }, body, gateSentence(series));
      if (series.history && series.history.thin) {
        const al = h("div", { role: "alert", class: "alert alert-warning alert-soft" }, body);
        h("span", {}, al, `Limited history: this entrance has only ${series.history.days} days of data. Information the data does not capture, such as parking, shuttles or signage, can be added through Claude or as a change in a scenario.`);
      }
      const chart = h("div", {}, body);
      const toggleRow = h("label", { class: "flex w-fit cursor-pointer items-center gap-3 text-sm" }, body);
      const tg = h("input", { type: "checkbox", class: "toggle toggle-sm toggle-secondary shrink-0" }, toggleRow);
      tg.checked = S.showWhy;
      h("span", {}, toggleRow, "Show sources of uncertainty by hour");
      tg.addEventListener("change", () => { S.showWhy = tg.checked; render(true); });
      FC.renderGate(chart, fc, S.gate, { strip: S.showWhy, legend: S.showWhy });
      if (S.showWhy) {
        const more = h("p", { class: "text-sm text-base-content/75" }, body);
        more.appendChild(document.createTextNode("Orange indicates limited history for that hour; blue indicates day-to-day variation. "));
        const a = h("button", { type: "button", class: "link" }, more, "Learn more");
        a.addEventListener("click", () => explain("uncertainty"));
      }
    }

    const det = h("details", { class: "collapse collapse-arrow bg-base-100 shadow-sm" }, root);
    h("summary", { class: "collapse-title font-semibold" }, det, "Forecast assumptions");
    const dc = h("div", { class: "collapse-content text-sm space-y-1" }, det);
    for (const a of fc.assumptions || []) h("p", {}, dc, a.text);
    if (fc.backtest) h("p", { class: "text-base-content/70" }, dc, `Backtest: on days held out from training, ${pct(fc.backtest.coverage_p10_p90)} of actual arrivals fell within the 80% range (target: 80%).`);
    const numbers = h("details", { class: "collapse collapse-arrow bg-base-100 shadow-sm" }, root);
    h("summary", { class: "collapse-title font-semibold" }, numbers, "Data table");
    const nc = h("div", { class: "collapse-content overflow-x-auto" }, numbers);
    const table = h("table", { class: "table table-sm" }, nc);
    const hr = h("tr", {}, h("thead", {}, table));
    for (const c of ["Entrance", "Hour", "Median", "80% range", "Share from limited history"]) h("th", {}, hr, c);
    const tb = h("tbody", {}, table);
    for (const s of fc.series) {
      if (S.gate !== "all" && s.gate_id !== S.gate) continue;
      for (const p of s.points) {
        const tr = h("tr", {}, tb);
        for (const v of [s.gate_name.replace(/\s*\(.*\)$/, ""), p.hour, fmt(p.p50), `${fmt(p.p10)}–${fmt(p.p90)}`, pct(p.share_missing_history)]) h("td", {}, tr, v);
      }
    }
  }

  function pagePlans(root) {
    header(root, "Staffing plans", "Each plan balances staffing cost against guest waiting time.", "plans");
    const st = S.scenario, result = st.result, sel = st.selected_solution, sc = st.scenario;
    if (!result || !sel) { waiting(root); return; }
    const editable = st.editable && !isRunning() && !st.result_is_stale;

    const top = card(root);
    h("h2", { class: "card-title" }, top, sc.kind === "official" ? "Official plan" : `Selected plan in “${sc.name}”`);
    const labelText = { cheapest: "the lowest-cost plan", balanced: "the balanced plan", shortest_wait: "the shortest-wait plan" }[sel.label] || "a plan you selected";
    h("p", { class: "text-base-content/85" }, top,
      `This is ${labelText}: ${sel.staff_hours} staff-hours (${eur(sel.staff_cost)}). The average wait is about ${fmt1(sel.expected_wait)} minutes per guest and stays below ${fmt1(sel.wait_p90)} minutes on 9 days out of 10. Demand peaks around ${sel.peak_hour}.`);
    if (st.comparison) {
      const c = st.comparison;
      const al = h("div", { role: "status", class: "alert alert-soft" }, top);
      const more = c.delta_staff_hours >= 0 ? `${c.delta_staff_hours} more` : `${-c.delta_staff_hours} fewer`;
      const wait = c.delta_expected_wait <= 0 ? `${fmt1(-c.delta_expected_wait)} minutes shorter` : `${fmt1(c.delta_expected_wait)} minutes longer`;
      h("span", {}, al, `Compared with the official plan: ${more} staff-hours (${c.delta_staff_cost >= 0 ? "+" : "−"}${eur(Math.abs(c.delta_staff_cost))}); average wait ${wait}.`);
    }
    if (result.service_cap_met === false) {
      const al = h("div", { role: "alert", class: "alert alert-warning alert-soft" }, top);
      h("span", {}, al, `Under these changes, no plan keeps the average wait below ${result.max_expected_wait} minutes. The plans shown are the best available.`);
    }
    const actions = h("div", { class: "card-actions" }, top);
    btn(actions, "Explain this plan", "btn-sm btn-primary", () => explainPlan(st), "info");
    if (sc.kind === "what_if") {
      const b = btn(actions, "Adopt as official plan", "btn-primary", () => confirmBox(
        "Adopt this plan?",
        `“${sc.name}” (${sel.staff_hours} staff-hours, ${eur(sel.staff_cost)}, average wait about ${fmt1(sel.expected_wait)} min) will replace the current official plan. The previous plan remains in the activity log.`,
        "Adopt plan",
        () => act(() => post(`/api/scenarios/${sc.id}/promote`, { solution_id: sel.id }), "Official plan updated.")));
      b.disabled = !editable;
    } else {
      h("p", { class: "text-sm text-base-content/70" }, actions, "To change the official plan, test the changes in a scenario first.");
      link(actions, "Open scenarios", "#/whatifs", "btn btn-sm");
    }

    const chartCard = card(root);
    h("h2", { class: "card-title" }, chartCard, "All candidate plans");
    h("p", { class: "text-sm text-base-content/75" }, chartCard, editable
      ? "Each point is a plan. Plans further right cost more; lower plans have shorter queues. Select a point to choose that plan."
      : "Each point is a plan. Plans further right cost more; lower plans have shorter queues.");
    const chartBox = h("div", {}, chartCard);
    FC.renderTradeoff(chartBox, result, {
      title: false,
      selectedId: sc.selected_solution_id,
      official: st.comparison ? st.comparison.official : null,
      editable,
      onSelect: (id) => act(() => post(`/api/scenarios/${sc.id}/select`, { solution_id: id }), "Plan selected."),
    });

    const det = h("details", { class: "collapse collapse-arrow bg-base-100 shadow-sm" }, root);
    h("summary", { class: "collapse-title font-semibold" }, det, "Staffing schedule");
    const dc = h("div", { class: "collapse-content" }, det);
    h("p", { class: "text-sm text-base-content/75 mb-2" }, dc, "Open lanes per entrance and hour. Darker cells indicate a higher share of the entrance's lanes in use; grey indicates a closed entrance.");
    const sched = h("div", {}, dc);
    FC.renderSchedule(sched, sel, result, S.event.gates, { title: false });
  }

  const TYPES = {
    gate_closed: { label: "Entrance closure", hint: "Roadworks, maintenance, security", fields: ["gate_id", "start", "end"] },
    gate_capacity_limit: { label: "Reduced lane capacity", hint: "Scanner failure, narrowed access", fields: ["gate_id", "max_lanes", "start", "end"] },
    staff_limit: { label: "Staff limit", hint: "Maximum number of staff on duty at once", fields: ["max_staff", "start", "end"] },
    competing_event: { label: "Competing event", hint: "A concert or match nearby", fields: ["name", "impact", "start", "end"] },
    schedule_shift: { label: "Show time change", hint: "A new start time for the evening show", fields: ["show_start"] },
    note_only: { label: "Note only", hint: "Record information without changing the plan", fields: ["summary"] },
  };
  const FIELD_LABELS = { gate_id: "Entrance", start: "From", end: "Until", max_lanes: "Maximum open lanes", max_staff: "Maximum staff on duty",
                         name: "Event name", impact: "Expected impact", show_start: "New start time", summary: "Note" };

  function openChangeWizard(scenarioId) {
    const box = $("change-box");
    const ev = S.event;
    const hours = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => `${String(from + i).padStart(2, "0")}:00`);
    function stepOne() {
      box.textContent = "";
      h("h3", { class: "text-lg font-bold" }, box, "Add a change");
      h("p", { class: "text-sm text-base-content/75 mb-3" }, box, "Select the closest type. You can add a note in the next step.");
      const grid = h("div", { class: "grid gap-2 sm:grid-cols-2" }, box);
      for (const [key, t] of Object.entries(TYPES)) {
        const b = h("button", { type: "button", class: "btn h-auto flex-col items-start gap-0 py-3 text-left" }, grid);
        h("span", { class: "font-bold" }, b, t.label);
        h("span", { class: "text-xs font-normal text-base-content/70" }, b, t.hint);
        b.addEventListener("click", () => stepTwo(key));
      }
      const act_ = h("div", { class: "modal-action" }, box);
      btn(act_, "Cancel", "btn-ghost", () => $("change").close());
    }
    function stepTwo(type) {
      box.textContent = "";
      h("h3", { class: "text-lg font-bold" }, box, TYPES[type].label);
      const inputs = {};
      for (const f of TYPES[type].fields) {
        const fs = h("fieldset", { class: "fieldset" }, box);
        h("legend", { class: "fieldset-legend" }, fs, FIELD_LABELS[f]);
        let input;
        if (f === "gate_id") { input = h("select", { class: "select w-full bg-base-200" }, fs); for (const g of ev.gates) h("option", { value: g.id }, input, g.name); }
        else if (f === "start") { input = h("select", { class: "select w-full bg-base-200" }, fs); for (const o of hours(ev.open_hour, ev.close_hour - 1)) h("option", { value: o }, input, o); input.value = "17:00"; }
        else if (f === "end") { input = h("select", { class: "select w-full bg-base-200" }, fs); for (const o of hours(ev.open_hour + 1, ev.close_hour)) h("option", { value: o }, input, o); input.value = "20:00"; }
        else if (f === "show_start") { input = h("select", { class: "select w-full bg-base-200" }, fs); for (const o of hours(ev.open_hour, ev.close_hour - 1)) h("option", { value: o }, input, o); input.value = "20:00"; }
        else if (f === "impact") { input = h("select", { class: "select w-full bg-base-200" }, fs); h("option", { value: "minor" }, input, "Minor (a few hundred people)"); h("option", { value: "major" }, input, "Major (thousands of people)"); }
        else if (f === "max_lanes" || f === "max_staff") { input = h("input", { type: "number", min: 1, class: "input w-full bg-base-200", value: f === "max_lanes" ? 2 : 12 }, fs); }
        else input = h("input", { type: "text", class: "input w-full bg-base-200", maxlength: 200 }, fs);
        inputs[f] = input;
      }
      const fs = h("fieldset", { class: "fieldset" }, box);
      h("legend", { class: "fieldset-legend" }, fs, "Note (optional)");
      const note = h("textarea", { class: "textarea w-full bg-base-200", rows: 2, maxlength: 2000, placeholder: "e.g. Roadworks on the north road from 5 pm" }, fs);
      h("p", { class: "label" }, fs, "Stored with the change as its rationale.");
      const err = h("div", {}, box);
      const actions = h("div", { class: "modal-action" }, box);
      btn(actions, "Back", "btn-ghost", stepOne);
      const add = btn(actions, "Add change", "btn-primary", async () => {
        const constraint = { type };
        for (const [k, el] of Object.entries(inputs)) constraint[k] = el.type === "number" ? Number(el.value) : el.value;
        add.disabled = true;
        try {
          await post(`/api/scenarios/${scenarioId}/constraints`, { constraint, note_text: note.value });
          $("change").close();
          toast("Change added. Plans are being recalculated.");
          await reload();
        } catch (e) {
          add.disabled = false;
          err.textContent = "";
          const al = h("div", { role: "alert", class: "alert alert-error alert-soft mt-2" }, err);
          h("span", {}, al, e.message);
        }
      });
    }
    stepOne();
    $("change").showModal();
  }

  function pageWhatIfs(root) {
    header(root, "Scenarios", "Test changes without affecting the official plan. The official plan changes only when you adopt a scenario.", "whatifs");
    const ev = S.event, st = S.scenario;

    const create = card(root);
    h("h2", { class: "card-title" }, create, "New scenario");
    h("p", { class: "text-sm text-base-content/75" }, create, "A new scenario starts as a copy of the official plan.");
    const row = h("div", { class: "join w-full max-w-lg" }, create);
    const name = h("input", { type: "text", class: "input join-item w-full bg-base-200", placeholder: "e.g. Roadworks on the north road", maxlength: 200, "aria-label": "Scenario name" }, row);
    btn(row, "Create", "join-item", () => act(async () => {
      const r = await post("/api/scenarios", { event_id: S.eventId, name: name.value.trim() || "New scenario" });
      S.scenarioId = r.id; store.set(`fm-scenario-${S.eventId}`, r.id);
    }, "Scenario created. Add a change to continue."), "plus");

    if (st.scenario.kind === "what_if") {
      const body = card(root, "border-2 border-secondary");
      const head = h("div", { class: "flex flex-wrap items-center justify-between gap-2" }, body);
      h("h2", { class: "card-title" }, head, `Changes in “${st.scenario.name}”`);
      const hb = h("div", { class: "flex gap-2" }, head);
      const undo = btn(hb, "Undo last change", "btn-sm btn-ghost", () => act(() => post(`/api/scenarios/${st.scenario.id}/undo`), "Change undone."));
      undo.disabled = !st.can_undo;
      btn(hb, "Add change", "btn-sm btn-secondary", () => openChangeWizard(st.scenario.id), "plus");
      if (!st.constraints.length) h("p", { class: "text-base-content/70" }, body, "No changes yet. Add a change to recalculate the plans.");
      const list = h("ul", { class: "list" }, body);
      for (const c of st.constraints) {
        const li = h("li", { class: "list-row items-center" }, list);
        const txt = h("div", { class: "list-col-grow" }, li);
        h("div", { class: "font-semibold" }, txt, c.readback);
        if (c.note) h("div", { class: "text-sm italic text-base-content/70" }, txt, `“${c.note.text}”`);
        h("div", { class: "text-xs text-base-content/70" }, txt, c.proposed_via === "chat" ? "Proposed in Claude, confirmed by you" : "Added on the website");
        btn(li, "Remove", "btn-sm btn-ghost", () => act(() => api(`/api/constraints/${c.id}`, { method: "DELETE" }), "Removed. Recalculating."));
      }
      const foot = h("div", { class: "card-actions" }, body);
      link(foot, "View plans for this scenario", "#/plans", "btn btn-sm");
    }

    h("h2", { class: "text-lg font-bold" }, root, "Plans and scenarios");
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    for (const sc of ev.scenarios.filter((s) => s.status === "active")) {
      const c = h("div", { class: `card bg-base-100 shadow-sm ${sc.id === S.scenarioId ? "ring-2 ring-secondary" : ""}`.trim() }, grid);
      const b = h("div", { class: "card-body gap-2" }, c);
      const t = h("div", { class: "flex items-center gap-2" }, b);
      h("h3", { class: "card-title text-base" }, t, sc.kind === "official" ? "Official plan" : sc.name);
      h("span", { class: `badge badge-sm ${sc.kind === "official" ? "badge-primary" : "badge-secondary"}` }, t, sc.kind === "official" ? "Official" : "Scenario");
      h("p", { class: "text-xs text-base-content/70" }, b, sc.created_via === "system" ? "Created automatically" : `Created ${sc.created_via === "dashboard" ? "on the website" : "in Claude"}`);
      const a = h("div", { class: "card-actions justify-end" }, b);
      if (sc.id !== S.scenarioId) btn(a, "View", "btn-sm", () => setScenario(sc.id));
      else h("span", { class: "badge badge-ghost" }, a, "Currently viewing");
      if (sc.kind === "what_if") btn(a, "Discard", "btn-sm btn-ghost", () => confirmBox(
        "Discard this scenario?", `“${sc.name}” will be removed from the list. Its record remains in the activity log.`, "Discard",
        () => act(async () => { await post(`/api/scenarios/${sc.id}/discard`); if (S.scenarioId === sc.id) S.scenarioId = null; }, "Scenario discarded.")));
    }
  }

  function pageInbox(root) {
    header(root, "Inbox", "Changes proposed in Claude. Review each interpretation, then confirm or reject it.", "inbox");
    const pending = S.event.pending;
    if (!pending.length) {
      const b = card(root);
      const row = h("div", { class: "flex items-center gap-4" }, b);
      const ic = h("div", { class: "flex size-12 shrink-0 items-center justify-center rounded-full bg-success/15" }, row);
      ic.appendChild(icon("check", "size-6"));
      const t = h("div", {}, row);
      h("h2", { class: "font-bold" }, t, "No pending changes");
      h("p", { class: "text-sm text-base-content/75" }, t, "Changes you describe to Claude, such as roadworks or a schedule change, appear here for review.");
      return;
    }
    for (const c of pending) {
      const b = card(root);
      if (c.note) {
        h("div", { class: "text-xs font-semibold uppercase text-base-content/70" }, b, "Original note");
        h("blockquote", { class: "border-l-4 border-secondary pl-3 italic" }, b, `“${c.note.text}”`);
      }
      h("div", { class: "text-xs font-semibold uppercase text-base-content/70" }, b, "Interpretation");
      h("p", { class: "font-semibold" }, b, c.readback);
      if (c.interpretation && c.interpretation !== c.readback) h("p", { class: "text-sm text-base-content/75" }, b, c.interpretation);
      if (c.assumptions.length) {
        const d = h("details", { class: "collapse collapse-arrow bg-base-200" }, b);
        h("summary", { class: "collapse-title text-sm font-semibold" }, d, `Assumptions (${c.assumptions.length})`);
        const ul = h("ul", { class: "collapse-content list-disc pl-8 text-sm" }, d);
        for (const a of c.assumptions) h("li", {}, ul, a);
      }
      const a = h("div", { class: "card-actions items-center justify-end" }, b);
      const reason = h("input", { type: "text", class: "input input-sm w-full max-w-xs bg-base-200", placeholder: "Reason (optional)", maxlength: 1000, "aria-label": "Reason for rejecting" }, a);
      btn(a, "Reject", "btn-sm btn-ghost", () => act(() => post(`/api/constraints/${c.id}/reject`, { reason: reason.value }), "Change rejected."));
      btn(a, "Confirm", "btn-sm btn-success", () => act(async () => {
        const r = await post(`/api/constraints/${c.id}/confirm`);
        S.scenarioId = r.scenario_id; store.set(`fm-scenario-${S.eventId}`, r.scenario_id);
      }, "Change confirmed and added to a scenario. Plans are being recalculated."), "check");
    }
  }

  const TYPE_WORDS = { gate_closed: "an entrance closure", gate_capacity_limit: "reduced lane capacity", staff_limit: "a staff limit",
                       competing_event: "a competing event", schedule_shift: "a show time change", note_only: "a note" };
  function historySentence(r) {
    const d = r.details || {};
    switch (r.action) {
      case "propose_constraint": return `Claude proposed ${TYPE_WORDS[d.type] || "a change"}`;
      case "confirm_constraint": return "Confirmed a change proposed in Claude";
      case "reject_constraint": return `Rejected a change proposed in Claude${d.reason ? `: “${d.reason}”` : ""}`;
      case "create_what_if": return `Created the scenario “${d.name || ""}”`;
      case "add_constraint": return `Added a change: ${TYPE_WORDS[d.type] || "a change"}`;
      case "remove_constraint": return "Removed a change";
      case "select_plan": return `Selected plan ${d.solution_id}`;
      case "undo": return "Undid the last change";
      case "discard_scenario": return "Discarded a scenario";
      case "promote_scenario": return `Adopted a scenario as the official plan (${d.staff_hours} staff-hours, ${eur(d.staff_cost || 0)})`;
      default: return r.action.replaceAll("_", " ");
    }
  }
  const WHERE = { dashboard: "on the website", chat: "in Claude", chat_panel: "in the Claude panel", system: "automatically" };

  function pageHistory(root) {
    header(root, "Activity log", "All changes, with author, time and channel.", "history");
    const box = h("div", {}, root);
    waiting(box, "Loading the activity log…");
    api(`/api/events/${S.eventId}/audit`).then((rows) => {
      box.textContent = "";
      if (!rows.length) { const b = card(box); h("p", {}, b, "No activity yet."); return; }
      const ul = h("ul", { class: "list rounded-box bg-base-100 shadow-sm" }, box);
      for (const r of rows.slice(0, 60)) {
        const li = h("li", { class: "list-row items-center" }, ul);
        h("span", { class: `status status-md ${r.channel === "dashboard" ? "status-primary" : r.channel === "system" ? "status-neutral" : "status-secondary"}`,
                    "aria-hidden": "true" }, li);
        const txt = h("div", { class: "list-col-grow" }, li);
        h("div", { class: "font-semibold" }, txt, historySentence(r));
        const who = r.actor === (S.me && S.me.name) ? "You" : r.actor;
        h("div", { class: "text-xs text-base-content/70" }, txt, `${who} · ${WHERE[r.channel] || r.channel} · ${new Date(r.at).toLocaleString()}`);
      }
    }).catch((e) => { box.textContent = ""; toast(e.message, "error"); });
  }

  /* ---------------- learn & tour ---------------- */

  const TOUR = [
    { title: "The demo venue", text: [
      "Parkland Theme Park is a fictional venue for demonstrating the tool. Guests enter through three entrances, each with several lanes.",
      "The event selector at the top of the page switches between events." ], visual: tourPark },
    { title: "Reading a forecast", text: [
      "The line shows the median number of guests arriving each hour at one entrance.",
      "The shaded band is the 80% range: on about 8 days out of 10, actual arrivals fall inside it." ], visual: (b) => tourGate(b, false) },
    { title: "Sources of uncertainty", text: [
      "The forecast separates two sources of uncertainty, shown in two colors below the chart.",
      "Blue is day-to-day variation, which cannot be removed and is covered by a small staffing buffer. Orange is limited history; here it arises because the entrance is new. Local knowledge can reduce it." ], visual: (b) => tourGate(b, true) },
    { title: "Choosing a staffing plan", text: [
      "The optimizer evaluates thousands of staffing schedules and keeps the best trade-offs. Each point is one plan.",
      "Plans further right cost more; lower plans have shorter queues. The tool marks a balanced plan, and you make the final choice." ], visual: tourPlans },
    { title: "Testing a scenario", text: [
      "For example, roadworks close the North Gate road from 5 to 8 pm. Create a scenario, add that change, and the plans are recalculated within seconds.",
      "The official plan stays unchanged during testing." ], visual: tourWhatIf },
    { title: "Adopting a plan", text: [
      "When a scenario is ready, open its plans and adopt it as the official plan. Changes proposed in Claude remain in the Inbox until you confirm them.",
      "Every step is recorded in the activity log, including who made each change and why." ], visual: tourOfficial },
    { title: "Claude integration", text: [
      "The same functions are available in Claude. Add the tool as a connector, then ask in plain language.",
      "Claude displays the same charts in the conversation, and changes confirmed there appear here as well." ], visual: tourClaude },
  ];

  function tourPark(b) {
    const grid = h("div", { class: "grid gap-3 sm:grid-cols-3" }, b);
    const fc = S.scenario.forecast;
    const colors = ["bg-primary/12", "bg-secondary/12", "bg-accent/18"];
    S.event.gates.forEach((g, i) => {
      const c = h("div", { class: `card ${colors[i % 3]}` }, grid);
      const cb = h("div", { class: "card-body p-4 gap-1" }, c);
      h("div", { class: "font-bold" }, cb, g.name);
      h("div", { class: "text-sm" }, cb, `${g.lanes} lanes`);
      const s = fc && fc.series.find((x) => x.gate_id === g.id);
      if (s) h("div", { class: "text-xs text-base-content/70" }, cb, `${s.history.days} days of history`);
    });
  }
  function tourGate(b, thin) {
    const fc = S.scenario.forecast;
    if (!fc) { waiting(b); return; }
    const pick = thin ? (thinGates(fc)[0] || fc.series[fc.series.length - 1]) : fc.series[0];
    h("div", { class: "font-semibold" }, b, pick.gate_name);
    const box = h("div", {}, b);
    FC.renderGate(box, fc, pick.gate_id, { strip: thin, legend: thin, height: 200 });
  }
  function tourPlans(b) {
    const st = S.scenario;
    if (!st.result) { waiting(b); return; }
    const box = h("div", {}, b);
    FC.renderTradeoff(box, st.result, { title: false, selectedId: st.scenario.selected_solution_id, editable: false });
  }
  function flow(b, items) {
    const ul = h("ul", { class: "steps steps-vertical" }, b);
    for (const [t, d] of items) {
      const li = h("li", { class: "step step-secondary" }, ul);
      const div = h("div", { class: "py-2 text-left" }, li);
      h("div", { class: "font-semibold" }, div, t);
      h("div", { class: "text-sm text-base-content/75" }, div, d);
    }
  }
  function tourWhatIf(b) {
    flow(b, [["Create a scenario", "A copy of the official plan."], ["Add the change", "“North Gate closed 17:00–20:00”."], ["Review the new plans", "Recalculated within seconds."]]);
  }
  function tourOfficial(b) {
    flow(b, [["Review the plans", "Select the trade-off that fits."], ["Adopt the plan", "A second confirmation is required."], ["Activity log", "Records the author, time and source note."]]);
  }
  function tourClaude(b) {
    claudeBox(b);
  }
  function claudeBox(parent) {
    const b = h("div", { class: "space-y-4" }, parent);
    const ol = h("ol", { class: "list-decimal space-y-3 pl-5" }, b);
    h("li", {}, ol, "In Claude, open Settings > Connectors and add a custom connector.");
    const li = h("li", {}, ol);
    li.appendChild(document.createTextNode("Enter this address:"));
    const row = h("div", { class: "join mt-2 w-full max-w-lg" }, li);
    const input = h("input", { class: "input join-item w-full font-mono text-sm", readonly: "", "aria-label": "Connector address", value: S.me.mcp_url }, row);
    const copy = btn(row, "Copy", "join-item", async () => {
      try { await navigator.clipboard.writeText(S.me.mcp_url); copy.textContent = "Copied"; } catch (e) { input.select(); }
      setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    });
    const last = h("li", {}, ol, "Sign in when prompted, then try a request such as:");
    const ex = h("div", { class: "mt-2 flex flex-wrap gap-2" }, last);
    for (const q of ["Show me the forecast for Halloween Night", "Roadworks close the north road from 5 to 8 pm", "What does the balanced plan cost?"]) h("span", { class: "badge badge-soft badge-secondary h-auto py-1" }, ex, q);
    h("p", { class: "text-sm text-base-content/70" }, b, "Custom connectors require a paid Claude plan.");
  }

  function pageTour(root, arg) {
    const n = Math.max(1, Math.min(TOUR.length, Number(arg) || 1));
    const step = TOUR[n - 1];
    const steps = h("ul", { class: "steps w-full text-xs *:min-w-0" }, root);
    TOUR.forEach((t, i) => {
      const li = h("li", { class: `step ${i < n ? "step-primary" : ""}`.trim(), "data-content": String(i + 1) }, steps);
      li.setAttribute("aria-label", t.title);
    });
    const b = card(root);
    h("div", { class: "text-sm text-base-content/70" }, b, `Step ${n} of ${TOUR.length}`);
    h("h1", { class: "text-2xl font-bold" }, b, step.title);
    for (const p of step.text) h("p", { class: "text-base-content/85" }, b, p);
    const vis = h("div", { class: "mt-2" }, b);
    step.visual(vis);
    const nav = h("div", { class: "card-actions items-center justify-between border-t border-base-300 pt-4" }, b);
    if (n > 1) link(nav, "Back", `#/tour/${n - 1}`, "btn btn-ghost"); else h("span", {}, nav);
    const right = h("div", { class: "flex gap-2" }, nav);
    if (n < TOUR.length) {
      btn(right, "Skip tour", "btn-ghost", () => { store.set("fm-tour-done", "1"); location.hash = "#/home"; });
      link(right, "Next", `#/tour/${n + 1}`, "btn btn-primary");
    } else {
      btn(right, "Finish", "btn-primary", () => { store.set("fm-tour-done", "1"); location.hash = "#/home"; });
    }
  }

  function pageLearn(root) {
    header(root, "Guide", "Short explanations of each part of the tool. Read them in order or individually.");
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    TOUR.forEach((t, i) => {
      const a = h("a", { href: `#/tour/${i + 1}`, class: "card bg-base-100 shadow-sm transition hover:shadow-md" }, grid);
      const b = h("div", { class: "card-body flex-row items-center gap-4" }, a);
      h("div", { class: "flex size-10 shrink-0 items-center justify-center rounded-full bg-accent font-bold text-accent-content" }, b, String(i + 1));
      const t_ = h("div", {}, b);
      h("h2", { class: "font-bold" }, t_, t.title);
      h("p", { class: "mt-1 text-sm text-base-content/70" }, t_, t.text[0]);
    });
    const more = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    for (const [href, title, text] of [["#/about", "About this prototype", "Data sources, methods and the research question."],
                                        ["/references", "References", "The research and tools the methods are based on."],
                                        ["/privacy", "Privacy", "Data stored about you and how to delete it."]]) {
      const a = h("a", { href, class: "card card-border bg-base-100" }, more);
      const b = h("div", { class: "card-body" }, a);
      h("h2", { class: "font-bold" }, b, title);
      h("p", { class: "text-sm text-base-content/70" }, b, text);
    }
  }

  function pageClaude(root) {
    header(root, "Claude integration", "Ask questions in plain language. Claude displays the same charts in the conversation.", "claude");
    const b = card(root);
    claudeBox(b);
    const how = card(root);
    h("h2", { class: "card-title" }, how, "How proposed changes are handled");
    flow(how, [["You describe a change", "For example: “a large concert in town from 7 pm”."], ["Claude proposes a constraint", "With its interpretation and any assumptions."],
               ["You confirm it", "In the conversation or in the Inbox. Nothing changes before confirmation."], ["Plans are recalculated", "In a scenario; the official plan is not affected."]]);
  }

  function pageAbout(root) {
    header(root, "About this prototype", "Data, methods and research question.");
    const fc = S.scenario.forecast;
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    const days = fc ? Math.max(...fc.series.map((s) => s.history.days)) : 120;
    const cov = fc && fc.backtest ? `${pct(fc.backtest.coverage_p10_p90)} of actual arrivals fell within the 80% range (target: 80%)` : "the 80% range is checked against actual arrivals";
    for (const [title, text, cls] of [
      ["Synthetic data", `Parkland Theme Park, its entrances and ${days} days of visitor history are fictional. The data reproduces realistic arrival patterns: morning peaks, an evening rush before the show, lower attendance on rainy days and one newly opened entrance.`, "bg-secondary/12"],
      ["Computed results", "No figures are entered by hand. All forecasts and staffing plans are computed from this history by a forecasting model and an optimization algorithm.", "bg-primary/12"],
      ["Validated forecast", `On days held out from training, ${cov}.`, "bg-accent/18"],
      ["Simplified methods", "The forecasting model and the optimization algorithm are intentionally simple, designed to test the research questions. A production deployment would use the venue's own data and stronger models.", "bg-info/12"],
    ]) {
      const c = h("div", { class: `card ${cls}` }, grid);
      const b = h("div", { class: "card-body" }, c);
      h("h2", { class: "card-title text-base" }, b, title);
      h("p", { class: "text-sm" }, b, text);
    }
    const t = card(root);
    h("h2", { class: "card-title" }, t, "Research question");
    h("p", {}, t, "Whether explaining the sources of forecast uncertainty, and converting planners' knowledge into constraints for the optimizer, helps people make better staffing decisions with less effort.");
    h("p", { class: "text-sm text-base-content/70" }, t, "Your workspace is private. You can reset it from the account menu at any time.");
    link(h("div", { class: "card-actions" }, t), "References", "/references", "btn btn-sm");
  }

  /* ---------------- account actions ---------------- */

  $("help-btn").addEventListener("click", () => {
    const name = route().name;
    explain({ forecast: "forecast", plans: "plans", whatifs: "whatifs", inbox: "inbox", history: "history", claude: "claude" }[name] || "home");
  });
  $("reset-btn").addEventListener("click", () => confirmBox(
    "Reset workspace?",
    "This deletes all workspace data, including scenarios, notes and the activity log, and restores the demo park.",
    "Reset",
    () => act(async () => { await post("/api/workspace/reset-demo"); S.scenarioId = null; await loadEvents(); }, "Workspace reset. Initial plans take a few seconds to compute.")));

  (async function start() {
    try {
      await loadMe();
      await loadEvents();
      if (!location.hash) location.hash = store.get("fm-tour-done") ? "#/home" : "#/home";
      await reload();
      render("nav");
    } catch (e) {
      page.textContent = "";
      const al = h("div", { role: "alert", class: "alert alert-error" }, page);
      h("span", {}, al, e.message);
    }
    setTimeout(poll, 2000);
  })();
})();
