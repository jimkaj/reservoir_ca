"""Empirical area->volume capacity curve construction, per ProjectPlan.docx Stage 2 and James's
2026-09-23 decision on how to source it.

The plan originally called for capacity curves "manually curated from CDEC/DWR sources" per
reservoir -- but real bathymetric area-capacity tables turn out to be scattered across
operator-specific PDFs (USBR/USACE water control manuals, hydrographic surveys), inconsistent
per agency, and not something scrapeable at scale across all 48 reservoirs. Instead, each
reservoir's own daily storage (AF) is already public on CDEC (sensor 15, see
reservoir_ca_data_sources memory), and Stage 1 already measures dated surface area for the same
reservoirs -- so an area -> volume curve can be built empirically by pairing our own measured
area with CDEC's reported storage on the same date, fully automatable across the fleet using data
already in hand. Tradeoff, discussed with the user before choosing this: it's self-referential
(calibrating against the same CDEC number Stage 3 was originally meant to independently
cross-check) rather than an independent ground-truth bathymetric survey, and its accuracy
inherits whatever error already exists in the area measurement itself.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from reservoir_ca.config import RESERVOIRS_DIR, Reservoir

CDEC_STORAGE_URL = "https://cdec.water.ca.gov/dynamicapp/req/JSONDataServlet"
CDEC_STORAGE_SENSOR = 15  # AF, daily
CDEC_USER_AGENT = "reservoir-ca-project/0.1 (research use)"
# Transient network failures (seen 2026-09-25: a raw ConnectionResetError mid-response, which is
# an OSError but not a urllib URLError) are retried before giving up, so one blip doesn't abort an
# unattended run or make a reservoir look like it has no CDEC data.
CDEC_FETCH_ATTEMPTS = 3
CDEC_RETRY_BACKOFF_S = 5

CAPACITY_CURVES_DIR = RESERVOIRS_DIR / "capacity_curves"
# One row per empirical curve: the date range of the (area, storage) pairs it was fit on. Stage 3
# needs fit_through to tell a visitor which satellite-vs-CDEC comparisons are genuinely
# out-of-sample (observed after the curve was fit) vs. partly agreeing by construction.
FIT_METADATA_CSV = CAPACITY_CURVES_DIR / "fit_metadata.csv"
FIT_METADATA_COLUMNS = ["cdec_station_id", "fit_from", "fit_through", "n_pairs", "fitted_on"]

# Below this many (area, storage) pairs, a fitted curve is too sparse/narrow-range to trust --
# see reservoir_ca_status memory, 2026-09-23: only reservoirs with a full historical backfill
# (DON, FOL) have enough measured-area range for a real curve as of this writing; most others
# only have a handful of smoke-test-era rows clustered around one fill level.
MIN_PAIRS_TO_FIT = 10

# CDEC uses -9999 (and other negative values) as a missing-data sentinel, not a real reading --
# confirmed on PAR (2026-09-23): 204 of 753 days over a 2-year window read exactly -9999.
# Real storage is never negative, so any non-positive value is dropped outright.
MIN_PLAUSIBLE_STORAGE_AF = 0

# A real reservoir's storage changes at least slightly day to day (evaporation, seepage,
# releases, inflow) -- a run of the exact same value for this many consecutive days is CDEC
# telemetry gone stale, not genuine stability. Confirmed on LBS (2026-09-23): storage read
# exactly 8315 AF for 57 straight days (2026-07-29 to 2026-09-22) before the feed died into
# -9999 entirely. The whole run is dropped, not just days after the threshold, since there's no
# way to tell whether even the first day of a long stuck run was ever a fresh reading.
STUCK_RUN_THRESHOLD_DAYS = 4


def fetch_cdec_storage(cdec_station_id: str, since_date: str, until_date: str) -> pd.DataFrame:
    """Daily reported storage (AF) for `cdec_station_id` between since_date/until_date
    (inclusive), from CDEC's sensor 15 (STORAGE). Returns columns [date, storage_af].

    Drops a date for any of three reasons, none of them a real reading: missing/non-numeric
    value (sensor outage); a non-positive value (CDEC's -9999 missing-data sentinel, see
    MIN_PLAUSIBLE_STORAGE_AF); or membership in a run of STUCK_RUN_THRESHOLD_DAYS+ consecutive
    identical values (stale/stuck telemetry, see STUCK_RUN_THRESHOLD_DAYS).
    """
    params = {
        "Stations": cdec_station_id,
        "SensorNums": CDEC_STORAGE_SENSOR,
        "dur_code": "D",
        "Start": since_date,
        "End": until_date,
    }
    url = f"{CDEC_STORAGE_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": CDEC_USER_AGENT})
    records = None
    for attempt in range(CDEC_FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                records = json.loads(response.read())
            break
        except (OSError, json.JSONDecodeError):  # URLError, ConnectionResetError, timeouts
            if attempt + 1 < CDEC_FETCH_ATTEMPTS:
                time.sleep(CDEC_RETRY_BACKOFF_S * (attempt + 1))
    if records is None:
        return pd.DataFrame(columns=["date", "storage_af"])

    rows = []
    for record in records:
        value = record.get("value")
        if not isinstance(value, (int, float)) or value <= MIN_PLAUSIBLE_STORAGE_AF:
            continue
        date_str = record["date"].split(" ")[0]
        date = _dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        rows.append({"date": date.isoformat(), "storage_af": float(value)})
    df = pd.DataFrame(rows, columns=["date", "storage_af"])
    if df.empty:
        return df

    df = df.sort_values("date").reset_index(drop=True)
    run_id = (df["storage_af"] != df["storage_af"].shift()).cumsum()
    run_length = df.groupby(run_id)["storage_af"].transform("size")
    return df[run_length < STUCK_RUN_THRESHOLD_DAYS].reset_index(drop=True)


def build_area_volume_pairs(
    reservoir: Reservoir, processed: pd.DataFrame, cdec_storage: pd.DataFrame
) -> pd.DataFrame:
    """Same-day (measured area, CDEC storage) pairs for one reservoir, pooling both sensors --
    Sentinel-1 and Sentinel-2 both measure the same physical surface area, so the area->volume
    relationship doesn't depend on which one made a given measurement. Only ledger rows with a
    non-null area_m2 are used -- every "don't trust this row" case (skipped/hazy/cirrus/None)
    already nulls that field, so no extra filtering is needed here.
    """
    measured = processed[
        (processed["cdec_station_id"] == reservoir.cdec_station_id) & processed["area_m2"].notna()
    ][["scene_date", "area_m2"]].rename(columns={"scene_date": "date"})
    return measured.merge(cdec_storage, on="date", how="inner")


def _isotonic_fit(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Monotonic non-decreasing least-squares fit of y over x (pool-adjacent-violators
    algorithm), for x already sorted ascending. A physical reservoir never has less volume at a
    larger surface area, so this both smooths real measurement noise (SAR/optical area error,
    day-to-day CDEC storage reporting noise) and enforces that constraint, rather than literally
    interpolating raw noisy pairs.
    """
    values: list[float] = []
    weights: list[float] = []
    for yi in y:
        values.append(float(yi))
        weights.append(1.0)
        while len(values) > 1 and values[-2] > values[-1]:
            merged_weight = weights[-2] + weights[-1]
            merged_value = (values[-2] * weights[-2] + values[-1] * weights[-1]) / merged_weight
            values.pop()
            weights.pop()
            values[-1] = merged_value
            weights[-1] = merged_weight

    fitted = np.empty(len(y), dtype=float)
    i = 0
    for value, weight in zip(values, weights):
        n = int(round(weight))
        fitted[i : i + n] = value
        i += n
    return fitted


