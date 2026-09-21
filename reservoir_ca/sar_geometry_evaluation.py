"""Evaluation agent: find (reservoir, Sentinel-1 geometry) combinations whose measurements
consistently disagree with optical, and either recalibrate a threshold just for that geometry
or -- only if recalibration doesn't fix it -- exclude that geometry from future scene
discovery for that reservoir.

Per the 2026-09-16 investigation (planning/smoke_test_findings.md, "Addendum"): the same
Sentinel-1 orbit is fine for most reservoirs that share it, so a bad-looking geometry is almost
always a (reservoir, geometry) *interaction* (e.g. a narrow valley's terrain causing layover/
shadow from one specific look direction), not a defect in the orbit itself. This evaluates per
reservoir, not per orbit globally, and requires the bias to be both large *and* consistently
one-directional -- a reservoir with occasional bad dates in both directions (noise, or a local
event) shouldn't have a geometry excluded on that basis alone.
"""

from __future__ import annotations

import datetime as _dt
import math

import numpy as np
import pandas as pd

from reservoir_ca import config
from reservoir_ca import sar_threshold_calibration as calib
from reservoir_ca.config import Reservoir

MIN_SAMPLES_TO_EVALUATE = 5
MAGNITUDE_THRESHOLD = 0.2  # 20% relative error
FRACTION_BAD_THRESHOLD = 0.7
DIRECTION_THRESHOLD = 0.8
# Recalibration succeeds if its area-ratio validation error clears this bar -- deliberately
# tighter than MAGNITUDE_THRESHOLD (the ~18% flagging bar above, log(1.2)) so a geometry doesn't
# flip between flagged and fixed right at the boundary. Area-ratio, not Dynamic World IoU, is
# the acceptance metric per the 2026-09-16 discussion with the user: IoU is still computed and
# reported (calib.run_geometry_recalibration returns it) as an independent diagnostic, but a
# threshold that fits area-ratio well and shows unexpectedly poor IoU is a signal to investigate
# (see reservoir_ca/aoi_diagnosis.py), not a reason to reject the recalibration outright.
RECALIBRATION_VALIDATION_AREA_RATIO_MAX_ERROR = math.log(1.15)

_LOG_MAGNITUDE_THRESHOLD = math.log(1.0 + MAGNITUDE_THRESHOLD)


def paired_measurements(processed: pd.DataFrame) -> pd.DataFrame:
    """Same-day (S1, S2) measurement pairs from the Stage 1 ledger, with SAR geometry attached.

    Requires ProcessedImagery.csv to carry area_m2 (both sensors) and orbit_pass/relative_orbit
    (S1) -- populated by main.py going forward. Historical rows logged before this schema
    existed won't have these fields and are silently excluded (a null area_m2 or orbit_pass
    just fails to join/cast), not treated as errors.
    """
    s1 = processed[(processed["sensor"] == "S1") & processed["area_m2"].notna()][
        ["cdec_station_id", "scene_date", "area_m2", "orbit_pass", "relative_orbit"]
    ].dropna(subset=["orbit_pass", "relative_orbit"])
    s1 = s1.rename(columns={"scene_date": "date", "area_m2": "sar_area_m2"})
    s1["relative_orbit"] = s1["relative_orbit"].astype(int)

    s2 = processed[(processed["sensor"] == "S2") & processed["area_m2"].notna()][
        ["cdec_station_id", "scene_date", "area_m2"]
    ].rename(columns={"scene_date": "date", "area_m2": "optical_area_m2"})

    merged = s1.merge(s2, on=["cdec_station_id", "date"], how="inner")
    merged["log_ratio"] = np.log(merged["sar_area_m2"] / merged["optical_area_m2"])
    return merged


