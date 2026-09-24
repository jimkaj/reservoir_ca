"""Entry point: run Stage 2 (Volume Conversion & Record Update) across every reservoir with a
capacity curve, per ProjectPlan.docx.

No Earth Engine cost -- pure local interpolation over reservoirs/ProcessedImagery.csv (already
measured by Stage 1) and each reservoir's reservoirs/capacity_curves/<STATION>.csv (built by
build_capacity_curves.py). Run build_capacity_curves.py first for any reservoir that doesn't
have a curve yet.
"""

from __future__ import annotations

import argparse

import pandas as pd

from reservoir_ca import config
from reservoir_ca import stage2_volume_conversion as s2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert measured area to volume and write per-reservoir time series."
    )
    parser.add_argument(
        "--reservoir",
        default=None,
        help="CDEC station ID to convert (e.g. DON). Default: all reservoirs.",
    )
    args = parser.parse_args()

    if args.reservoir:
        reservoirs = config.load_reservoirs(include_excluded=True)
        reservoirs = [r for r in reservoirs if r.cdec_station_id == args.reservoir]
        if not reservoirs:
            raise SystemExit(f"No reservoir with cdec_station_id={args.reservoir!r}")
    else:
        reservoirs = config.load_reservoirs()

    processed = pd.read_csv(config.PROCESSED_IMAGERY_CSV)

    converted = 0
    for reservoir in reservoirs:
        timeseries = s2.convert_reservoir_timeseries(reservoir, processed)
        if timeseries is None:
            print(f"{reservoir.cdec_station_id}: no capacity curve yet, skipped")
            continue
        if timeseries.empty:
            print(f"{reservoir.cdec_station_id}: capacity curve exists but no measured scenes")
            continue
        s2.save_timeseries(reservoir.cdec_station_id, timeseries)
        n_flagged = int(timeseries["out_of_range"].sum())
        print(
            f"{reservoir.cdec_station_id}: {len(timeseries)} record(s) written "
            f"({n_flagged} out-of-range)"
        )
        converted += 1

    print(f"Wrote time series for {converted} reservoir(s).")


if __name__ == "__main__":
    main()
