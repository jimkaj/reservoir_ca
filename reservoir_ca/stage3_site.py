"""Stage 3: build the static reporting website, per planning/ProjectPlan.docx Stage 3.

Writes a self-contained site directory: a home page (fleet total volume/area charts, a map of
California with a %-full circle per reservoir, a reservoir table) and one page per monitored
reservoir (representative image with AOI / latest-water-mask overlays, volume chart with the CDEC
reference line, area chart). Each page's data is embedded as JSON in the page itself, so the site
works from any static host -- or opened straight from disk -- with no server and no fetches of
our own. Leaflet (cdnjs) and Esri basemap tiles are the only external resources.

Inputs are all local: Stage 2's time series, the imagery produced by stage3_imagery.py, the
capacity curves' fit metadata, and CDEC's reported storage (fetched here, one request per
reservoir). No Earth Engine calls.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import shutil
from pathlib import Path
from string import Template

import numpy as np
import pandas as pd

from reservoir_ca import capacity_curve as cc
from reservoir_ca import stage3_aggregate as agg
from reservoir_ca import stage3_imagery as im
from reservoir_ca.config import RESERVOIR_LIST_CSV, REPO_ROOT, Reservoir

TEMPLATE_DIR = Path(__file__).resolve().parent / "site_template"
DEFAULT_SITE_DIR = REPO_ROOT / "site"
REPO_URL = "https://github.com/jimkaj/reservoir_ca"
LEAFLET_CSS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"
LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"
CDEC_COMPARISON_DAYS = 90
# The AOI outline drawn on a reservoir page is a display copy of the measurement polygon, simplified
# (Douglas-Peucker) and rounded -- the OSM shorelines carry up to ~57k vertices (SHA: 5.7 MB),
# far below one pixel of the 1024 px image. 0.00005 deg is ~5 m; rounding to 5 decimals ~1 m.
AOI_DISPLAY_TOLERANCE_DEG = 0.00005
AOI_DISPLAY_DECIMALS = 5


ACRONYMS = {"USBR", "USACE", "DWR", "PG&E"}
WORD_OVERRIDES = {"LK": "Lake"}


def display_name(raw: str) -> str:
    """CDEC's all-caps names ("SHASTA DAM  (USBR)", "NEW EXCHEQUER-LK MCCLURE") in title case
    for display, keeping agency acronyms uppercase."""
    def word(match: re.Match) -> str:
        w = match.group(0)
        if w in ACRONYMS:
            return w
        if w in WORD_OVERRIDES:
            return WORD_OVERRIDES[w]
        if w.startswith("MC") and len(w) > 3:
            return "Mc" + w[2:].capitalize()
        return w.capitalize()
    return re.sub(r"[A-Z&]+", word, " ".join(raw.split()))


def _simplify_ring(ring: list[list[float]], tolerance: float) -> list[list[float]]:
    """Douglas-Peucker on one closed ring (iterative, so 50k-vertex rings don't hit recursion
    limits). Keeps the ring closed; returns [] if it collapses below a valid ring."""
    points = np.asarray(ring, dtype=float)
    if len(points) < 4:
        return []
    keep = np.zeros(len(points), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        a, b = points[start], points[end]
        segment = points[start + 1 : end]
        ab = b - a
        length = np.hypot(*ab)
        if length == 0:
            distances = np.hypot(*(segment - a).T)
        else:
            distances = np.abs(ab[0] * (segment[:, 1] - a[1]) - ab[1] * (segment[:, 0] - a[0])) / length
        index = int(np.argmax(distances))
        if distances[index] > tolerance:
            split = start + 1 + index
            keep[split] = True
            stack.append((start, split))
            stack.append((split, end))
    simplified = np.round(points[keep], AOI_DISPLAY_DECIMALS)
    return simplified.tolist() if len(simplified) >= 4 else []


def display_geometry(geometry: dict) -> dict:
    """A lightweight copy of an AOI polygon for drawing (see AOI_DISPLAY_TOLERANCE_DEG)."""
    def polygon(rings):
        simplified = [_simplify_ring(r, AOI_DISPLAY_TOLERANCE_DEG) for r in rings]
        if not simplified or not simplified[0]:
            return None
        return [simplified[0]] + [r for r in simplified[1:] if r]
    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": polygon(geometry["coordinates"])}
    polygons = [p for p in (polygon(rings) for rings in geometry["coordinates"]) if p]
    return {"type": "MultiPolygon", "coordinates": polygons}


def _json_for_script(payload: dict) -> str:
    """JSON safe to embed in a <script type="application/json"> block."""
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


def _round(value: float, digits: int = 0) -> float:
    return round(float(value), digits)


def _cdec_comparison(observations: pd.DataFrame, cdec: pd.DataFrame, fit_through: str | None) -> dict:
    """Median absolute % difference between satellite estimates and same-day CDEC storage over
    the last CDEC_COMPARISON_DAYS days, plus how many of those observations post-date the
    capacity curve's fit (the genuinely out-of-sample ones)."""
    if cdec.empty:
        return {"n": 0}
    latest = pd.Timestamp(observations["date"].max())
    since = (latest - pd.Timedelta(days=CDEC_COMPARISON_DAYS)).strftime("%Y-%m-%d")
    recent = observations[observations["date"] > since].merge(cdec, on="date", how="inner")
    if recent.empty:
        return {"n": 0}
    pct = (recent["volume_af"] - recent["storage_af"]).abs() / recent["storage_af"] * 100
    after_fit = recent[recent["date"] > fit_through] if fit_through else recent.iloc[0:0]
    return {
        "n": int(len(recent)),
        "median_abs_pct": _round(pct.median(), 1),
        "n_after_fit": int(len(after_fit)),
    }