def evaluate_geometries(paired: pd.DataFrame) -> pd.DataFrame:
    """Flag (station, orbit_pass, relative_orbit) combinations with enough paired dates, a high
    fraction of them over MAGNITUDE_THRESHOLD error, and consistent bias direction.

    Both the fraction *and* direction checks matter: a reservoir that's occasionally way off in
    both directions (small-AOI pixel-count noise, or an isolated local event -- see SCC in the
    2026-09-16 findings) shouldn't be flagged just because its error magnitude is often large.
    Only a geometry whose errors are large **and** consistently one-directional (see LBS in the
    same findings) looks like a genuine geometry defect worth acting on.
    """
    rows = []
    for (station_id, orbit_pass, relative_orbit), group in paired.groupby(
        ["cdec_station_id", "orbit_pass", "relative_orbit"]
    ):
        errors = group["log_ratio"].to_numpy()
        n = len(errors)
        is_bad = np.abs(errors) > _LOG_MAGNITUDE_THRESHOLD
        fraction_bad = float(is_bad.mean()) if n else 0.0
        bad_errors = errors[is_bad]
        direction_fraction = (
            float(max((bad_errors > 0).mean(), (bad_errors < 0).mean())) if bad_errors.size else 0.0
        )
        flagged = (
            n >= MIN_SAMPLES_TO_EVALUATE
            and fraction_bad >= FRACTION_BAD_THRESHOLD
            and direction_fraction >= DIRECTION_THRESHOLD
        )
        rows.append(
            {
                "cdec_station_id": station_id,
                "orbit_pass": orbit_pass,
                "relative_orbit": int(relative_orbit),
                "n_samples": n,
                "fraction_biased": fraction_bad,
                "direction_fraction": direction_fraction,
                "mean_log_error": float(np.mean(errors)) if n else 0.0,
                "flagged": flagged,
            }
        )
    return pd.DataFrame(rows)


def _load_threshold_records() -> dict[tuple, dict]:
    """Existing sar_threshold_calibration.csv rows, keyed for round-trip-safe rewriting."""
    records: dict[tuple, dict] = {}
    thresholds = config.load_sar_thresholds()
    for station_id, entry in thresholds.items():
        if entry["default"] is not None:
            records[(station_id, None, None)] = {
                "cdec_station_id": station_id,
                "orbit_pass": None,
                "relative_orbit": None,
                **entry["default"],
            }
        for (orbit_pass, relative_orbit), record in entry["overrides"].items():
            records[(station_id, orbit_pass, relative_orbit)] = {
                "cdec_station_id": station_id,
                "orbit_pass": orbit_pass,
                "relative_orbit": relative_orbit,
                **record,
            }
    return records


def _load_excluded_records() -> list[dict]:
    """Existing excluded_sar_geometries.csv rows as full dicts (not just the identity set
    config.load_excluded_geometries() returns), so appending a new exclusion doesn't blank out
    the reason/stats columns of previously-excluded geometries on rewrite."""
    if not config.EXCLUDED_SAR_GEOMETRIES_CSV.exists():
        return []
    return pd.read_csv(config.EXCLUDED_SAR_GEOMETRIES_CSV).to_dict("records")


