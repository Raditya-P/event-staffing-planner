/* Dashboard: reads and writes the same store as the MCP tools, and polls one revision
   number per event so changes made in Claude show up here within a couple of seconds. */
(function () {
  "use strict";
  const { h, fmt, fmt1, eur } = window.FC;
  const $ = (id) => document.getElementById(id);

  const state = { events: [], eventId: null, event: null, scenarioId: null, scenario: null, revision: null, lastSync: null };

  async function api(path, opts) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) { location.href = "/"; throw new Error("Please sign in."); }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

  function toast(text, isErr) {
    const t = h("div", { class: "toast" + (isErr ? " err" : ""), role: isErr ? "alert" : "status" }, document.body, text);
    setTimeout(() => t.remove(), isErr ? 6000 : 3000);
  }
  async function act(fn, okText) {
    try { await fn(); if (okText) toast(okText); await reload(); } catch (e) { toast(e.message, true); }
  }

  /* ---------------- loading ---------------- */

  async function loadEvents() {
    state.events = await api("/api/events");
    const sel = $("event-select");
    sel.textContent = "";
    for (const ev of state.events) h("option", { value: ev.id }, sel, `${ev.name} · ${ev.weekday} ${ev.date}`);
    const fromHash = new URLSearchParams(location.hash.slice(1));
    const wanted = fromHash.get("event");
    state.eventId = state.events.some((e) => e.id === wanted) ? wanted : (state.events[0] && state.events[0].id);
    state.scenarioId = fromHash.get("scenario");
    sel.value = state.eventId;
  }

  async function reload() {
    if (!state.eventId) return;
    state.event = await api(`/api/events/${state.eventId}`);
    state.revision = state.event.revision;
    const active = state.event.scenarios.filter((s) => s.status === "active");
    if (!state.scenarioId || !state.event.scenarios.some((s) => s.id === state.scenarioId)) {
      state.scenarioId = (active.find((s) => s.kind === "official") || active[0]).id;
    }
    state.scenario = await api(`/api/scenarios/${state.scenarioId}`);
    history.replaceState(null, "", `#event=${state.eventId}&scenario=${state.scenarioId}`);
    render();
    loadAudit();
    markSynced();
  }

  async function loadAudit() {
    const rows = await api(`/api/events/${state.eventId}/audit`);
    const box = $("audit");
    box.textContent = "";
    if (!rows.length) { h("div", { class: "muted" }, box, "No changes yet. Every proposal, confirmation, edit, undo and promotion appears here."); return; }
    const table = h("table", { class: "audit" }, box);
    const head = h("tr", {}, h("thead", {}, table));
    for (const c of ["When (UTC)", "Who", "Via", "Action", "Target", "From note"]) h("th", {}, head, c);
    const body = h("tbody", {}, table);
    for (const r of rows.slice(0, 40)) {
      const tr = h("tr", {}, body);
      for (const v of [r.at.replace("T", " ").slice(0, 19), r.actor, r.channel.replace("_", " "), r.action.replaceAll("_", " "), r.target_id, r.note_id || ""]) h("td", {}, tr, v);
    }
  }

  function markSynced() {
    state.lastSync = Date.now();
    $("sync").classList.remove("off");
  }
  setInterval(() => {
    const txt = $("sync").querySelector(".txt");
    if (!state.lastSync) return;
    const s = Math.round((Date.now() - state.lastSync) / 1000);
    txt.textContent = s < 5 ? "Up to date" : `Last synced ${s}s ago`;
  }, 1000);

  // Poll one number; refetch only when something changed (here, in Claude, or in another tab).
  async function poll() {
    try {
      if (state.eventId) {
        const { revision } = await api(`/api/events/${state.eventId}/revision`);
        const run = state.scenario && state.scenario.run;
        const running = run && (run.status === "queued" || run.status === "running");
        if (revision !== state.revision) await reload();
        else if (running) { state.scenario = await api(`/api/scenarios/${state.scenarioId}`); renderScenarioPanel(); markSynced(); }
        else markSynced();
      }
    } catch (e) {
      $("sync").classList.add("off");
      $("sync").querySelector(".txt").textContent = "Offline, retrying…";
    }
    setTimeout(poll, 2000);
  }

  /* ---------------- rendering ---------------- */

  function render() {
    renderScenarioList();
    renderPending();
    renderScenarioPanel();
    renderCharts();
    renderConstraints();
  }

  function renderScenarioList() {
    const box = $("scenario-list");
    box.textContent = "";
    for (const sc of state.event.scenarios.filter((s) => s.status === "active")) {
      const b = h("button", { class: "scn" + (sc.id === state.scenarioId ? " active" : ""), type: "button" }, box);
      const name = h("div", { class: "name" }, b, sc.name);
      h("span", { class: "tag" + (sc.kind === "official" ? " official" : "") }, name, sc.kind === "official" ? "Official" : "What-if");
      h("div", { class: "meta" }, b, `by ${sc.created_by} via ${sc.created_via.replace("_", " ")} · ${sc.run_status || "no run"}`);
      b.addEventListener("click", () => { state.scenarioId = sc.id; reload(); });
    }
    const old = state.event.scenarios.filter((s) => s.status !== "active");
    if (old.length) h("div", { class: "muted", style: "margin-top:6px" }, box, `${old.length} archived or discarded (in the audit log).`);
  }

  function renderPending() {
    const box = $("pending-list");
    box.textContent = "";
    if (!state.event.pending.length) { h("div", { class: "muted" }, box, "Nothing waiting. Notes Claude records from chat appear here too."); return; }
    for (const c of state.event.pending) {
      const card = h("div", { class: "pending" }, box);
      if (c.note) h("blockquote", {}, card, `“${c.note.text}”`);
      h("div", { class: "rb" }, card, c.readback);
      if (c.interpretation && c.interpretation !== c.readback) h("div", { class: "muted" }, card, `Read as: ${c.interpretation}`);
      if (c.assumptions.length) { const ul = h("ul", {}, card); for (const a of c.assumptions) h("li", {}, ul, a); }
      h("div", { class: "muted" }, card, `Proposed by ${c.proposed_by} via ${c.proposed_via} · acts on the ${c.acts_on}`);
      const row = h("div", { class: "row" }, card);
      h("button", { class: "btn primary small", type: "button" }, row, "Confirm").addEventListener("click", () =>
        act(async () => { const r = await post(`/api/constraints/${c.id}/confirm`); state.scenarioId = r.scenario_id; }, "Confirmed; re-optimizing the what-if."));
      h("button", { class: "btn small", type: "button" }, row, "Reject").addEventListener("click", () => {
        const reason = prompt("Why reject it? (optional)") ;
        if (reason === null) return;
        act(() => post(`/api/constraints/${c.id}/reject`, { reason }), "Rejected.");
      });
    }
  }

  function renderScenarioPanel() {
    const d = state.scenario, sc = d.scenario, run = d.run;
    const box = $("scenario-panel");
    box.textContent = "";
    const head = h("div", { class: "scn-head" }, box);
    const left = h("div", {}, head);
    const title = h("h2", { class: "title" }, left, sc.name);
    h("span", { class: "tag" + (sc.kind === "official" ? " official" : "") }, title, sc.kind === "official" ? "Official" : "What-if");
    h("div", { class: "muted" }, left, `${d.event.name} · ${d.event.weekday} ${d.event.date} · ${d.event.venue} · open ${d.event.opening_hours}`);
    const actions = h("div", { class: "row" }, head);
    const running = run && (run.status === "queued" || run.status === "running");
    if (d.editable) {
      const undo = h("button", { class: "btn", type: "button" }, actions, "Undo last change");
      undo.disabled = !d.can_undo;
      undo.addEventListener("click", () => act(() => post(`/api/scenarios/${sc.id}/undo`), "Undone."));
      h("button", { class: "btn danger", type: "button" }, actions, "Discard").addEventListener("click", () => {
        if (!confirm(`Discard “${sc.name}”? It stays in the audit log.`)) return;
        act(async () => { await post(`/api/scenarios/${sc.id}/discard`); state.scenarioId = null; }, "Discarded.");
      });
      const promote = h("button", { class: "btn primary", type: "button" }, actions, "Make official…");
      promote.disabled = running || !d.selected_solution || d.result_is_stale;
      promote.addEventListener("click", () => openPromote(d));
    }

    if (running) {
      const st = h("div", { class: "status" }, box, `Re-optimizing… ${run.stage === "forecast" ? "updating the forecast" : "searching staffing plans"} (trigger: ${run.trigger})`);
      const bar = h("div", { class: "progress" }, st);
      h("div", { style: `width:${Math.round((run.stage === "optimize" ? 0.2 + 0.8 * run.progress : 0.1) * 100)}%` }, bar);
    } else if (run && run.status === "failed") {
      h("div", { class: "status err" }, box, `The last run failed: ${run.error}`);
    }

    const sel = d.selected_solution;
    if (sel) {
      const stats = h("div", { class: "stats" + (running ? " stale" : "") }, box);
      const stat = (v, l) => { const s = h("div", { class: "stat" }, stats); h("div", { class: "v" }, s, v); h("div", { class: "l" }, s, l); };
      stat(eur(sel.staff_cost), `${sel.staff_hours} staff-hours · plan ${sel.id}`);
      stat(`${fmt1(sel.expected_wait)} min`, "expected wait per guest");
      stat(`${fmt1(sel.wait_p90)} min`, "on a bad day (9 of 10 better)");
      if (d.forecast) stat(fmt(d.forecast.totals.day.p50), `expected guests (${fmt(d.forecast.totals.day.p10)}–${fmt(d.forecast.totals.day.p90)})`);
      if (d.comparison) {
        const c = d.comparison;
        h("div", { class: "muted" }, box,
          `Versus the official plan: ${c.delta_staff_hours >= 0 ? "+" : ""}${c.delta_staff_hours} staff-hours (${c.delta_staff_cost >= 0 ? "+" : "−"}${eur(Math.abs(c.delta_staff_cost))}), ` +
          `expected wait ${c.delta_expected_wait >= 0 ? "+" : "−"}${fmt1(Math.abs(c.delta_expected_wait))} min, bad-day wait ${c.delta_wait_p90 >= 0 ? "+" : "−"}${fmt1(Math.abs(c.delta_wait_p90))} min.`);
      }
      if (d.explanation) h("div", { class: "muted", style: "margin-top:4px" }, box, d.explanation);
      if (d.result && d.result.service_cap_met === false) h("div", { class: "status err" }, box, `No plan keeps the expected wait under ${d.result.max_expected_wait} minutes with these constraints.`);
    }
  }

  function renderCharts() {
    const d = state.scenario;
    const tradeoff = $("tradeoff"), schedule = $("schedule"), forecast = $("forecast");
    const running = d.run && (d.run.status === "queued" || d.run.status === "running");
    for (const el of [tradeoff, schedule, forecast]) el.classList.toggle("stale", !!running);
    if (d.result) {
      FC.renderTradeoff(tradeoff, d.result, {
        selectedId: d.scenario.selected_solution_id,
        official: d.comparison ? d.comparison.official : null,
        editable: d.editable && !running && !d.result_is_stale,
        onSelect: (id) => act(() => post(`/api/scenarios/${d.scenario.id}/select`, { solution_id: id })),
      });
      if (!d.editable) h("div", { class: "muted" }, tradeoff, "To choose a different plan, make a what-if and promote it.");
    } else { tradeoff.textContent = ""; h("div", { class: "muted" }, tradeoff, "No trade-off yet."); }
    if (d.selected_solution && d.result) FC.renderSchedule(schedule, d.selected_solution, d.result, d.event.gates);
    else schedule.textContent = "";
    if (d.forecast) FC.renderForecast(forecast, d.forecast, { title: true });
    else { forecast.textContent = ""; h("div", { class: "muted" }, forecast, "No forecast yet."); }
  }

  /* ---------------- constraints ---------------- */

  const TYPES = {
    gate_closed: { label: "Gate closed", fields: ["gate_id", "start", "end"] },
    gate_capacity_limit: { label: "Fewer lanes at a gate", fields: ["gate_id", "max_lanes", "start", "end"] },
    staff_limit: { label: "Staff limit (all gates)", fields: ["max_staff", "start", "end"] },
    competing_event: { label: "Competing event nearby", fields: ["name", "impact", "start", "end"] },
    schedule_shift: { label: "Show moves", fields: ["show_start"] },
    note_only: { label: "Note only", fields: ["summary"] },
  };

  function renderConstraints() {
    const d = state.scenario, box = $("constraints-panel");
    box.textContent = "";
    h("h2", {}, box, "Constraints in this scenario");
    const ul = h("ul", { class: "cons" }, box);
    if (!d.constraints.length) h("li", { class: "muted" }, ul, "None.");
    for (const c of d.constraints) {
      const li = h("li", {}, ul);
      const left = h("div", {}, li);
      h("div", {}, left, c.readback);
      const src = c.note ? `“${c.note.text}” · ` : "";
      h("div", { class: "src" }, left, `${src}acts on the ${c.acts_on} · ${c.proposed_via === "chat" ? "from chat, confirmed by " + c.decided_by : "added by " + c.proposed_by}`);
      if (d.editable) h("button", { class: "btn small", type: "button" }, li, "Remove").addEventListener("click", () =>
        act(() => api(`/api/constraints/${c.id}`, { method: "DELETE" }), "Removed; re-optimizing."));
    }
    if (!d.editable) {
      h("div", { class: "muted", style: "margin-top:8px" }, box, "The official plan is read-only. Create a what-if to change it.");
      return;
    }
    h("div", { class: "muted", style: "margin-top:12px" }, box, "Add a change directly (applies at once; undo is available):");
    const form = h("div", { class: "form-grid" }, box);
    const typeSel = h("select", { "aria-label": "Constraint type" }, h("label", {}, form, "Type"));
    for (const [k, v] of Object.entries(TYPES)) h("option", { value: k }, typeSel, v.label);
    const fieldsBox = h("div", { class: "form-grid", style: "margin:0" }, form);
    const note = h("input", { type: "text", placeholder: "Your note (optional)", style: "min-width:220px" }, h("label", {}, form, "Note"));
    const add = h("button", { class: "btn primary", type: "button" }, form, "Add");
    const inputs = {};
    const ev = d.event;
    const hourOpts = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => `${String(from + i).padStart(2, "0")}:00`);
    function build() {
      fieldsBox.textContent = "";
      for (const k of Object.keys(inputs)) delete inputs[k];
      for (const f of TYPES[typeSel.value].fields) {
        const lab = h("label", {}, fieldsBox, { gate_id: "Gate", start: "From", end: "Until", max_lanes: "Max lanes", max_staff: "Max staff",
          name: "Event name", impact: "Impact", show_start: "Show starts", summary: "Summary" }[f]);
        let input;
        if (f === "gate_id") { input = h("select", {}, lab); for (const g of ev.gates) h("option", { value: g.id }, input, g.name); }
        else if (f === "start") { input = h("select", {}, lab); for (const o of hourOpts(ev.open_hour, ev.close_hour - 1)) h("option", { value: o }, input, o); }
        else if (f === "end") { input = h("select", {}, lab); for (const o of hourOpts(ev.open_hour + 1, ev.close_hour)) h("option", { value: o }, input, o); input.value = `${String(ev.open_hour + 3).padStart(2, "0")}:00`; }
        else if (f === "show_start") { input = h("select", {}, lab); for (const o of hourOpts(ev.open_hour, ev.close_hour - 1)) h("option", { value: o }, input, o); input.value = "20:00"; }
        else if (f === "impact") { input = h("select", {}, lab); h("option", { value: "minor" }, input, "Minor"); h("option", { value: "major" }, input, "Major"); }
        else if (f === "max_lanes" || f === "max_staff") { input = h("input", { type: "number", min: 1, value: f === "max_lanes" ? 2 : 12, style: "width:80px" }, lab); }
        else input = h("input", { type: "text" }, lab);
        inputs[f] = input;
      }
    }
    typeSel.addEventListener("change", build);
    build();
    add.addEventListener("click", () => {
      const constraint = { type: typeSel.value };
      for (const [k, el] of Object.entries(inputs)) constraint[k] = el.type === "number" ? Number(el.value) : el.value;
      act(() => post(`/api/scenarios/${d.scenario.id}/constraints`, { constraint, note_text: note.value }), "Added; re-optimizing.");
    });
  }

  /* ---------------- promote ---------------- */

  function openPromote(d) {
    const sel = d.selected_solution, dlg = $("promote-dialog");
    $("promote-text").textContent =
      `“${d.scenario.name}” with plan ${sel.id} (${sel.staff_hours} staff-hours, ${eur(sel.staff_cost)}, ${fmt1(sel.expected_wait)} min expected wait) ` +
      `becomes the official plan. The current official plan is archived and stays in the history.`;
    dlg.showModal();
    $("promote-cancel").onclick = () => dlg.close();
    $("promote-yes").onclick = () => {
      dlg.close();
      act(() => post(`/api/scenarios/${d.scenario.id}/promote`, { solution_id: sel.id }), "This is now the official plan.");
    };
  }

  /* ---------------- start ---------------- */

  $("event-select").addEventListener("change", (e) => { state.eventId = e.target.value; state.scenarioId = null; reload(); });
  $("new-btn").addEventListener("click", () => {
    const name = $("new-name").value.trim() || "What-if";
    act(async () => { const r = await post("/api/scenarios", { event_id: state.eventId, name }); state.scenarioId = r.id; $("new-name").value = ""; }, "What-if created; optimizing.");
  });

  /* ---------------- account, Claude, reset ---------------- */

  async function loadMe() {
    const me = await api("/api/me");
    $("mcp-url").textContent = me.mcp_url;
    if (me.auth_mode === "oidc") {
      $("who").textContent = me.name || "";
      $("signout").hidden = false;
    }
    $("reset-btn").hidden = !me.can_reset;
    try { if (!localStorage.getItem("fc-intro-seen")) { $("intro").open = true; localStorage.setItem("fc-intro-seen", "1"); } } catch (e) { /* storage blocked */ }
  }
  $("claude-btn").addEventListener("click", () => $("claude-dialog").showModal());
  $("claude-close").addEventListener("click", () => $("claude-dialog").close());
  $("reset-btn").addEventListener("click", () => {
    if (!confirm("Delete everything in your workspace (scenarios, notes, audit log) and load a fresh demo?")) return;
    $("claude-dialog").close();
    act(async () => { await post("/api/workspace/reset-demo"); await loadEvents(); state.scenarioId = null; }, "Fresh demo loaded; the first plans take a few seconds.");
  });

  (async function start() {
    try { await loadMe(); await loadEvents(); await reload(); } catch (e) { toast(e.message, true); }
    setTimeout(poll, 2000);
  })();
})();
