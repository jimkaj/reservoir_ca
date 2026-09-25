// Time-series chart: SVG, no dependencies. One y-axis, thin marks, hairline grid, crosshair +
// tooltip, legend for >= 2 series, and a data-table view. Series are drawn as a 2px line
// ("line") or as dots ("dots"); reference lines (e.g. capacity) are labeled hairlines.
(function () {
  "use strict";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const DAY = 86400000;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function el(tag, attrs, parent) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }
  function h(tag, cls, parent, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  const parseDate = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
  const fmtDate = (t) => {
    const d = new Date(t);
    return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
  };

  function niceStep(span, target) {
    const raw = span / target;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  }

  function monthTicks(t0, t1, width) {
    const spanMonths = (t1 - t0) / (30.44 * DAY);
    const maxTicks = Math.max(2, Math.floor(width / 70));
    const every = [1, 2, 3, 6, 12].find((m) => spanMonths / m <= maxTicks) || 24;
    const ticks = [];
    const d = new Date(t0);
    let y = d.getUTCFullYear();
    let m = d.getUTCMonth() + 1;
    for (;;) {
      if (m > 11) { y += Math.floor(m / 12); m %= 12; }
      const t = Date.UTC(y, m, 1);
      if (t > t1) break;
      if (m % every === 0) ticks.push(t);
      m += 1;
    }
    return ticks.map((t) => {
      const d2 = new Date(t);
      const label = d2.getUTCMonth() === 0 || every >= 12
        ? `${MONTHS[d2.getUTCMonth()]} ${d2.getUTCFullYear()}`
        : MONTHS[d2.getUTCMonth()];
      return { t, label };
    });
  }

  // options: {series: [{key, label, kind: "line"|"dots", cssVar, data: [[isoDate, value, note?]]}],
  //           refs: [{value, label, dashed?}], format: v => string, height, ariaLabel,
  //           legendOrder?: [series key, ...], toggleable?: bool}  -- series draw in array
  //           order (first = bottom); legendOrder only reorders the legend. toggleable turns each
  //           legend entry into an on/off button (all on initially); hidden elements drop out of
  //           the plot, the tooltip and the y-axis fit.
  function TimeChart(container, options) {
    this.container = container;
    this.opts = options;
    this.series = options.series.map((s) => ({
      ...s,
      points: s.data.map((d) => ({ t: parseDate(d[0]), v: d[1], note: d[2] || null, rejected: !!d[3], iso: d[0] })),
    }));
    this.range = null;
    this.hidden = new Set();  // series keys, "ref:<index>", "rejected"
    this.root = h("div", "chart", container);
    if (this.series.length >= 2 || (options.refs || []).length) this.buildLegend();
    this.plot = h("div", "chart-plot", this.root);
    this.tooltip = h("div", "chart-tooltip", this.plot);
    this.tooltip.setAttribute("role", "status");
    this.tooltip.hidden = true;
    this.buildTable();
    this.render = this.render.bind(this);
    new ResizeObserver(() => this.render()).observe(this.plot);
  }

  TimeChart.prototype.buildLegend = function () {
    const legend = h("div", "chart-legend", this.root);
    const toggleable = !!this.opts.toggleable;
    if (toggleable) {
      legend.setAttribute("role", "group");
      legend.setAttribute("aria-label", "Show or hide chart elements");
    }
    const item = (id, keyClass, label, keyColor) => {
      const node = h(toggleable ? "button" : "span", toggleable ? "legend-item legend-toggle" : "legend-item", legend);
      const key = h("span", `key ${keyClass}`, node);
      if (keyColor) key.style.setProperty("--key-color", keyColor);
      h("span", null, node, label);
      if (toggleable) {
        node.type = "button";
        node.setAttribute("aria-pressed", "true");
        node.addEventListener("click", () => {
          const on = this.hidden.has(id);
          if (on) this.hidden.delete(id); else this.hidden.add(id);
          node.setAttribute("aria-pressed", String(on));
          this.render();
        });
      }
    };
    const order = this.opts.legendOrder || [];
    const rank = (s) => (order.includes(s.key) ? order.indexOf(s.key) : order.length);
    for (const s of [...this.series].sort((a, b) => rank(a) - rank(b))) {
      item(s.key, `key-${s.kind}`, s.label, `var(${s.cssVar})`);
    }
    (this.opts.refs || []).forEach((r, i) => item(`ref:${i}`, r.dashed ? "key-ref key-ref-dashed" : "key-ref", r.label));
    if (this.series.some((s) => s.points.some((p) => p.rejected))) {
      item("rejected", "key-rejected", "Rejected (far from nearby observations)");
    }
  };

  TimeChart.prototype.buildTable = function () {
    const details = h("details", "chart-table", this.root);
    h("summary", null, details, "Show data table");
    details.addEventListener("toggle", () => {
      if (!details.open || details.querySelector("table")) return;
      const table = h("table", null, details);
      const head = h("tr", null, h("thead", null, table));
      h("th", null, head, "Date");
      for (const s of this.series) h("th", "num", head, s.label);
      const byDate = new Map();
      this.series.forEach((s, i) => {
        for (const p of s.points) {
          if (!byDate.has(p.iso)) byDate.set(p.iso, []);
          const row = byDate.get(p.iso);
          (row[i] = row[i] || []).push(p.v);
        }
      });
      const body = h("tbody", null, table);
      [...byDate.keys()].sort().reverse().forEach((iso) => {
        const tr = h("tr", null, body);
        h("td", null, tr, iso);
        this.series.forEach((s, i) => {
          const vals = byDate.get(iso)[i];
          h("td", "num", tr, vals ? vals.map(this.opts.format).join(", ") : "");
        });
      });
    });
  };

  TimeChart.prototype.setRange = function (days) {
    this.range = days;
    this.render();
  };

  TimeChart.prototype.domain = function () {
    let t1 = -Infinity;
    for (const s of this.series) for (const p of s.points) t1 = Math.max(t1, p.t);
    let t0 = Infinity;
    for (const s of this.series) for (const p of s.points) t0 = Math.min(t0, p.t);
    if (this.range) t0 = Math.max(t0, t1 - this.range * DAY);
    return [t0, t1];
  };

  TimeChart.prototype.render = function () {
    const width = this.plot.clientWidth;
    if (!width) return;
    const height = this.opts.height || 280;
    const m = { top: 12, right: 16, bottom: 28, left: 60 };
    const [t0, t1] = this.domain();
    const showRejected = !this.hidden.has("rejected");
    const visible = this.series.map((s) => (this.hidden.has(s.key) ? [] : s.points.filter(
      (p) => p.t >= t0 && p.t <= t1 && (showRejected || !p.rejected))));
    // Y-axis fitted tightly to the visible data (no forced zero). Rejected points don't widen it
    // -- one collapsed scene would otherwise stretch the axis -- and are pinned to the edge when
    // off-scale. A reference line (capacity) widens it only when it's within REF_REACH of the
    // data; further away it's marked at the top edge instead of drawn.
    const REF_REACH = 0.3;
    let vmin = Infinity, vmax = -Infinity;
    visible.forEach((pts) => pts.forEach((p) => {
      if (p.rejected) return;
      vmin = Math.min(vmin, p.v); vmax = Math.max(vmax, p.v);
    }));
    if (!isFinite(vmin)) { vmin = 0; vmax = 1; }
    const dataMax = vmax;
    const refs = (this.opts.refs || [])
      .filter((r, i) => !this.hidden.has(`ref:${i}`))
      .map((r) => ({ ...r, inRange: r.value <= dataMax * (1 + REF_REACH) }));
    this.activeRefs = refs;
    for (const r of refs) if (r.inRange) vmax = Math.max(vmax, r.value);
    const pad = (vmax - vmin) * 0.06 || Math.abs(vmax) * 0.05 || 1;
    vmin -= pad;
    vmax += pad;
    const step = niceStep(vmax - vmin, 5);
    const x = (t) => m.left + ((t - t0) / Math.max(t1 - t0, DAY)) * (width - m.left - m.right);
    const clamp = (v) => Math.min(vmax, Math.max(vmin, v));
    const y = (v) => m.top + (1 - (clamp(v) - vmin) / (vmax - vmin)) * (height - m.top - m.bottom);

    if (this.svg) this.svg.remove();
    const svg = el("svg", {
      width, height, viewBox: `0 0 ${width} ${height}`, class: "chart-svg", tabindex: "0",
      role: "img", "aria-label": this.opts.ariaLabel || "Time series chart",
    });
    this.plot.insertBefore(svg, this.tooltip);
    this.svg = svg;

    const grid = el("g", { class: "grid" }, svg);
    for (let v = Math.ceil(vmin / step) * step; v <= vmax; v += step) {
      el("line", { x1: m.left, x2: width - m.right, y1: y(v), y2: y(v), class: v === 0 ? "baseline" : "gridline" }, grid);
      el("text", { x: m.left - 8, y: y(v), class: "tick tick-y", "dominant-baseline": "middle", "text-anchor": "end" }, grid)
        .textContent = this.opts.format(v, true);
    }
    for (const tick of monthTicks(t0, t1, width - m.left - m.right)) {
      el("text", { x: x(tick.t), y: height - 8, class: "tick", "text-anchor": "middle" }, grid).textContent = tick.label;
    }

    for (const r of refs) {
      if (r.inRange) {
        el("line", { x1: m.left, x2: width - m.right, y1: y(r.value), y2: y(r.value), class: r.dashed ? "refline refline-dashed" : "refline" }, svg);
        el("text", { x: width - m.right, y: y(r.value) - 6, class: "reflabel", "text-anchor": "end" }, svg).textContent = r.label;
      } else {
        el("text", { x: width - m.right, y: m.top + 4, class: "reflabel", "text-anchor": "end", "dominant-baseline": "hanging" }, svg)
          .textContent = `${r.label} (above chart range)`;
      }
    }

    this.series.forEach((s, i) => {
      const pts = visible[i];
      const g = el("g", { class: `series series-${s.kind}` }, svg);
      g.style.setProperty("--series-color", `var(${s.cssVar})`);
      if (s.kind === "line") {
        if (pts.length) el("path", { d: pts.map((p, j) => `${j ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("") }, g);
      } else {
        for (const p of pts) {
          const dot = el("circle", { cx: x(p.t).toFixed(1), cy: y(p.v).toFixed(1), r: 4, class: p.rejected ? "rejected" : "" }, g);
          if (p.v < vmin || p.v > vmax) el("title", {}, dot).textContent = "Off scale (pinned to the edge); the tooltip shows its value";
        }
      }
    });

    const cross = el("line", { y1: m.top, y2: height - m.bottom, class: "crosshair", visibility: "hidden" }, svg);
    const allTimes = [...new Set(visible.flatMap((pts) => pts.map((p) => p.t)))].sort((a, b) => a - b);
    const show = (t) => {
      cross.setAttribute("x1", x(t)); cross.setAttribute("x2", x(t));
      cross.setAttribute("visibility", "visible");
      this.showTooltip(t, x(t), visible, width);
    };
    const hide = () => { cross.setAttribute("visibility", "hidden"); this.tooltip.hidden = true; };
    const nearestTime = (px) => {
      const t = t0 + ((px - m.left) / (width - m.left - m.right)) * (t1 - t0);
      let best = allTimes[0];
      for (const c of allTimes) if (Math.abs(c - t) < Math.abs(best - t)) best = c;
      return best;
    };
    svg.addEventListener("pointermove", (ev) => {
      if (!allTimes.length) return;
      const rect = svg.getBoundingClientRect();
      show(nearestTime(ev.clientX - rect.left));
    });
    svg.addEventListener("pointerleave", hide);
    let focusIndex = allTimes.length - 1;
    svg.addEventListener("focus", () => allTimes.length && show(allTimes[focusIndex]));
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      focusIndex = Math.min(allTimes.length - 1, Math.max(0, focusIndex + (ev.key === "ArrowRight" ? 1 : -1)));
      show(allTimes[focusIndex]);
    });
  };

  // One tooltip, every series: each series' value on the crosshair date, or its nearest
  // observation within 3 days (labeled with its own date).
  TimeChart.prototype.showTooltip = function (t, px, visible, width) {
    const tip = this.tooltip;
    tip.replaceChildren();
    h("div", "tip-date", tip, fmtDate(t));
    this.series.forEach((s, i) => {
      let best = null;
      for (const p of visible[i]) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
      if (!best || Math.abs(best.t - t) > 3 * DAY) return;
      const same = visible[i].filter((p) => p.t === best.t);
      const row = h("div", "tip-row", tip);
      h("span", `key key-${s.kind === "dots" ? "line" : s.kind}`, row).style.setProperty("--key-color", `var(${s.cssVar})`);
      h("strong", null, row, same.map((p) => this.opts.format(p.v)).join(" / "));
      const label = best.t === t ? s.label : `${s.label} (${fmtDate(best.t)})`;
      h("span", "tip-label", row, label);
      for (const p of same) if (p.note) h("div", "tip-note", tip, p.note);
    });
    for (const r of this.activeRefs || []) {
      const row = h("div", "tip-row", tip);
      h("span", "key key-ref", row);
      h("strong", null, row, this.opts.format(r.value));
      h("span", "tip-label", row, r.label.split(" ")[0]);
    }
    tip.hidden = false;
    const tipWidth = tip.offsetWidth;
    tip.style.left = `${px + 12 + tipWidth > width ? px - 12 - tipWidth : px + 12}px`;
  };

  // Compact acre-feet: 17.7M AF, 412K AF, 950 AF.
  function formatAF(v, axis) {
    const unit = axis ? "" : " AF";
    const a = Math.abs(v);
    if (a >= 1e6) return `${(v / 1e6).toFixed(a >= 1e7 || axis ? 1 : 2).replace(/\.0+$/, "")}M${unit}`;
    if (a >= 1e3) return `${Math.round(v / 1e3).toLocaleString("en-US")}K${unit}`;
    return `${Math.round(v).toLocaleString("en-US")}${unit}`;
  }
  function formatKm2(v, axis) {
    const km2 = v / 1e6;
    if (axis) return Number(km2.toPrecision(6)).toLocaleString("en-US");
    const s = km2 >= 100 ? Math.round(km2).toLocaleString("en-US") : km2.toFixed(1);
    return `${s} km²`;
  }

  // Range presets, one row above the charts they scope.
  function RangeControl(container, charts, presets, initial) {
    const row = h("div", "range-control", container);
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "Date range");
    const buttons = presets.map(([label, days]) => {
      const b = h("button", null, row, label);
      b.type = "button";
      b.addEventListener("click", () => select(days));
      return [b, days];
    });
    function select(days) {
      for (const [b, d] of buttons) b.setAttribute("aria-pressed", String(d === days));
      charts.forEach((c) => c.setRange(days));
    }
    select(initial);
  }

  window.ReservoirCharts = { TimeChart, RangeControl, formatAF, formatKm2, fmtDate, parseDate };
})();
