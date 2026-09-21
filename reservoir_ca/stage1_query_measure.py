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


def find_new_scenes(
    reservoir: Reservoir,
    since_date: str,
    processed_scene_ids: set[tuple[str, str]],
    until_date: str | None = None,
    excluded_geometries: set[tuple[str, int]] | None = None,
) -> list[dict]:
    """Find S1 and S2 scenes covering this reservoir's AOI since `since_date`.

    Excludes scenes already in `processed_scene_ids` (a set of (sensor, scene_id) pairs, per
    config.load_processed_scene_ids), scenes whose footprint doesn't fully cover the AOI, and
    -- for S1 -- any scene whose (orbit_pass, relative_orbit) is in `excluded_geometries`
    (station-specific entries from config.load_excluded_geometries; see
    reservoir_ca/sar_geometry_evaluation.py). A reservoir with no excluded geometries gets
    every covering scene, as before.

    Returns a list of dicts: {"sensor": "S1"|"S2", "scene_id": str, "date": iso date str,
    "orbit_pass": str | None, "relative_orbit": int | None} -- the orbit fields are S1-only
    and None for S2 scenes.
    """
    until_date = until_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    excluded_geometries = excluded_geometries or set()
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
    for collection, sensor in ((s1, "S1"), (s2, "S2")):
        covering = collection.map(
            lambda img: img.set(
                "covers_aoi", img.geometry().contains(aoi, ee.ErrorMargin(100))
            )
        ).filter(ee.Filter.eq("covers_aoi", True))

        ids = covering.aggregate_array("system:index").getInfo()
        millis = covering.aggregate_array("system:time_start").getInfo()
        if sensor == "S1":
            passes = covering.aggregate_array("orbitProperties_pass").getInfo()
            orbits = covering.aggregate_array("relativeOrbitNumber_start").getInfo()
        else:
            passes = [None] * len(ids)
            orbits = [None] * len(ids)

        for scene_id, ts, orbit_pass, relative_orbit in zip(ids, millis, passes, orbits):
            if (sensor, scene_id) in processed_scene_ids:
                continue
            if sensor == "S1" and (orbit_pass, relative_orbit) in excluded_geometries:
                continue
            date = _dt.datetime.fromtimestamp(ts / 1000, tz=_dt.timezone.utc).strftime(
                "%Y-%m-%d"
            )
            scenes.append(
                {
                    "sensor": sensor,
                    "scene_id": scene_id,
                    "date": date,
                    "orbit_pass": orbit_pass,
                    "relative_orbit": relative_orbit,
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


def s2_cloud_fraction(
    image: ee.Image,
    aoi_geometry: ee.Geometry,
    cloud_prob_threshold: float = 40,
    scale: int = 10,
) -> float | None:
    """Fraction (0-1) of `aoi_geometry` classified cloudy in `image`, per s2cloudless.

    Shared between measure_water_area_optical (to gate scene acceptance) and the SAR
    Threshold Calibration step (to gate which S2/Dynamic World dates are usable as a
    calibration target) — see ProjectPlan.docx Stage 1.

    Returns None if no s2cloudless image exists for this scene (seen on older Sentinel-2
    scenes, mostly pre-~2019, when calibrating against a multi-year lookback — Stage 1's own
    14-day default lookback never surfaced this) rather than crashing on a null `.first()`.
    """
    scene_index = image.get("system:index")
    matches = ee.ImageCollection(S2_CLOUD_PROBABILITY_COLLECTION).filter(
        ee.Filter.eq("system:index", scene_index)
    )
    if matches.size().getInfo() == 0:
        return None

    cloud_prob = matches.first().select("probability")
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
    aoi_geometry: ee.Geometry,
    cloud_prob_threshold: float = DEFAULT_CLOUD_PROB_THRESHOLD,
    max_cloud_fraction: float = DEFAULT_MAX_CLOUD_FRACTION,
    max_aod: float = DEFAULT_MAX_AOD,
    max_scl_bad_fraction: float = DEFAULT_MAX_SCL_BAD_FRACTION,
    ndwi_threshold: float = 0.0,
    scale: int = 10,
) -> dict | None:
    """Water surface area (m^2) from a Sentinel-2 SR image, per ProjectPlan.docx Stage 1.

    Returns None (skip this scene) if more than `max_cloud_fraction` of the AOI is cloudy per
    s2cloudless, if MODIS MAIAC aerosol optical depth exceeds `max_aod` (smoke/haze the cloud
    screen misses, see aod_value), or if more than `max_scl_bad_fraction` of the AOI falls in a
    contaminated Sentinel-2 Scene Classification class -- thin cirrus in particular, which
    s2cloudless also misses (see scl_bad_fraction) -- rather than measuring through any of them.
    """
    cloud_fraction = s2_cloud_fraction(image, aoi_geometry, cloud_prob_threshold, scale)
    if cloud_fraction is None or cloud_fraction > max_cloud_fraction:
        return None

    bad_fraction = scl_bad_fraction(image, aoi_geometry, scale)
    if bad_fraction is not None and bad_fraction > max_scl_bad_fraction:
        return None

    date_str = ee.Date(image.get("system:time_start")).format("YYYY-MM-dd").getInfo()
    aod = aod_value(aoi_geometry, date_str)
    if aod is not None and aod > max_aod:
        return None

    scene_index = image.get("system:index")
    cloud_prob = (
        ee.ImageCollection(S2_CLOUD_PROBABILITY_COLLECTION)
        .filter(ee.Filter.eq("system:index", scene_index))
        .first()
        .select("probability")
    )
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
