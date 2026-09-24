"""Stage 1: Query & Measure, per planning/ProjectPlan.docx.

NOTE: this module has not yet been run against live Earth Engine (no GEE project/credentials
are configured in this environment). The Otsu and NDWI logic follows the standard, published
techniques referenced in the plan, but treat it as unverified until it's been exercised against
real imagery and the results spot-checked.
"""

from __future__ import annotations

import datetime as _dt

import ee

from reservoir_ca.config import Reservoir

S1_COLLECTION = "COPERNICUS/S1_GRD"
S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_CLOUD_PROBABILITY_COLLECTION = "COPERNICUS/S2_CLOUD_PROBABILITY"
# MODIS MAIAC aerosol optical depth -- a direct measurement of atmospheric haziness (smoke,
# dust, pollution), unlike s2cloudless (S2_CLOUD_PROBABILITY_COLLECTION above), which is trained
# specifically to detect cloud and often misses smoke: smoke has a different spectral signature
# than water/ice cloud droplets, and Sentinel-2's own Scene Classification Layer has no smoke
# class either. See reservoir_ca/haze_screening.py, built after DON's SAR-vs-optical
# disagreement investigation (2026-09-16) turned up several dates where SAR stayed steady while
# optical area collapsed to near-zero -- the same signature already found for Hetch Hetchy and
# the 2026-08-08/09-07/09-12 smoke-date cluster, none of which the cloud screen caught.
AOD_COLLECTION = "MODIS/061/MCD19A2_GRANULES"
AOD_BAND = "Optical_Depth_055"
AOD_SCALE_FACTOR = 0.001  # MCD19A2's AOD bands are stored as AOD * 1000

# Sentinel-2's own Scene Classification Layer (SCL), part of S2_SR_HARMONIZED -- catches thin
# cirrus that s2cloudless (S2_CLOUD_PROBABILITY_COLLECTION above) misses. Confirmed directly
# 2026-09-17 while investigating DON's SAR-vs-optical disagreement: on a date where optical area
# collapsed to near-zero while SAR stayed steady, cloud probability over the water was only ~8%
# (well under the 40% gate) and AOD was low/normal too, yet SCL classified more of the AOI as
# THIN_CIRRUS than as WATER -- a ~79x jump from a clean date's negligible cirrus count. s2cloudless
# works from surface-reflectance bands alone; SCL's cirrus flag comes from Sen2Cor using the
# dedicated cirrus-detection band (B10) at L1C, before it's dropped from the surface-reflectance
# product -- which is exactly why the two disagree on thin cirrus specifically. AOD's separate
# haze screen stays in place for genuine smoke/aerosol events; this catches a different failure
# mode neither it nor the cloud-probability gate can see.
SCL_BAND = "SCL"
# THIN_CIRRUS(10), CLOUD_SHADOWS(3), CLOUD_MEDIUM_PROBABILITY(8), CLOUD_HIGH_PROBABILITY(9),
# SNOW(11), SATURATED_OR_DEFECTIVE(1), NO_DATA(0). Excludes DARK_AREA_PIXELS(2) deliberately --
# too broad a class (ordinary terrain shadow routinely falls in it) to treat as contamination.
SCL_BAD_CLASSES = [0, 1, 3, 8, 9, 10, 11]

DEFAULT_CLOUD_PROB_THRESHOLD = 40
DEFAULT_MAX_CLOUD_FRACTION = 0.2
# >0.4 is a widely-used "significant haze/smoke" cutoff for 550nm AOD (background is usually
# 0.05-0.15 in rural California); 0.3 catches moderate-and-up haze while leaving typical
# background days alone. Tunable -- see planning discussion, not derived from this project's
# own data yet.
DEFAULT_MAX_AOD = 0.3
# The confirmed DON crash date had ~35% of its AOI in a bad SCL class vs. ~0.4% on a clean date
# -- wide margin either side of 0.1, chosen to reject clearly-contaminated scenes without being
# sensitive to a few stray misclassified pixels.
DEFAULT_MAX_SCL_BAD_FRACTION = 0.1


