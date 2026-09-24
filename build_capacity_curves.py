"""Entry point: build each reservoir's empirical area->volume capacity curve.

No Earth Engine cost -- this only reads reservoirs/ProcessedImagery.csv (already-measured area)
and fetches CDEC's own reported daily storage. See reservoir_ca/capacity_curve.py for the
sourcing rationale (James's 2026-09-23 decision) and reservoir_ca_status memory for why most
reservoirs don't have enough measured-area range yet -- a full historical Stage 1 backfill per
reservoir is the real prerequisite for a trustworthy curve, separate from this step.
"""

from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd

from reservoir_ca import capacity_curve as cc
from reservoir_ca import config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build empirical area->volume capacity curves from measured area vs. CDEC storage."
    )
    parser.add_argument(
        "--since",
        default=(dt.date.today() - dt.timedelta(days=730)).isoformat(),
        help="Earliest date to draw area/storage pairs from (YYYY-MM-DD). Default: 2 years ago.",
    )
    parser.add_argument(
        "--reservoir",
        default=None,
        help="CDEC station ID to build a curve for (e.g. DON). Default: all reservoirs.",
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

    reservoir_list_df = pd.read_csv(config.RESERVOIR_LIST_CSV)
    reservoir_list_df["capacity_curve_path"] = reservoir_list_df["capacity_curve_path"].astype(object)
    built = 0
    changed = False
    for reservoir in reservoirs:
        idx = reservoir_list_df.index[
            reservoir_list_df["cdec_station_id"] == reservoir.cdec_station_id
        ][0]
        curve = cc.build_capacity_curve(reservoir, processed, args.since)
        if curve is None:
            print(f"{reservoir.cdec_station_id}: skipped (insufficient area/storage pairs)")
            # Clear any stale path/file from a previous run that no longer has enough data
            # (e.g. CDEC sentinel/stuck-sensor readings got filtered out and dropped this
            # reservoir below MIN_PAIRS_TO_FIT) -- a lingering path would make Stage 2 silently
            # keep using an old, now-untrusted curve instead of skipping the reservoir.
            stale_path = cc.capacity_curve_path(reservoir.cdec_station_id)
            if stale_path.exists():
                stale_path.unlink()
            if pd.notna(reservoir_list_df.loc[idx, "capacity_curve_path"]):
                reservoir_list_df.loc[idx, "capacity_curve_path"] = None
                changed = True
            continue
        cc.save_capacity_curve(reservoir.cdec_station_id, curve)
        reservoir_list_df.loc[idx, "capacity_curve_path"] = (
            f"capacity_curves/{reservoir.cdec_station_id}.csv"
        )
        area_range = f"{curve['area_m2'].min():.3e}-{curve['area_m2'].max():.3e} m^2"
        volume_range = f"{curve['volume_af'].min():.0f}-{curve['volume_af'].max():.0f} AF"
        print(
            f"{reservoir.cdec_station_id}: {len(curve)} points, area {area_range}, "
            f"volume {volume_range}"
        )
        built += 1
        changed = True

    if changed:
        reservoir_list_df.to_csv(config.RESERVOIR_LIST_CSV, index=False)
        print(f"Built {built} capacity curve(s); {config.RESERVOIR_LIST_CSV} updated.")
    else:
        print(f"Built {built} capacity curve(s); ReservoirList.csv unchanged.")


if __name__ == "__main__":
    main()
