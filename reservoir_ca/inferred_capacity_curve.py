"""Infer a capacity curve for a reservoir with too little satellite history to fit an empirical
one (see reservoir_ca/capacity_curve.py), using only metadata available for every reservoir:
capacity_af, AOI polygon area, operator agency, and AOI shape.

Model:  V(A) = capacity_af * (A / aoi_area_m2)^n
        n    = a + b*is_usace + c*edge_pixel_fraction

with (a, b, c) fit across the reservoirs that DO have empirical curves. The curve is anchored so
that V(aoi_area_m2) == capacity_af by construction.

**Accuracy, measured not assumed.** Validated 2026-09-24 by leave-one-reservoir-out
cross-validation over the 38 empirically-curved reservoirs, scored against real CDEC reported
storage (never against our own fitted curves): median error 12.5%, 31/38 reservoirs under 20%,
one above 50%. An *empirical* curve fit from a reservoir's own history achieves ~2-5% by the same
measure, so an inferred curve is roughly 3-5x worse and must stay labeled as such wherever it is
consumed -- hence `curve_method`/`curve_confidence` in ReservoirList.csv.

**Why the operator term matters (the finding that made this workable).** US Army Corps of
Engineers dams are flood-control structures, deliberately held low to preserve flood storage, so
they operate in the deep, narrow bottom of their basin where volume grows steeply with area.
Their fitted exponents average 1.22 versus 0.49 for every other operator, and every large failure
of an operator-blind model was a USACE reservoir (HID went from 141% error to 18% once this single
binary term was added). Basin *shape* metrics alone could not substitute for it.

**Hypotheses tested and rejected**, recorded so a future session doesn't retry them:
- *Terrain steepness* (SRTM slope and elevation standard deviation in a 500m ring around the
  shoreline): correlates only -0.14 with the exponent (-0.07 for elevation SD) and adding it makes
  cross-validation worse (12.5% -> 12.5/12.7% with one more >50% failure). Nearly every reservoir
  here sits in mountainous terrain, so surrounding slope does not discriminate operating regime.
- *A free intercept* (not forcing the curve through the (aoi_area_m2, capacity_af) anchor): worse
  (19.7% vs 18.2% median), ruling out a uniform, globally-correctable anchor bias.
- *capacity_af or average depth as the exponent predictor*: only r=0.25 / r=0.23.

**Precondition.** The model is anchored on the AOI polygon's area, so it fails when that polygon
is untrustworthy. The single >50% cross-validation failure, LBS, is exactly the reservoir with a
known 24.7% AOI over-inclusion problem and two failed redraw attempts. Run
reservoir_ca/aoi_diagnosis.py before trusting an inferred curve for a new reservoir.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from reservoir_ca.config import RESERVOIRS_DIR, Reservoir

INFERRED_CURVES_DIR = RESERVOIRS_DIR / "capacity_curves_inferred"

# Points in the synthesized lookup table, spanning this fraction of AOI area up to the anchor.
# The low end is deliberately well below any plausible real measurement so Stage 2's
# out-of-range flag fires only for genuinely anomalous areas, not for ordinary low pool.
CURVE_POINTS = 100
CURVE_MIN_AREA_FRACTION = 0.02

# A predicted exponent outside the range actually observed among empirically-curved reservoirs is
# unjustified extrapolation, not a physical claim, so no curve is produced for it. Percentile
# bounds rather than raw min/max so a single odd training reservoir can't widen the gate.
# This is what rejects TUL (predicted n=0.025, below every training reservoir): an exponent that
# low implies draining half the surface area barely changes volume, which is not a real reservoir.
EXPONENT_PERCENTILE_BOUNDS = (5.0, 95.0)


def is_usace(operator: str | None) -> bool:
    """Whether this operator is the US Army Corps of Engineers (i.e. the reservoir is
    flood-control operated, held deliberately low) -- the single most important exponent
    predictor, see module docstring.

    Takes the operator string explicitly rather than a Reservoir: `operator_agency` lives in
    ReservoirList.csv but is NOT a field on config.Reservoir, so an attribute-based lookup
    silently returns False for every reservoir and quietly mispredicts every USACE one.
    """
    if not isinstance(operator, str):
        return False
    return "army corps" in operator.lower()


def fit_exponent_model(training: pd.DataFrame) -> dict:
    """Fit n = a + b*is_usace + c*edge_pixel_fraction over reservoirs with empirical curves.

    `training` needs columns: exponent, is_usace, edge_pixel_fraction. One row per reservoir
    (not per observation), so reservoirs with more satellite history don't dominate the fit.
    """
    X = np.column_stack(
        [
            np.ones(len(training)),
            training["is_usace"].astype(float).to_numpy(),
            training["edge_pixel_fraction"].to_numpy(),
        ]
    )
    coefficients, *_ = np.linalg.lstsq(X, training["exponent"].to_numpy(), rcond=None)
    low, high = np.percentile(training["exponent"], EXPONENT_PERCENTILE_BOUNDS)
    return {
        "intercept": float(coefficients[0]),
        "usace": float(coefficients[1]),
        "edge_pixel_fraction": float(coefficients[2]),
        "exponent_min": float(low),
        "exponent_max": float(high),
    }


def predict_exponent(model: dict, usace: bool, edge_pixel_fraction: float) -> float:
    return (
        model["intercept"]
        + model["usace"] * float(usace)
        + model["edge_pixel_fraction"] * edge_pixel_fraction
    )


def infer_capacity_curve(
    reservoir: Reservoir,
    operator: str | None,
    aoi_area_m2: float,
    edge_pixel_fraction: float,
    model: dict,
) -> tuple[pd.DataFrame | None, dict]:
    """Synthesize an (area_m2, volume_af) lookup table for `reservoir`, in the same format
    reservoir_ca/stage2_volume_conversion.py reads for empirical curves.

    Returns (curve, report). `curve` is None when the predicted exponent falls outside the
    training range (see EXPONENT_PERCENTILE_BOUNDS) -- an unjustified extrapolation is refused
    rather than written out looking like any other curve. `report` always explains the outcome.
    """
    usace = is_usace(operator)
    exponent = predict_exponent(model, usace, edge_pixel_fraction)
    report = {
        "cdec_station_id": reservoir.cdec_station_id,
        "predicted_exponent": exponent,
        "is_usace": usace,
        "edge_pixel_fraction": edge_pixel_fraction,
        "aoi_area_m2": aoi_area_m2,
        "capacity_af": reservoir.capacity_af,
    }

    if not (model["exponent_min"] <= exponent <= model["exponent_max"]):
        report["outcome"] = "refused_implausible_exponent"
        return None, report

    areas = np.linspace(CURVE_MIN_AREA_FRACTION * aoi_area_m2, aoi_area_m2, CURVE_POINTS)
    volumes = reservoir.capacity_af * (areas / aoi_area_m2) ** exponent
    report["outcome"] = "inferred"
    return pd.DataFrame({"area_m2": areas, "volume_af": volumes}), report


def inferred_curve_path(cdec_station_id: str):
    """Inferred curves live in their own directory, never mixed in with the empirical ones --
    build_capacity_curves.py deletes a stale empirical curve when a reservoir drops below its
    pair threshold, and would otherwise clobber these."""
    return INFERRED_CURVES_DIR / f"{cdec_station_id}.csv"


def save_inferred_curve(cdec_station_id: str, curve: pd.DataFrame) -> None:
    path = inferred_curve_path(cdec_station_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    curve.to_csv(path, index=False)
