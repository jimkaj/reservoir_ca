"""Stage 3 aggregation: per-reservoir current status and the fleet-wide daily totals the home
page charts, per planning/ProjectPlan.docx Stage 3.

Pure local computation over Stage 2's reservoirs/timeseries/<ID>.csv -- no Earth Engine cost.

Carry-forward rule (James, 2026-09-25): each day's fleet total is the sum of every reservoir's
most recent volume as of that day, whether that observation is from today or several days
earlier -- a reservoir's contribution changes only when a new image of it is processed.

Which observations count (James, 2026-09-25): only those Stage 2's neighbour-consistency check
didn't reject (see stage2_volume_conversion.neighbour_consistency). On a date with an accepted
Sentinel-2 observation, that date's value is the S2 one -- optical is the more accurate sensor
(median error vs CDEC 3.3% vs 4.9% for S1 over 2 years) -- and S1 fills in only on dates without
one. Several same-sensor observations on one date (two S2 tiles, two S1 orbits) are averaged.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import pandas as pd

from reservoir_ca.config import Reservoir
from reservoir_ca.stage2_volume_conversion import timeseries_path


@dataclass(frozen=True)
class ReservoirStatus:
    cdec_station_id: str
    name: str
    capacity_af: int
    latest_date: str
    latest_sensors: tuple[str, ...]
    volume_af: float
    area_m2: float

    @property
    def percent_full(self) -> float:
        return 100.0 * self.volume_af / self.capacity_af


def usable(timeseries: pd.DataFrame) -> pd.DataFrame:
    """The observations that count: everything except QC-rejected ones, and on each date only the
    preferred sensor's (S2 if present, else S1)."""
    kept = timeseries[timeseries["qc_status"] != "rejected"]
    has_s2 = kept.groupby("date")["sensor"].transform(lambda s: (s == "S2").any())
    return kept[~has_s2 | (kept["sensor"] == "S2")]


def load_timeseries(reservoir: Reservoir) -> pd.DataFrame | None:
    """Stage 2's per-observation time series for `reservoir`, or None if it has none yet."""
    path = timeseries_path(reservoir.cdec_station_id)
    if not path.exists():
        return None
    df = pd.read_csv(path)
    return df if not df.empty else None


def daily_values(timeseries: pd.DataFrame) -> pd.DataFrame:
    """One row per observed date: mean volume/area of that date's usable observations."""
    return (
        usable(timeseries).groupby("date", as_index=False)[["volume_af", "area_m2"]]
        .mean()
        .sort_values("date")
        .reset_index(drop=True)
    )


def reservoir_status(reservoir: Reservoir, timeseries: pd.DataFrame) -> ReservoirStatus:
    """The reservoir's current value -- its latest usable date's volume/area."""
    kept = usable(timeseries)
    latest_date = kept["date"].max()
    latest = kept[kept["date"] == latest_date]
    return ReservoirStatus(
        cdec_station_id=reservoir.cdec_station_id,
        name=reservoir.name,
        capacity_af=int(reservoir.capacity_af),
        latest_date=latest_date,
        latest_sensors=tuple(sorted(latest["sensor"].unique())),
        volume_af=float(latest["volume_af"].mean()),
        area_m2=float(latest["area_m2"].mean()),
    )


def fleet_daily_totals(
    timeseries_by_station: dict[str, pd.DataFrame], until_date: str | None = None
) -> pd.DataFrame:
    """Daily fleet totals under the carry-forward rule (see module docstring).

    The set of reservoirs summed is the same on every date (every key of
    `timeseries_by_station`), so the line never jumps because a reservoir entered or left the
    sum; the series therefore starts on the first date *every* reservoir has at least one
    observation. Runs through `until_date` (default: today) so the latest total is carried
    forward to the present, matching what the map shows.

    Returns columns [date, volume_af, area_m2, n_observed] -- n_observed is how many
    reservoirs had a fresh observation on that exact date (the rest are carried forward).
    """
    until = pd.Timestamp(until_date or _dt.date.today().isoformat())
    daily = {sid: daily_values(ts) for sid, ts in timeseries_by_station.items()}
    start = max(pd.Timestamp(df["date"].min()) for df in daily.values())
    grid = pd.date_range(start, until, freq="D")

    volume = pd.DataFrame(index=grid)
    area = pd.DataFrame(index=grid)
    observed = pd.DataFrame(index=grid)
    for sid, df in daily.items():
        series = df.assign(date=pd.to_datetime(df["date"])).set_index("date")
        # Reindex over the union so the value just before `start` carries into it, then trim.
        full_index = series.index.union(grid)
        volume[sid] = series["volume_af"].reindex(full_index).ffill().reindex(grid)
        area[sid] = series["area_m2"].reindex(full_index).ffill().reindex(grid)
        observed[sid] = grid.isin(series.index)

    return pd.DataFrame(
        {
            "date": grid.strftime("%Y-%m-%d"),
            "volume_af": volume.sum(axis=1).to_numpy(),
            "area_m2": area.sum(axis=1).to_numpy(),
            "n_observed": observed.sum(axis=1).to_numpy(),
        }
    )
