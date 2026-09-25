"""Entry point: the scheduled, end-to-end run -- Stage 1 for reservoirs that are due, then Stage 2,
then Stage 3 (imagery + site), then optionally publish. Designed for unattended execution (e.g.
Windows Task Scheduler), per planning/ProjectPlan.docx Orchestration.

Update cadence (James, 2026-09-25): a reservoir needs a new observation about every two days,
not daily. A reservoir is searched for new imagery only if its most recent *measured* scene
(area_m2 present -- a cloud-skipped S2 scene doesn't count) is at least DUE_AFTER_DAYS old;
otherwise it's skipped this run. A due reservoir is searched from SEARCH_OVERLAP_DAYS before its
latest logged scene, because Earth Engine ingests scenes a few days after acquisition -- a scene
dated before our latest logged one can still be new to us. The ledger's (sensor, scene_id,
station) dedup keeps the overlap from double-counting anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

from main import DEFAULT_WORKERS, run_stage1
from publish_site import publish
from reservoir_ca import config, gee_auth
from reservoir_ca import stage2_volume_conversion as s2
from reservoir_ca import stage3_imagery as im
from reservoir_ca import stage3_site as site

DUE_AFTER_DAYS = 2
SEARCH_OVERLAP_DAYS = 7
# A reservoir with nothing in the ledger yet (newly added) is searched this far back.
NEW_RESERVOIR_LOOKBACK_DAYS = 14


def due_reservoirs(
    reservoirs: list[config.Reservoir], processed: pd.DataFrame, today: dt.date
) -> dict[str, str]:
    """{station_id: since_date} for every reservoir due for a Stage 1 search today."""
    due = {}
    for reservoir in reservoirs:
        rows = processed[processed["cdec_station_id"] == reservoir.cdec_station_id]
        measured = rows[rows["area_m2"].notna()]
        if not measured.empty:
            latest_measured = dt.date.fromisoformat(measured["scene_date"].max())
            if (today - latest_measured).days < DUE_AFTER_DAYS:
                continue
        if rows.empty:
            since = today - dt.timedelta(days=NEW_RESERVOIR_LOOKBACK_DAYS)
        else:
            since = dt.date.fromisoformat(rows["scene_date"].max()) - dt.timedelta(days=SEARCH_OVERLAP_DAYS)
        due[reservoir.cdec_station_id] = since.isoformat()
    return due


def run_stage2(reservoirs: list[config.Reservoir]) -> None:
    processed = pd.read_csv(config.PROCESSED_IMAGERY_CSV)
    for reservoir in reservoirs:
        timeseries = s2.convert_reservoir_timeseries(reservoir, processed)
        if timeseries is not None and not timeseries.empty:
            s2.save_timeseries(reservoir.cdec_station_id, timeseries)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full pipeline (Stages 1-3).")
    parser.add_argument("--project", default=None, help="Google Cloud project ID for Earth Engine.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--publish", action="store_true", help="Publish the built site to gh-pages.")
    parser.add_argument("--dry-run", action="store_true", help="Only list which reservoirs are due.")
    args = parser.parse_args()

    today = dt.date.today()
    reservoirs = config.load_reservoirs()
    processed = pd.read_csv(config.PROCESSED_IMAGERY_CSV)
    due = due_reservoirs(reservoirs, processed, today)
    print(f"{today}: {len(due)} of {len(reservoirs)} reservoirs due for a new-imagery search")
    for sid, since in sorted(due.items()):
        print(f"  {sid}: searching since {since}")
    if args.dry_run:
        return

    gee_auth.initialize(project=args.project)
    if due:
        due_list = sorted(
            (r for r in reservoirs if r.cdec_station_id in due),
            key=lambda r: r.capacity_af, reverse=True,
        )
        run_stage1(due_list, due, args.workers)

    run_stage2(reservoirs)

    im.ensure_representative_images(reservoirs)
    im.update_water_masks(reservoirs, pd.read_csv(config.PROCESSED_IMAGERY_CSV))
    summary = site.build_site(reservoirs)
    print(f"Site built: {summary['n_reservoirs']} reservoirs")

    if args.publish:
        commit = publish(Path(summary["site_dir"]), "origin")
        print(f"Published to gh-pages as {commit}")


if __name__ == "__main__":
    main()
