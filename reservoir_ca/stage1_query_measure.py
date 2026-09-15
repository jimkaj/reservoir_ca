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


def find_new_scenes(
    reservoir: Reservoir,
    since_date: str,
    processed_scene_ids: set[tuple[str, str]],
    until_date: str | None = None,
) -> list[dict]:
    """Find S1 and S2 scenes covering this reservoir's AOI since `since_date`.

    Excludes scenes already in `processed_scene_ids` (a set of (sensor, scene_id) pairs, per
    config.load_processed_scene_ids) and scenes whose footprint doesn't fully cover the AOI.

    Returns a list of {"sensor": "S1"|"S2", "scene_id": str, "date": iso date str} dicts.
    """
    until_date = until_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
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

        for scene_id, ts in zip(ids, millis):
            if (sensor, scene_id) in processed_scene_ids:
                continue
            date = _dt.datetime.fromtimestamp(ts / 1000, tz=_dt.timezone.utc).strftime(
                "%Y-%m-%d"
            )
            scenes.append({"sensor": sensor, "scene_id": scene_id, "date": date})

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


def measure_water_area_optical(
    image: ee.Image,
    aoi_geometry: ee.Geometry,
    cloud_prob_threshold: float = 40,
    max_cloud_fraction: float = 0.2,
    ndwi_threshold: float = 0.0,
    scale: int = 10,
) -> dict | None:
    """Water surface area (m^2) from a Sentinel-2 SR image, per ProjectPlan.docx Stage 1.

    Returns None (skip this scene) if more than `max_cloud_fraction` of the AOI is cloudy,
    per s2cloudless, rather than measuring through cloud cover.
    """
    scene_index = image.get("system:index")
    cloud_prob = (
        ee.ImageCollection(S2_CLOUD_PROBABILITY_COLLECTION)
        .filter(ee.Filter.eq("system:index", scene_index))
        .first()
        .select("probability")
    )

    cloudy = cloud_prob.gt(cloud_prob_threshold).rename("cloudy")
    cloud_fraction = ee.Number(
        cloudy.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=aoi_geometry,
            scale=scale,
            bestEffort=True,
        ).get("cloudy")
    ).getInfo()

    if cloud_fraction is None or cloud_fraction > max_cloud_fraction:
        return None

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
        "ndwi_threshold": ndwi_threshold,
    }
