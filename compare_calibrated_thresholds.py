"""Entry point: re-run the 2026-09-14 smoke test's same-day S1/S2 comparisons now that
reservoirs/sar_threshold_calibration.csv exists, to check whether per-reservoir calibration
resolved the SAR-hot/SAR-cold disagreements recorded in planning/smoke_test_findings.md.

Re-measures the exact scenes already logged in reservoirs/ProcessedImagery.csv -- doesn't
query Earth Engine for new scenes or touch the ledger. Writes
planning/investigations/smoke_test_calibrated_comparison.csv.

Requires Google Earth Engine access -- see reservoir_ca/gee_auth.py.
"""

from __future__ import annotations

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from reservoir_ca import config, gee_auth
from reservoir_ca import smoke_test_compare as compare
from reservoir_ca.config import Reservoir

DEFAULT_WORKERS = 6
OUTPUT_CSV = config.REPO_ROOT / "planning" / "investigations" / "smoke_test_calibrated_comparison.csv"


def _compare_one(
    reservoir: Reservoir, processed: pd.DataFrame, calibration: dict | None
) -> tuple[str, list[dict], Exception | None]:
    try:
        return reservoir.cdec_station_id, compare.compare_reservoir(reservoir, processed, calibration), None
    except Exception as exc:  # one reservoir's failure shouldn't abort the whole comparison
        return reservoir.cdec_station_id, [], exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare SAR vs. optical area for already-logged same-day scene pairs, "
        "using calibrated SAR thresholds where available."
    )
    parser.add_argument(
        "--project", default=None, help="Google Cloud project ID for Earth Engine."
    )
    parser.add_argument(
        "--reservoir",
        default=None,
        help="CDEC station ID to compare (e.g. SHA). Default: all reservoirs.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Reservoirs to compare concurrently (default: {DEFAULT_WORKERS}).",
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    reservoirs = config.load_reservoirs()
    if args.reservoir:
        reservoirs = [r for r in reservoirs if r.cdec_station_id == args.reservoir]
        if not reservoirs:
            raise SystemExit(f"No reservoir with cdec_station_id={args.reservoir!r}")

    processed = pd.read_csv(config.PROCESSED_IMAGERY_CSV)
    sar_thresholds = config.load_sar_thresholds()

    all_rows: list[dict] = []
    lock = threading.Lock()

    def _save_locked() -> None:
        pd.DataFrame(all_rows).to_csv(OUTPUT_CSV, index=False)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _compare_one, reservoir, processed, sar_thresholds.get(reservoir.cdec_station_id)
            ): reservoir
            for reservoir in reservoirs
        }
        for future in as_completed(futures):
            station_id, rows, error = future.result()
            if error is not None:
                print(f"{station_id}: FAILED ({error})")
                continue
            if not rows:
                print(f"{station_id}: no same-day pairs to compare")
                continue
            ratios = [r["ratio"] for r in rows if r["ratio"] is not None]
            avg_ratio = sum(ratios) / len(ratios) if ratios else float("nan")
            method = rows[0]["sar_method"]
            print(
                f"{station_id}: {len(rows)} pair(s), avg ratio {avg_ratio:.2f}x "
                f"(method={method})"
            )
            with lock:
                all_rows.extend(rows)
                _save_locked()

    print(f"Wrote {len(all_rows)} row(s) to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
