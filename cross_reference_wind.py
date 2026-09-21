"""Entry point: check whether wind explains SAR/optical disagreement for one reservoir's
Sentinel-1 geometry.

Motivated by DON, whose per-geometry recalibration didn't resolve on any of its three orbits
even though its AOI and shape both checked out clean (reservoir_ca/aoi_diagnosis.py) --
unlike LBS, whose disagreement was a fixable AOI mismatch. Wind-roughened water was already
suspected in the original smoke test (San Luis ran SAR-cold). See
reservoir_ca/weather_cross_reference.py for the approach: real per-date SAR/optical area-ratio
error vs. ERA5-Land wind speed matched to each S1 scene's actual acquisition hour.

Pass the (band, threshold_db) for the specific geometry to check -- from
reservoirs/sar_threshold_calibration.csv if it has an override row for that geometry, or from a
one-off reservoir_ca.sar_threshold_calibration.run_geometry_recalibration() call otherwise.

Requires Google Earth Engine access -- see reservoir_ca/gee_auth.py.
"""

from __future__ import annotations

import argparse
import datetime as dt

from reservoir_ca import config, gee_auth
from reservoir_ca import weather_cross_reference as wx


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correlate SAR/optical area-ratio error with wind speed for one "
        "reservoir's Sentinel-1 geometry."
    )
    parser.add_argument("--reservoir", required=True, help="CDEC station ID, e.g. DON.")
    parser.add_argument(
        "--orbit-pass", required=True, choices=["ASCENDING", "DESCENDING"]
    )
    parser.add_argument("--relative-orbit", required=True, type=int)
    parser.add_argument("--band", required=True, choices=["VV", "VH"])
    parser.add_argument(
        "--threshold-db", required=True, type=float, help="Calibrated threshold for this geometry."
    )
    parser.add_argument(
        "--since",
        default=(dt.date.today() - dt.timedelta(days=730)).isoformat(),
        help="Earliest date to draw paired imagery from (YYYY-MM-DD). Default: 2 years ago.",
    )
    parser.add_argument(
        "--project", default=None, help="Google Cloud project ID for Earth Engine."
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    reservoirs = {r.cdec_station_id: r for r in config.load_reservoirs()}
    reservoir = reservoirs.get(args.reservoir)
    if reservoir is None:
        raise SystemExit(f"No reservoir with cdec_station_id={args.reservoir!r}")

    series = wx.per_date_series(
        reservoir, args.orbit_pass, args.relative_orbit, args.band, args.threshold_db, args.since
    )
    if series.empty:
        print(f"{args.reservoir} {args.orbit_pass}/{args.relative_orbit}: no paired dates found")
        return

    summary = wx.correlate_with_wind(series)

    out_csv = (
        config.RESERVOIRS_DIR
        / f"wind_cross_reference_{args.reservoir}_{args.orbit_pass}_{args.relative_orbit}.csv"
    )
    series.to_csv(out_csv, index=False)

    print(series.sort_values("wind_speed_mps").to_string(index=False))
    print()
    if summary["correlation"] is None:
        print(f"n={summary['n']}: too few dates with both a valid error and wind reading to correlate")
    else:
        print(f"n={summary['n']}, correlation(|log error|, wind speed) = {summary['correlation']:.3f}")
        print(
            f"calmest third: mean wind {summary['calm_mean_wind_mps']:.2f} m/s, "
            f"mean |log error| {summary['calm_mean_log_error']:.3f}"
        )
        print(
            f"windiest third: mean wind {summary['windy_mean_wind_mps']:.2f} m/s, "
            f"mean |log error| {summary['windy_mean_log_error']:.3f}"
        )
    print(f"Wrote {out_csv}")


if __name__ == "__main__":
    main()
