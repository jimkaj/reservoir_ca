// Reservoir page: image with AOI / water-mask overlays, volume chart with CDEC reference, area chart.
(function () {
  "use strict";
  const { TimeChart, RangeControl, formatAF, formatKm2 } = window.ReservoirCharts;
  const data = JSON.parse(document.getElementById("site-data").textContent);

  // --- Image + overlays ----------------------------------------------------------------
  const map = L.map("reservoir-map", { scrollWheelZoom: false, zoomSnap: 0.25 });
  const rep = data.representative;
  L.imageOverlay(rep.url, rep.bounds, { alt: `Satellite image of ${data.name}` }).addTo(map);
  map.fitBounds(rep.bounds);
  map.setMaxBounds(L.latLngBounds(rep.bounds).pad(0.25));
  map.attributionControl.addAttribution("Contains modified Copernicus Sentinel data");

  const overlays = {
    aoi: L.geoJSON(data.aoi, { style: { color: "#ffd400", weight: 2, fill: false }, interactive: false }),
    mask: data.mask ? L.imageOverlay(data.mask.url, data.mask.bounds, { opacity: 0.6, alt: "Latest water mask" }) : null,
  };
  document.querySelectorAll(".toggles button").forEach((button) => {
    const layer = overlays[button.dataset.layer];
    if (!layer) { button.disabled = true; return; }
    const set = (on) => {
      button.setAttribute("aria-pressed", String(on));
      if (on) layer.addTo(map); else layer.remove();
    };
    set(button.getAttribute("aria-pressed") === "true");
    button.addEventListener("click", () => set(button.getAttribute("aria-pressed") !== "true"));
  });

  // --- Charts --------------------------------------------------------------------------
  const note = (o) => {
    if (o.rejected) {
      const dir = o.deviation_pct < 0 ? "below" : "above";
      return `Rejected: area ${Math.abs(o.deviation_pct).toFixed(0)}% ${dir} nearby observations`;
    }
    return o.out_of_range ? "Outside the capacity curve's range" : null;
  };
  const bySensor = (sensor, field) => data.observations
    .filter((o) => o.sensor === sensor)
    .map((o) => [o.date, o[field], note(o), o.rejected]);

  const volumeSeries = [
    { key: "s1", label: "Sentinel-1 (radar)", kind: "dots", cssVar: "--series-s1", data: bySensor("S1", "volume_af") },
    { key: "s2", label: "Sentinel-2 (optical)", kind: "dots", cssVar: "--series-s2", data: bySensor("S2", "volume_af") },
  ];
  if (data.cdec.length) {
    volumeSeries.unshift({ key: "cdec", label: "CDEC reported storage (reference)", kind: "line",
                           cssVar: "--reference", data: data.cdec });
  }
  // Drawn last so it sits on top of the dots it summarizes.
  volumeSeries.push({ key: "estimate", label: "Computed volume", kind: "line",
                      cssVar: "--series-estimate", data: data.estimate });
  const volumeChart = new TimeChart(document.getElementById("volume-chart"), {
    series: volumeSeries,
    // Dashed (as on the home page) so it can't be mistaken for the solid grey CDEC line.
    refs: [{ value: data.capacity_af, label: `Capacity ${formatAF(data.capacity_af)}`, dashed: true }],
    legendOrder: ["s1", "s2", "estimate", "cdec"],
    format: formatAF, height: 320,
    ariaLabel: `Estimated volume of ${data.name} over time from Sentinel-1 and Sentinel-2, with CDEC reported storage and capacity`,
  });
  const areaChart = new TimeChart(document.getElementById("area-chart"), {
    series: [
      { key: "s1", label: "Sentinel-1 (radar)", kind: "dots", cssVar: "--series-s1", data: bySensor("S1", "area_m2") },
      { key: "s2", label: "Sentinel-2 (optical)", kind: "dots", cssVar: "--series-s2", data: bySensor("S2", "area_m2") },
    ],
    format: formatKm2, height: 190,
    ariaLabel: `Measured water surface area of ${data.name} over time`,
  });
  RangeControl(document.getElementById("range"), [volumeChart, areaChart],
               [["90 days", 90], ["1 year", 365], ["All", null]], 365);
})();
