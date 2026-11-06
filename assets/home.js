// Home page: fleet total charts, California map of reservoirs, reservoir table.
(function () {
  "use strict";
  const { TimeChart, RangeControl, formatAF, formatKm2 } = window.ReservoirCharts;
  const data = JSON.parse(document.getElementById("site-data").textContent);

  // --- Charts --------------------------------------------------------------------------
  const volumeChart = new TimeChart(document.getElementById("total-volume-chart"), {
    series: [
      { key: "average", label: "5-year average (CDEC)", kind: "line", dotted: true,
        cssVar: "--series-average", data: data.five_year_average },
      { key: "total", label: "Total volume", kind: "line", cssVar: "--series-total",
        data: data.totals.map((r) => [r[0], r[1]]) },
    ],
    legendOrder: ["total", "average"],
    refs: [{ value: data.total_capacity_af, label: `Combined capacity ${formatAF(data.total_capacity_af)}`, dashed: true }],
    format: formatAF, height: 300,
    ariaLabel: "Total volume of all tracked reservoirs over time, with combined capacity line",
  });
  const areaChart = new TimeChart(document.getElementById("total-area-chart"), {
    series: [{ key: "area", label: "Total water area", kind: "line", cssVar: "--series-total",
               data: data.totals.map((r) => [r[0], r[2]]) }],
    format: formatKm2, height: 170,
    ariaLabel: "Total water surface area of all tracked reservoirs over time",
  });
  RangeControl(document.getElementById("range"), [volumeChart, areaChart],
               [["90 days", 90], ["1 year", 365], ["All", null]], 365);

  // --- Map -----------------------------------------------------------------------------
  const CIRCLE = 26;
  const dark = () => getComputedStyle(document.documentElement).colorScheme === "dark";
  const map = L.map("map", { scrollWheelZoom: false, zoomSnap: 0.25 });
  // Esri gray canvas basemaps: no API key required.
  const canvas = dark() ? "World_Dark_Gray_Base" : "World_Light_Gray_Base";
  L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${canvas}/MapServer/tile/{z}/{y}/{x}`, {
    maxZoom: 16, attribution: "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
  }).addTo(map);

  function pieSvg(pct) {
    const r = CIRCLE / 2;
    const frac = Math.max(0, Math.min(1, pct / 100));
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", CIRCLE); svg.setAttribute("height", CIRCLE);
    svg.setAttribute("viewBox", `0 0 ${CIRCLE} ${CIRCLE}`);
    const track = document.createElementNS(ns, "circle");
    track.setAttribute("cx", r); track.setAttribute("cy", r); track.setAttribute("r", r - 1);
    track.setAttribute("class", "pie-track");
    svg.appendChild(track);
    if (frac > 0) {
      let fill;
      if (frac >= 0.999) {
        fill = document.createElementNS(ns, "circle");
        fill.setAttribute("cx", r); fill.setAttribute("cy", r); fill.setAttribute("r", r - 2);
      } else {
        const a = frac * 2 * Math.PI;
        const rr = r - 2;
        const x = r + rr * Math.sin(a), y = r - rr * Math.cos(a);
        fill = document.createElementNS(ns, "path");
        fill.setAttribute("d", `M${r},${r} L${r},${r - rr} A${rr},${rr} 0 ${frac > 0.5 ? 1 : 0} 1 ${x.toFixed(2)},${y.toFixed(2)} Z`);
      }
      fill.setAttribute("class", "pie-fill");
      svg.appendChild(fill);
    }
    return svg;
  }

  const markers = data.reservoirs.map((res) => {
    const node = document.createElement("div");
    node.className = "res-marker";
    node.appendChild(pieSvg(res.percent_full));
    const label = document.createElement("span");
    label.className = "res-label";
    label.textContent = formatAF(res.volume_af);
    node.appendChild(label);
    const marker = L.marker([res.lat, res.lon], {
      icon: L.divIcon({ html: node, className: "", iconSize: [CIRCLE, CIRCLE], iconAnchor: [CIRCLE / 2, CIRCLE / 2] }),
      title: `${res.name}: ${formatAF(res.volume_af)}, ${res.percent_full.toFixed(0)}% full`,
      keyboard: true, riseOnHover: true,
    }).addTo(map);
    const tip = document.createElement("div");
    tip.className = "map-tip";
    const name = document.createElement("strong"); name.textContent = res.name; tip.appendChild(name);
    for (const line of [
      `${formatAF(res.volume_af)} of ${formatAF(res.capacity_af)} (${res.percent_full.toFixed(0)}% full)`,
      `Last observed ${res.latest_date}`,
    ]) { tip.appendChild(document.createElement("br")); tip.appendChild(document.createTextNode(line)); }
    marker.bindTooltip(tip, { direction: "top", offset: [0, -CIRCLE / 2] });
    marker.on("click", () => { window.location.href = res.url; });
    marker.on("keypress", (ev) => { if (ev.originalEvent.key === "Enter") window.location.href = res.url; });
    return { res, marker, label };
  });

  // Label placement: greedy, largest reservoirs first; each label takes the first side
  // (right, left, above, below) that doesn't collide with an already-placed label or any circle,
  // and is hidden if none is free.
  function placeLabels() {
    const circles = markers.map((m) => {
      const p = map.latLngToContainerPoint(m.marker.getLatLng());
      return { x0: p.x - CIRCLE / 2, y0: p.y - CIRCLE / 2, x1: p.x + CIRCLE / 2, y1: p.y + CIRCLE / 2 };
    });
    const placed = [];
    const hit = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
    const order = [...markers.keys()].sort((a, b) => markers[b].res.capacity_af - markers[a].res.capacity_af);
    for (const i of order) {
      const { marker, label } = markers[i];
      const p = map.latLngToContainerPoint(marker.getLatLng());
      const w = label.offsetWidth || 50, hgt = label.offsetHeight || 12, gap = 4, r = CIRCLE / 2;
      const options = [
        [r + gap, -hgt / 2], [-r - gap - w, -hgt / 2], [-w / 2, -r - gap - hgt], [-w / 2, r + gap],
      ];
      let chosen = null;
      for (const [dx, dy] of options) {
        const box = { x0: p.x + dx, y0: p.y + dy, x1: p.x + dx + w, y1: p.y + dy + hgt };
        if (!placed.some((b) => hit(box, b)) && !circles.some((c, j) => j !== i && hit(box, c))) {
          chosen = [dx, dy, box]; break;
        }
      }
      // No free side at this zoom: hide the label rather than overprint a neighbour's. The value
      // stays in the marker's tooltip and the table below, and zooming in brings the label back.
      label.style.visibility = chosen ? "visible" : "hidden";
      if (!chosen) continue;
      placed.push(chosen[2]);
      label.style.left = `${r + chosen[0]}px`;
      label.style.top = `${r + chosen[1]}px`;
    }
  }
  map.fitBounds(L.latLngBounds(data.reservoirs.map((r) => [r.lat, r.lon])).pad(0.08));
  map.on("zoomend moveend", placeLabels);
  requestAnimationFrame(placeLabels);

  // --- Reservoir table -----------------------------------------------------------------
  const body = document.querySelector("#reservoir-table tbody");
  [...data.reservoirs].sort((a, b) => b.capacity_af - a.capacity_af).forEach((res) => {
    const tr = document.createElement("tr");
    const nameCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = res.url; link.textContent = res.name;
    nameCell.appendChild(link);
    tr.appendChild(nameCell);
    for (const [text, cls] of [
      [formatAF(res.volume_af), "num"], [`${res.percent_full.toFixed(0)}%`, "num"],
      [formatAF(res.capacity_af), "num"], [res.latest_date, ""],
    ]) {
      const td = document.createElement("td");
      td.className = cls; td.textContent = text; tr.appendChild(td);
    }
    body.appendChild(tr);
  });
})();
