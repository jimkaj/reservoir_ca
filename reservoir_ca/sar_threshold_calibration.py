"""SAR Threshold Calibration, per planning/ProjectPlan.docx Stage 1.

One-time-per-reservoir (re-run periodically as paired imagery accumulates) calibration of the
SAR backscatter threshold that measure_water_area_sar() uses to classify water pixels.

**Primary objective (2026-09-16 revision): area-ratio agreement with our own optical
measurement** -- the threshold sweep picks the (band, threshold_db) that minimizes
|log(SAR_area / optical_area)| across calibration dates, because that's the actual quantity
Stage 1 delivers and Stage 2/3 consume, not a pixel mask. IoU against Google's Dynamic World
product (GOOGLE/DYNAMICWORLD/V1) is still computed at the chosen threshold and reported
alongside it, but purely as an independent diagnostic, not something the sweep optimizes for or
that gates acceptance. This was a deliberate trade discussed with the user: Dynamic World is a
genuinely independent cross-check (not our own pipeline), so a threshold that fits area-ratio
well but shows notably worse IoU than expected is a real signal worth investigating (see
reservoir_ca/aoi_diagnosis.py) rather than one to ignore -- see the LBS investigation in
planning/smoke_test_findings.md for why the two metrics can diverge (LBS's ascending-orbit-35
area ratio was extremely consistent, ~1.38x on 4/4 dates, while its Dynamic-World IoU at the
IoU-optimal threshold was still only ~0.62 -- optimizing for IoU there was optimizing for the
wrong thing).

NOTE: like stage1_query_measure, this has not yet been run end-to-end against live imagery for
every reservoir. The 2026-09-14 smoke test (planning/smoke_test_findings.md) is the motivation
for building it: most disagreement there was small/narrow reservoirs where per-scene Otsu runs
SAR hot, which a per-reservoir calibrated threshold should directly address.
"""

from __future__ import annotations

import datetime as _dt
import math
import random

import ee
import numpy as np

from reservoir_ca.config import Reservoir
from reservoir_ca.stage1_query_measure import (
    DEFAULT_MAX_AOD,
    DEFAULT_MAX_SCL_BAD_FRACTION,
    S1_COLLECTION,
    S2_COLLECTION,
    aod_value,
    measure_water_area_optical,
    s2_cloud_fraction,
    scl_bad_fraction,
)

DYNAMIC_WORLD_COLLECTION = "GOOGLE/DYNAMICWORLD/V1"

BANDS = ("VV", "VH")
CANDIDATE_THRESHOLDS_DB = [-25.0 + 0.5 * i for i in range(31)]  # -25.0 .. -10.0 step 0.5

MATCH_WINDOW_DAYS = 5
CLOUD_PROB_THRESHOLD = 40
MAX_CLOUD_FRACTION = 0.2
DW_WATER_PROB_THRESHOLD = 0.5
VALIDATION_HOLDOUT_FRACTION = 0.3
SAMPLE_PIXELS_PER_DATE = 2000
SAMPLE_SEED = 42

MIN_PAIRS_TO_CALIBRATE = 6
# A geometry-specific recalibration (reservoir_ca/sar_geometry_evaluation.py) draws from a much
# smaller pool than whole-station calibration -- one orbit's share of a reservoir's paired
# dates, not all of them -- so it needs a lower floor. Below this, the evaluation agent leaves
# the geometry flagged but takes no action, rather than either recalibrating on too little data
# or excluding a geometry that might just need more time to accumulate pairs.
MIN_PAIRS_FOR_GEOMETRY_CALIBRATION = 4
# A reservoir needs enough paired dates before a per-date outlier is distinguishable from
# normal seasonal drawdown/refill — below this, skip the shadow-contamination screen entirely.
MIN_PAIRS_FOR_SHADOW_SCREEN = 5
# How far (multiplicatively) a date's Dynamic World water area may sit from the reservoir's own
# median before it's treated as contaminated (e.g. canyon shadow misread as water, or vice
# versa) rather than real level change — see the Hetch Hetchy finding in smoke_test_findings.md.
SHADOW_OUTLIER_FACTOR = 3.0


