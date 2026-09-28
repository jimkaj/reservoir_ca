# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Estimates California reservoir water capacity from satellite imagery. Sentinel-1 SAR (primary,
cloud-immune) and Sentinel-2 optical (secondary, validates SAR) are queried and measured entirely
server-side through Google Earth Engine — no imagery is downloaded locally. Full design rationale
is in `planning/ProjectPlan.docx`; `planning/smoke_test_findings.md` has the original 48-reservoir
smoke-test writeup. This is a sequential pipeline, not an agent system: Stage 1 (Query & Measure)
→ Stage 2 (Volume Conversion) → Stage 3 (Reporting: a static website).

## Commands

Earth Engine auth (once per machine): `earthengine authenticate`. Every script below takes
`--project <gcp-project-id>` (or set `GEE_PROJECT`); the project used in development is
`reservoir-ca`. Dependencies are managed with `uv`: `uv sync` builds `.venv` from `uv.lock`,
`uv add <pkg>` adds one. Prefix the commands below with `uv run` (e.g. `uv run python main.py ...`)
or activate `.venv` first.

```bash
# Stage 1: query + measure new scenes for all monitored reservoirs since a date
python main.py --project reservoir-ca --since 2024-09-23 --workers 6
python main.py --project reservoir-ca --reservoir SHA --since 2024-09-23   # single reservoir,
                                                                             # works even if excluded

# SAR threshold calibration (run after enough imagery has accumulated for a reservoir)
python calibrate_sar_thresholds.py --project reservoir-ca --since 2024-09-23
python evaluate_sar_geometries.py --project reservoir-ca   # per-orbit override/exclusion agent

# AOI/shape diagnosis (no imagery cost, just geometry + Dynamic World)
python diagnose_aois.py --project reservoir-ca --reservoir <ID>

# Stage 2: build capacity curves, then convert measured area -> volume
python build_capacity_curves.py --since 2024-09-23          # empirical, needs >=10 area/storage pairs
python build_inferred_capacity_curves.py --project reservoir-ca --dry-run   # for reservoirs without one
python run_stage2.py                                        # writes reservoirs/timeseries/<ID>.csv

# Stage 3: refresh imagery (representative images once, water masks when a newer scene lands),
# build the static site into ./site, publish it to the gh-pages branch
python build_site.py --project reservoir-ca                 # --skip-imagery: no Earth Engine
python publish_site.py                                      # force-pushes a history-less gh-pages

# Scheduled end-to-end run: Stage 1 only for reservoirs whose latest measured scene is >=2 days
# old, then Stage 2, then Stage 3
python run_pipeline.py --project reservoir-ca --dry-run     # list what's due; add --publish to publish
```

**Scheduling:** Windows Task Scheduler task "reservoir_ca daily pipeline" runs
`scheduling/scheduled_run.ps1` (-> `run_pipeline.py --publish`) at logon (+5 min) and daily at
09:00, at most once per day: a successful run writes the date to `logs/last_success.txt`, a failed
one is retried at the next trigger. After a successful run it commits only the files the pipeline
writes (`ProcessedImagery.csv`, `timeseries/`, `site_assets/representative/`) and pushes master
-- which also pushes any other unpushed local commits. Daily logs in `logs/` (gitignored). Re-create the task with
`powershell -ExecutionPolicy Bypass -File schedulingegister_task.ps1`.

There is no automated test suite and no linter config. Every non-trivial script/function in this
repo has instead been validated by running it live against real Earth Engine / CDEC data at
production scale (not a small sample) and checking the output against an independent reference —
usually CDEC's own reported storage, or Google's Dynamic World product. Follow that pattern for
new work: a quick small-scale check is not sufficient, this codebase has repeatedly had bugs that
only appeared at full scale or on specific reservoirs.

## Architecture

**Three-stage pipeline, one row of state per (reservoir, sensor, scene) or (reservoir, date):**