def _format_af(value: float) -> str:
    if value >= 1e6:
        return f"{value / 1e6:.2f}M AF"
    if value >= 1e3:
        return f"{value / 1e3:,.0f}K AF"
    return f"{value:,.0f} AF"


def _page(template_name: str, **fields: str) -> str:
    template = Template((TEMPLATE_DIR / template_name).read_text(encoding="utf-8"))
    return template.substitute(leaflet_css=LEAFLET_CSS, leaflet_js=LEAFLET_JS, repo_url=REPO_URL, **fields)


def build_site(
    reservoirs: list[Reservoir],
    site_dir: Path = DEFAULT_SITE_DIR,
    today: str | None = None,
) -> dict:
    """Write the full site into `site_dir` (replaced wholesale). Returns a small summary."""
    today = today or _dt.date.today().isoformat()
    generated_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    reservoir_csv = pd.read_csv(RESERVOIR_LIST_CSV).set_index("cdec_station_id")
    fit_metadata = cc.load_fit_metadata().set_index("cdec_station_id")
    representative_index = json.loads(im.REPRESENTATIVE_INDEX.read_text()) if im.REPRESENTATIVE_INDEX.exists() else {}
    masks_index = json.loads(im.MASKS_INDEX.read_text()) if im.MASKS_INDEX.exists() else {}

    timeseries = {}
    for reservoir in reservoirs:
        ts = agg.load_timeseries(reservoir)
        if ts is not None:
            timeseries[reservoir.cdec_station_id] = (reservoir, ts)
    if not timeseries:
        raise RuntimeError("No reservoir has a Stage 2 time series -- run run_stage2.py first")

    if site_dir.exists():
        shutil.rmtree(site_dir)
    (site_dir / "reservoirs").mkdir(parents=True)
    shutil.copytree(TEMPLATE_DIR / "assets", site_dir / "assets")
    (site_dir / ".nojekyll").write_text("")

    home_reservoirs = []
    missing_imagery = []
    for sid, (reservoir, ts) in timeseries.items():
        status = agg.reservoir_status(reservoir, ts)
        name = display_name(reservoir.name)
        meta = reservoir_csv.loc[sid]
        first_date = ts["date"].min()
        cdec = cc.fetch_cdec_storage(sid, first_date, today)
        fit = fit_metadata.loc[sid].to_dict() if sid in fit_metadata.index else None
        comparison = _cdec_comparison(agg.usable(ts), cdec, fit["fit_through"] if fit else None)

        rep = representative_index.get(sid)
        mask = masks_index.get(sid)
        if rep is None or not im.representative_path(sid).exists():
            missing_imagery.append(sid)
            continue
        (site_dir / "images" / "representative").mkdir(parents=True, exist_ok=True)
        shutil.copy(im.representative_path(sid), site_dir / "images" / "representative" / f"{sid}.jpg")
        mask_payload = None
        if mask is not None and im.mask_path(sid).exists():
            (site_dir / "images" / "masks").mkdir(parents=True, exist_ok=True)
            shutil.copy(im.mask_path(sid), site_dir / "images" / "masks" / f"{sid}.png")
            mask_payload = {
                "url": f"../images/masks/{sid}.png?v={mask['date']}",
                "bounds": mask["bounds"],
            }

        page_data = {
            "name": name,
            "capacity_af": int(reservoir.capacity_af),
            "observations": [
                {
                    "date": row.date,
                    "sensor": row.sensor,
                    "volume_af": _round(row.volume_af),
                    "area_m2": _round(row.area_m2),
                    "out_of_range": bool(row.out_of_range),
                    "rejected": row.qc_status == "rejected",
                    "deviation_pct": None if pd.isna(row.qc_deviation_pct) else _round(row.qc_deviation_pct, 1),
                }
                for row in ts.itertuples(index=False)
            ],
            "cdec": [[row.date, _round(row.storage_af)] for row in cdec.itertuples(index=False)],
            "representative": {"url": f"../images/representative/{sid}.jpg", "bounds": rep["bounds"]},
            "mask": mask_payload,
            "aoi": display_geometry(reservoir.aoi_geometry()),
        }

        sensor_names = {"S1": "Sentinel-1 radar", "S2": "Sentinel-2 optical"}
        mask_caption = (
            f"Latest water mask: {sensor_names[mask['sensor']]}, {mask['date']} "
            f"({mask['area_m2'] / 1e6:,.1f} km² of water)."
            if mask_payload else "No water mask available yet."
        )
        if comparison["n"]:
            cdec_sentence = (
                f"Over the last {CDEC_COMPARISON_DAYS} days of observations, satellite estimates "
                f"differ from CDEC's reported storage by a median of "
                f"{comparison['median_abs_pct']}% ({comparison['n']} observations)."
            )
        else:
            cdec_sentence = "No same-day CDEC storage was available for recent observations."
        if fit:
            n_after = comparison.get("n_after_fit", 0)
            fit_sentence = (
                f"This reservoir's area-to-volume curve was fit to CDEC storage from "
                f"{fit['fit_from']} to {fit['fit_through']}, so agreement over that period partly "
                f"reflects the fit itself. Observations after {fit['fit_through']} are an "
                f"independent comparison"
                + (f" ({n_after} so far in the last {CDEC_COMPARISON_DAYS} days)." if n_after
                   else "; none have been made yet.")
            )
        else:
            fit_sentence = ""

        sensors = " and ".join(sensor_names[s] for s in status.latest_sensors)
        operator = meta.get("operator_agency")
        county = meta.get("county")
        subtitle_parts = [f"CDEC station {sid}"]
        if isinstance(county, str) and county:
            subtitle_parts.append(f"{display_name(county)} County")
        if isinstance(operator, str) and operator:
            subtitle_parts.append(operator)

        page = _page(
            "reservoir.html",
            title=html.escape(name),
            subtitle=html.escape(" · ".join(subtitle_parts)),
            volume=html.escape(_format_af(status.volume_af)),
            percent_full=f"{status.percent_full:.0f}%",
            capacity=html.escape(_format_af(status.capacity_af)),
            latest_date=status.latest_date,
            latest_sensors=html.escape(sensors),
            rep_caption=html.escape(
                f"Image: {rep['source']}, {rep['from']} to {rep['to']}."
            ),
            mask_caption=html.escape(mask_caption),
            cdec_note=html.escape(f"{cdec_sentence} {fit_sentence}".strip()),
            generated_at=generated_at,
            data=_json_for_script(page_data),
        )
        (site_dir / "reservoirs" / f"{sid}.html").write_text(page, encoding="utf-8")

        home_reservoirs.append(
            {
                "id": sid,
                "name": name,
                "lat": reservoir.dam_lat,
                "lon": reservoir.dam_lon,
                "capacity_af": int(reservoir.capacity_af),
                "volume_af": _round(status.volume_af),
                "percent_full": _round(status.percent_full, 1),
                "latest_date": status.latest_date,
                "url": f"reservoirs/{sid}.html",
            }
        )

    if missing_imagery:
        raise RuntimeError(
            f"No representative image for {', '.join(missing_imagery)} -- run build_site.py "
            "with Earth Engine access (not --skip-imagery) first"
        )

    included = {r["id"] for r in home_reservoirs}
    totals = agg.fleet_daily_totals(
        {sid: ts for sid, (_, ts) in timeseries.items() if sid in included}, until_date=today
    )
    total_capacity = sum(r["capacity_af"] for r in home_reservoirs)
    total_volume = float(totals["volume_af"].iloc[-1])
    latest_observation = max(r["latest_date"] for r in home_reservoirs)
    home_data = {
        "totals": [
            [row.date, _round(row.volume_af), _round(row.area_m2)]
            for row in totals.itertuples(index=False)
        ],
        "total_capacity_af": total_capacity,
        "reservoirs": home_reservoirs,
    }
    page = _page(
        "index.html",
        total_volume=html.escape(_format_af(total_volume)),
        percent_full=f"{100 * total_volume / total_capacity:.0f}%",
        total_capacity=html.escape(_format_af(total_capacity)),
        n_reservoirs=str(len(home_reservoirs)),
        latest_observation=latest_observation,
        totals_start=totals["date"].iloc[0],
        generated_at=generated_at,
        data=_json_for_script(home_data),
    )
    (site_dir / "index.html").write_text(page, encoding="utf-8")

    return {
        "site_dir": str(site_dir),
        "n_reservoirs": len(home_reservoirs),
        "total_volume_af": total_volume,
        "total_capacity_af": total_capacity,
    }