def pair_s1_s2_dates(
    reservoir: Reservoir,
    aoi: ee.Geometry,
    since_date: str,
    until_date: str | None = None,
    match_window_days: int = MATCH_WINDOW_DAYS,
    cloud_prob_threshold: float = CLOUD_PROB_THRESHOLD,
    max_cloud_fraction: float = MAX_CLOUD_FRACTION,
    max_aod: float = DEFAULT_MAX_AOD,
    max_scl_bad_fraction: float = DEFAULT_MAX_SCL_BAD_FRACTION,
) -> list[dict]:
    """Pair each Sentinel-1 acquisition with its nearest usable Sentinel-2 date.

    A usable S2 date is within `match_window_days` of the S1 date, fully covers the AOI, has
    AOI cloud cover (per s2cloudless) at or below `max_cloud_fraction`, aerosol optical depth
    (per MODIS MAIAC, see stage1_query_measure.aod_value -- catches smoke/haze the cloud screen
    alone misses) at or below `max_aod`, and Sentinel-2 Scene Classification contamination (per
    stage1_query_measure.scl_bad_fraction -- catches thin cirrus that neither the cloud screen
    nor AOD catch) at or below `max_scl_bad_fraction`. Candidates are tried nearest-date-first;
    a rejected S2 date is skipped in favor of the next nearest, rather than failing the whole
    S1 date. Dates without any acceptable match are dropped — this only affects calibration,
    not Stage 1 measurement itself.
    """
    until_date = until_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")

    def _covering(collection: ee.ImageCollection) -> ee.ImageCollection:
        return collection.map(
            lambda img: img.set(
                "covers_aoi", img.geometry().contains(aoi, ee.ErrorMargin(100))
            )
        ).filter(ee.Filter.eq("covers_aoi", True))

    s1 = _covering(
        ee.ImageCollection(S1_COLLECTION)
        .filterBounds(aoi)
        .filterDate(since_date, until_date)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
    )
    s2 = _covering(
        ee.ImageCollection(S2_COLLECTION).filterBounds(aoi).filterDate(since_date, until_date)
    )

    def _dated(collection: ee.ImageCollection) -> list[tuple[str, _dt.date]]:
        ids = collection.aggregate_array("system:index").getInfo()
        millis = collection.aggregate_array("system:time_start").getInfo()
        return [
            (scene_id, _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).date())
            for scene_id, ms in zip(ids, millis)
        ]

    def _dated_s1(collection: ee.ImageCollection) -> list[tuple[str, _dt.date, str, int]]:
        ids = collection.aggregate_array("system:index").getInfo()
        millis = collection.aggregate_array("system:time_start").getInfo()
        passes = collection.aggregate_array("orbitProperties_pass").getInfo()
        orbits = collection.aggregate_array("relativeOrbitNumber_start").getInfo()
        return [
            (
                scene_id,
                _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).date(),
                orbit_pass,
                relative_orbit,
            )
            for scene_id, ms, orbit_pass, relative_orbit in zip(ids, millis, passes, orbits)
        ]

    s1_dated = _dated_s1(s1)
    s2_dated = _dated(s2)

    pairs: list[dict] = []
    for s1_id, s1_date, orbit_pass, relative_orbit in s1_dated:
        candidates = sorted(
            (
                (scene_id, s2_date)
                for scene_id, s2_date in s2_dated
                if abs((s2_date - s1_date).days) <= match_window_days
            ),
            key=lambda c: abs((c[1] - s1_date).days),
        )
        for s2_id, s2_date in candidates:
            image = ee.Image(f"{S2_COLLECTION}/{s2_id}")
            cloud_fraction = s2_cloud_fraction(image, aoi, cloud_prob_threshold)
            if cloud_fraction is None or cloud_fraction > max_cloud_fraction:
                continue
            bad_fraction = scl_bad_fraction(image, aoi)
            if bad_fraction is not None and bad_fraction > max_scl_bad_fraction:
                continue
            aod = aod_value(aoi, s2_date.isoformat())
            if aod is not None and aod > max_aod:
                continue
            pairs.append(
                {
                    "s1_scene_id": s1_id,
                    "s1_date": s1_date.isoformat(),
                    "orbit_pass": orbit_pass,
                    "relative_orbit": relative_orbit,
                    "s2_scene_id": s2_id,
                    "s2_date": s2_date.isoformat(),
                    "day_gap": abs((s2_date - s1_date).days),
                }
            )
            break

    return pairs


def build_calibration_target(aoi: ee.Geometry, matched_date: str) -> ee.Image | None:
    """Binary Dynamic World water mask for `matched_date` (YYYY-MM-DD), clipped to `aoi`.

    This is the independent reference the SAR threshold is calibrated against, not ground
    truth in an absolute sense — see ProjectPlan.docx, Key Architecture Decisions.

    Returns None if Dynamic World has no image for this date/AOI (e.g. processing latency on
    very recent dates) — mosaicking an empty collection yields a 0-band image, which fails
    downstream .gt() comparisons rather than just being "no water".
    """
    start = ee.Date(matched_date)
    dw = (
        ee.ImageCollection(DYNAMIC_WORLD_COLLECTION)
        .filterBounds(aoi)
        .filterDate(start, start.advance(1, "day"))
        .select("water")
    )
    if dw.size().getInfo() == 0:
        return None
    return dw.mosaic().gt(DW_WATER_PROB_THRESHOLD).rename("water").clip(aoi)


