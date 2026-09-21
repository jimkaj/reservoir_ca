"""Haze/smoke screening agent: retroactively finds already-measured optical scenes that were
contaminated by atmospheric haze or smoke, and corrects the ledger to exclude them.

Motivated by the DON wind investigation (2026-09-16): several of DON's already-logged optical
measurements showed SAR staying rock-steady while optical area collapsed to near-zero on
specific dates (one date measured 1,099 m^2 against a SAR reading of ~1.3M m^2) -- the same
signature already found for Hetch Hetchy and the 2026-08-08/09-07/09-12 smoke-date cluster.
Neither the existing cloud screen (s2cloudless, trained on cloud specifically) nor Sentinel-2's
own Scene Classification Layer has a dedicated smoke/haze class, so none of them caught it.
This uses MODIS MAIAC aerosol optical depth (AOD) instead -- a direct physical measurement of
atmospheric haziness -- via stage1_query_measure.aod_value().

Two halves to this fix:
- The *live* gate: stage1_query_measure.measure_water_area_optical()'s max_aod parameter and
  sar_threshold_calibration.pair_s1_s2_dates()'s equivalent gate prevent *future* hazy scenes
  from ever being measured or paired, exactly like the existing cloud gate.
- This module (the retroactive half): scans scenes already in ProcessedImagery.csv that were
  measured before the live gate existed, computes AOD for each, and corrects the ledger for any
  that were hazy -- area_m2 -> None, method -> "hazy". Every downstream consumer that already
  filters on area_m2.notna() (calibration pairing, reservoir_ca/sar_geometry_evaluation.py's
  paired_measurements, any future Stage 2/3 code) then ignores those scenes automatically,
  without needing its own haze-awareness.
"""

from __future__ import annotations

import ee
import pandas as pd

from reservoir_ca.config import Reservoir
from reservoir_ca.stage1_query_measure import DEFAULT_MAX_AOD, aod_value


def screen_ledger_for_haze(
    processed: pd.DataFrame,
    reservoirs: list[Reservoir],
    max_aod: float = DEFAULT_MAX_AOD,
) -> pd.DataFrame:
    """Compute AOD for every already-measured S2 ledger row that predates haze-awareness
    (area_m2 not null, method blank) and correct the ones exceeding `max_aod`: area_m2 -> None,
    method -> "hazy".

    Returns a corrected copy of `processed` (same shape, same columns) -- doesn't write to disk
    itself, so a caller can inspect what changed before committing it.
    """
    reservoir_by_id = {r.cdec_station_id: r for r in reservoirs}
    corrected = processed.copy()
    corrected["area_m2"] = corrected["area_m2"].astype(object)
    corrected["method"] = corrected["method"].astype(object)
    corrected["aod"] = corrected["aod"].astype(object)

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
        aod = aod_value(aoi, row["scene_date"])
        corrected.loc[idx, "aod"] = aod
        if aod is not None and aod > max_aod:
            corrected.loc[idx, "area_m2"] = None
            corrected.loc[idx, "method"] = "hazy"

    return corrected
