"""Cirrus screening agent: retroactively finds already-measured optical scenes contaminated by
thin cirrus that s2cloudless missed, and corrects the ledger to exclude them.

Confirmed directly 2026-09-17 while investigating DON's SAR-vs-optical disagreement: on a date
where optical area collapsed to near-zero while SAR stayed steady, s2cloudless's own cloud
probability over the water was low (~8%, well under the 40% gate) and MODIS MAIAC aerosol
optical depth (reservoir_ca/haze_screening.py) was low too -- but Sentinel-2's own Scene
Classification Layer (SCL) classified more of the AOI as THIN_CIRRUS than as WATER, a ~79x jump
over a clean date's negligible cirrus count. s2cloudless works from surface-reflectance bands
alone; SCL's cirrus flag comes from a dedicated cirrus-detection band (B10) used at L1C, before
it's dropped from the surface-reflectance product -- which is why the two disagree specifically
on thin cirrus. Snow/ice was checked and ruled out in the same investigation (MSK_SNWPRB was
exactly 0 on the contaminated date).

Two halves to this fix, mirroring reservoir_ca/haze_screening.py's structure:
- The *live* gate: stage1_query_measure.measure_water_area_optical()'s max_scl_bad_fraction
  parameter and sar_threshold_calibration.pair_s1_s2_dates()'s equivalent gate prevent *future*
  cirrus-contaminated scenes from ever being measured or paired.
- This module (the retroactive half): scans scenes already in ProcessedImagery.csv that were
  measured before the live gate existed, computes SCL contamination for each, and corrects the
  ledger for any that were contaminated -- area_m2 -> None, method -> "cirrus". Every downstream
  consumer that already filters on area_m2.notna() then ignores those scenes automatically.
"""

from __future__ import annotations

import ee
import pandas as pd

from reservoir_ca.config import Reservoir
from reservoir_ca.stage1_query_measure import (
    DEFAULT_MAX_SCL_BAD_FRACTION,
    S2_COLLECTION,
    scl_bad_fraction,
)


def screen_ledger_for_cirrus(
    processed: pd.DataFrame,
    reservoirs: list[Reservoir],
    max_scl_bad_fraction: float = DEFAULT_MAX_SCL_BAD_FRACTION,
) -> pd.DataFrame:
    """Compute SCL contamination for every already-measured S2 ledger row that predates this
    screen (area_m2 not null, method blank) and correct the ones exceeding
    `max_scl_bad_fraction`: area_m2 -> None, method -> "cirrus".

    Returns a corrected copy of `processed` (same shape, same columns) -- doesn't write to disk
    itself, so a caller can inspect what changed before committing it.
    """
    reservoir_by_id = {r.cdec_station_id: r for r in reservoirs}
    corrected = processed.copy()
    corrected["area_m2"] = corrected["area_m2"].astype(object)
    corrected["method"] = corrected["method"].astype(object)
    corrected["scl_bad_fraction"] = corrected["scl_bad_fraction"].astype(object)

    candidate_mask = (
        (corrected["sensor"] == "S2")
        & corrected["area_m2"].notna()
        & corrected["method"].isna()
    )

    for idx in corrected.index[candidate_mask]:
        row = corrected.loc[idx]
        reservoir = reservoir_by_id.get(row["cdec_station_id"])
        if reservoir is None:
            continue
        aoi = ee.Geometry(reservoir.aoi_geometry())
        image = ee.Image(f"{S2_COLLECTION}/{row['scene_id']}")
        bad_fraction = scl_bad_fraction(image, aoi)
        corrected.loc[idx, "scl_bad_fraction"] = bad_fraction
        if bad_fraction is not None and bad_fraction > max_scl_bad_fraction:
            corrected.loc[idx, "area_m2"] = None
            corrected.loc[idx, "method"] = "cirrus"

    return corrected
