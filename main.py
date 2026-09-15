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

    reservoirs = config.load_reservoirs()
    if args.reservoir:
        reservoirs = [r for r in reservoirs if r.cdec_station_id == args.reservoir]
        if not reservoirs:
            raise SystemExit(f"No reservoir with cdec_station_id={args.reservoir!r}")
    processed = config.load_processed_scene_ids()
    sar_thresholds = config.load_sar_thresholds()

    for reservoir in reservoirs:
        aoi = ee.Geometry(reservoir.aoi_geometry())
        scenes = s1qm.find_new_scenes(reservoir, args.since, processed)
        if not scenes:
            print(f"{reservoir.cdec_station_id}: no new scenes since {args.since}")
            continue

        for scene in scenes:
            if scene["sensor"] == "S1":
                image = ee.Image(f"{s1qm.S1_COLLECTION}/{scene['scene_id']}")
                calibration = sar_thresholds.get(reservoir.cdec_station_id)
                result = s1qm.measure_water_area_sar(
                    image,
                    aoi,
                    calibrated_threshold_db=(
                        calibration["threshold_db"] if calibration else None
                    ),
                    band=calibration["band"] if calibration else "VV",
                )
            else:
                image = ee.Image(f"{s1qm.S2_COLLECTION}/{scene['scene_id']}")
                result = s1qm.measure_water_area_optical(image, aoi)
                if result is None:
                    print(
                        f"{reservoir.cdec_station_id}: skipped {scene['scene_id']} "
                        "(too cloudy over AOI)"
                    )
                    config.append_processed_scene(
                        scene["sensor"],
                        scene["scene_id"],
                        reservoir.cdec_station_id,
                        scene["date"],
                    )
                    continue

            print(f"{reservoir.cdec_station_id} {scene['sensor']} {scene['date']}: {result}")
            config.append_processed_scene(
                scene["sensor"], scene["scene_id"], reservoir.cdec_station_id, scene["date"]
            )


if __name__ == "__main__":
    main()
