"""Stage 3 imagery: each reservoir's representative image (generated once, kept on file) and its
latest water mask (regenerated whenever a newer scene is processed), per
planning/ProjectPlan.docx Stage 3.

Both are rendered by Earth Engine's thumbnail endpoint over the *same* region, projection
(EPSG:3857, what Leaflet displays in) and pixel dimensions, so they overlay pixel-for-pixel and
drop straight into a Leaflet imageOverlay with the region's lat/lon bounds. These small PNG/JPEG
renders are the only imagery this project ever downloads -- measurement itself stays server-side.

The mask is rebuilt from the ledger row of the scene behind the reservoir's current value (its
latest observation that passed Stage 2's consistency check) through the
same Stage 1 classification functions that produced its area (stage1_query_measure.
sar_water_mask / optical_water_mask), and its area is recomputed and compared with the ledger's
area_m2 as a check that the right scene and threshold were reconstructed.
"""

from __future__ import annotations

import datetime as _dt
import json
import urllib.request
from pathlib import Path

import ee
import pandas as pd

from reservoir_ca import stage1_query_measure as s1qm
from reservoir_ca import stage3_aggregate as agg
from reservoir_ca.config import RESERVOIRS_DIR, Reservoir

SITE_ASSETS_DIR = RESERVOIRS_DIR / "site_assets"
# Committed: static, generated once per reservoir.
REPRESENTATIVE_DIR = SITE_ASSETS_DIR / "representative"
REPRESENTATIVE_INDEX = REPRESENTATIVE_DIR / "index.json"
# Not committed (.gitignore): replaced every time a newer scene comes in; only the published
# site branch carries them -- see publish_site.py.
MASKS_DIR = SITE_ASSETS_DIR / "masks"
MASKS_INDEX = MASKS_DIR / "index.json"

THUMB_CRS = "EPSG:3857"
THUMB_MAX_DIMENSION = 1024
# Fraction of the AOI's bounding box added on every side, so the shoreline isn't flush with the
# image edge.
REGION_PADDING = 0.08
# Representative image: median of the clearest Sentinel-2 scenes over the last year. A median
# composite rather than one scene so no single day's cloud, smoke or unusual water level defines
# how the reservoir "looks".
REPRESENTATIVE_LOOKBACK_DAYS = 365
REPRESENTATIVE_MAX_CLOUDY_PERCENT = 10
REPRESENTATIVE_VIS = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 2500, "gamma": 1.2}
MASK_COLOR = "00E5FF"
# The mask's recomputed area must match the ledger to this relative tolerance. Both use the same
# reducer with bestEffort=True, so an exact match is expected; the tolerance only absorbs float
# noise.
MASK_AREA_TOLERANCE = 1e-3


def thumbnail_region(reservoir: Reservoir) -> tuple[ee.Geometry, list[list[float]]]:
    """The padded lon/lat bounding box every thumbnail for this reservoir is rendered over, as
    (ee.Geometry, Leaflet bounds [[south, west], [north, east]]).

    Planar (non-geodesic) so its edges are lines of constant lat/lon -- the rectangle that maps
    exactly onto Leaflet's lat/lon image bounds.
    """
    coords = _all_coordinates(reservoir.aoi_geometry())
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    west, east, south, north = min(lons), max(lons), min(lats), max(lats)
    pad_x = (east - west) * REGION_PADDING
    pad_y = (north - south) * REGION_PADDING
    west, east, south, north = west - pad_x, east + pad_x, south - pad_y, north + pad_y
    region = ee.Geometry.Rectangle([west, south, east, north], "EPSG:4326", False)
    return region, [[south, west], [north, east]]


def _all_coordinates(geometry: dict) -> list[list[float]]:
    if geometry["type"] == "Polygon":
        return [c for ring in geometry["coordinates"] for c in ring]
    if geometry["type"] == "MultiPolygon":
        return [c for poly in geometry["coordinates"] for ring in poly for c in ring]
    raise ValueError(f"Unsupported AOI geometry type {geometry['type']!r}")