def fit_capacity_curve(pairs: pd.DataFrame) -> pd.DataFrame:
    """Fit an (area_m2, volume_af) lookup table from raw (area_m2, storage_af) pairs.

    Pairs sharing an exact area_m2 are averaged first so the fit has one y per x (ties would
    otherwise let isotonic regression assign them different values, which isn't meaningful for a
    single-valued curve), then isotonic regression enforces monotonicity across the rest.
    """
    grouped = pairs.groupby("area_m2", as_index=False)["storage_af"].mean().sort_values("area_m2")
    area_m2 = grouped["area_m2"].to_numpy()
    fitted_af = _isotonic_fit(area_m2, grouped["storage_af"].to_numpy())
    return pd.DataFrame({"area_m2": area_m2, "volume_af": fitted_af})


def build_capacity_curve(
    reservoir: Reservoir,
    processed: pd.DataFrame,
    since_date: str,
    until_date: str | None = None,
) -> pd.DataFrame | None:
    """Full per-reservoir curve-building step: fetch CDEC storage, pair it against our own
    measured area, and fit. Returns None if fewer than MIN_PAIRS_TO_FIT pairs are available.
    """
    until_date = until_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    cdec_storage = fetch_cdec_storage(reservoir.cdec_station_id, since_date, until_date)
    if cdec_storage.empty:
        return None
    pairs = build_area_volume_pairs(reservoir, processed, cdec_storage)
    if len(pairs) < MIN_PAIRS_TO_FIT:
        return None
    curve = fit_capacity_curve(pairs)
    curve.attrs["fit_metadata"] = {
        "cdec_station_id": reservoir.cdec_station_id,
        "fit_from": pairs["date"].min(),
        "fit_through": pairs["date"].max(),
        "n_pairs": len(pairs),
        "fitted_on": _dt.date.today().isoformat(),
    }
    return curve


def capacity_curve_path(cdec_station_id: str) -> Path:
    return CAPACITY_CURVES_DIR / f"{cdec_station_id}.csv"


def save_capacity_curve(cdec_station_id: str, curve: pd.DataFrame) -> None:
    path = capacity_curve_path(cdec_station_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    curve.to_csv(path, index=False)


def load_fit_metadata() -> pd.DataFrame:
    if not FIT_METADATA_CSV.exists():
        return pd.DataFrame(columns=FIT_METADATA_COLUMNS)
    return pd.read_csv(FIT_METADATA_CSV)


def update_fit_metadata(cdec_station_id: str, metadata: dict | None) -> None:
    """Replace this station's row in FIT_METADATA_CSV, or drop it when `metadata` is None (its
    curve was removed as stale -- see build_capacity_curves.py)."""
    df = load_fit_metadata()
    df = df[df["cdec_station_id"] != cdec_station_id]
    if metadata is not None:
        df = pd.concat([df, pd.DataFrame([metadata], columns=FIT_METADATA_COLUMNS)])
    FIT_METADATA_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values("cdec_station_id").to_csv(FIT_METADATA_CSV, index=False)