def _target_area_m2(target: ee.Image, aoi: ee.Geometry, scale: int = 10) -> float:
    return ee.Number(
        target.multiply(ee.Image.pixelArea())
        .reduceRegion(reducer=ee.Reducer.sum(), geometry=aoi, scale=scale, bestEffort=True)
        .get("water")
    ).getInfo()


def _filter_shadow_contaminated(pairs: list[dict]) -> list[dict]:
    """Drop paired dates whose Dynamic World target water area is a wild outlier.

    Guards against exactly the failure mode found at Hetch Hetchy in the smoke test: a narrow,
    steep-walled canyon reservoir where terrain/cloud shadow (not cloud itself, which
    s2cloudless already screens for) can flip the optical water read on an otherwise "clear"
    date, so Dynamic World's own S2-derived water mask swings wildly between nominally-clear
    dates in a way normal drawdown/refill wouldn't. See smoke_test_findings.md.
    """
    if len(pairs) < MIN_PAIRS_FOR_SHADOW_SCREEN:
        return pairs
    areas = np.array([p["target_area_m2"] for p in pairs], dtype=float)
    median = float(np.median(areas))
    if median <= 0:
        return pairs
    kept = [
        p
        for p, area in zip(pairs, areas)
        if (1.0 / SHADOW_OUTLIER_FACTOR) <= (area / median) <= SHADOW_OUTLIER_FACTOR
    ]
    return kept


def _sample_pair(aoi: ee.Geometry, pair: dict, seed: int) -> dict:
    """Random per-pixel (VV, VH, target) sample for one paired date.

    Sampled (via ee.Image.sample) rather than pulled in full: a large reservoir's AOI can hold
    millions of 10m pixels, too many to transfer with reduceRegion(toList()); a few thousand
    random pixels per date is enough for a stable threshold sweep and keeps this scalable
    across all 48 reservoirs regardless of size.
    """
    target = pair["_target_image"] if pair.get("_target_image") is not None else (
        build_calibration_target(aoi, pair["s2_date"])
    )
    if target is None:
        return {"VV": np.array([]), "VH": np.array([]), "target": np.array([], dtype=bool)}
    s1_image = ee.Image(f"{S1_COLLECTION}/{pair['s1_scene_id']}").select(list(BANDS))
    rows = (
        s1_image.addBands(target)
        .sample(
            region=aoi,
            scale=10,
            numPixels=SAMPLE_PIXELS_PER_DATE,
            seed=seed,
            geometries=False,
        )
        .reduceColumns(reducer=ee.Reducer.toList(3), selectors=["VV", "VH", "water"])
        .get("list")
        .getInfo()
    )
    if not rows:
        return {"VV": np.array([]), "VH": np.array([]), "target": np.array([], dtype=bool)}
    arr = np.array(rows, dtype=float)
    return {"VV": arr[:, 0], "VH": arr[:, 1], "target": arr[:, 2].astype(bool)}


def _pooled_samples(aoi: ee.Geometry, pairs: list[dict], seed: int) -> dict:
    per_date = [_sample_pair(aoi, pair, seed) for pair in pairs]
    return {
        band: np.concatenate([d[band] for d in per_date]) if per_date else np.array([])
        for band in (*BANDS, "target")
    }


def _iou(water: np.ndarray, target: np.ndarray) -> float | None:
    union = np.count_nonzero(water | target)
    if union == 0:
        return None
    intersection = np.count_nonzero(water & target)
    return intersection / union


def _dynamic_world_iou(
    aoi: ee.Geometry, pairs: list[dict], band: str, threshold_db: float, seed: int
) -> float | None:
    """IoU of (band, threshold_db) against the pooled Dynamic World target over `pairs` --
    purely a diagnostic (see module docstring): reports how well the area-ratio-chosen
    threshold happens to agree with an independent classifier, without influencing the choice.
    """
    samples = _pooled_samples(aoi, pairs, seed)
    if samples["target"].size == 0:
        return None
    water = samples[band] < threshold_db
    return _iou(water, samples["target"])