- **Stage 1** (`reservoir_ca/stage1_query_measure.py`, driven by `main.py`): finds new S1/S2
  scenes covering each reservoir's AOI, measures water surface area (calibrated SAR backscatter
  threshold, or Otsu fallback; NDWI for optical with cloud/cirrus/haze screening), and appends one
  row per scene to `reservoirs/ProcessedImagery.csv` — the ledger every other stage reads from.
  Runs reservoirs concurrently (`--workers`, `ThreadPoolExecutor`), each reservoir's own Earth
  Engine calls stay serial.
- **SAR Threshold Calibration** (`reservoir_ca/sar_threshold_calibration.py`): pairs S1 dates with
  the nearest usable S2/Dynamic World date, sweeps candidate backscatter thresholds, and picks the
  one that best matches Stage 1's *own* optical area measurement (not Dynamic World IoU — that's
  computed only as a secondary diagnostic). Writes `reservoirs/sar_threshold_calibration.csv`.
  Per-geometry overrides (one Sentinel-1 orbit reading consistently biased for one reservoir) and
  exclusions live in the same file / `reservoirs/excluded_sar_geometries.csv`, written by
  `reservoir_ca/sar_geometry_evaluation.py`.
- **Stage 2** (`reservoir_ca/capacity_curve.py`, `stage2_volume_conversion.py`): converts measured
  area to volume via a per-reservoir area→volume curve, `reservoirs/capacity_curves/<ID>.csv`.
  Curves are **not** manually sourced from agency bathymetric tables (the original plan) — they're
  fit empirically by pairing Stage 1's own measured area against CDEC's publicly reported daily
  storage on the same date (`reservoir_ca/capacity_curve.py:fetch_cdec_storage`), isotonic
  regression enforces monotonicity. For reservoirs with too little history to fit one,
  `reservoir_ca/inferred_capacity_curve.py` predicts a curve from reservoir metadata alone (see
  below); these live in a *separate* directory, `reservoirs/capacity_curves_inferred/`, and are
  flagged in `ReservoirList.csv`. Output time series: `reservoirs/timeseries/<ID>.csv`, with a
  neighbour-consistency QC flag per observation (`neighbour_consistency`: area >20% from the
  median of other observations within ±7 days → `rejected`; flagged, never deleted). Stage 3 uses
  only non-rejected observations and prefers Sentinel-2 over Sentinel-1 on a shared date
  (`stage3_aggregate.usable`).
- **Stage 3** (`reservoir_ca/stage3_aggregate.py`, `stage3_imagery.py`, `stage3_site.py`, page
  templates in `reservoir_ca/site_template/`): a static site with page data embedded as JSON, so it
  needs no server. The home page's fleet total carries each reservoir's latest value forward
  daily (`fleet_daily_totals`). Water masks are rebuilt from the latest ledger row via the *same*
  Stage 1 classifiers (`sar_water_mask`/`optical_water_mask`), and each mask's area must match the
  ledger or it isn't published. Representative images are committed
  (`reservoirs/site_assets/representative/`); masks and `./site` are gitignored and exist only on
  the `gh-pages` branch, which `publish_site.py` replaces with a single parentless commit each time.
  `reservoirs/capacity_curves/fit_metadata.csv` records each curve's fit window, so the site can
  say which CDEC comparisons are out-of-sample.

**Config and the reservoir list** (`reservoir_ca/config.py`): `ReservoirList.csv` is the master
table — one row per reservoir, `excluded`/`exclusion_reason` columns gate whether
`config.load_reservoirs()` includes it (pass `include_excluded=True` for one-off tooling or an
explicit `--reservoir <ID>` — a reservoir named explicitly should always be reachable even if
excluded). `seasonal_exclusion_months` additionally skips specific calendar months per reservoir
(e.g. winter-ice contamination at high-Sierra reservoirs) at scene-discovery time, not just in
reporting.

**Scene identity**: a scene's ledger key is `(sensor, scene_id, cdec_station_id)` — the station id
is load-bearing, not decorative. Scene IDs are unique within a sensor's collection but *not* unique
to one reservoir: a single Sentinel-1 swath or Sentinel-2 tile routinely covers several reservoirs.
A 2-tuple key would make one reservoir's logged scene look "already processed" for a different
reservoir sharing the same swath/tile. An AOI that straddles an MGRS tile boundary (so no single
S2 scene ever covers it) gets a multi-tile mosaic instead, identified by a `"+"`-joined,
sorted list of tile scene_ids (`stage1_query_measure.s2_covering_groups`/`build_s2_mosaic`).

