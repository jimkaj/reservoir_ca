"""Correlate SAR/optical measurement error with Sentinel-1 acquisition geometry.

Motivated by a question after the 2026-09-16 calibrated-threshold comparison
(planning/smoke_test_findings.md): a handful of reservoirs (SCC, LBS, ...) stayed elevated
even on dates unaffected by the smoke/haze pattern found there. If a specific S1 acquisition
geometry -- ascending vs. descending pass, or a specific relative orbit -- views a reservoir
from an angle that causes radar layover/shadow against surrounding terrain, that geometry
would show consistently high error regardless of threshold calibration, and would be a
candidate to exclude from scene selection (find_new_scenes) rather than something
recalibration can ever fix.

Fetches each S1 scene's orbitProperties_pass, relativeOrbitNumber_start, and platform_number
(cheap metadata-only calls, no pixel data) and joins them onto the per-date SAR/optical ratios
already computed by compare_calibrated_thresholds.py.
"""

from __future__ import annotations

import math

import ee
import pandas as pd

from reservoir_ca import config

S1_COLLECTION = "COPERNICUS/S1_GRD"
GEOMETRY_PROPERTIES = ["orbitProperties_pass", "relativeOrbitNumber_start", "platform_number"]


def scene_geometry(scene_id: str) -> dict:
    """Acquisition geometry metadata for one S1 scene (no pixel data fetched)."""
    return ee.Image(f"{S1_COLLECTION}/{scene_id}").toDictionary(GEOMETRY_PROPERTIES).getInfo()


def build_geometry_table(scene_ids: list[str]) -> pd.DataFrame:
    """One row per unique S1 scene_id with its acquisition geometry."""
    rows = []
    for scene_id in scene_ids:
        props = scene_geometry(scene_id)
        rows.append(
            {
                "s1_scene_id": scene_id,
                "pass": props.get("orbitProperties_pass"),
                "relative_orbit": props.get("relativeOrbitNumber_start"),
                "platform": props.get("platform_number"),
            }
        )
    return pd.DataFrame(rows)


def join_ratios_to_geometry(
    comparison: pd.DataFrame, processed: pd.DataFrame
) -> pd.DataFrame:
    """Attach each comparison row's S1 scene_id and geometry.

    `comparison` is smoke_test_calibrated_comparison.csv (cdec_station_id, date, ratio, ...);
    `processed` is ProcessedImagery.csv, used to look up which S1 scene_id produced each
    (cdec_station_id, date) row (the comparison CSV doesn't carry scene_id itself).

    A handful of (station, date) pairs have more than one logged S1 scene (overlapping
    ascending/descending swaths on the same calendar date) -- smoke_test_compare.py's
    compare_reservoir() took `.iloc[0]` of that subset when it originally measured the pair,
    so this must reproduce the same first-row selection or it'll join one comparison row to
    multiple scenes and fabricate duplicate rows with the wrong geometry attached to some.
    """
    s1 = (
        processed[processed["sensor"] == "S1"][["cdec_station_id", "scene_date", "scene_id"]]
        .rename(columns={"scene_date": "date", "scene_id": "s1_scene_id"})
        .groupby(["cdec_station_id", "date"], as_index=False, sort=False)
        .first()
    )
    merged = comparison.merge(s1, on=["cdec_station_id", "date"], how="left")

    geometry = build_geometry_table(sorted(merged["s1_scene_id"].dropna().unique()))
    merged = merged.merge(geometry, on="s1_scene_id", how="left")
    merged["log_abs_error"] = merged["ratio"].apply(
        lambda r: abs(math.log(r)) if r and r > 0 else None
    )
    return merged
