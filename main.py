"""Entry point: run Stage 1 (Query & Measure) across every reservoir in ReservoirList.csv.

Requires Google Earth Engine access — see reservoir_ca/gee_auth.py. For local development,
run `earthengine authenticate` once, then pass --project with a Google Cloud project ID that
has the Earth Engine API enabled (or set the GEE_PROJECT environment variable).

Reservoirs are processed concurrently (--workers, mirroring calibrate_sar_thresholds.py's
pattern), each reservoir's own Earth Engine calls stay serial. A full multi-year backfill across
the fleet is exactly the kind of run that benefits from this -- see reservoir_ca_status memory,
2026-09-23.
"""

from __future__ import annotations

import argparse
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import ee

from reservoir_ca import config, gee_auth
from reservoir_ca import stage1_query_measure as s1qm
from reservoir_ca.config import Reservoir

DEFAULT_WORKERS = 6


def _process_reservoir(
    reservoir: Reservoir,
    since_date: str,
    processed: set[tuple[str, str, str]],
    sar_thresholds: dict[str, dict],
    excluded_geometries: set[tuple[str, str, int]],
    ledger_lock: threading.Lock,
) -> tuple[str, int, Exception | None]:
    """Find and measure every new scene for one reservoir. Returns (station_id, n_scenes_logged,
    error) -- one reservoir's failure is caught and reported rather than aborting the whole run.
    """
    try:
        aoi = ee.Geometry(reservoir.aoi_geometry())
        reservoir_excluded = {
            (orbit_pass, relative_orbit)
            for station_id, orbit_pass, relative_orbit in excluded_geometries
            if station_id == reservoir.cdec_station_id
        }
        scenes = s1qm.find_new_scenes(
            reservoir, since_date, processed, excluded_geometries=reservoir_excluded
        )
        if not scenes:
            return reservoir.cdec_station_id, 0, None

        n_logged = 0
        for scene in scenes:
            ledger_row = {
                "sensor": scene["sensor"],
                "scene_id": scene["scene_id"],
                "cdec_station_id": reservoir.cdec_station_id,
                "scene_date": scene["date"],
            }

            if scene["sensor"] == "S1":
                image = ee.Image(f"{s1qm.S1_COLLECTION}/{scene['scene_id']}")
                calibration = config.resolve_sar_threshold(
                    sar_thresholds,
                    reservoir.cdec_station_id,
                    scene["orbit_pass"],
                    scene["relative_orbit"],
                )
                result = s1qm.measure_water_area_sar(
                    image,
                    aoi,
                    calibrated_threshold_db=(
                        calibration["threshold_db"] if calibration else None
                    ),
                    band=calibration["band"] if calibration else "VV",
                )
                ledger_row.update(
                    area_m2=result["area_m2"],
                    method=result["method"],
                    band=result["band"],
                    threshold_db=result["threshold_db"],
                    orbit_pass=scene["orbit_pass"],
                    relative_orbit=scene["relative_orbit"],
                )
            else:
                # scene["scene_id"] may be several "+"-joined tile ids for an AOI that straddles
                # an MGRS tile boundary (see s2_covering_groups) -- s2_tile_ids carries the list
                # to build from; a single-tile scene has s2_tile_ids == [scene_id].
                tile_ids = scene["s2_tile_ids"]
                image = s1qm.build_s2_mosaic(tile_ids)
                result = s1qm.measure_water_area_optical(image, tile_ids, scene["date"], aoi)
                if result is None:
                    # measure_water_area_optical() collapses every rejection reason to None;
                    # cheaply recompute each check here just to log which one it was (otherwise
                    # invisible in the ledger).
                    cloud_fraction = s1qm.s2_cloud_fraction(tile_ids, aoi)
                    if cloud_fraction is not None and cloud_fraction > s1qm.DEFAULT_MAX_CLOUD_FRACTION:
                        reason, method = "too cloudy", "skipped_cloudy"
                    else:
                        bad_fraction = s1qm.scl_bad_fraction(image, aoi)
                        if bad_fraction is not None and bad_fraction > s1qm.DEFAULT_MAX_SCL_BAD_FRACTION:
                            reason, method = "cirrus/SCL contamination", "skipped_cirrus"
                        else:
                            reason, method = "too hazy (AOD)", "skipped_hazy"
                    print(
                        f"{reservoir.cdec_station_id}: skipped {scene['scene_id']} "
                        f"({reason} over AOI)"
                    )
                    ledger_row["method"] = method
                    with ledger_lock:
                        config.append_processed_scene(ledger_row)
                    n_logged += 1
                    continue
                ledger_row.update(
                    area_m2=result["area_m2"],
                    cloud_fraction=result["cloud_fraction"],
                    aod=result["aod"],
                    scl_bad_fraction=result["scl_bad_fraction"],
                )

            print(f"{reservoir.cdec_station_id} {scene['sensor']} {scene['date']}: {result}")
            with ledger_lock:
                config.append_processed_scene(ledger_row)
            n_logged += 1

        return reservoir.cdec_station_id, n_logged, None
    except Exception as exc:  # one reservoir's failure shouldn't abort a multi-hour run
        return reservoir.cdec_station_id, 0, exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Stage 1 (Query & Measure) for all reservoirs."
    )
    parser.add_argument(
        "--since",
        default=(dt.date.today() - dt.timedelta(days=14)).isoformat(),
        help="Earliest scene date to consider (YYYY-MM-DD). Default: 14 days ago.",
    )
    parser.add_argument(
        "--project", default=None, help="Google Cloud project ID for Earth Engine."
    )
    parser.add_argument(
        "--reservoir",
        default=None,
        help="CDEC station ID to run against (e.g. SHA), for a quick single-reservoir test. "
        "Default: all reservoirs in ReservoirList.csv.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Reservoirs to process concurrently (default: {DEFAULT_WORKERS}). Raise "
        "cautiously — each worker issues a steady stream of Earth Engine requests, and going "
        "too high risks hitting Earth Engine's own rate limits.",
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    if args.reservoir:
        # Naming a reservoir explicitly is a deliberate, targeted request -- it should work even
        # for an excluded one (e.g. FOL, kept out of the monitored/calibration set by a Stage 1
        # imagery-coverage bug rather than a measurement-quality problem, but still worth scanning
        # for new scenes against the day a fix lands), unlike the full-fleet default run below.
        reservoirs = config.load_reservoirs(include_excluded=True)
        reservoirs = [r for r in reservoirs if r.cdec_station_id == args.reservoir]
        if not reservoirs:
            raise SystemExit(f"No reservoir with cdec_station_id={args.reservoir!r}")
    else:
        reservoirs = config.load_reservoirs()
        # Larger AOIs cost more per Earth Engine call; starting the slowest reservoirs first
        # lets them run alongside the long tail of small, fast ones instead of stalling the
        # whole run at the end -- mirrors calibrate_sar_thresholds.py.
        reservoirs = sorted(reservoirs, key=lambda r: r.capacity_af, reverse=True)

    run_stage1(reservoirs, {r.cdec_station_id: args.since for r in reservoirs}, args.workers)


def run_stage1(reservoirs: list[Reservoir], since_by_station: dict[str, str], workers: int) -> None:
    """Find and measure new scenes for each reservoir from its own since-date, concurrently.
    Shared by main() (one --since for every reservoir) and run_pipeline.py (a per-reservoir
    since-date for only the reservoirs that are due)."""
    processed = config.load_processed_scene_ids()
    sar_thresholds = config.load_sar_thresholds()
    excluded_geometries = config.load_excluded_geometries()
    ledger_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _process_reservoir,
                reservoir,
                since_by_station[reservoir.cdec_station_id],
                processed,
                sar_thresholds,
                excluded_geometries,
                ledger_lock,
            ): reservoir
            for reservoir in reservoirs
        }
        for future in as_completed(futures):
            station_id, n_logged, error = future.result()
            if error is not None:
                print(f"{station_id}: FAILED ({error})")
            elif n_logged == 0:
                print(f"{station_id}: no new scenes since {since_by_station[station_id]}")
            else:
                print(f"{station_id}: {n_logged} scene(s) logged")


if __name__ == "__main__":
    main()
