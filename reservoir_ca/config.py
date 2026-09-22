"""Reservoir list and processed-scene ledger loading, per planning/ProjectPlan.docx."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
RESERVOIRS_DIR = REPO_ROOT / "reservoirs"
RESERVOIR_LIST_CSV = RESERVOIRS_DIR / "ReservoirList.csv"
PROCESSED_IMAGERY_CSV = RESERVOIRS_DIR / "ProcessedImagery.csv"
SAR_THRESHOLD_CALIBRATION_CSV = RESERVOIRS_DIR / "sar_threshold_calibration.csv"
EXCLUDED_SAR_GEOMETRIES_CSV = RESERVOIRS_DIR / "excluded_sar_geometries.csv"

# area_m2/method/band/threshold_db are populated for S1 rows; cloud_fraction/aod/scl_bad_fraction
# for S2 rows; orbit_pass/relative_orbit (S1 only) are the Sentinel-1 acquisition geometry -- see
# reservoir_ca/sar_geometry_evaluation.py, which groups by this geometry to find reservoirs
# where a specific orbit consistently disagrees with optical, per planning/ProjectPlan.docx.
# method also doubles as an exclusion flag for S2 rows: "skipped_cloudy"/"skipped_hazy"/
# "skipped_cirrus" (never measured) or "hazy"/"cirrus" (measured before the relevant screen
# existed, then retroactively corrected -- area_m2 is nulled out when that happens, see
# reservoir_ca/haze_screening.py and reservoir_ca/cirrus_screening.py) all mean "don't trust
# this row's area_m2", same as it being null in the first place.
PROCESSED_IMAGERY_COLUMNS = [
    "sensor",
    "scene_id",
    "cdec_station_id",
    "scene_date",
    "area_m2",
    "method",
    "band",
    "threshold_db",
    "orbit_pass",
    "relative_orbit",
    "cloud_fraction",
    "aod",
    "scl_bad_fraction",
]


@dataclass(frozen=True)
class Reservoir:
    name: str
    cdec_station_id: str
    aoi_geojson_path: Path
    capacity_curve_path: Path | None
    capacity_af: int
    dam_lat: float
    dam_lon: float
    seasonal_exclusion_months: frozenset[int] = frozenset()

    def aoi_geometry(self) -> dict:
        """The AOI polygon as a GeoJSON geometry dict, suitable for ee.Geometry(...)."""
        data = json.loads(self.aoi_geojson_path.read_text())
        return data["features"][0]["geometry"]


def load_reservoirs(
    csv_path: Path = RESERVOIR_LIST_CSV, include_excluded: bool = False
) -> list[Reservoir]:
    """Load the monitored reservoir set.

    ReservoirList.csv's `excluded`/`exclusion_reason` columns mark reservoirs dropped from
    monitoring because no SAR/optical configuration produces trustworthy data for them (see
    the 2026-09-21 large-error-cluster investigation, planning/smoke_test_findings.md) -- these
    are skipped by default so every CLI (main.py, calibrate_sar_thresholds.py, etc.) naturally
    stops processing them without individual changes. Pass include_excluded=True for one-off
    diagnostic/review tooling that still needs to look at an excluded reservoir directly.
    """
    df = pd.read_csv(csv_path)
    if not include_excluded and "excluded" in df.columns:
        df = df[~df["excluded"].fillna(False)]
    reservoirs = []
    for row in df.itertuples(index=False):
        capacity_curve = (
            RESERVOIRS_DIR / row.capacity_curve_path
            if isinstance(row.capacity_curve_path, str) and row.capacity_curve_path
            else None
        )
        seasonal_months = getattr(row, "seasonal_exclusion_months", None)
        seasonal_exclusion = (
            frozenset(int(m) for m in str(seasonal_months).split(","))
            if isinstance(seasonal_months, str) and seasonal_months
            else frozenset()
        )
        reservoirs.append(
            Reservoir(
                name=row.reservoir_name,
                cdec_station_id=row.cdec_station_id,
                aoi_geojson_path=RESERVOIRS_DIR / row.aoi_geojson_path,
                capacity_curve_path=capacity_curve,
                capacity_af=row.capacity_af,
                dam_lat=row.dam_lat,
                dam_lon=row.dam_lon,
                seasonal_exclusion_months=seasonal_exclusion,
            )
        )
    return reservoirs


def load_processed_scene_ids(
    csv_path: Path = PROCESSED_IMAGERY_CSV,
) -> set[tuple[str, str]]:
    """Return the set of (sensor, scene_id) pairs already recorded, so Stage 1 can skip them.

    Scene IDs are only unique within a given sensor's collection (per ProjectPlan.docx),
    hence the (sensor, scene_id) pair rather than scene_id alone.
    """
    if not csv_path.exists():
        return set()
    df = pd.read_csv(csv_path)
    return set(zip(df["sensor"], df["scene_id"]))


def append_processed_scene(
    row: dict,
    csv_path: Path = PROCESSED_IMAGERY_CSV,
) -> None:
    """Append one scene to the ledger.

    `row` must include "sensor", "scene_id", "cdec_station_id", "scene_date"; any other
    PROCESSED_IMAGERY_COLUMNS field it omits is written blank. Dict-based (rather than one
    positional arg per column) so the schema can keep growing -- e.g. area_m2/orbit_pass were
    added after S1/S2 measurement, geometry tracking, and calibration were built -- without
    every call site needing to pass every field.
    """
    full_row = {column: row.get(column) for column in PROCESSED_IMAGERY_COLUMNS}
    frame = pd.DataFrame([full_row], columns=PROCESSED_IMAGERY_COLUMNS)
    if csv_path.exists():
        frame.to_csv(csv_path, mode="a", header=False, index=False)
    else:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(csv_path, mode="w", header=True, index=False)


# area_ratio_* is the primary calibration objective (mean |log(SAR area / optical area)| across
# paired dates); calibration_iou/validation_iou (against Dynamic World) are a secondary,
# independent diagnostic reported at that same threshold, not something the sweep optimizes for
# -- see reservoir_ca/sar_threshold_calibration.py's module docstring for why (2026-09-16).
SAR_THRESHOLD_CALIBRATION_COLUMNS = [
    "cdec_station_id",
    "orbit_pass",
    "relative_orbit",
    "band",
    "threshold_db",
    "area_ratio_calibration_error",
    "area_ratio_validation_error",
    "calibration_iou",
    "validation_iou",
    "n_calibration_pairs",
    "n_validation_pairs",
    "calibrated_at",
]


def load_sar_thresholds(
    csv_path: Path = SAR_THRESHOLD_CALIBRATION_CSV,
) -> dict[str, dict]:
    """Return per-station calibrated thresholds, with optional per-geometry overrides.

    {cdec_station_id: {"default": {"band", "threshold_db"} | None,
                        "overrides": {(orbit_pass, relative_orbit): {"band", "threshold_db"}}}}

    A row with blank orbit_pass/relative_orbit is that station's whole-AOI default (what
    calibrate_sar_thresholds.py writes). A row with both filled in is a geometry-specific
    override -- written by reservoir_ca/sar_geometry_evaluation.py when a specific Sentinel-1
    orbit consistently disagrees with optical for that reservoir and a threshold recalibrated
    just for that geometry closes the gap. Use resolve_sar_threshold() to pick the right one
    for a given scene rather than indexing this structure directly.

    Empty until the SAR Threshold Calibration step (see ProjectPlan.docx, Stage 1) has been
    run; measure_water_area_sar() falls back to per-scene Otsu thresholding for any reservoir
    (or geometry) missing from this table.
    """
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path)
    thresholds: dict[str, dict] = {}
    for row in df.itertuples(index=False):
        entry = thresholds.setdefault(row.cdec_station_id, {"default": None, "overrides": {}})
        record = {"band": row.band, "threshold_db": row.threshold_db}
        has_geometry = isinstance(row.orbit_pass, str) and row.orbit_pass and not pd.isna(
            row.relative_orbit
        )
        if has_geometry:
            entry["overrides"][(row.orbit_pass, int(row.relative_orbit))] = record
        else:
            entry["default"] = record
    return thresholds


def resolve_sar_threshold(
    sar_thresholds: dict[str, dict],
    cdec_station_id: str,
    orbit_pass: str | None = None,
    relative_orbit: int | None = None,
) -> dict | None:
    """The calibrated {"band", "threshold_db"} to use for one S1 scene, or None (Otsu fallback).

    Prefers an exact (orbit_pass, relative_orbit) override for this station over its
    whole-AOI default -- see load_sar_thresholds.
    """
    entry = sar_thresholds.get(cdec_station_id)
    if entry is None:
        return None
    if orbit_pass is not None and relative_orbit is not None:
        override = entry["overrides"].get((orbit_pass, int(relative_orbit)))
        if override is not None:
            return override
    return entry["default"]


def save_sar_thresholds(
    records: list[dict],
    csv_path: Path = SAR_THRESHOLD_CALIBRATION_CSV,
) -> None:
    """Write the full SAR Threshold Calibration table, replacing any existing file.

    Calibration is re-run periodically for the whole reservoir set (per ProjectPlan.docx), so
    this always rewrites the complete table rather than appending, unlike ProcessedImagery.csv.
    `records` holds a mix of whole-station default rows and geometry-specific override rows
    (blank orbit_pass/relative_orbit for the former) -- see load_sar_thresholds.
    """
    df = pd.DataFrame(records, columns=SAR_THRESHOLD_CALIBRATION_COLUMNS)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)


EXCLUDED_SAR_GEOMETRIES_COLUMNS = [
    "cdec_station_id",
    "orbit_pass",
    "relative_orbit",
    "reason",
    "n_samples",
    "fraction_biased",
    "mean_log_error",
    "excluded_at",
]


def load_excluded_geometries(
    csv_path: Path = EXCLUDED_SAR_GEOMETRIES_CSV,
) -> set[tuple[str, str, int]]:
    """Return {(cdec_station_id, orbit_pass, relative_orbit)} to skip at scene discovery time.

    Written by reservoir_ca/sar_geometry_evaluation.py when a geometry is consistently biased
    against optical *and* a per-geometry threshold recalibration still doesn't bring it within
    tolerance -- exclusion is the fallback, not the first response, to a bad geometry (see
    planning discussion after the 2026-09-16 SAR-geometry finding).
    """
    if not csv_path.exists():
        return set()
    df = pd.read_csv(csv_path)
    return {
        (row.cdec_station_id, row.orbit_pass, int(row.relative_orbit))
        for row in df.itertuples(index=False)
    }


def save_excluded_geometries(
    records: list[dict],
    csv_path: Path = EXCLUDED_SAR_GEOMETRIES_CSV,
) -> None:
    """Write the full excluded-geometries table, replacing any existing file (mirrors
    save_sar_thresholds -- this is periodically re-evaluated for the whole reservoir set, not
    appended to)."""
    df = pd.DataFrame(records, columns=EXCLUDED_SAR_GEOMETRIES_COLUMNS)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
