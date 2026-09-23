"""Entry point: run SAR Threshold Calibration across every reservoir in ReservoirList.csv.

Per planning/ProjectPlan.docx Stage 1, this is a one-time-per-reservoir step, re-run
periodically as more paired Sentinel-1/Sentinel-2 imagery accumulates. It writes
reservoirs/sar_threshold_calibration.csv, which stage1_query_measure.measure_water_area_sar
uses in place of its per-scene Otsu fallback for any reservoir it covers.

Requires Google Earth Engine access — see reservoir_ca/gee_auth.py.

Reservoirs are calibrated concurrently (--workers), each reservoir's own internal Earth Engine
calls stay serial. A live single-reservoir test (Shasta, 168 km^2 AOI, 60-day window) took 22
minutes serially — most of that is per-date GEE compute cost (cloud-fraction checks, Dynamic
World targets, pixel sampling), not anything this script controls, so a full 48-reservoir run
over a multi-year window is only practical run concurrently. Cross-reservoir concurrency (rather
than also parallelizing each reservoir's internal per-date calls) keeps total in-flight Earth
Engine requests bounded and predictable rather than compounding two levels of fan-out.
"""

from __future__ import annotations

import argparse
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from reservoir_ca import config, gee_auth
from reservoir_ca.config import Reservoir
from reservoir_ca import sar_threshold_calibration as calib

DEFAULT_WORKERS = 6


def _calibrate_one(reservoir: Reservoir, since_date: str) -> tuple[str, dict | None, Exception | None]:
    try:
        return reservoir.cdec_station_id, calib.run_calibration(reservoir, since_date), None
    except Exception as exc:  # one reservoir's failure shouldn't abort a multi-hour run
        return reservoir.cdec_station_id, None, exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calibrate the SAR water-detection threshold for all reservoirs."
    )
    parser.add_argument(
        "--since",
        default=(dt.date.today() - dt.timedelta(days=730)).isoformat(),
        help="Earliest scene date to draw paired imagery from (YYYY-MM-DD). Default: 2 years ago.",
    )
    parser.add_argument(
        "--project", default=None, help="Google Cloud project ID for Earth Engine."
    )
    parser.add_argument(
        "--reservoir",
        default=None,
        help="CDEC station ID to calibrate (e.g. SHA), for a quick single-reservoir test. "
        "Default: all reservoirs in ReservoirList.csv.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Reservoirs to calibrate concurrently (default: {DEFAULT_WORKERS}). Raise "
        "cautiously — each worker issues a steady stream of Earth Engine requests, and going "
        "too high risks hitting Earth Engine's own rate limits.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip any reservoir that already has a row in sar_threshold_calibration.csv, "
        "instead of recalibrating it. Useful for resuming an interrupted or partially-failed "
        "run without redoing reservoirs that already succeeded.",
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    if args.reservoir:
        # Same fix as main.py's --reservoir handling: naming a reservoir explicitly should work
        # even for an excluded one (e.g. FOL, kept out of the default fleet run/calibration by a
        # Stage 1 imagery-coverage bug rather than a measurement-quality problem).
        reservoirs = config.load_reservoirs(include_excluded=True)
        reservoirs = [r for r in reservoirs if r.cdec_station_id == args.reservoir]
        if not reservoirs:
            raise SystemExit(f"No reservoir with cdec_station_id={args.reservoir!r}")
    else:
        reservoirs = config.load_reservoirs()
        # Larger AOIs cost more per Earth Engine call; starting the slowest reservoirs first
        # lets them run alongside the long tail of small, fast ones instead of stalling the
        # whole run at the end.
        reservoirs = sorted(reservoirs, key=lambda r: r.capacity_af, reverse=True)

    # Keyed by (station_id, orbit_pass, relative_orbit) rather than station_id alone: this
    # script only ever writes whole-station default rows (blank orbit_pass/relative_orbit),
    # but reservoir_ca/sar_geometry_evaluation.py adds geometry-specific override rows to the
    # same CSV, and a station-id-only key would collide a station's default row with its own
    # override row on the next load/rewrite here.
    def _record_key(row: dict) -> tuple:
        orbit_pass = row.get("orbit_pass")
        relative_orbit = row.get("relative_orbit")
        has_geometry = isinstance(orbit_pass, str) and orbit_pass and not pd.isna(relative_orbit)
        return (
            row["cdec_station_id"],
            orbit_pass if has_geometry else None,
            int(relative_orbit) if has_geometry else None,
        )

    records: dict[tuple, dict] = {}
    if config.SAR_THRESHOLD_CALIBRATION_CSV.exists():
        existing_df = pd.read_csv(config.SAR_THRESHOLD_CALIBRATION_CSV)
        records = {_record_key(row): row for row in existing_df.to_dict("records")}

    if args.skip_existing:
        already_done = {station_id for station_id, orbit_pass, _ in records if orbit_pass is None}
        reservoirs = [r for r in reservoirs if r.cdec_station_id not in already_done]
        print(f"Skipping {len(already_done)} already-calibrated reservoir(s)")

    lock = threading.Lock()

    def _save_locked() -> None:
        config.save_sar_thresholds(list(records.values()))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_calibrate_one, reservoir, args.since): reservoir
            for reservoir in reservoirs
        }
        for future in as_completed(futures):
            station_id, result, error = future.result()
            if error is not None:
                print(f"{station_id}: FAILED ({error})")
                continue
            if result is None:
                print(f"{station_id}: skipped (insufficient paired imagery)")
                continue
            cal_iou = result["calibration_iou"]
            val_iou = result["validation_iou"]
            print(
                f"{station_id}: {result['band']} @ {result['threshold_db']} dB "
                f"(area-ratio validation error {result['area_ratio_validation_error']:.3f}, "
                f"IoU diagnostic: calibration {cal_iou if cal_iou is None else f'{cal_iou:.3f}'}, "
                f"validation {val_iou if val_iou is None else f'{val_iou:.3f}'})"
            )
            with lock:
                records[(station_id, None, None)] = result
                _save_locked()  # write after every reservoir, so a crash mid-run loses nothing

    print(f"Wrote {len(records)} calibrated threshold(s) to {config.SAR_THRESHOLD_CALIBRATION_CSV}")


if __name__ == "__main__":
    main()
