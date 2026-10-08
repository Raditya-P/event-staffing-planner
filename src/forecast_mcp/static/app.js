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
    if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
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
    home: { title: "Your overview", tour: 1, body: [
      "This page sums up the plan you are looking at: how many guests to expect, what the staffing costs, and how long guests wait.",
      "Use the menu to go deeper, one topic at a time." ] },
    forecast: { title: "Reading the forecast", tour: 2, body: [
      "The line is the most likely number of guests arriving at this entrance in each hour.",
      "The shaded band is the likely range: on about 8 days out of 10, the real number falls inside it. A wider band means less certainty." ] },
    uncertainty: { title: "Why the forecast is unsure", tour: 3, body: [
      "Blue is normal day-to-day ups and downs. Nobody can remove those, so the answer is a few extra staff.",
      "Orange is uncertainty because the tool has seen little history for this situation. That's where your knowledge helps: tell it what you know." ] },
    plans: { title: "Choosing a staffing plan", tour: 4, body: [
      "Each dot is one staffing plan. Further right means more staff and higher cost; lower means shorter queues.",
      "The thin line above a dot shows the wait on a bad day (worse than 9 days out of 10). The tool marks a balanced plan, but the choice is yours." ] },
    whatifs: { title: "What-ifs", tour: 5, body: [
      "A what-if is a safe copy of the official plan. Add changes such as a closed entrance or a staff shortage and the plans are recalculated in seconds.",
      "The official plan only changes when you make a what-if official." ] },
    inbox: { title: "Notes from Claude", tour: 6, body: [
      "When you tell Claude about roadworks, another event or a change of schedule, it writes down how it understood you.",
      "Those notes wait here. Nothing changes until you confirm one; then it goes into a what-if." ] },
    history: { title: "History", tour: 6, body: [
      "Every proposal, confirmation, change and decision is recorded here: what happened, who did it and whether it was on the website or in Claude." ] },
    claude: { title: "Using Claude", tour: 7, body: [
      "Add this tool to Claude as a connector, then ask in plain words. Claude shows the same charts inside the chat." ] },
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
    if (helpKey) btn(row, "What am I looking at?", "btn-sm btn-soft", () => explain(helpKey), "info");
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
    h("span", {}, row, text || "Calculating… this takes a few seconds.");
  }

  /* ---------------- navigation ---------------- */

  const NAV = [
    ["home", "Home", "home"], ["forecast", "Forecast", "chart"], ["plans", "Staffing plans", "plans"],
    ["whatifs", "What-ifs", "whatif"], ["inbox", "Inbox", "inbox"], ["history", "History", "history"],
    ["learn", "Learn", "learn"], ["claude", "Use in Claude", "claude"],
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
    for (const sc of active) h("option", { value: sc.id }, ssel, sc.kind === "official" ? "Official plan" : `What-if: ${sc.name.replace(/^What-if:\s*/, "")}`);
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
      h("h2", { class: "card-title" }, t, "New here? Take the 2-minute tour");
      h("p", {}, t, "Seven short steps show you how to read the charts and make a plan.");
      const a = h("div", { class: "card-actions" }, b);
      link(a, "Start the tour", "#/tour/1", "btn");
      btn(a, "Skip", "btn-ghost", () => { store.set("fm-tour-done", "1"); render(true); });
    }

    if (ev.pending.length) {
      const al = h("div", { role: "alert", class: "alert alert-info alert-soft alert-vertical sm:alert-horizontal" }, root);
      al.appendChild(icon("inbox"));
      h("span", {}, al, `Claude wrote down ${ev.pending.length} note${ev.pending.length > 1 ? "s" : ""} for you to check.`);
      link(al, "Open inbox", "#/inbox", "btn btn-sm");
    }
    if (st.scenario.kind === "what_if") {
      const al = h("div", { role: "status", class: "alert alert-soft alert-vertical sm:alert-horizontal" }, root);
      h("span", {}, al, `You are looking at a what-if: “${st.scenario.name}”. The official plan is unchanged.`);
      btn(al, "Show the official plan", "btn-sm", () => setScenario(official().id));
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
    stat("Guests expected", fmt(t.p50), `likely ${fmt(t.p10)} to ${fmt(t.p90)}`);
    stat("Staff cost", eur(sel.staff_cost), `${sel.staff_hours} staff-hours in total`);
    stat("Average wait", `${fmt1(sel.expected_wait)} min`, `under ${fmt1(sel.wait_p90)} min on 9 days in 10`);

    h("h2", { class: "text-lg font-bold" }, root, "What would you like to do?");
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-3" }, root);
    const tile = (href, ic, title, text) => {
      const a = h("a", { href, class: "card bg-base-100 shadow-sm transition hover:shadow-md" }, grid);
      const b = h("div", { class: "card-body" }, a);
      const ih = h("div", { class: "flex size-10 items-center justify-center rounded-full bg-secondary text-secondary-content" }, b);
      ih.appendChild(icon(ic));
      h("h3", { class: "card-title text-base" }, b, title);
      h("p", { class: "text-sm text-base-content/75" }, b, text);
    };
    tile("#/forecast", "chart", "See the forecast", "Who arrives at each entrance, and how sure the tool is.");
    tile("#/plans", "plans", "Compare staffing plans", "From cheapest to shortest queues. Pick your balance.");
    tile("#/whatifs", "whatif", "Try a what-if", "Close an entrance or add a concert in town, safely.");
  }

  function gateSentence(series) {
    const peak = series.points.reduce((a, b) => (b.p50 > a.p50 ? b : a));
    const share = series.points.reduce((s, p) => s + p.share_missing_history, 0) / series.points.length;
    const busiest = `Busiest hour: ${peak.hour}, with about ${fmt(peak.p50)} guests (likely ${fmt(peak.p10)} to ${fmt(peak.p90)}).`;
    const why = share >= 0.35 ? "A lot of the uncertainty here comes from limited history." : "Most of the uncertainty here is ordinary day-to-day variation.";
    return `${busiest} About ${fmt(series.day_total.p50)} guests over the whole day. ${why}`;
  }

  function pageForecast(root) {
    header(root, "Who arrives, and when?", "Guests expected at each entrance, hour by hour.", "forecast");
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
      h("p", { class: "text-base-content/80" }, body, `About ${fmt(fc.totals.day.p50)} guests in total (likely ${fmt(fc.totals.day.p10)} to ${fmt(fc.totals.day.p90)}). Each panel uses the same scale, so you can compare entrances.`);
      const box = h("div", {}, body);
      FC.renderForecast(box, fc, { title: false, table: false, notes: false, legend: true });
    } else {
      const series = fc.series.find((s) => s.gate_id === S.gate);
      h("h2", { class: "card-title" }, body, series.gate_name);
      h("p", { class: "text-base-content/85" }, body, gateSentence(series));
      if (series.history && series.history.thin) {
        const al = h("div", { role: "alert", class: "alert alert-warning alert-soft" }, body);
        h("span", {}, al, `Little history here: this entrance has only ${series.history.days} days of data. If you know something the data can't show (parking, shuttles, signs), tell Claude or add a note in a what-if.`);
      }
      const chart = h("div", {}, body);
      const toggleRow = h("label", { class: "flex w-fit cursor-pointer items-center gap-3 text-sm" }, body);
      const tg = h("input", { type: "checkbox", class: "toggle toggle-sm toggle-secondary shrink-0" }, toggleRow);
      tg.checked = S.showWhy;
      h("span", {}, toggleRow, "Show why it's uncertain, hour by hour");
      tg.addEventListener("change", () => { S.showWhy = tg.checked; render(true); });
      FC.renderGate(chart, fc, S.gate, { strip: S.showWhy, legend: S.showWhy });
      if (S.showWhy) {
        const more = h("p", { class: "text-sm text-base-content/75" }, body);
        more.appendChild(document.createTextNode("Orange means the tool has little history for that hour; blue is normal day-to-day variation. "));
        const a = h("button", { type: "button", class: "link" }, more, "Tell me more");
        a.addEventListener("click", () => explain("uncertainty"));
      }
    }

    const det = h("details", { class: "collapse collapse-arrow bg-base-100 shadow-sm" }, root);
    h("summary", { class: "collapse-title font-semibold" }, det, "What this forecast assumes");
    const dc = h("div", { class: "collapse-content text-sm space-y-1" }, det);
    for (const a of fc.assumptions || []) h("p", {}, dc, a.text);
    if (fc.backtest) h("p", { class: "text-base-content/70" }, dc, `Checked on past days: the likely ranges contained ${pct(fc.backtest.coverage_p10_p90)} of what really happened (aim: 80%).`);
    const numbers = h("details", { class: "collapse collapse-arrow bg-base-100 shadow-sm" }, root);
    h("summary", { class: "collapse-title font-semibold" }, numbers, "See the numbers");
    const nc = h("div", { class: "collapse-content overflow-x-auto" }, numbers);
    const table = h("table", { class: "table table-sm" }, nc);
    const hr = h("tr", {}, h("thead", {}, table));
    for (const c of ["Entrance", "Hour", "Most likely", "Likely range", "Limited history share"]) h("th", {}, hr, c);
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
    header(root, "How many staff, and at what cost?", "Each plan balances what staff cost against how long guests wait.", "plans");
    const st = S.scenario, result = st.result, sel = st.selected_solution, sc = st.scenario;
    if (!result || !sel) { waiting(root); return; }
    const editable = st.editable && !isRunning() && !st.result_is_stale;

    const top = card(root);
    h("h2", { class: "card-title" }, top, sc.kind === "official" ? "The official plan" : `Your plan in “${sc.name}”`);
    const labelText = { cheapest: "the cheapest plan", balanced: "the balanced plan", shortest_wait: "the plan with the shortest queues" }[sel.label] || "a plan you picked";
    h("p", { class: "text-base-content/85" }, top,
      `This is ${labelText}: ${sel.staff_hours} staff-hours (${eur(sel.staff_cost)}). Guests wait about ${fmt1(sel.expected_wait)} minutes on average, and less than ${fmt1(sel.wait_p90)} minutes on 9 days out of 10. The busiest moment is around ${sel.peak_hour}.`);
    if (st.comparison) {
      const c = st.comparison;
      const al = h("div", { role: "status", class: "alert alert-soft" }, top);
      const more = c.delta_staff_hours >= 0 ? `${c.delta_staff_hours} more` : `${-c.delta_staff_hours} fewer`;
      const wait = c.delta_expected_wait <= 0 ? `${fmt1(-c.delta_expected_wait)} minutes shorter` : `${fmt1(c.delta_expected_wait)} minutes longer`;
      h("span", {}, al, `Compared with the official plan: ${more} staff-hours (${c.delta_staff_cost >= 0 ? "+" : "−"}${eur(Math.abs(c.delta_staff_cost))}), and waits are ${wait}.`);
    }
    if (result.service_cap_met === false) {
      const al = h("div", { role: "alert", class: "alert alert-warning alert-soft" }, top);
      h("span", {}, al, `With these changes no plan keeps the average wait under ${result.max_expected_wait} minutes. These are the best possible plans.`);
    }
    const actions = h("div", { class: "card-actions" }, top);
    if (sc.kind === "what_if") {
      const b = btn(actions, "Make this the official plan", "btn-primary", () => confirmBox(
        "Make this the official plan?",
        `“${sc.name}” with ${sel.staff_hours} staff-hours (${eur(sel.staff_cost)}, about ${fmt1(sel.expected_wait)} min wait) replaces the current official plan. The old one stays in History.`,
        "Yes, make it official",
        () => act(() => post(`/api/scenarios/${sc.id}/promote`, { solution_id: sel.id }), "This is now the official plan.")));
      b.disabled = !editable;
    } else {
      h("p", { class: "text-sm text-base-content/70" }, actions, "To change the official plan, try your changes in a what-if first.");
      link(actions, "Go to what-ifs", "#/whatifs", "btn btn-sm");
    }

    const chartCard = card(root);
    h("h2", { class: "card-title" }, chartCard, "All the plans the tool found");
    h("p", { class: "text-sm text-base-content/75" }, chartCard, editable
      ? "Each dot is a plan. Further right costs more; lower means shorter queues. Click a dot to choose it."
      : "Each dot is a plan. Further right costs more; lower means shorter queues.");
    const chartBox = h("div", {}, chartCard);
    FC.renderTradeoff(chartBox, result, {
      title: false,
      selectedId: sc.selected_solution_id,
      official: st.comparison ? st.comparison.official : null,
      editable,
      onSelect: (id) => act(() => post(`/api/scenarios/${sc.id}/select`, { solution_id: id }), "Plan chosen."),
    });

    const det = h("details", { class: "collapse collapse-arrow bg-base-100 shadow-sm" }, root);
    h("summary", { class: "collapse-title font-semibold" }, det, "See the staffing schedule");
    const dc = h("div", { class: "collapse-content" }, det);
    h("p", { class: "text-sm text-base-content/75 mb-2" }, dc, "How many lanes are open at each entrance, hour by hour. Darker means more of that entrance's lanes are open; grey means closed.");
    const sched = h("div", {}, dc);
    FC.renderSchedule(sched, sel, result, S.event.gates, { title: false });
  }

  const TYPES = {
    gate_closed: { label: "An entrance is closed", hint: "Roadworks, maintenance, security", fields: ["gate_id", "start", "end"] },
    gate_capacity_limit: { label: "Fewer lanes at an entrance", hint: "Broken scanners, a narrowed path", fields: ["gate_id", "max_lanes", "start", "end"] },
    staff_limit: { label: "Not enough staff", hint: "A cap on how many can work at once", fields: ["max_staff", "start", "end"] },
    competing_event: { label: "Something else is on nearby", hint: "A concert or match drawing people away", fields: ["name", "impact", "start", "end"] },
    schedule_shift: { label: "The show moves", hint: "A new start time for the evening show", fields: ["show_start"] },
    note_only: { label: "Just a note", hint: "Remember something without changing the plan", fields: ["summary"] },
  };
  const FIELD_LABELS = { gate_id: "Which entrance?", start: "From", end: "Until", max_lanes: "Lanes that can open", max_staff: "Most staff working at once",
                         name: "What is it called?", impact: "How big?", show_start: "New start time", summary: "Your note" };

  function openChangeWizard(scenarioId) {
    const box = $("change-box");
    const ev = S.event;
    const hours = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => `${String(from + i).padStart(2, "0")}:00`);
    function stepOne() {
      box.textContent = "";
      h("h3", { class: "text-lg font-bold" }, box, "What changed?");
      h("p", { class: "text-sm text-base-content/75 mb-3" }, box, "Pick the closest match. You can add a note in your own words next.");
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
        else if (f === "impact") { input = h("select", { class: "select w-full bg-base-200" }, fs); h("option", { value: "minor" }, input, "Small (a few hundred people)"); h("option", { value: "major" }, input, "Big (thousands of people)"); }
        else if (f === "max_lanes" || f === "max_staff") { input = h("input", { type: "number", min: 1, class: "input w-full bg-base-200", value: f === "max_lanes" ? 2 : 12 }, fs); }
        else input = h("input", { type: "text", class: "input w-full bg-base-200", maxlength: 200 }, fs);
        inputs[f] = input;
      }
      const fs = h("fieldset", { class: "fieldset" }, box);
      h("legend", { class: "fieldset-legend" }, fs, "In your own words (optional)");
      const note = h("textarea", { class: "textarea w-full bg-base-200", rows: 2, maxlength: 2000, placeholder: "e.g. Roadworks on the north road from 5 pm" }, fs);
      h("p", { class: "label" }, fs, "Kept word for word with the change, so others can see why it was made.");
      const err = h("div", {}, box);
      const actions = h("div", { class: "modal-action" }, box);
      btn(actions, "Back", "btn-ghost", stepOne);
      const add = btn(actions, "Add this change", "btn-primary", async () => {
        const constraint = { type };
        for (const [k, el] of Object.entries(inputs)) constraint[k] = el.type === "number" ? Number(el.value) : el.value;
        add.disabled = true;
        try {
          await post(`/api/scenarios/${scenarioId}/constraints`, { constraint, note_text: note.value });
          $("change").close();
          toast("Change added. The plans are being recalculated.");
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
    header(root, "What if…?", "Try changes safely. The official plan only changes when you make a what-if official.", "whatifs");
    const ev = S.event, st = S.scenario;

    const create = card(root);
    h("h2", { class: "card-title" }, create, "Start a new what-if");
    h("p", { class: "text-sm text-base-content/75" }, create, "It starts as a copy of the official plan.");
    const row = h("div", { class: "join w-full max-w-lg" }, create);
    const name = h("input", { type: "text", class: "input join-item w-full bg-base-200", placeholder: "e.g. Roadworks on the north road", maxlength: 200, "aria-label": "Name of the what-if" }, row);
    btn(row, "Create", "join-item", () => act(async () => {
      const r = await post("/api/scenarios", { event_id: S.eventId, name: name.value.trim() || "My what-if" });
      S.scenarioId = r.id; store.set(`fm-scenario-${S.eventId}`, r.id);
    }, "What-if created. Now add a change."), "plus");

    if (st.scenario.kind === "what_if") {
      const body = card(root, "border-2 border-secondary");
      const head = h("div", { class: "flex flex-wrap items-center justify-between gap-2" }, body);
      h("h2", { class: "card-title" }, head, `Changes in “${st.scenario.name}”`);
      const hb = h("div", { class: "flex gap-2" }, head);
      const undo = btn(hb, "Undo last change", "btn-sm btn-ghost", () => act(() => post(`/api/scenarios/${st.scenario.id}/undo`), "Undone."));
      undo.disabled = !st.can_undo;
      btn(hb, "Add a change", "btn-sm btn-secondary", () => openChangeWizard(st.scenario.id), "plus");
      if (!st.constraints.length) h("p", { class: "text-base-content/70" }, body, "No changes yet. Add one to see how the plans respond.");
      const list = h("ul", { class: "list" }, body);
      for (const c of st.constraints) {
        const li = h("li", { class: "list-row items-center" }, list);
        const txt = h("div", { class: "list-col-grow" }, li);
        h("div", { class: "font-semibold" }, txt, c.readback);
        if (c.note) h("div", { class: "text-sm italic text-base-content/70" }, txt, `“${c.note.text}”`);
        h("div", { class: "text-xs text-base-content/60" }, txt, c.proposed_via === "chat" ? "From Claude, confirmed by you" : "Added on the website");
        btn(li, "Remove", "btn-sm btn-ghost", () => act(() => api(`/api/constraints/${c.id}`, { method: "DELETE" }), "Removed. Recalculating."));
      }
      const foot = h("div", { class: "card-actions" }, body);
      link(foot, "See the plans for this what-if", "#/plans", "btn btn-sm");
    }

    h("h2", { class: "text-lg font-bold" }, root, "Your plans and what-ifs");
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    for (const sc of ev.scenarios.filter((s) => s.status === "active")) {
      const c = h("div", { class: `card bg-base-100 shadow-sm ${sc.id === S.scenarioId ? "ring-2 ring-secondary" : ""}`.trim() }, grid);
      const b = h("div", { class: "card-body gap-2" }, c);
      const t = h("div", { class: "flex items-center gap-2" }, b);
      h("h3", { class: "card-title text-base" }, t, sc.kind === "official" ? "Official plan" : sc.name);
      h("span", { class: `badge badge-sm ${sc.kind === "official" ? "badge-primary" : "badge-secondary"}` }, t, sc.kind === "official" ? "Official" : "What-if");
      h("p", { class: "text-xs text-base-content/60" }, b, sc.created_via === "system" ? "Created automatically" : `Started ${sc.created_via === "dashboard" ? "on the website" : "in Claude"}`);
      const a = h("div", { class: "card-actions justify-end" }, b);
      if (sc.id !== S.scenarioId) btn(a, "Look at it", "btn-sm", () => setScenario(sc.id));
      else h("span", { class: "badge badge-ghost" }, a, "You are looking at this");
      if (sc.kind === "what_if") btn(a, "Discard", "btn-sm btn-ghost", () => confirmBox(
        "Discard this what-if?", `“${sc.name}” will be removed from the list. It stays in History.`, "Discard",
        () => act(async () => { await post(`/api/scenarios/${sc.id}/discard`); if (S.scenarioId === sc.id) S.scenarioId = null; }, "Discarded.")));
    }
  }

  function pageInbox(root) {
    header(root, "Inbox", "Notes Claude wrote down for you. Check how each was understood, then confirm or reject it.", "inbox");
    const pending = S.event.pending;
    if (!pending.length) {
      const b = card(root);
      const row = h("div", { class: "flex items-center gap-4" }, b);
      const ic = h("div", { class: "flex size-12 shrink-0 items-center justify-center rounded-full bg-success/30" }, row);
      ic.appendChild(icon("check", "size-6"));
      const t = h("div", {}, row);
      h("h2", { class: "font-bold" }, t, "Nothing waiting");
      h("p", { class: "text-sm text-base-content/75" }, t, "When you tell Claude about roadworks, another event or a schedule change, the note appears here first.");
      return;
    }
    for (const c of pending) {
      const b = card(root);
      if (c.note) {
        h("div", { class: "text-xs font-semibold uppercase text-base-content/60" }, b, "You said");
        h("blockquote", { class: "border-l-4 border-secondary pl-3 italic" }, b, `“${c.note.text}”`);
      }
      h("div", { class: "text-xs font-semibold uppercase text-base-content/60" }, b, "Understood as");
      h("p", { class: "font-semibold" }, b, c.readback);
      if (c.interpretation && c.interpretation !== c.readback) h("p", { class: "text-sm text-base-content/75" }, b, c.interpretation);
      if (c.assumptions.length) {
        const d = h("details", { class: "collapse collapse-arrow bg-base-200" }, b);
        h("summary", { class: "collapse-title text-sm font-semibold" }, d, `What Claude had to guess (${c.assumptions.length})`);
        const ul = h("ul", { class: "collapse-content list-disc pl-8 text-sm" }, d);
        for (const a of c.assumptions) h("li", {}, ul, a);
      }
      const a = h("div", { class: "card-actions items-center justify-end" }, b);
      const reason = h("input", { type: "text", class: "input input-sm w-full max-w-xs bg-base-200", placeholder: "Why reject? (optional)", maxlength: 1000, "aria-label": "Reason for rejecting" }, a);
      btn(a, "Reject", "btn-sm btn-ghost", () => act(() => post(`/api/constraints/${c.id}/reject`, { reason: reason.value }), "Rejected."));
      btn(a, "Confirm", "btn-sm btn-success", () => act(async () => {
        const r = await post(`/api/constraints/${c.id}/confirm`);
        S.scenarioId = r.scenario_id; store.set(`fm-scenario-${S.eventId}`, r.scenario_id);
      }, "Confirmed. It's now in a what-if, and the plans are being recalculated."), "check");
    }
  }

  const TYPE_WORDS = { gate_closed: "a closed entrance", gate_capacity_limit: "fewer lanes", staff_limit: "a staff limit",
                       competing_event: "an event nearby", schedule_shift: "a moved show", note_only: "a note" };
  function historySentence(r) {
    const d = r.details || {};
    switch (r.action) {
      case "propose_constraint": return `Claude wrote down a note about ${TYPE_WORDS[d.type] || "a change"}`;
      case "confirm_constraint": return "Confirmed a note from Claude";
      case "reject_constraint": return `Rejected a note from Claude${d.reason ? `: “${d.reason}”` : ""}`;
      case "create_what_if": return `Started the what-if “${d.name || ""}”`;
      case "add_constraint": return `Added a change: ${TYPE_WORDS[d.type] || "a change"}`;
      case "remove_constraint": return "Removed a change";
      case "select_plan": return `Picked plan ${d.solution_id}`;
      case "undo": return "Undid the last change";
      case "discard_scenario": return "Discarded a what-if";
      case "promote_scenario": return `Made a what-if the official plan (${d.staff_hours} staff-hours, ${eur(d.staff_cost || 0)})`;
      default: return r.action.replaceAll("_", " ");
    }
  }
  const WHERE = { dashboard: "on the website", chat: "via Claude", chat_panel: "in the Claude chat", system: "automatically" };

  function pageHistory(root) {
    header(root, "History", "Every change: what happened, who did it, and where.", "history");
    const box = h("div", {}, root);
    waiting(box, "Loading the history…");
    api(`/api/events/${S.eventId}/audit`).then((rows) => {
      box.textContent = "";
      if (!rows.length) { const b = card(box); h("p", {}, b, "Nothing has changed yet."); return; }
      const ul = h("ul", { class: "list rounded-box bg-base-100 shadow-sm" }, box);
      for (const r of rows.slice(0, 60)) {
        const li = h("li", { class: "list-row items-center" }, ul);
        h("span", { class: `status status-md ${r.channel === "dashboard" ? "status-primary" : r.channel === "system" ? "status-neutral" : "status-secondary"}`,
                    "aria-hidden": "true" }, li);
        const txt = h("div", { class: "list-col-grow" }, li);
        h("div", { class: "font-semibold" }, txt, historySentence(r));
        const who = r.actor === (S.me && S.me.name) ? "You" : r.actor;
        h("div", { class: "text-xs text-base-content/60" }, txt, `${who} · ${WHERE[r.channel] || r.channel} · ${new Date(r.at).toLocaleString()}`);
      }
    }).catch((e) => { box.textContent = ""; toast(e.message, "error"); });
  }

  /* ---------------- learn & tour ---------------- */

  const TOUR = [
    { title: "Meet your park", text: [
      "This is Parkland Theme Park, a made-up park for trying the tool. Guests come in through three entrances, and each can open several lanes.",
      "Today's event is the one in the menu at the top. You can switch events there." ], visual: tourPark },
    { title: "Reading a forecast", text: [
      "The line shows the most likely number of guests arriving each hour at one entrance.",
      "The shaded band is the likely range: on about 8 days out of 10, the real number lands inside it." ], visual: (b) => tourGate(b, false) },
    { title: "Why the forecast is unsure", text: [
      "There are two different reasons, shown in two colors below the chart.",
      "Blue is ordinary day-to-day variation. Nobody can remove it, so plan a few extra staff. Orange means the tool has little history, here because this entrance is brand new. What you know can fill that gap." ], visual: (b) => tourGate(b, true) },
    { title: "Choosing a staffing plan", text: [
      "The tool tries thousands of staffing schedules and keeps the best ones. Each dot is one plan.",
      "Further right costs more; lower means shorter queues. It marks a balanced plan, but you choose." ], visual: tourPlans },
    { title: "Trying a what-if", text: [
      "Suppose roadworks close the North Gate road from 5 to 8 pm. Start a what-if, add that change, and the plans are recalculated in seconds.",
      "The official plan stays untouched while you explore." ], visual: tourWhatIf },
    { title: "Making it official", text: [
      "When a what-if looks right, open its plans and make it official. Notes from Claude always wait in your Inbox until you confirm them.",
      "Every step is kept in History, so you can see who changed what, and why." ], visual: tourOfficial },
    { title: "Use it in Claude", text: [
      "You can do all of this by chatting. Add the tool to Claude as a connector, then ask in your own words.",
      "Claude shows the same charts in the chat, and anything you confirm there shows up here too." ], visual: tourClaude },
  ];

  function tourPark(b) {
    const grid = h("div", { class: "grid gap-3 sm:grid-cols-3" }, b);
    const fc = S.scenario.forecast;
    const colors = ["bg-primary/40", "bg-secondary/50", "bg-accent/60"];
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
    flow(b, [["Start a what-if", "A safe copy of the official plan."], ["Add the change", "“North Gate closed 17:00–20:00”."], ["See new plans", "Recalculated in a few seconds."]]);
  }
  function tourOfficial(b) {
    flow(b, [["Check the plans", "Pick the balance you like."], ["Make it official", "You confirm once more."], ["It's in History", "Who, when, and from which note."]]);
  }
  function tourClaude(b) {
    claudeBox(b);
  }
  function claudeBox(b) {
    const ol = h("ol", { class: "list-decimal space-y-2 pl-5" }, b);
    h("li", {}, ol, "In Claude, open Settings, then Connectors, and add a custom connector.");
    const li = h("li", {}, ol);
    li.appendChild(document.createTextNode("Paste this address:"));
    const row = h("div", { class: "join mt-2 w-full max-w-lg" }, li);
    const input = h("input", { class: "input join-item w-full font-mono text-sm", readonly: "", "aria-label": "Connector address", value: S.me.mcp_url }, row);
    const copy = btn(row, "Copy", "join-item", async () => {
      try { await navigator.clipboard.writeText(S.me.mcp_url); copy.textContent = "Copied"; } catch (e) { input.select(); }
      setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    });
    h("li", {}, ol, "Sign in when Claude asks, then try one of these:");
    const ex = h("div", { class: "flex flex-wrap gap-2" }, b);
    for (const q of ["Show me the forecast for Halloween Night", "Roadworks close the north road from 5 to 8 pm", "What does the balanced plan cost?"]) h("span", { class: "badge badge-soft badge-secondary h-auto py-1" }, ex, q);
    h("p", { class: "text-sm text-base-content/60" }, b, "Custom connectors need a paid Claude plan.");
  }

  function pageTour(root, arg) {
    const n = Math.max(1, Math.min(TOUR.length, Number(arg) || 1));
    const step = TOUR[n - 1];
    const steps = h("ul", { class: "steps w-full text-xs" }, root);
    TOUR.forEach((t, i) => {
      const li = h("li", { class: `step ${i < n ? "step-primary" : ""}`.trim(), "data-content": String(i + 1) }, steps);
      li.setAttribute("aria-label", t.title);
    });
    const b = card(root);
    h("div", { class: "text-sm text-base-content/60" }, b, `Step ${n} of ${TOUR.length}`);
    h("h1", { class: "text-2xl font-bold" }, b, step.title);
    for (const p of step.text) h("p", { class: "text-base-content/85" }, b, p);
    const vis = h("div", { class: "mt-2" }, b);
    step.visual(vis);
    const nav = h("div", { class: "card-actions items-center justify-between border-t border-base-300 pt-4" }, b);
    if (n > 1) link(nav, "Back", `#/tour/${n - 1}`, "btn btn-ghost"); else h("span", {}, nav);
    const right = h("div", { class: "flex gap-2" }, nav);
    if (n < TOUR.length) {
      btn(right, "Skip the tour", "btn-ghost", () => { store.set("fm-tour-done", "1"); location.hash = "#/home"; });
      link(right, "Next", `#/tour/${n + 1}`, "btn btn-primary");
    } else {
      btn(right, "Go to my overview", "btn-primary", () => { store.set("fm-tour-done", "1"); location.hash = "#/home"; });
    }
  }

  function pageLearn(root) {
    header(root, "Learn", "Short explanations, one idea at a time. Read them in order or jump to any.");
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    TOUR.forEach((t, i) => {
      const a = h("a", { href: `#/tour/${i + 1}`, class: "card bg-base-100 shadow-sm transition hover:shadow-md" }, grid);
      const b = h("div", { class: "card-body flex-row items-center gap-4" }, a);
      h("div", { class: "flex size-10 shrink-0 items-center justify-center rounded-full bg-accent font-bold text-accent-content" }, b, String(i + 1));
      const t_ = h("div", {}, b);
      h("h2", { class: "font-bold" }, t_, t.title);
      h("p", { class: "text-sm text-base-content/70" }, t_, t.text[0]);
    });
    const more = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    for (const [href, title, text] of [["#/about", "About this prototype", "What is real here, what is made up, and what this research tests."],
                                        ["/privacy", "Privacy", "What is stored about you, and how to delete it."]]) {
      const a = h("a", { href, class: "card card-border bg-base-100" }, more);
      const b = h("div", { class: "card-body" }, a);
      h("h2", { class: "font-bold" }, b, title);
      h("p", { class: "text-sm text-base-content/70" }, b, text);
    }
  }

  function pageClaude(root) {
    header(root, "Use it in Claude", "Ask in your own words. Claude shows the same charts in the chat.", "claude");
    const b = card(root);
    claudeBox(b);
    const how = card(root);
    h("h2", { class: "card-title" }, how, "What happens to what you tell Claude");
    flow(how, [["You mention something", "For example: “a big concert in town from 7 pm”."], ["Claude writes it down", "With how it understood you, and anything it had to guess."],
               ["You confirm it", "In the chat or in your Inbox here. Until then, nothing changes."], ["Plans update", "In a what-if, so the official plan stays safe."]]);
  }

  function pageAbout(root) {
    header(root, "About this prototype", "What is real here, and what isn't.");
    const fc = S.scenario.forecast;
    const grid = h("div", { class: "grid gap-4 sm:grid-cols-2" }, root);
    const days = fc ? Math.max(...fc.series.map((s) => s.history.days)) : 120;
    const cov = fc && fc.backtest ? `${pct(fc.backtest.coverage_p10_p90)} of what really happened (the aim is 80%)` : "close to the aim of 80%";
    for (const [title, text, cls] of [
      ["The park is made up", `Parkland Theme Park, its entrances and ${days} days of visitor history are invented. They are built to behave like real arrivals: busy mornings, an evening rush before the show, quieter rainy days, and one brand-new entrance.`, "bg-secondary/30"],
      ["The numbers are really calculated", "Nothing on screen is faked or typed in. Every forecast and every staffing plan is worked out live from that history by a forecasting model and an optimization algorithm.", "bg-primary/30"],
      ["The forecast is checked", `On past days it had not seen, the forecast's likely ranges contained ${cov}. So its sense of its own uncertainty is about right.`, "bg-accent/40"],
      ["The methods are simple stand-ins", "The forecasting model and the optimization algorithm are deliberately simple, made to test the ideas. A real venue would plug in its own data and stronger models.", "bg-info/20"],
    ]) {
      const c = h("div", { class: `card ${cls}` }, grid);
      const b = h("div", { class: "card-body" }, c);
      h("h2", { class: "card-title text-base" }, b, title);
      h("p", { class: "text-sm" }, b, text);
    }
    const t = card(root);
    h("h2", { class: "card-title" }, t, "What this research tests");
    h("p", {}, t, "Whether showing why a forecast is unsure, and turning a planner's knowledge into rules the staffing planner follows, helps people make better staffing decisions with less effort.");
    h("p", { class: "text-sm text-base-content/70" }, t, "Your workspace is private to you. You can start over with a fresh demo from the account menu at any time.");
  }

  /* ---------------- account actions ---------------- */

  $("help-btn").addEventListener("click", () => {
    const name = route().name;
    explain({ forecast: "forecast", plans: "plans", whatifs: "whatifs", inbox: "inbox", history: "history", claude: "claude" }[name] || "home");
  });
  $("reset-btn").addEventListener("click", () => confirmBox(
    "Start over with a fresh demo?",
    "This deletes everything in your workspace (what-ifs, notes, history) and loads a fresh copy of the demo park.",
    "Start over",
    () => act(async () => { await post("/api/workspace/reset-demo"); S.scenarioId = null; await loadEvents(); }, "Fresh demo loaded. The first plans take a few seconds.")));

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
