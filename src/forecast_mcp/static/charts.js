/* Shared chart drawing for the dashboard and the in-chat panel. Plain SVG, no libraries.
   All labels are inserted with textContent: names come from data, never trusted as HTML. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";

  function svg(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }
  function svgText(parent, text, attrs) {
    const node = svg("text", attrs, parent);
    node.textContent = text;
    return node;
  }
  function h(tag, attrs, parent, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else node.setAttribute(k, v);
    }
    if (text !== undefined && text !== null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  const fmt = (n) => Math.round(n).toLocaleString("en-US");
  const fmt1 = (n) => (Math.round(n * 10) / 10).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const eur = (n) => "EUR " + fmt(n);
  const pct = (n) => Math.round(n * 100) + "%";

  function niceStep(max, ticks) {
    const raw = max / Math.max(ticks, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const norm = raw / mag;
    const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return step * mag;
  }
  function niceMax(max, ticks) {
    const step = niceStep(max, ticks);
    return { max: Math.ceil((max || 1) / step) * step, step };
  }

  /* ---------- tooltip ---------- */
  function Tooltip(root) {
    const tip = h("div", { class: "fc-tooltip", role: "status", "aria-live": "polite" }, root);
    return {
      show(x, y, title, rows) {
        tip.textContent = "";
        h("div", { class: "fc-tt-title" }, tip, title);
        for (const r of rows) {
          const row = h("div", { class: "fc-tt-row" }, tip);
          const name = h("span", { class: "fc-tt-name" }, row);
          if (r.key) h("span", { class: r.keyShape === "rect" ? "fc-key-rect" : "fc-key-line", style: `background:${r.key}` }, name);
          name.appendChild(document.createTextNode(r.name));
          h("b", {}, row, r.value);
        }
        tip.style.display = "block";
        const rootBox = root.getBoundingClientRect();
        const w = tip.offsetWidth, hgt = tip.offsetHeight;
        let left = x - rootBox.left + 14, top = y - rootBox.top - hgt - 10;
        if (left + w > root.clientWidth) left = x - rootBox.left - w - 14;
        if (left < 0) left = 4;
        if (top < 0) top = y - rootBox.top + 16;
        tip.style.left = left + "px";
        tip.style.top = top + "px";
      },
      hide() { tip.style.display = "none"; },
    };
  }

  function tableToggle(parent, label, build) {
    const btn = h("button", { class: "fc-table-toggle", type: "button", "aria-expanded": "false" }, parent, "Show " + label);
    const wrap = h("div", { class: "fc-table-wrap", hidden: "" }, parent);
    btn.addEventListener("click", () => {
      const open = wrap.hasAttribute("hidden");
      if (open && !wrap.firstChild) build(wrap);
      if (open) wrap.removeAttribute("hidden"); else wrap.setAttribute("hidden", "");
      btn.textContent = (open ? "Hide " : "Show ") + label;
      btn.setAttribute("aria-expanded", String(open));
    });
  }

  /* ---------- forecast: one card per gate ---------- */
  function forecastCard(grid, series, yMax, yStep, tooltip, labels, opts) {
    opts = opts || {};
    const card = h("div", { class: opts.plain ? "" : "fc-card" }, grid);
    if (opts.head !== false) {
      const head = h("div", { class: "fc-card-head" }, card);
      h("div", { class: "fc-card-title" }, head, series.gate_name);
      const meta = h("div", { class: "fc-card-meta" }, head);
      meta.textContent = `About ${fmt(series.day_total.p50)} guests today (likely ${fmt(series.day_total.p10)}–${fmt(series.day_total.p90)})`;
    }
    if (opts.head !== false && series.history && series.history.thin) {
      const badge = h("div", { class: "fc-badge", title: "This gate has only a few days of data" }, card);
      badge.textContent = `Little history: ${series.history.days} days`;
    }

    const pts = series.points;
    const W = opts.width || 320, H = opts.height || 132, m = { l: 38, r: 8, t: 10, b: 20 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const x = (i) => m.l + (pts.length === 1 ? iw / 2 : (i / (pts.length - 1)) * iw);
    const y = (v) => m.t + ih - (v / yMax) * ih;

    const chart = svg("svg", { class: "fc-svg", viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `${series.gate_name}: arrivals per hour, median and 80% range` }, card);
    for (let v = 0; v <= yMax + 1e-9; v += yStep) {
      svg("line", { class: v === 0 ? "fc-baseline" : "fc-gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }, chart);
      svgText(chart, fmt(v), { x: m.l - 6, y: y(v) + 3, "text-anchor": "end" });
    }
    pts.forEach((p, i) => {
      if (i % 3 === 0) svgText(chart, p.hour, { x: x(i), y: H - 5, "text-anchor": "middle" });
    });
    const band = pts.map((p, i) => `${x(i)},${y(p.p90)}`).join(" L") + " L" +
      pts.slice().reverse().map((p, j) => `${x(pts.length - 1 - j)},${y(p.p10)}`).join(" L");
    svg("path", { d: "M" + band + " Z", fill: "var(--series-1)", "fill-opacity": "0.16", stroke: "none" }, chart);
    svg("path", { d: "M" + pts.map((p, i) => `${x(i)},${y(p.p50)}`).join(" L"), fill: "none", stroke: "var(--series-1)",
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, chart);

    // Split strip: share of the range's variance from each source, per hour (100% columns).
    const SH = opts.strip === false ? 0 : 34, sm = { t: 3, b: 3 };
    const strip = svg("svg", { class: "fc-svg", viewBox: `0 0 ${W} ${Math.max(SH, 1)}`, role: "img",
      "aria-label": `${series.gate_name}: share of uncertainty from limited history per hour` }, card);
    if (opts.strip === false) strip.setAttribute("hidden", "");
    const colW = Math.min(14, (iw / pts.length) - 3);
    const sih = SH - sm.t - sm.b;
    pts.forEach((p, i) => {
      const cx = x(i) - colW / 2;
      const miss = Math.max(0, Math.min(1, p.share_missing_history));
      const hMiss = sih * miss, hVol = sih * (1 - miss);
      const gap = hMiss > 0.5 && hVol > 0.5 ? 2 : 0;
      if (hVol > 0.5) svg("rect", { x: cx, y: sm.t + hMiss + gap, width: colW, height: Math.max(hVol - gap, 0.5), rx: 2,
        fill: "var(--series-1)", "fill-opacity": 0.55 }, strip);
      if (hMiss > 0.5) svg("rect", { x: cx, y: sm.t, width: colW, height: hMiss, rx: 2, fill: "var(--series-2)" }, strip);
    });
    if (opts.strip !== false) h("div", { class: "fc-strip-label" }, card,
      `Why it's uncertain, hour by hour: orange = ${labels.missing_history.toLowerCase()}, blue = ${labels.volatility.toLowerCase()}`);

    // Hover / keyboard layer shared by both SVGs.
    const cross1 = svg("line", { class: "fc-crosshair", y1: m.t, y2: m.t + ih, visibility: "hidden" }, chart);
    const cross2 = svg("line", { class: "fc-crosshair", y1: 0, y2: SH, visibility: "hidden" }, strip);
    const dot = svg("circle", { r: 4, fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden" }, chart);
    const hit = svg("rect", { class: "fc-hit", x: m.l, y: 0, width: iw, height: H, tabindex: 0,
      "aria-label": `${series.gate_name} hourly values; use arrow keys` }, chart);
    let current = -1;
    function showAt(i, cx, cy) {
      current = i;
      const p = pts[i];
      for (const c of [cross1, cross2]) { c.setAttribute("x1", x(i)); c.setAttribute("x2", x(i)); c.setAttribute("visibility", "visible"); }
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.p50)); dot.setAttribute("visibility", "visible");
      tooltip.show(cx, cy, `${series.gate_name} · ${p.hour}`, [
        { name: "Most likely", value: fmt(p.p50), key: "var(--series-1)" },
        { name: "Likely range (8 in 10 days)", value: `${fmt(p.p10)}–${fmt(p.p90)}` },
        { name: labels.volatility, value: `±${fmt(p.sd_volatility)}`, key: "var(--series-1)", keyShape: "rect" },
        { name: labels.missing_history, value: `±${fmt(p.sd_missing_history)} (${pct(p.share_missing_history)})`, key: "var(--series-2)", keyShape: "rect" },
      ]);
    }
    function hideAll() {
      for (const c of [cross1, cross2, dot]) c.setAttribute("visibility", "hidden");
      tooltip.hide();
    }
    hit.addEventListener("pointermove", (e) => {
      const box = chart.getBoundingClientRect();
      const px = ((e.clientX - box.left) / box.width) * W;
      const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - m.l) / iw) * (pts.length - 1))));
      showAt(i, e.clientX, e.clientY);
    });
    hit.addEventListener("pointerleave", hideAll);
    hit.addEventListener("blur", hideAll);
    hit.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = Math.max(0, Math.min(pts.length - 1, (current < 0 ? 0 : current) + (e.key === "ArrowRight" ? 1 : -1)));
      const box = chart.getBoundingClientRect();
      showAt(next, box.left + (x(next) / W) * box.width, box.top + (y(pts[next].p50) / H) * box.height);
    });
  }

  function renderForecast(root, forecast, opts) {
    opts = opts || {};
    root.textContent = "";
    root.classList.add("fc-root");
    const tooltip = Tooltip(root);
    const labels = forecast.uncertainty_labels || { volatility: "Day-to-day variation", missing_history: "Limited history" };
    if (opts.title !== false) {
      h("div", { class: "fc-h" }, root, "Guests arriving per gate and hour");
      const t = forecast.totals.day;
      h("div", { class: "fc-sub" }, root,
        `${forecast.date} · line = most likely, band = likely range (8 in 10 days) · whole day about ${fmt(t.p50)} (${fmt(t.p10)}–${fmt(t.p90)})`);
    }
    if (opts.legend !== false) uncertaintyLegend(root, labels);

    const maxP90 = Math.max(...forecast.series.flatMap((s) => s.points.map((p) => p.p90)));
    const { max, step } = niceMax(maxP90, 4);
    const grid = h("div", { class: "fc-grid" }, root);
    for (const s of forecast.series) forecastCard(grid, s, max, step, tooltip, labels);

    const notes = (forecast.data_notes || []).map((n) => n.text);
    const bt = forecast.backtest;
    if (bt) notes.push(`Check on past days: the likely ranges contained ${pct(bt.coverage_p10_p90)} of what really happened (aim: 80%).`);
    if (notes.length && opts.notes !== false) {
      const ul = h("ul", { class: "fc-notes" }, root);
      for (const n of notes) h("li", {}, ul, n);
    }
    if (opts.table !== false) tableToggle(root, "the numbers", (wrap) => {
      const table = h("table", { class: "fc-table" }, wrap);
      const head = h("tr", {}, h("thead", {}, table));
      for (const c of ["Gate", "Hour", "p10", "Median", "p90", labels.volatility + " ±", labels.missing_history + " ±", "Share limited history"]) h("th", {}, head, c);
      const body = h("tbody", {}, table);
      for (const s of forecast.series) for (const p of s.points) {
        const tr = h("tr", {}, body);
        for (const v of [s.gate_name, p.hour, fmt(p.p10), fmt(p.p50), fmt(p.p90), fmt(p.sd_volatility), fmt(p.sd_missing_history), pct(p.share_missing_history)]) h("td", {}, tr, v);
      }
    });
  }

  function uncertaintyLegend(root, labels) {
    const legend = h("div", { class: "fc-legend" }, root);
    const l1 = h("span", {}, legend); h("span", { class: "fc-key-rect", style: "background:var(--series-1);opacity:.55" }, l1);
    l1.appendChild(document.createTextNode(`${labels.volatility}: plan a few extra staff`));
    const l2 = h("span", {}, legend); h("span", { class: "fc-key-rect", style: "background:var(--series-2)" }, l2);
    l2.appendChild(document.createTextNode(`${labels.missing_history}: what you know can help`));
    return legend;
  }

  /* One gate, drawn large: the size follows the container so text keeps its real size. */
  function renderGate(root, forecast, gateId, opts) {
    opts = opts || {};
    root.textContent = "";
    root.classList.add("fc-root");
    const series = forecast.series.find((s) => s.gate_id === gateId) || forecast.series[0];
    const labels = forecast.uncertainty_labels || { volatility: "Day-to-day variation", missing_history: "Limited history" };
    const tooltip = Tooltip(root);
    if (opts.legend) uncertaintyLegend(root, labels);
    const width = Math.max(300, Math.min(900, Math.round(root.clientWidth || 640)));
    const { max, step } = niceMax(Math.max(...series.points.map((p) => p.p90)), 4);
    forecastCard(root, series, max, step, tooltip, labels,
      { width, height: opts.height || Math.round(Math.min(280, width * 0.42)), strip: opts.strip !== false, head: false, plain: true });
    return series;
  }

  /* ---------- trade-off: staff cost vs expected wait ---------- */
  function renderTradeoff(root, result, opts) {
    opts = opts || {};
    root.textContent = "";
    root.classList.add("fc-root");
    const tooltip = Tooltip(root);
    const sols = result.solutions;
    const selected = opts.selectedId;
    const official = opts.official || null;

    if (opts.title !== false) {
      h("div", { class: "fc-h" }, root, "Staff cost vs. guest waiting");
      h("div", { class: "fc-sub" }, root,
        `Each dot is one staffing plan (${sols.length} in total). Further right costs more; lower means shorter queues. The thin line above a dot shows a bad day.` +
        (opts.editable ? " Click a dot to choose it." : ""));
    }
    const legend = h("div", { class: "fc-legend" }, root);
    const a = h("span", {}, legend); h("span", { class: "fc-key-rect", style: "background:var(--series-1);border-radius:50%" }, a);
    a.appendChild(document.createTextNode("Plan"));
    const b = h("span", {}, legend); h("span", { class: "fc-key-rect", style: "background:var(--surface-1);border:2px solid var(--text-primary);border-radius:50%" }, b);
    b.appendChild(document.createTextNode("Selected"));
    if (official) {
      const c = h("span", {}, legend); h("span", { class: "fc-key-rect", style: "border:2px solid var(--text-muted);border-radius:50%" }, c);
      c.appendChild(document.createTextNode("Official plan now"));
    }

    // Size the drawing to the container so text stays at its real size on wide screens.
    const W = Math.max(320, Math.min(900, Math.round(root.clientWidth || 560))), H = 260, m = { l: 46, r: 24, t: 22, b: 36 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const costs = sols.map((s) => s.staff_cost).concat(official ? [official.staff_cost] : []);
    const waits = sols.map((s) => s.wait_p90).concat(official ? [official.expected_wait] : []);
    let cMin = Math.min(...costs), cMax = Math.max(...costs);
    const pad = Math.max((cMax - cMin) * 0.06, 50);
    cMin = Math.max(0, cMin - pad); cMax = cMax + pad;
    const xs = niceStep(cMax - cMin, 5);
    cMin = Math.floor(cMin / xs) * xs; cMax = Math.ceil(cMax / xs) * xs;
    const { max: wMax, step: ws } = niceMax(Math.max(...waits) * 1.05, 4);
    const x = (v) => m.l + ((v - cMin) / (cMax - cMin)) * iw;
    const y = (v) => m.t + ih - (v / wMax) * ih;

    const chart = svg("svg", { class: "fc-svg", viewBox: `0 0 ${W} ${H}`, role: "group",
      "aria-label": "Trade-off between staff cost and expected waiting time per guest" }, root);
    for (let v = 0; v <= wMax + 1e-9; v += ws) {
      svg("line", { class: v === 0 ? "fc-baseline" : "fc-gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }, chart);
      svgText(chart, fmt(v), { x: m.l - 6, y: y(v) + 3, "text-anchor": "end" });
    }
    for (let v = cMin; v <= cMax + 1e-9; v += xs) svgText(chart, eur(v), { x: x(v), y: H - 20, "text-anchor": "middle" });
    svgText(chart, "Staff cost for the day", { x: m.l + iw / 2, y: H - 4, "text-anchor": "middle", class: "fc-label" });
    svgText(chart, "Expected wait (min per guest)", { x: m.l - 40, y: 10, class: "fc-label" });

    for (const s of sols) {
      svg("line", { x1: x(s.staff_cost), x2: x(s.staff_cost), y1: y(s.expected_wait), y2: y(s.wait_p90),
        stroke: "var(--series-1)", "stroke-opacity": 0.35, "stroke-width": 1.5 }, chart);
    }
    if (official) {
      svg("circle", { cx: x(official.staff_cost), cy: y(official.expected_wait), r: 7, fill: "none",
        stroke: "var(--text-muted)", "stroke-width": 2 }, chart);
    }
    const named = { cheapest: "Cheapest", balanced: "Balanced", shortest_wait: "Shortest wait" };
    const labels = [];
    sols.forEach((s, idx) => {
      const g = svg("g", { class: "fc-point", tabindex: 0, role: opts.editable ? "button" : "img",
        "aria-label": `Plan ${s.id}${s.label ? " (" + named[s.label] + ")" : ""}: ${eur(s.staff_cost)}, ${fmt1(s.expected_wait)} min expected wait` }, chart);
      const isSel = s.id === selected;
      svg("circle", { cx: x(s.staff_cost), cy: y(s.expected_wait), r: 12, fill: "transparent" }, g);
      svg("circle", { class: "fc-ring", cx: x(s.staff_cost), cy: y(s.expected_wait), r: isSel ? 7 : 4.5,
        fill: isSel ? "var(--surface-1)" : "var(--series-1)", stroke: isSel ? "var(--text-primary)" : "var(--surface-1)",
        "stroke-width": isSel ? 2.5 : 2 }, g);
      if (isSel) svg("circle", { cx: x(s.staff_cost), cy: y(s.expected_wait), r: 3.5, fill: "var(--series-1)" }, g);
      if (s.label || isSel) labels.push({ s, isSel });
      const show = (cx, cy) => tooltip.show(cx, cy, `Plan ${s.id}${s.label ? " · " + named[s.label] : ""}`, [
        { name: "Staff cost", value: `${eur(s.staff_cost)} (${s.staff_hours} staff-h)` },
        { name: "Expected wait", value: `${fmt1(s.expected_wait)} min` },
        { name: "Bad day (9 of 10 better)", value: `${fmt1(s.wait_p90)} min` },
        { name: `Day avg over ${result.wait_threshold_min} min`, value: pct(s.prob_wait_over_threshold) },
        { name: "Busiest hour", value: `${s.peak_hour}, ${fmt1(s.peak_hour_wait)} min` },
      ]);
      g.addEventListener("pointermove", (e) => show(e.clientX, e.clientY));
      g.addEventListener("pointerleave", () => tooltip.hide());
      g.addEventListener("focus", () => { const r = g.getBoundingClientRect(); show(r.left + r.width / 2, r.top); });
      g.addEventListener("blur", () => tooltip.hide());
      if (opts.editable && opts.onSelect) {
        g.addEventListener("click", () => opts.onSelect(s.id));
        g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); opts.onSelect(s.id); } });
      }
    });

    const placed = [];
    if (official) {  // label the official plan below its ring so it never sits on the selected plan's label
      const ox = x(official.staff_cost), oy = y(official.expected_wait);
      const below = oy + 20 < m.t + ih;
      svgText(chart, "Official", { x: ox, y: below ? oy + 20 : oy - 12, "text-anchor": "middle", class: "fc-label fc-halo" });
      placed.push({ x: ox, y: below ? oy + 20 : oy - 12 });
    }
    for (const { s, isSel } of labels) {
      const text = isSel ? `Selected${s.label ? " · " + named[s.label] : ""}` : named[s.label];
      const px = x(s.staff_cost), py = y(s.expected_wait);
      const right = px > W - m.r - 110;
      let ly = py - (isSel ? 14 : 11);
      for (const p of placed) if (Math.abs(p.x - px) < 110 && Math.abs(p.y - ly) < 13) ly = p.y - 14;
      placed.push({ x: px, y: ly });
      svgText(chart, text, { x: right ? px - 4 : px + 4, y: ly, "text-anchor": right ? "end" : "start",
        class: (isSel ? "fc-label-strong" : "fc-label") + " fc-halo" });
    }

    tableToggle(root, "plans table", (wrap) => {
      const table = h("table", { class: "fc-table" }, wrap);
      const head = h("tr", {}, h("thead", {}, table));
      for (const c of ["Plan", "", "Staff-h", "Cost", "Expected wait", "Bad-day wait", `P(avg > ${result.wait_threshold_min} min)`]) h("th", {}, head, c);
      const body = h("tbody", {}, table);
      for (const s of sols) {
        const tr = h("tr", {}, body);
        for (const v of [s.id + (s.id === selected ? " ✓" : ""), s.label ? named[s.label] : "", s.staff_hours, eur(s.staff_cost),
          fmt1(s.expected_wait) + " min", fmt1(s.wait_p90) + " min", pct(s.prob_wait_over_threshold)]) h("td", {}, tr, v);
      }
    });
  }

  /* ---------- staffing schedule heat table ---------- */
  function renderSchedule(root, solution, result, gates, opts) {
    root.textContent = "";
    root.classList.add("fc-root");
    if (!opts || opts.title !== false) {
      h("div", { class: "fc-h" }, root, `Staffed lanes per gate and hour · plan ${solution.id}`);
      h("div", { class: "fc-sub" }, root, `${solution.staff_hours} staff-hours. Darker = more of the gate's lanes open. Grey = closed.`);
    }
    const wrap = h("div", { class: "fc-table-wrap" }, root);
    const table = h("table", { class: "fc-heat" }, wrap);
    const head = h("tr", {}, h("thead", {}, table));
    h("th", {}, head, "");
    for (const hour of result.hours) h("th", { scope: "col" }, head, hour.slice(0, 2));
    const body = h("tbody", {}, table);
    const names = Object.fromEntries(gates.map((g) => [g.id, g.name]));
    const lanes = Object.fromEntries(gates.map((g) => [g.id, g.lanes]));
    const totals = result.hours.map(() => 0);
    for (const gid of result.gates) {
      const tr = h("tr", {}, body);
      h("th", { class: "fc-row-head", scope: "row" }, tr, names[gid] || gid);
      solution.schedule[gid].forEach((v, i) => {
        totals[i] += v;
        const maxL = result.bounds.max_lanes[gid][i];
        if (maxL === 0) { h("td", { class: "fc-closed", title: "Closed" }, tr, "–"); return; }
        const level = Math.min(6, Math.max(1, Math.ceil((v / (lanes[gid] || 1)) * 6)));
        h("td", { style: `background:var(--seq-${level});color:var(--seq-ink-${level})`,
          title: `${names[gid]} ${result.hours[i]}: ${v} of ${lanes[gid]} lanes` }, tr, v);
      });
    }
    const tr = h("tr", { class: "fc-total" }, body);
    h("th", { class: "fc-row-head", scope: "row" }, tr, "All gates");
    for (const t of totals) h("td", {}, tr, t);
  }

  window.FC = { renderForecast, renderGate, renderTradeoff, renderSchedule, uncertaintyLegend, fmt, fmt1, eur, pct, h, svg };
})();
