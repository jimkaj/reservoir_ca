"""Entry point: the SAR geometry evaluation agent.

Looks for reservoirs where a specific Sentinel-1 acquisition geometry (ascending/descending
pass + relative orbit) consistently disagrees with optical, per
reservoir_ca/sar_geometry_evaluation.py. For each one it finds, it first tries a per-geometry
threshold recalibration (reservoir_ca/sar_threshold_calibration.run_geometry_recalibration);
only if that doesn't bring validation IoU above a acceptance bar does it exclude that geometry
for that reservoir going forward (reservoirs/excluded_sar_geometries.csv, which
find_new_scenes consults).

Requires reservoirs/ProcessedImagery.csv to already contain area_m2 and orbit_pass/
relative_orbit -- these are only populated for scenes processed by main.py after the schema
was extended (2026-09-16); run main.py at least once first if the ledger predates that.

Requires Google Earth Engine access -- see reservoir_ca/gee_auth.py.
"""

from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd

from reservoir_ca import config, gee_auth
from reservoir_ca import sar_geometry_evaluation as evaluation


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flag consistently-biased (reservoir, SAR geometry) combinations and "
        "recalibrate or exclude them."
    )
    parser.add_argument(
        "--since",
        default=(dt.date.today() - dt.timedelta(days=730)).isoformat(),
        help="Earliest date to draw a per-geometry recalibration's paired imagery from "
        "(YYYY-MM-DD). Default: 2 years ago, matching calibrate_sar_thresholds.py.",
    )
    parser.add_argument(
        "--project", default=None, help="Google Cloud project ID for Earth Engine."
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    reservoirs = config.load_reservoirs()
    processed = pd.read_csv(config.PROCESSED_IMAGERY_CSV)

    report = evaluation.evaluate_and_resolve(reservoirs, processed, args.since)

    if not report:
        print("No (reservoir, geometry) combinations met the flagging criteria.")
        return

    for row in report:
        detail = ""
        if row["recalibration"] is not None:
            recal = row["recalibration"]
            val_iou = recal["validation_iou"]
            detail = (
                f" -- {recal['band']} @ {recal['threshold_db']} dB, "
                f"area-ratio validation error {recal['area_ratio_validation_error']:.3f}, "
                f"IoU diagnostic: {val_iou if val_iou is None else f'{val_iou:.3f}'}"
            )
        print(
            f"{row['cdec_station_id']} {row['orbit_pass']}/orbit {row['relative_orbit']}: "
            f"{row['outcome']} (n={row['n_samples']}, "
            f"fraction_biased={row['fraction_biased']:.2f}, "
            f"mean_log_error={row['mean_log_error']:+.3f}){detail}"
        )

    outcomes = pd.Series([row["outcome"] for row in report]).value_counts()
    print(f"\n{len(report)} geometry(ies) flagged: " + ", ".join(f"{k}={v}" for k, v in outcomes.items()))


if __name__ == "__main__":
    main()
