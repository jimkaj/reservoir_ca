"""Entry point: the haze/smoke screening agent.

Retroactively finds already-measured Sentinel-2 optical scenes in ProcessedImagery.csv that
were contaminated by atmospheric haze/smoke (via MODIS MAIAC AOD, see
reservoir_ca/haze_screening.py) and corrects the ledger: nulls their area_m2 and sets
method="hazy", so every downstream consumer that already filters on area_m2.notna() ignores
them without further changes.

Doesn't touch scenes measured after the live gate
(stage1_query_measure.measure_water_area_optical's max_aod parameter) was added -- those were
already screened prospectively and never got an area_m2 to begin with if hazy.

Requires Google Earth Engine access -- see reservoir_ca/gee_auth.py.
"""

from __future__ import annotations

import argparse

import pandas as pd

from reservoir_ca import config, gee_auth
from reservoir_ca import haze_screening as haze
from reservoir_ca.stage1_query_measure import DEFAULT_MAX_AOD


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retroactively screen the ledger for haze-contaminated optical scenes."
    )
    parser.add_argument(
        "--max-aod",
        type=float,
        default=DEFAULT_MAX_AOD,
        help=f"AOD above which a scene is considered hazy (default: {DEFAULT_MAX_AOD}).",
    )
    parser.add_argument(
        "--project", default=None, help="Google Cloud project ID for Earth Engine."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing the ledger.",
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    reservoirs = config.load_reservoirs()
    processed = pd.read_csv(config.PROCESSED_IMAGERY_CSV)

    corrected = haze.screen_ledger_for_haze(processed, reservoirs, max_aod=args.max_aod)

    was_blank = processed["method"].isna()
    now_hazy = corrected["method"] == "hazy"
    newly_flagged = corrected[was_blank.to_numpy() & now_hazy.to_numpy()]

    print(f"Checked {(processed['sensor'] == 'S2').sum()} S2 scene(s); flagged {len(newly_flagged)} as hazy:")
    if len(newly_flagged):
        print(newly_flagged[["cdec_station_id", "scene_date", "scene_id", "aod"]].to_string(index=False))

    if args.dry_run:
        print("Dry run -- ledger not written.")
        return

    corrected.to_csv(config.PROCESSED_IMAGERY_CSV, index=False)
    print(f"Wrote corrected ledger to {config.PROCESSED_IMAGERY_CSV}")


if __name__ == "__main__":
    main()
