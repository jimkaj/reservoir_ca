"""Stage 2: Volume Conversion & Record Update, per ProjectPlan.docx.

Converts Stage 1's measured surface area to volume via each reservoir's capacity curve (see
reservoir_ca/capacity_curve.py) and writes a per-reservoir time-series record -- one row per
measured scene, sensor labeled separately per the plan (S1 and S2 observations are never merged
into a single number, so Stage 3 can compare the two methods directly).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from reservoir_ca.config import RESERVOIRS_DIR, Reservoir

TIMESERIES_DIR = RESERVOIRS_DIR / "timeseries"


def load_capacity_curve(reservoir: Reservoir) -> pd.DataFrame | None:
    """The (area_m2, volume_af) lookup table built by build_capacity_curves.py, or None if this
    reservoir doesn't have one yet (capacity_curve_path blank, or the file is missing)."""
    if reservoir.capacity_curve_path is None or not reservoir.capacity_curve_path.exists():
        return None
    return pd.read_csv(reservoir.capacity_curve_path)


def interpolate_volume(curve: pd.DataFrame, area_m2: float) -> dict:
    """Volume (AF) for `area_m2` via linear interpolation on `curve`.

    `out_of_range` is set (per ProjectPlan.docx: "flag any case where the measured area falls
    outside the curve's known range") whenever area_m2 falls outside the curve's observed area
    span -- extrapolating a monotonic fit beyond its support is exactly the wrong place to trust
    it. numpy.interp clamps to the nearest known endpoint value in that case (a bounded, if
    imprecise, estimate) rather than extrapolating wildly; the flag is what tells a consumer not
    to trust the number outright, not the clamping itself.
    """
    area_min = curve["area_m2"].iloc[0]
    area_max = curve["area_m2"].iloc[-1]
    volume_af = float(np.interp(area_m2, curve["area_m2"], curve["volume_af"]))
    return {
        "volume_af": volume_af,
        "out_of_range": area_m2 < area_min or area_m2 > area_max,
    }


def convert_reservoir_timeseries(reservoir: Reservoir, processed: pd.DataFrame) -> pd.DataFrame | None:
    """Every measured scene (both sensors) for `reservoir`, converted to volume.

    Returns None if this reservoir has no capacity curve yet, rather than a table of
    unconvertible rows -- see load_capacity_curve. Regenerated fresh from the full ledger each
    run rather than appended to incrementally (mirrors sar_threshold_calibration.py's
    always-rewrite pattern): cheap, since this step is pure local interpolation with no Earth
    Engine cost, and avoids partial-update bugs.
    """
    curve = load_capacity_curve(reservoir)
    if curve is None:
        return None

    measured = processed[
        (processed["cdec_station_id"] == reservoir.cdec_station_id) & processed["area_m2"].notna()
    ][["scene_date", "sensor", "area_m2"]].rename(columns={"scene_date": "date"})

    rows = []
    for row in measured.itertuples(index=False):
        result = interpolate_volume(curve, row.area_m2)
        rows.append(
            {
                "date": row.date,
                "sensor": row.sensor,
                "area_m2": row.area_m2,
                "volume_af": result["volume_af"],
                "out_of_range": result["out_of_range"],
            }
        )
    return pd.DataFrame(rows).sort_values(["date", "sensor"]).reset_index(drop=True)


def timeseries_path(cdec_station_id: str):
    return TIMESERIES_DIR / f"{cdec_station_id}.csv"


def save_timeseries(cdec_station_id: str, timeseries: pd.DataFrame) -> None:
    path = timeseries_path(cdec_station_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    timeseries.to_csv(path, index=False)