**Diagnostic/investigation scripts** (`aoi_diagnosis.py`, `weather_cross_reference.py`,
`cirrus_screening.py`, `haze_screening.py`, `smoke_test_compare.py`; their result CSVs live in
`planning/investigations/`, not `reservoirs/`, which holds only what the pipeline uses)
exist because a reservoir's SAR/optical disagreement has, historically, had several different
root causes (AOI mismatch, wind-roughened water, thin cirrus that `s2cloudless` misses, smoke/haze,
winter ice, USACE flood-control drawdown regime, GEE catalog scenes with corrupted footprints).
Each script isolates one hypothesis against real data rather than guessing; check
`planning/smoke_test_findings.md` and the project's saved investigation history before assuming a
new disagreement needs a new root cause.

## Gotchas

- **GEE noncommercial compute quota is a real, hit-in-practice limit.** A multi-hour, many-
  reservoir run can silently exhaust it; the project then enters "restricted mode" and calls
  either fail loudly or hang with no error, no crash, and no log line (Python's stdout buffers
  when redirected to a file). If a GEE-heavy background run looks stalled far longer than any
  comparable prior step took, check for a `UserWarning` mentioning "restricted mode" before
  assuming a code bug.
- **This machine (dev environment) runs an antivirus TLS-inspection proxy** that breaks Python's
  bundled CA bundle for any `requests`/`urllib3` call, including `ee.Initialize()`/
  `ee.Authenticate()`. Handled by `pip-system-certs` (a Windows-only project dependency), which
  makes Python use the Windows certificate store. `uv` itself trusts the proxy via the
  `UV_SYSTEM_CERTS=true` user env var. Do **not** set `SSL_CERT_FILE` to a hand-built bundle:
  Norton regenerates its root CA (it did on 2026-09-09), and a stale copy with the same name makes
  uv fail with `invalid peer certificate: BadSignature`.
- **`ee.ImageCollection.geometry().contains(aoi, ...)` can return `True` for a corrupted GEE
  catalog scene** whose footprint is a degenerate global polygon (`[-Infinity,-Infinity]` to
  `[Infinity,Infinity]`) — seen on a real Sentinel-2 scene. Every coverage check in this codebase
  runs through `stage1_query_measure.filter_plausible_footprint` to guard against this; don't
  reintroduce a raw `.contains()` check without it.
- **Don't duck-type attributes off `config.Reservoir`.** It's a narrow dataclass (see
  `config.py`) — fields like `operator_agency` live only in `ReservoirList.csv`, not on the
  dataclass. `getattr(reservoir, "operator_agency", default)` silently returns the default instead
  of erroring; this exact pattern was caught in code review before it shipped, after it would have
  silently mispredicted every USACE reservoir. Pass the value explicitly from the CSV instead.
- **CDEC's daily storage feed has two real, distinct failure modes**, both handled in
  `capacity_curve.fetch_cdec_storage`: `-9999` as a silent missing-data sentinel, and multi-week
  runs of a frozen/stale sensor reading (real example: one station read the exact same value for
  57 consecutive days). Neither is flagged by CDEC's own `dataFlag` field — both are detected
  heuristically (non-positive values dropped; runs of 4+ identical consecutive values dropped).

## Current status

38 of 48 reservoirs are actively monitored; 10 are excluded (unfixable SAR/optical disagreement,
each with a specific documented reason in `ReservoirList.csv`'s `exclusion_reason` column — not a
blanket "data quality" note). All 38 monitored reservoirs have a full 2-year Stage 1 backfill and
an empirical Stage 2 capacity curve; the 10 excluded reservoirs mostly have an *inferred* curve
instead (metadata-only prediction, ~12% median error vs. ~2-5% for empirical — see
`reservoir_ca/inferred_capacity_curve.py`'s docstring for the validation methodology and the
operator-regime finding that made it work). Stage 3 (the reporting site) is built; see Architecture.
