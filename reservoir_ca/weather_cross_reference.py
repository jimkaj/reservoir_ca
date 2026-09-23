"""Weather cross-reference agent: does wind explain SAR/optical disagreement on specific dates?

Motivated by DON: unlike LBS (a fixable AOI mismatch, see reservoir_ca/aoi_diagnosis.py), DON's
per-geometry recalibration (2026-09-16) failed to bring area-ratio agreement into line on any
of its three Sentinel-1 orbits, and its AOI and shape both checked out clean. Wind-roughened
water was already flagged as a suspected failure mode in the original 2026-09-14 smoke test
(San Luis reservoir ran SAR-cold, consistent with rough water reading brighter in SAR and
getting missed as "land"). This checks that hypothesis directly against DON: ERA5-Land
reanalysis wind speed, matched to each S1 scene's actual acquisition hour (wind-roughening is a
backscatter-time physical effect, not a daily-average one), compared against that date's real
SAR/optical area-ratio error.

Pulled via Earth Engine (ECMWF/ERA5_LAND/HOURLY) rather than an external weather API, since
this project is already authenticated against GEE and every other data source lives there --
no new auth path or dependency needed.
"""

from __future__ import annotations

import datetime as _dt

import ee
import numpy as np
import pandas as pd

from reservoir_ca.config import Reservoir
from reservoir_ca.sar_threshold_calibration import S1_COLLECTION, pair_s1_s2_dates
from reservoir_ca.stage1_query_measure import (
    build_s2_mosaic,
    measure_water_area_optical,
    measure_water_area_sar,
)

ERA5_LAND_COLLECTION = "ECMWF/ERA5_LAND/HOURLY"
WIND_SEARCH_WINDOW_HOURS = 3  # nearest available hourly reanalysis image within this window
WIND_SAMPLE_SCALE_M = 10000  # ERA5-Land's native resolution (~9-11km) -- a regional wind
# proxy, not a resolved local wind field over the reservoir surface specifically.


def scene_acquisition_time(scene_id: str, collection: str) -> _dt.datetime:
    millis = ee.Image(f"{collection}/{scene_id}").get("system:time_start").getInfo()
    return _dt.datetime.fromtimestamp(millis / 1000, tz=_dt.timezone.utc)


def wind_speed_mps(lat: float, lon: float, when: _dt.datetime) -> float | None:
    """ERA5-Land 10m wind speed (m/s) nearest `when`, at (lat, lon).

    Returns None if no reanalysis image falls within WIND_SEARCH_WINDOW_HOURS (shouldn't
    normally happen -- ERA5-Land's hourly record runs from 1950 to a few days behind present).
    """
    point = ee.Geometry.Point([lon, lat])
    start = ee.Date(when.isoformat())
    window = ee.ImageCollection(ERA5_LAND_COLLECTION).filterDate(
        start.advance(-WIND_SEARCH_WINDOW_HOURS, "hour"),
        start.advance(WIND_SEARCH_WINDOW_HOURS, "hour"),
    )
    if window.size().getInfo() == 0:
        return None

    def _tag_gap(img: ee.Image) -> ee.Image:
        gap = ee.Number(img.get("system:time_start")).subtract(start.millis()).abs()
        return img.set("gap", gap)

    nearest = ee.Image(window.map(_tag_gap).sort("gap").first())
    values = (
        nearest.select(["u_component_of_wind_10m", "v_component_of_wind_10m"])
        .reduceRegion(reducer=ee.Reducer.first(), geometry=point, scale=WIND_SAMPLE_SCALE_M)
        .getInfo()
    )
    u = values.get("u_component_of_wind_10m")
    v = values.get("v_component_of_wind_10m")
    if u is None or v is None:
        return None
    return float((u**2 + v**2) ** 0.5)


def per_date_series(
    reservoir: Reservoir,
    orbit_pass: str,
    relative_orbit: int,
    band: str,
    threshold_db: float,
    since_date: str,
    until_date: str | None = None,
) -> pd.DataFrame:
    """Real (whole-AOI, not point-sampled) SAR/optical area ratio and matched wind speed for
    every paired date on one Sentinel-1 geometry.

    Uses the exact production measurement functions (measure_water_area_sar/optical) rather
    than calibration's point-sampled area estimate, since a real per-date error is what should
    be correlated against a real per-date wind reading.
    """
    aoi = ee.Geometry(reservoir.aoi_geometry())
    pairs = pair_s1_s2_dates(reservoir, aoi, since_date, until_date)
    pairs = [
        p for p in pairs if p["orbit_pass"] == orbit_pass and p["relative_orbit"] == relative_orbit
    ]

    rows = []
    for pair in pairs:
        s1_image = ee.Image(f"{S1_COLLECTION}/{pair['s1_scene_id']}")
        s2_image = build_s2_mosaic(pair["s2_tile_ids"])

        sar_result = measure_water_area_sar(
            s1_image, aoi, calibrated_threshold_db=threshold_db, band=band
        )
        optical_result = measure_water_area_optical(s2_image, pair["s2_tile_ids"], pair["s2_date"], aoi)
        if optical_result is None:
            continue

        s1_time = scene_acquisition_time(pair["s1_scene_id"], S1_COLLECTION)
        wind = wind_speed_mps(reservoir.dam_lat, reservoir.dam_lon, s1_time)

        sar_area = sar_result["area_m2"]
        optical_area = optical_result["area_m2"]
        log_abs_error = (
            abs(np.log(sar_area / optical_area)) if optical_area and sar_area > 0 else None
        )
        rows.append(
            {
                "date": pair["s1_date"],
                "sar_area_m2": sar_area,
                "optical_area_m2": optical_area,
                "ratio": (sar_area / optical_area) if optical_area else None,
                "log_abs_error": log_abs_error,
                "wind_speed_mps": wind,
                "s1_acquisition_utc": s1_time.isoformat(),
            }
        )

    return pd.DataFrame(rows)


def correlate_with_wind(series: pd.DataFrame) -> dict:
    """Simple, transparent summary of whether error tracks wind speed: a Pearson correlation
    plus a windy-vs-calm tercile comparison, rather than a formal significance test -- easy to
    sanity-check by eye against the underlying per-date table (not a black-box p-value).
    """
    valid = series.dropna(subset=["log_abs_error", "wind_speed_mps"])
    if len(valid) < 4:
        return {"n": len(valid), "correlation": None}

    correlation = float(valid["log_abs_error"].corr(valid["wind_speed_mps"]))
    sorted_by_wind = valid.sort_values("wind_speed_mps")
    third = max(1, len(sorted_by_wind) // 3)
    calm = sorted_by_wind.iloc[:third]
    windy = sorted_by_wind.iloc[-third:]

    return {
        "n": len(valid),
        "correlation": correlation,
        "calm_mean_wind_mps": float(calm["wind_speed_mps"].mean()),
        "calm_mean_log_error": float(calm["log_abs_error"].mean()),
        "windy_mean_wind_mps": float(windy["wind_speed_mps"].mean()),
        "windy_mean_log_error": float(windy["log_abs_error"].mean()),
    }