def evaluate_and_resolve(
    reservoirs: list[Reservoir],
    processed: pd.DataFrame,
    since_date: str,
    until_date: str | None = None,
) -> list[dict]:
    """Run the full evaluation agent: flag bad (reservoir, geometry) combos, attempt a
    per-geometry recalibration for each, and record the outcome:

    - "recalibrated": a geometry-specific override threshold now lives in
      sar_threshold_calibration.csv (area-ratio validation error <=
      RECALIBRATION_VALIDATION_AREA_RATIO_MAX_ERROR; Dynamic World IoU is still computed and
      carried in the result as a diagnostic, but doesn't gate this decision).
    - "excluded": recalibration didn't clear that bar, so the geometry is now in
      excluded_sar_geometries.csv and find_new_scenes will skip it for this reservoir.
    - "insufficient_data": too few matching pairs to recalibrate yet (see
      sar_threshold_calibration.MIN_PAIRS_FOR_GEOMETRY_CALIBRATION) -- left flagged, no action
      taken; revisit once more paired dates accumulate rather than excluding on too little data.
    - "would_exclude_last_geometry": recalibration failed its bar, but this is the only
      geometry ever observed for this reservoir that isn't already excluded -- a biased-but-
      present measurement beats zero SAR coverage, so the exclusion is withheld and the
      geometry is left as-is (using whatever threshold/fallback already applies) for manual
      review, rather than silently leaving the reservoir with no SAR data at all.

    Returns one report row per flagged geometry regardless of outcome; writes to
    sar_threshold_calibration.csv / excluded_sar_geometries.csv only for resolved geometries.
    """
    paired = paired_measurements(processed)
    flagged = evaluate_geometries(paired)
    flagged = flagged[flagged["flagged"]]

    reservoir_by_id = {r.cdec_station_id: r for r in reservoirs}
    threshold_records = _load_threshold_records()
    excluded_records = _load_excluded_records()

    # The set of geometries ever seen for each station, from every paired measurement on
    # record (not just flagged ones) -- the best available proxy for "every geometry this
    # reservoir could be measured from" without an extra live Earth Engine query. Used below to
    # make sure exclusion never removes a reservoir's last remaining geometry: a bad-but-present
    # measurement beats none at all, so if excluding a geometry would leave zero, that decision
    # gets downgraded to "would_exclude_last_geometry" (flagged for manual review, no write)
    # instead of silently zeroing out the reservoir's SAR coverage.
    known_geometries: dict[str, set[tuple[str, int]]] = {}
    for station_id, group in paired.groupby("cdec_station_id"):
        known_geometries[station_id] = set(
            zip(group["orbit_pass"], group["relative_orbit"].astype(int))
        )
    excluded_by_station: dict[str, set[tuple[str, int]]] = {}
    for record in excluded_records:
        excluded_by_station.setdefault(record["cdec_station_id"], set()).add(
            (record["orbit_pass"], int(record["relative_orbit"]))
        )

    report = []
    for row in flagged.to_dict("records"):
        station_id = row["cdec_station_id"]
        geometry = (row["orbit_pass"], row["relative_orbit"])
        reservoir = reservoir_by_id.get(station_id)
        if reservoir is None:
            report.append({**row, "outcome": "unknown_reservoir", "recalibration": None})
            continue

        result = calib.run_geometry_recalibration(
            reservoir, row["orbit_pass"], row["relative_orbit"], since_date, until_date
        )
        area_ratio_error = result["area_ratio_validation_error"] if result is not None else None
        if result is None:
            outcome = "insufficient_data"
        elif area_ratio_error is not None and area_ratio_error <= RECALIBRATION_VALIDATION_AREA_RATIO_MAX_ERROR:
            outcome = "recalibrated"
            threshold_records[(station_id, row["orbit_pass"], row["relative_orbit"])] = result
        else:
            already_excluded = excluded_by_station.get(station_id, set())
            remaining = known_geometries.get(station_id, {geometry}) - already_excluded - {geometry}
            if not remaining:
                outcome = "would_exclude_last_geometry"
            else:
                outcome = "excluded"
                excluded_by_station.setdefault(station_id, set()).add(geometry)
                error_str = f"{area_ratio_error:.3f}" if area_ratio_error is not None else "undefined"
                excluded_records.append(
                    {
                        "cdec_station_id": station_id,
                        "orbit_pass": row["orbit_pass"],
                        "relative_orbit": row["relative_orbit"],
                        "reason": (
                            f"recalibration area-ratio validation error {error_str} "
                            f"above {RECALIBRATION_VALIDATION_AREA_RATIO_MAX_ERROR:.3f} "
                            f"(IoU diagnostic: {result['validation_iou']})"
                        ),
                        "n_samples": row["n_samples"],
                        "fraction_biased": row["fraction_biased"],
                        "mean_log_error": row["mean_log_error"],
                        "excluded_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    }
                )

        report.append({**row, "outcome": outcome, "recalibration": result})

    if any(r["outcome"] == "recalibrated" for r in report):
        config.save_sar_thresholds(list(threshold_records.values()))
    if any(r["outcome"] == "excluded" for r in report):
        config.save_excluded_geometries(excluded_records)

    return report