def _download_thumbnail(image: ee.Image, region: ee.Geometry, fmt: str, path: Path) -> None:
    url = image.getThumbURL(
        {
            "region": region,
            "dimensions": THUMB_MAX_DIMENSION,
            "crs": THUMB_CRS,
            "format": fmt,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as response:
        path.write_bytes(response.read())


def _load_index(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_index(path: Path, index: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, sort_keys=True))


def representative_path(cdec_station_id: str) -> Path:
    return REPRESENTATIVE_DIR / f"{cdec_station_id}.jpg"


def mask_path(cdec_station_id: str) -> Path:
    return MASKS_DIR / f"{cdec_station_id}.png"


def generate_representative_image(reservoir: Reservoir, until_date: str | None = None) -> dict:
    """Render and save the reservoir's representative image; returns its index entry."""
    until = _dt.date.fromisoformat(until_date) if until_date else _dt.date.today()
    since = until - _dt.timedelta(days=REPRESENTATIVE_LOOKBACK_DAYS)
    region, bounds = thumbnail_region(reservoir)
    scenes = (
        ee.ImageCollection(s1qm.S2_COLLECTION)
        .filterBounds(region)
        .filterDate(since.isoformat(), until.isoformat())
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", REPRESENTATIVE_MAX_CLOUDY_PERCENT))
    )
    n_scenes = scenes.size().getInfo()
    if n_scenes == 0:
        raise RuntimeError(f"{reservoir.cdec_station_id}: no clear Sentinel-2 scenes to composite")
    composite = scenes.median().visualize(**REPRESENTATIVE_VIS)
    _download_thumbnail(composite, region, "jpg", representative_path(reservoir.cdec_station_id))
    return {
        "bounds": bounds,
        "source": f"Sentinel-2 median composite, {n_scenes} clear scenes",
        "from": since.isoformat(),
        "to": until.isoformat(),
    }


def ensure_representative_images(reservoirs: list[Reservoir], regenerate: bool = False) -> dict:
    """Generate representative images for any reservoir that doesn't have one yet (all of them
    when `regenerate`). Returns the full index, {station_id: entry}."""
    index = _load_index(REPRESENTATIVE_INDEX)
    for reservoir in reservoirs:
        sid = reservoir.cdec_station_id
        if not regenerate and sid in index and representative_path(sid).exists():
            continue
        index[sid] = generate_representative_image(reservoir)
        _save_index(REPRESENTATIVE_INDEX, index)
        print(f"{sid}: representative image saved ({index[sid]['source']})")
    return index


def latest_measured_scene(reservoir: Reservoir, processed: pd.DataFrame) -> dict | None:
    """The ledger row for the scene behind the reservoir's current value: its latest *usable*
    observation (not rejected by Stage 2's consistency check; S2 preferred on a shared date --
    see stage3_aggregate.usable), so the mask always shows the measurement the page reports.
    """
    timeseries = agg.load_timeseries(reservoir)
    if timeseries is None:
        return None
    kept = agg.usable(timeseries)
    if kept.empty:
        return None
    chosen = kept[kept["date"] == kept["date"].max()].iloc[-1]
    rows = processed[
        (processed["cdec_station_id"] == reservoir.cdec_station_id)
        & (processed["sensor"] == chosen["sensor"])
        & (processed["scene_id"] == chosen["scene_id"])
    ]
    return rows.iloc[-1].to_dict() if not rows.empty else None


def _water_mask_for_scene(scene: dict) -> ee.Image:
    """Rebuild the exact Stage 1 water classification for one ledger row."""
    if scene["sensor"] == "S1":
        image = ee.Image(f"{s1qm.S1_COLLECTION}/{scene['scene_id']}")
        return s1qm.sar_water_mask(image, scene["band"], float(scene["threshold_db"]))
    tile_ids = sorted(scene["scene_id"].split("+"))
    image = s1qm.build_s2_mosaic(tile_ids)
    cloud_prob = s1qm._cloud_probability_image(tile_ids)
    if cloud_prob is None:
        raise RuntimeError(f"no s2cloudless image for {scene['scene_id']}")
    return s1qm.optical_water_mask(image, cloud_prob)


def generate_water_mask(reservoir: Reservoir, scene: dict) -> dict:
    """Render and save the water mask for `scene` (a ledger row); returns its index entry.

    Raises if the mask's recomputed area disagrees with the ledger -- a wrong mask must never be
    published silently.
    """
    aoi = ee.Geometry(reservoir.aoi_geometry())
    water = _water_mask_for_scene(scene)
    area_m2 = s1qm.water_area_m2(water, aoi).getInfo()
    ledger_area = float(scene["area_m2"])
    if abs(area_m2 - ledger_area) > MASK_AREA_TOLERANCE * ledger_area:
        raise RuntimeError(
            f"{reservoir.cdec_station_id}: rebuilt mask area {area_m2:.0f} m^2 != ledger "
            f"{ledger_area:.0f} m^2 for {scene['sensor']} {scene['scene_id']}"
        )
    region, bounds = thumbnail_region(reservoir)
    visual = water.clip(aoi).selfMask().visualize(palette=[MASK_COLOR])
    _download_thumbnail(visual, region, "png", mask_path(reservoir.cdec_station_id))
    return {
        "bounds": bounds,
        "sensor": scene["sensor"],
        "scene_id": scene["scene_id"],
        "date": scene["scene_date"],
        "area_m2": ledger_area,
    }


def update_water_masks(
    reservoirs: list[Reservoir], processed: pd.DataFrame, force: bool = False
) -> dict:
    """Regenerate the water mask for every reservoir whose latest measured scene differs from
    the one its current mask was built from (all of them when `force`). One reservoir's failure
    is reported and leaves its previous mask in place rather than aborting the rest. Returns the
    full index, {station_id: entry}."""
    index = _load_index(MASKS_INDEX)
    for reservoir in reservoirs:
        sid = reservoir.cdec_station_id
        scene = latest_measured_scene(reservoir, processed)
        if scene is None:
            continue
        current = index.get(sid)
        if (
            not force
            and current is not None
            and current["scene_id"] == scene["scene_id"]
            and current["sensor"] == scene["sensor"]
            and mask_path(sid).exists()
        ):
            continue
        try:
            index[sid] = generate_water_mask(reservoir, scene)
        except Exception as exc:  # keep going; the old mask (if any) stays published
            print(f"{sid}: water mask FAILED ({exc})")
            continue
        _save_index(MASKS_INDEX, index)
        print(f"{sid}: water mask saved ({scene['sensor']} {scene['scene_date']})")
    return index