def _ts_to_date(ts_millis: float) -> str:
    return _dt.datetime.fromtimestamp(ts_millis / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


# A real scene footprint should never come close to this -- ~100,000 km^2, generous margin over
# the largest legitimate footprint in this pipeline (a Sentinel-1 IW swath, ~42,500 km^2; a
# Sentinel-2 tile is ~12,000 km^2), but far below a degenerate/global one (~5.1e14 m^2, the whole
# Earth). Guards against a real GEE catalog defect found during FOL's backfill (2026-09-22/23):
# scene 20260921T081631_20260921T082016_T39WXT's system:footprint was literally
# [-Infinity,-Infinity] to [Infinity,Infinity], so `.geometry().contains(aoi, ...)` returned True
# against ANY AOI on Earth -- it passed FOL's (California) coverage check despite its nominal
# MGRS tile being in Kazakhstan. Harmless in that instance (caught downstream by the haze gate,
# logged as "skipped_hazy" -- a misleading reason, but no bad area_m2 reached the ledger), but the
# coverage check itself shouldn't have passed it.
MAX_PLAUSIBLE_FOOTPRINT_M2 = 1e11


def filter_plausible_footprint(collection: ee.ImageCollection) -> ee.ImageCollection:
    """Drop scenes whose footprint area exceeds MAX_PLAUSIBLE_FOOTPRINT_M2 -- see its comment.

    Folded into the same server-side map/filter every caller already runs its own coverage check
    through, so this is one extra area() computation per scene, not an extra round trip.
    """
    return collection.map(
        lambda img: img.set("_footprint_area_m2", img.geometry().area(1))
    ).filter(ee.Filter.lt("_footprint_area_m2", MAX_PLAUSIBLE_FOOTPRINT_M2))


def s2_covering_groups(s2_collection: ee.ImageCollection, aoi: ee.Geometry) -> list[dict]:
    """Group `s2_collection`'s scenes (already filterBounds/filterDate'd to a reservoir's AOI and
    a date window) by acquisition date, and return the ones whose combined footprint fully covers
    the AOI: either one tile (the common case) or several same-date tiles unioned together, for
    an AOI that straddles an MGRS tile boundary closely enough that no single S2 scene ever
    contains it alone.

    Motivated by FOL (Folsom Lake): confirmed live 2026-09-22 that FOL's AOI splits close to
    50/50 across two adjacent tiles (10SFH/10SFJ), so only 1 of 347 candidate S2 scenes over a
    2-year window individually covered it, vs. a normal ~1/3 for Sentinel-1's much wider swath --
    a near-total loss of optical coverage for that one reservoir. A same-day fleet-wide check
    found this isn't a widespread problem (27/48 reservoirs span 2+ tiles, but every other one
    sits mostly within a single tile with the AOI crossing just a corner into the neighbor) --
    see reservoir_ca_status memory, 2026-09-22 entries.

    Only pays the extra per-date EE round trip for dates that actually need it: any date where a
    single tile already covers the AOI is resolved by one batched contains-check, same cost as
    before this function existed.

    Returns [{"scene_ids": [...], "date": iso_date}, ...] -- `scene_ids` sorted for a stable,
    deterministic group identity (see find_new_scenes, which joins them into one ledger scene_id).
    """
    s2_collection = filter_plausible_footprint(s2_collection)

    single_covering = s2_collection.map(
        lambda img: img.set("covers_aoi", img.geometry().contains(aoi, ee.ErrorMargin(100)))
    ).filter(ee.Filter.eq("covers_aoi", True))
    single_ids = single_covering.aggregate_array("system:index").getInfo()
    single_millis = single_covering.aggregate_array("system:time_start").getInfo()
    single_id_set = set(single_ids)

    groups = [
        {"scene_ids": [scene_id], "date": _ts_to_date(ts)}
        for scene_id, ts in zip(single_ids, single_millis)
    ]

    all_ids = s2_collection.aggregate_array("system:index").getInfo()
    all_millis = s2_collection.aggregate_array("system:time_start").getInfo()
    by_date: dict[str, list[str]] = {}
    for scene_id, ts in zip(all_ids, all_millis):
        if scene_id in single_id_set:
            continue
        by_date.setdefault(_ts_to_date(ts), []).append(scene_id)

    for date, ids_on_date in by_date.items():
        if len(ids_on_date) < 2:
            continue  # a lone non-covering tile on its own date can't cover the AOI either
        mosaic_geometry = ee.ImageCollection(
            [ee.Image(f"{S2_COLLECTION}/{scene_id}") for scene_id in ids_on_date]
        ).geometry()
        if mosaic_geometry.contains(aoi, ee.ErrorMargin(100)).getInfo():
            groups.append({"scene_ids": sorted(ids_on_date), "date": date})

    return groups


def build_s2_mosaic(scene_ids: list[str]) -> ee.Image:
    """The S2 image to measure against for one scene group from s2_covering_groups: a single
    image unchanged for a single-tile group, or a mosaic of same-date tiles for a multi-tile one.

    mosaic() keeps every band each constituent scene has (SCL included, needed by
    scl_bad_fraction), but does not carry over scene-level metadata like
    system:index/system:time_start -- callers must pass scene_ids/date explicitly to anything
    downstream that used to read those properties off the image (s2_cloud_fraction, aod_value).
    """
    return ee.ImageCollection(
        [ee.Image(f"{S2_COLLECTION}/{scene_id}") for scene_id in scene_ids]
    ).mosaic()


def find_new_scenes(
    reservoir: Reservoir,
    since_date: str,
    processed_scene_ids: set[tuple[str, str, str]],
    until_date: str | None = None,
    excluded_geometries: set[tuple[str, int]] | None = None,
) -> list[dict]:
    """Find S1 and S2 scenes covering this reservoir's AOI since `since_date`.

    Excludes scenes already in `processed_scene_ids` (a set of (sensor, scene_id,
    cdec_station_id) triples, per config.load_processed_scene_ids -- the station id matters
    because one S1 swath or S2 tile routinely covers multiple reservoirs' AOIs, so a scene_id
    already logged for a *different* reservoir must not be treated as already-measured for this
    one), scenes whose footprint doesn't fully cover the AOI,
    -- for S1 -- any scene whose (orbit_pass, relative_orbit) is in `excluded_geometries`
    (station-specific entries from config.load_excluded_geometries; see
    reservoir_ca/sar_geometry_evaluation.py), and -- both sensors -- any scene dated within
    `reservoir.seasonal_exclusion_months` (e.g. winter months where ice contaminates the
    optical/SAR comparison for high-Sierra reservoirs like UNV -- see the 2026-09-21
    large-error-cluster investigation, planning/smoke_test_findings.md). A reservoir with no
    excluded geometries or seasonal exclusion gets every covering scene, as before.

    S2 coverage is grouped per s2_covering_groups: an AOI that straddles an MGRS tile boundary
    (see FOL) can need several same-date tiles mosaicked together to cover it. A multi-tile S2
    "scene" therefore has a scene_id that's a "+"-joined, sorted list of its constituent tile
    ids (a single-tile date keeps its bare original scene_id, unchanged from before this existed)
    and an extra `s2_tile_ids` list callers use to build the actual mosaic (see build_s2_mosaic).

    Returns a list of dicts: {"sensor": "S1"|"S2", "scene_id": str, "date": iso date str,
    "orbit_pass": str | None, "relative_orbit": int | None, "s2_tile_ids": list[str] | None} --
    the orbit fields are S1-only (None for S2); s2_tile_ids is S2-only (None for S1).
    """
    until_date = until_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    excluded_geometries = excluded_geometries or set()
    excluded_months = reservoir.seasonal_exclusion_months
    aoi = ee.Geometry(reservoir.aoi_geometry())

    s1 = (
        ee.ImageCollection(S1_COLLECTION)
        .filterBounds(aoi)
        .filterDate(since_date, until_date)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
    )
    s2 = ee.ImageCollection(S2_COLLECTION).filterBounds(aoi).filterDate(since_date, until_date)

    scenes: list[dict] = []

    covering_s1 = filter_plausible_footprint(s1).map(
        lambda img: img.set("covers_aoi", img.geometry().contains(aoi, ee.ErrorMargin(100)))
    ).filter(ee.Filter.eq("covers_aoi", True))
    ids = covering_s1.aggregate_array("system:index").getInfo()
    millis = covering_s1.aggregate_array("system:time_start").getInfo()
    passes = covering_s1.aggregate_array("orbitProperties_pass").getInfo()
    orbits = covering_s1.aggregate_array("relativeOrbitNumber_start").getInfo()
    for scene_id, ts, orbit_pass, relative_orbit in zip(ids, millis, passes, orbits):
        if ("S1", scene_id, reservoir.cdec_station_id) in processed_scene_ids:
            continue
        if (orbit_pass, relative_orbit) in excluded_geometries:
            continue
        date = _ts_to_date(ts)
        if excluded_months and _dt.date.fromisoformat(date).month in excluded_months:
            continue
        scenes.append(
            {
                "sensor": "S1",
                "scene_id": scene_id,
                "date": date,
                "orbit_pass": orbit_pass,
                "relative_orbit": relative_orbit,
                "s2_tile_ids": None,
            }
        )

    for group in s2_covering_groups(s2, aoi):
        scene_id = "+".join(group["scene_ids"])
        if ("S2", scene_id, reservoir.cdec_station_id) in processed_scene_ids:
            continue
        if excluded_months and _dt.date.fromisoformat(group["date"]).month in excluded_months:
            continue
        scenes.append(
            {
                "sensor": "S2",
                "scene_id": scene_id,
                "date": group["date"],
                "orbit_pass": None,
                "relative_orbit": None,
                "s2_tile_ids": group["scene_ids"],
            }
        )

    return scenes


def _otsu_threshold(histogram: ee.Dictionary) -> ee.Number:
    """Standard Otsu's-method threshold from a GEE histogram dictionary.

    Follows the well-established GEE community pattern for SAR water thresholding
    (as used in the UN-SPIDER / Gennadii Donchyts flood-mapping recipe this plan's
    Stage 1 fallback is based on).
    """
    histogram = ee.Dictionary(histogram)
    counts = ee.Array(histogram.get("histogram"))
    means = ee.Array(histogram.get("bucketMeans"))
    size = means.length().get([0])
    total = counts.reduce(ee.Reducer.sum(), [0]).get([0])
    total_mean_sum = means.multiply(counts).reduce(ee.Reducer.sum(), [0]).get([0])
    mean = total_mean_sum.divide(total)

    indices = ee.List.sequence(1, size)

    def between_class_variance(i):
        i = ee.Number(i)
        a_counts = counts.slice(0, 0, i)
        a_count = a_counts.reduce(ee.Reducer.sum(), [0]).get([0])
        a_means = means.slice(0, 0, i)
        a_mean = a_means.multiply(a_counts).reduce(ee.Reducer.sum(), [0]).get([0]).divide(
            a_count
        )
        b_count = total.subtract(a_count)
        b_mean = total_mean_sum.subtract(a_count.multiply(a_mean)).divide(b_count)
        return a_count.multiply(a_mean.subtract(mean).pow(2)).add(
            b_count.multiply(b_mean.subtract(mean).pow(2))
        )

    bss = indices.map(between_class_variance)
    return means.sort(bss).get([-1])


def measure_water_area_sar(
    image: ee.Image,
    aoi_geometry: ee.Geometry,
    calibrated_threshold_db: float | None = None,
    band: str = "VV",
    scale: int = 10,
) -> dict:
    """Water surface area (m^2) from a Sentinel-1 GRD image, per ProjectPlan.docx Stage 1.

    Uses the reservoir's calibrated backscatter threshold when provided (see
    config.load_sar_thresholds / sar_threshold_calibration.csv); otherwise derives a
    per-scene threshold via Otsu's method on the AOI's own backscatter histogram, matching
    the plan's documented fallback.
    """
    backscatter = image.select(band)

    if calibrated_threshold_db is not None:
        threshold = ee.Number(calibrated_threshold_db)
        method = "calibrated"
    else:
        histogram = backscatter.reduceRegion(
            reducer=ee.Reducer.histogram(255, 0.1),
            geometry=aoi_geometry,
            scale=scale,
            bestEffort=True,
        ).get(band)
        threshold = _otsu_threshold(histogram)
        method = "otsu"

    water = backscatter.lt(threshold).rename("water")
    area_m2 = (
        water.multiply(ee.Image.pixelArea())
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi_geometry,
            scale=scale,
            bestEffort=True,
        )
        .get("water")
    )

    return {
        "area_m2": ee.Number(area_m2).getInfo(),
        "threshold_db": threshold.getInfo(),
        "method": method,
        "band": band,
    }