def _optical_area_m2(aoi: ee.Geometry, pair: dict) -> float | None:
    """The true (non-sampled, whole-AOI) optical water area for a pair's S2 scene -- the actual
    reference area-ratio calibration measures SAR against, via the same
    measure_water_area_optical() Stage 1 uses in production. Returns None if it's gated out by
    cloud cover (shouldn't normally happen, since pair_s1_s2_dates already screened for that,
    but the gate is re-checked here rather than assumed).
    """
    image = ee.Image(f"{S2_COLLECTION}/{pair['s2_scene_id']}")
    result = measure_water_area_optical(
        image, aoi, cloud_prob_threshold=CLOUD_PROB_THRESHOLD, max_cloud_fraction=MAX_CLOUD_FRACTION
    )
    return result["area_m2"] if result is not None else None


def _pooled_samples_by_pair(aoi: ee.Geometry, pairs: list[dict], seed: int) -> list[dict]:
    """Per-pair (not pooled-across-dates) backscatter samples plus each date's true optical
    area -- area-ratio error is inherently per-date (SAR estimate vs. that date's own optical
    reading), unlike IoU which can pool every date's pixels together.
    """
    per_pair = []
    for pair in pairs:
        sample = _sample_pair(aoi, pair, seed)
        optical_area_m2 = pair.get("optical_area_m2")
        if optical_area_m2 is None or sample["VV"].size == 0:
            continue
        per_pair.append({"VV": sample["VV"], "VH": sample["VH"], "optical_area_m2": optical_area_m2})
    return per_pair


def _area_ratio_error(
    per_pair_samples: list[dict], band: str, threshold_db: float, aoi_area_m2: float
) -> float | None:
    """Mean |log(estimated SAR area / true optical area)| across dates for one candidate
    threshold. The SAR area is *estimated* from the same random pixel sample used for the IoU
    diagnostic (fraction of sampled pixels below threshold, scaled by the AOI's true area) --
    reusing the existing sample rather than an exact reduceRegion per candidate threshold, which
    would be ~60x more Earth Engine calls for the full (band, threshold) sweep.
    """
    errors = []
    for sample in per_pair_samples:
        backscatter = sample[band]
        if backscatter.size == 0:
            continue
        estimated_area = float(np.mean(backscatter < threshold_db)) * aoi_area_m2
        optical_area = sample["optical_area_m2"]
        if estimated_area <= 0 or optical_area <= 0:
            continue
        errors.append(abs(math.log(estimated_area / optical_area)))
    return float(np.mean(errors)) if errors else None


def calibrate_threshold_by_area_ratio(
    aoi: ee.Geometry,
    calibration_pairs: list[dict],
) -> tuple[str, float, float] | None:
    """Sweep candidate dB thresholds on both VV and VH; return the (band, threshold, mean
    |log-ratio| error) combination that best matches our own optical area measurement, per-date,
    across `calibration_pairs` -- see module docstring for why this is the primary objective.
    """
    aoi_area_m2 = aoi.area(1).getInfo()
    per_pair_samples = _pooled_samples_by_pair(aoi, calibration_pairs, SAMPLE_SEED)
    if not per_pair_samples:
        return None

    best: tuple[str, float, float] | None = None
    for band in BANDS:
        for threshold_db in CANDIDATE_THRESHOLDS_DB:
            error = _area_ratio_error(per_pair_samples, band, threshold_db, aoi_area_m2)
            if error is not None and (best is None or error < best[2]):
                best = (band, threshold_db, error)

    return best


def validate_threshold_by_area_ratio(
    aoi: ee.Geometry,
    validation_pairs: list[dict],
    band: str,
    threshold_db: float,
) -> float | None:
    """Mean |log-ratio| error of the already-chosen (band, threshold_db) against held-out
    `validation_pairs` -- checks the threshold generalizes rather than overfitting to the
    calibration dates.
    """
    if not validation_pairs:
        return None
    aoi_area_m2 = aoi.area(1).getInfo()
    per_pair_samples = _pooled_samples_by_pair(aoi, validation_pairs, SAMPLE_SEED + 1)
    if not per_pair_samples:
        return None
    return _area_ratio_error(per_pair_samples, band, threshold_db, aoi_area_m2)


