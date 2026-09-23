"""Entry point: run Stage 1 (Query & Measure) across every reservoir in ReservoirList.csv.

Requires Google Earth Engine access — see reservoir_ca/gee_auth.py. For local development,
run `earthengine authenticate` once, then pass --project with a Google Cloud project ID that
has the Earth Engine API enabled (or set the GEE_PROJECT environment variable).

Stage 2 (volume conversion) and Stage 3 (reporting) aren't implemented yet — capacity curves
haven't been sourced for any reservoir yet, so there's nothing for Stage 2 to convert against.
This only exercises Stage 1: find new scenes, measure water area, record them as processed.
"""

from __future__ import annotations

import argparse
import datetime as dt

import ee

from reservoir_ca import config, gee_auth
from reservoir_ca import stage1_query_measure as s1qm


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
    processed = config.load_processed_scene_ids()
    sar_thresholds = config.load_sar_thresholds()
    excluded_geometries = config.load_excluded_geometries()

    for reservoir in reservoirs:
        aoi = ee.Geometry(reservoir.aoi_geometry())
        reservoir_excluded = {
            (orbit_pass, relative_orbit)
            for station_id, orbit_pass, relative_orbit in excluded_geometries
            if station_id == reservoir.cdec_station_id
        }
        scenes = s1qm.find_new_scenes(
            reservoir, args.since, processed, excluded_geometries=reservoir_excluded
        )
        if not scenes:
            print(f"{reservoir.cdec_station_id}: no new scenes since {args.since}")
            continue

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
                image = ee.Image(f"{s1qm.S2_COLLECTION}/{scene['scene_id']}")
                result = s1qm.measure_water_area_optical(image, aoi)
                if result is None:
                    # measure_water_area_optical() collapses every rejection reason to None;
                    # cheaply recompute each check here just to log which one it was (otherwise
                    # invisible in the ledger).
                    cloud_fraction = s1qm.s2_cloud_fraction(image, aoi)
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
                    config.append_processed_scene(ledger_row)
                    continue
                ledger_row.update(
                    area_m2=result["area_m2"],
                    cloud_fraction=result["cloud_fraction"],
                    aod=result["aod"],
                    scl_bad_fraction=result["scl_bad_fraction"],
                )

            print(f"{reservoir.cdec_station_id} {scene['sensor']} {scene['date']}: {result}")
            config.append_processed_scene(ledger_row)


if __name__ == "__main__":
    main()
