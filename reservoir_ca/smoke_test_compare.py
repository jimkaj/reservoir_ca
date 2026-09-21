"""Re-measure the 2026-09-14 smoke test's same-day S1/S2 pairs with calibrated SAR thresholds.

planning/smoke_test_findings.md recorded 16/45 reservoirs disagreeing under plain per-scene
Otsu thresholding, mostly small reservoirs running SAR hot. sar_threshold_calibration.py was
built specifically to address that. This module re-measures the *exact same scenes* already
logged in ProcessedImagery.csv (rather than querying Earth Engine for new scenes) so the
before/after comparison isn't confounded by different imagery.
"""

from __future__ import annotations

import ee
import pandas as pd

from reservoir_ca import stage1_query_measure as s1qm
from reservoir_ca.config import Reservoir


def same_day_dates(processed: pd.DataFrame, cdec_station_id: str) -> list[str]:
    """Dates where both an S1 and an S2 scene were logged for this reservoir."""
    sub = processed[processed["cdec_station_id"] == cdec_station_id]
    s1_dates = set(sub.loc[sub["sensor"] == "S1", "scene_date"])
    s2_dates = set(sub.loc[sub["sensor"] == "S2", "scene_date"])
    return sorted(s1_dates & s2_dates)


def compare_reservoir(
    reservoir: Reservoir,
    processed: pd.DataFrame,
    calibration: dict | None,
) -> list[dict]:
    """Re-measure every same-day S1/S2 pair already logged for `reservoir`.

    Returns one row per pair with both areas, their ratio, and which SAR method was used
    (calibrated threshold vs. per-scene Otsu fallback) -- skips a date if the optical
    measurement is gated out by cloud cover, matching measure_water_area_optical's own logic.
    """
    dates = same_day_dates(processed, reservoir.cdec_station_id)
    if not dates:
        return []

    sub = processed[processed["cdec_station_id"] == reservoir.cdec_station_id]
    aoi = ee.Geometry(reservoir.aoi_geometry())
    rows: list[dict] = []

    for date in dates:
        s1_id = sub.loc[(sub["sensor"] == "S1") & (sub["scene_date"] == date), "scene_id"].iloc[0]
        s2_id = sub.loc[(sub["sensor"] == "S2") & (sub["scene_date"] == date), "scene_id"].iloc[0]

        s1_image = ee.Image(f"{s1qm.S1_COLLECTION}/{s1_id}")
        s2_image = ee.Image(f"{s1qm.S2_COLLECTION}/{s2_id}")

        sar_result = s1qm.measure_water_area_sar(
            s1_image,
            aoi,
            calibrated_threshold_db=calibration["threshold_db"] if calibration else None,
            band=calibration["band"] if calibration else "VV",
        )
        optical_result = s1qm.measure_water_area_optical(s2_image, aoi)
        if optical_result is None:
            continue

        sar_area = sar_result["area_m2"]
        optical_area = optical_result["area_m2"]
        rows.append(
            {
                "cdec_station_id": reservoir.cdec_station_id,
                "date": date,
                "sar_area_m2": sar_area,
                "optical_area_m2": optical_area,
                "ratio": (sar_area / optical_area) if optical_area else None,
                "sar_method": sar_result["method"],
                "sar_band": sar_result["band"],
            }
        )

    return rows