def _cloud_probability_image(scene_ids: list[str]) -> ee.Image | None:
    """s2cloudless cloud-probability image for one S2 scene group: the matching single image for
    a bare scene id, or a mosaic of each constituent tile's cloud-probability image for a
    multi-tile group (see s2_covering_groups/build_s2_mosaic) so it stays pixel-aligned with the
    S2 mosaic it's screening.

    Returns None if none of `scene_ids` has a matching s2cloudless image (seen on older
    Sentinel-2 scenes, mostly pre-~2019, when calibrating against a multi-year lookback --
    Stage 1's own 14-day default lookback never surfaced this) rather than crashing on an empty
    mosaic.
    """
    matches = ee.ImageCollection(S2_CLOUD_PROBABILITY_COLLECTION).filter(
        ee.Filter.inList("system:index", scene_ids)
    )
    if matches.size().getInfo() == 0:
        return None
    return matches.mosaic().select("probability")


def s2_cloud_fraction(
    scene_ids: list[str],
    aoi_geometry: ee.Geometry,
    cloud_prob_threshold: float = 40,
    scale: int = 10,
) -> float | None:
    """Fraction (0-1) of `aoi_geometry` classified cloudy per s2cloudless, for one S2 scene group
    (`scene_ids`: a single tile id, or several same-date tile ids for a multi-tile group -- see
    s2_covering_groups).

    Shared between measure_water_area_optical (to gate scene acceptance) and the SAR
    Threshold Calibration step (to gate which S2/Dynamic World dates are usable as a
    calibration target) — see ProjectPlan.docx Stage 1.

    Returns None if no s2cloudless image exists for any of `scene_ids` (see
    _cloud_probability_image) rather than crashing.
    """
    cloud_prob = _cloud_probability_image(scene_ids)
    if cloud_prob is None:
        return None
    cloudy = cloud_prob.gt(cloud_prob_threshold).rename("cloudy")
    return ee.Number(
        cloudy.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=aoi_geometry,
            scale=scale,
            bestEffort=True,
        ).get("cloudy")
    ).getInfo()


