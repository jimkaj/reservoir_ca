"""Entry point: the AOI/shape diagnosis agent.

For a reservoir whose SAR measurements stay unreliable even after per-geometry recalibration
(reservoir_ca/sar_geometry_evaluation.py) -- or any reservoir worth sanity-checking -- reports
whether Dynamic World's own water record agrees with our AOI polygon, and whether the
reservoir's shape is structurally hard to classify at Sentinel's 10m resolution. See
reservoir_ca/aoi_diagnosis.py for what each check means and why they're separate: the first is
fixable by redrawing the AOI, the second means the reservoir is a candidate for removal from
the monitored set.

Neither check needs per-date pixel sampling (unlike calibration), so this is cheap to run
across many reservoirs -- a couple of reduceRegions and two geometry calls each.

Requires Google Earth Engine access -- see reservoir_ca/gee_auth.py.
"""

from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd

from reservoir_ca import config, gee_auth
from reservoir_ca import aoi_diagnosis as diag


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose whether a reservoir's unreliable SAR measurements trace to an "
        "AOI mismatch (fixable) or intrinsic shape difficulty (not fixable)."
    )
    parser.add_argument(
        "--since",
        default=(dt.date.today() - dt.timedelta(days=730)).isoformat(),
        help="Earliest date for the Dynamic World 'ever water' composite (YYYY-MM-DD). "
        "Default: 2 years ago -- needs to be long enough to have caught the reservoir near "
        "full pool at least once, or over-inclusion can be understated or even manufactured.",
    )
    parser.add_argument(
        "--project", default=None, help="Google Cloud project ID for Earth Engine."
    )
    parser.add_argument(
        "--reservoir",
        default=None,
        help="CDEC station ID to diagnose (e.g. LBS). Default: all reservoirs.",
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    reservoirs = config.load_reservoirs()
    if args.reservoir:
        reservoirs = [r for r in reservoirs if r.cdec_station_id == args.reservoir]
        if not reservoirs:
            raise SystemExit(f"No reservoir with cdec_station_id={args.reservoir!r}")

    rows = []
    for reservoir in reservoirs:
        try:
            result = diag.diagnose_reservoir(reservoir, args.since)
        except Exception as exc:  # one reservoir's failure shouldn't abort the whole run
            print(f"{reservoir.cdec_station_id}: FAILED ({exc})")
            continue
        rows.append(result)
        over = result["over_inclusion_fraction"]
        under = result["under_inclusion_fraction"]
        print(
            f"{reservoir.cdec_station_id}: {result['primary_issue']} "
            f"(over_inclusion={over if over is None else f'{over:.3f}'}, "
            f"under_inclusion={under if under is None else f'{under:.3f}'}, "
            f"edge_pixel_fraction={result['edge_pixel_fraction']:.3f})"
        )

    out_csv = config.RESERVOIRS_DIR / "aoi_diagnosis.csv"
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"Wrote {len(rows)} row(s) to {out_csv}")


if __name__ == "__main__":
    main()
