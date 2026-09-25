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

# Neighbour-consistency check (James, 2026-09-25) -- the weather-station QC idea: an observation
# whose measured AREA is more than QC_MAX_DEVIATION_PCT away from the median area of the same
# reservoir's other observations (either sensor) within +-QC_WINDOW_DAYS is rejected. Checked on
# area, not volume: when area collapses (e.g. smoke the S2 screens miss), volume gets clamped at
# the capacity curve's floor and can look deceptively plausible. Validated 2026-09-25 across all
# 38 reservoirs x 2 years: at 20% it flags 6.0% of observations, whose median error vs CDEC is
# 20.3% against 3.7% for the kept ones, and catches every known-contaminated 2026-09-19/22 scene.
# Flag-don't-delete: the ledger keeps the raw measurement; downstream (Stage 3 totals, map,
# water-mask choice) uses only non-rejected rows, and the charts show rejected ones distinctly.
QC_WINDOW_DAYS = 7
QC_MAX_DEVIATION_PCT = 20.0
QC_MIN_NEIGHBOURS = 3


def neighbour_consistency(dates: pd.Series, areas: pd.Series) -> pd.DataFrame:
    """Per-observation QC against its neighbours (see QC_* above).

    Returns columns: qc_deviation_pct (vs the neighbour median; NaN when unchecked), qc_status
    ("accepted" / "rejected" / "unchecked" -- fewer than QC_MIN_NEIGHBOURS neighbours, treated as
    accepted downstream), and qc_provisional (True when no neighbour is later than the
    observation, i.e. it was checked against the past only; recomputed from scratch every run, so
    a provisional rejection of a real, sudden change is reversed once later images confirm it).
    """
    t = pd.to_datetime(dates).to_numpy()
    a = areas.to_numpy(dtype=float)
    window = np.timedelta64(QC_WINDOW_DAYS, "D")
    deviation = np.full(len(a), np.nan)
    status = np.full(len(a), "unchecked", dtype=object)
    provisional = np.zeros(len(a), dtype=bool)
    for i in range(len(a)):
        neighbours = np.abs(t - t[i]) <= window
        neighbours[i] = False
        provisional[i] = not np.any(neighbours & (t > t[i]))
        if neighbours.sum() < QC_MIN_NEIGHBOURS:
            continue
        median = np.median(a[neighbours])
        deviation[i] = (a[i] - median) / median * 100
        status[i] = "rejected" if abs(deviation[i]) > QC_MAX_DEVIATION_PCT else "accepted"
    return pd.DataFrame(
        {"qc_deviation_pct": deviation, "qc_status": status, "qc_provisional": provisional},
        index=dates.index,
    )


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
    ][["scene_date", "sensor", "scene_id", "area_m2"]].rename(columns={"scene_date": "date"})

    rows = []
    for row in measured.itertuples(index=False):
        result = interpolate_volume(curve, row.area_m2)
        rows.append(
            {
                "date": row.date,
                "sensor": row.sensor,
                "scene_id": row.scene_id,
                "area_m2": row.area_m2,
                "volume_af": result["volume_af"],
                "out_of_range": result["out_of_range"],
            }
        )
    timeseries = pd.DataFrame(rows).sort_values(["date", "sensor"]).reset_index(drop=True)
    if timeseries.empty:
        return timeseries
    return pd.concat(
        [timeseries, neighbour_consistency(timeseries["date"], timeseries["area_m2"])], axis=1
    )


def timeseries_path(cdec_station_id: str):
    return TIMESERIES_DIR / f"{cdec_station_id}.csv"


def save_timeseries(cdec_station_id: str, timeseries: pd.DataFrame) -> None:
    path = timeseries_path(cdec_station_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    timeseries.to_csv(path, index=False)