def aod_value(aoi_geometry: ee.Geometry, date: str, scale: int = 1000) -> float | None:
    """Mean MODIS MAIAC aerosol optical depth (550nm) over `aoi_geometry` on `date`.

    A direct physical measurement of atmospheric haziness (smoke, dust, pollution) -- unlike
    s2_cloud_fraction, which is trained specifically to detect cloud and often misses smoke
    (different spectral signature than water/ice cloud). See AOD_COLLECTION's comment for why
    this exists. Returns None if MCD19A2 has no valid retrieval for this location/date (thick
    cloud blocks the AOD retrieval itself too, or the date can fall at a tile-coverage gap).
    """
    start = ee.Date(date)
    composite = (
        ee.ImageCollection(AOD_COLLECTION)
        .filterBounds(aoi_geometry)
        .filterDate(start, start.advance(1, "day"))
        .select(AOD_BAND)
        .mean()
    )
    stats = composite.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi_geometry, scale=scale, bestEffort=True
    ).getInfo()
    value = stats.get(AOD_BAND)
    return value * AOD_SCALE_FACTOR if value is not None else None


def scl_bad_fraction(image: ee.Image, aoi_geometry: ee.Geometry, scale: int = 10) -> float | None:
    """Fraction (0-1) of `aoi_geometry` classified into one of SCL_BAD_CLASSES in `image`'s own
    Scene Classification Layer -- catches thin cirrus (and a few other contamination modes)
    that s2_cloud_fraction misses. See SCL_BAND's comment for why the two disagree.
    """
    scl = image.select(SCL_BAND)
    bad = scl.remap(SCL_BAD_CLASSES, [1] * len(SCL_BAD_CLASSES), 0).rename("bad")
    stats = bad.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi_geometry, scale=scale, bestEffort=True
    ).getInfo()
    return stats.get("bad")