def _calibrate_from_pairs(
    aoi: ee.Geometry,
    pairs: list[dict],
    min_pairs: int,
    shuffle_seed: str,
) -> dict | None:
    """Shared core of run_calibration and run_geometry_recalibration: screen for shadow
    contamination, split calibration/validation, sweep thresholds by area-ratio agreement, and
    validate -- plus report Dynamic World IoU at the chosen threshold as a secondary diagnostic
    (see module docstring). Returns None if there isn't enough paired imagery (after the shadow
    screen, and after dropping any pair the optical re-measurement itself gates out) to
    calibrate against.
    """
    if len(pairs) < min_pairs:
        return None

    dated_pairs = []
    for pair in pairs:
        target = build_calibration_target(aoi, pair["s2_date"])
        if target is None:
            continue
        optical_area_m2 = _optical_area_m2(aoi, pair)
        if optical_area_m2 is None:
            continue
        pair["_target_image"] = target
        pair["target_area_m2"] = _target_area_m2(target, aoi)
        pair["optical_area_m2"] = optical_area_m2
        dated_pairs.append(pair)
    pairs = _filter_shadow_contaminated(dated_pairs)
    if len(pairs) < min_pairs:
        return None

    shuffled = list(pairs)
    random.Random(shuffle_seed).shuffle(shuffled)
    n_validation = max(1, round(len(shuffled) * VALIDATION_HOLDOUT_FRACTION))
    validation_pairs = shuffled[:n_validation]
    calibration_pairs = shuffled[n_validation:]

    best = calibrate_threshold_by_area_ratio(aoi, calibration_pairs)
    if best is None:
        return None
    band, threshold_db, calibration_area_ratio_error = best

    validation_area_ratio_error = validate_threshold_by_area_ratio(
        aoi, validation_pairs, band, threshold_db
    )

    # Dynamic World IoU at this (area-ratio-chosen) threshold, reported for both splits purely
    # as a diagnostic -- see module docstring. Failing to compute it (e.g. no pairs survived)
    # isn't fatal to calibration itself, unlike the area-ratio numbers above.
    calibration_iou = _dynamic_world_iou(aoi, calibration_pairs, band, threshold_db, SAMPLE_SEED)
    validation_iou = _dynamic_world_iou(aoi, validation_pairs, band, threshold_db, SAMPLE_SEED + 1)

    return {
        "band": band,
        "threshold_db": threshold_db,
        "area_ratio_calibration_error": calibration_area_ratio_error,
        "area_ratio_validation_error": validation_area_ratio_error,
        "calibration_iou": calibration_iou,
        "validation_iou": validation_iou,
        "n_calibration_pairs": len(calibration_pairs),
        "n_validation_pairs": len(validation_pairs),
        "calibrated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


def run_calibration(
    reservoir: Reservoir,
    since_date: str,
    until_date: str | None = None,
) -> dict | None:
    """Full per-reservoir calibration across all Sentinel-1 geometries pooled together.
    Returns None (skip this reservoir) if there isn't enough paired imagery to calibrate
    against. The returned record has blank orbit_pass/relative_orbit -- it's this station's
    whole-AOI default, per config.load_sar_thresholds.
    """
    aoi = ee.Geometry(reservoir.aoi_geometry())
    pairs = pair_s1_s2_dates(reservoir, aoi, since_date, until_date)
    result = _calibrate_from_pairs(
        aoi, pairs, MIN_PAIRS_TO_CALIBRATE, shuffle_seed=reservoir.cdec_station_id
    )
    if result is None:
        return None
    return {
        "cdec_station_id": reservoir.cdec_station_id,
        "orbit_pass": None,
        "relative_orbit": None,
        **result,
    }


def run_geometry_recalibration(
    reservoir: Reservoir,
    orbit_pass: str,
    relative_orbit: int,
    since_date: str,
    until_date: str | None = None,
) -> dict | None:
    """Calibrate a threshold using only the paired dates whose S1 scene came from this one
    (orbit_pass, relative_orbit) -- see reservoir_ca/sar_geometry_evaluation.py, which calls
    this when that geometry's measurements consistently disagree with optical for this
    reservoir. Returns None if there aren't enough matching pairs
    (MIN_PAIRS_FOR_GEOMETRY_CALIBRATION, lower than whole-station calibration's threshold since
    one geometry only gets a fraction of a reservoir's paired dates) -- the caller should leave
    the geometry flagged rather than excluding it outright on too little data.
    """
    aoi = ee.Geometry(reservoir.aoi_geometry())
    pairs = pair_s1_s2_dates(reservoir, aoi, since_date, until_date)
    pairs = [
        p for p in pairs if p["orbit_pass"] == orbit_pass and p["relative_orbit"] == relative_orbit
    ]
    result = _calibrate_from_pairs(
        aoi,
        pairs,
        MIN_PAIRS_FOR_GEOMETRY_CALIBRATION,
        shuffle_seed=f"{reservoir.cdec_station_id}:{orbit_pass}:{relative_orbit}",
    )
    if result is None:
        return None
    return {
        "cdec_station_id": reservoir.cdec_station_id,
        "orbit_pass": orbit_pass,
        "relative_orbit": relative_orbit,
        **result,
    }