def measure_water_area_optical(
    image: ee.Image,
    scene_ids: list[str],
    date_str: str,
    aoi_geometry: ee.Geometry,
    cloud_prob_threshold: float = DEFAULT_CLOUD_PROB_THRESHOLD,
    max_cloud_fraction: float = DEFAULT_MAX_CLOUD_FRACTION,
    max_aod: float = DEFAULT_MAX_AOD,
    max_scl_bad_fraction: float = DEFAULT_MAX_SCL_BAD_FRACTION,
    ndwi_threshold: float = 0.0,
    scale: int = 10,
) -> dict | None:
    """Water surface area (m^2) from a Sentinel-2 SR image, per ProjectPlan.docx Stage 1.

    `image` is the scene to measure -- build via build_s2_mosaic(scene_ids), which handles both
    a single tile and a multi-tile mosaic (see s2_covering_groups, motivated by FOL). `scene_ids`
    and `date_str` are passed explicitly rather than read off `image`'s own properties, because
    mosaic() doesn't carry those over.

    Returns None (skip this scene) if more than `max_cloud_fraction` of the AOI is cloudy per
    s2cloudless, if MODIS MAIAC aerosol optical depth exceeds `max_aod` (smoke/haze the cloud
    screen misses, see aod_value), or if more than `max_scl_bad_fraction` of the AOI falls in a
    contaminated Sentinel-2 Scene Classification class -- thin cirrus in particular, which
    s2cloudless also misses (see scl_bad_fraction) -- rather than measuring through any of them.
    """
    cloud_fraction = s2_cloud_fraction(scene_ids, aoi_geometry, cloud_prob_threshold, scale)
    if cloud_fraction is None or cloud_fraction > max_cloud_fraction:
        return None

    bad_fraction = scl_bad_fraction(image, aoi_geometry, scale)
    if bad_fraction is not None and bad_fraction > max_scl_bad_fraction:
        return None

    aod = aod_value(aoi_geometry, date_str)
    if aod is not None and aod > max_aod:
        return None

    cloud_prob = _cloud_probability_image(scene_ids)
    clear = cloud_prob.lte(cloud_prob_threshold)
    ndwi = image.normalizedDifference(["B3", "B8"]).rename("ndwi")
    water = ndwi.gt(ndwi_threshold).And(clear).rename("water")

    area_m2 = (
        water.multiply(ee.Image.pixelArea())
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi_geometry,
            scale=scale,
            bestEffort=True,
        )
        .get("water")
    )

    return {
        "area_m2": ee.Number(area_m2).getInfo(),
        "cloud_fraction": cloud_fraction,
        "aod": aod,
        "scl_bad_fraction": bad_fraction,
        "ndwi_threshold": ndwi_threshold,
    }
