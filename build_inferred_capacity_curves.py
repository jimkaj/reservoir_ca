"""Entry point: infer capacity curves for reservoirs that have no empirical one.

Fits the exponent model (see reservoir_ca/inferred_capacity_curve.py) on every reservoir that
DOES have an empirical curve, then applies it to those that don't. Curves land in
reservoirs/capacity_curves_inferred/<STATION>.csv and are recorded in ReservoirList.csv with
`capacity_curve_method` / `capacity_curve_confidence` so a consumer can never mistake an
inferred curve (~12.5% median error) for an empirical one (~2-5%).

Earth Engine is used only for AOI geometry (area and a one-pixel erosion), not imagery.
"""

from __future__ import annotations

import argparse

import ee
import numpy as np
import pandas as pd

from reservoir_ca import aoi_diagnosis, capacity_curve as cc, config, gee_auth
from reservoir_ca import inferred_capacity_curve as icc


def _reservoir_geometry_features(reservoir) -> tuple[float, float]:
    """(aoi_area_m2, edge_pixel_fraction) -- the two geometric inputs the exponent model needs."""
    shape = aoi_diagnosis.shape_difficulty(reservoir)
    return shape["area_m2"], shape["edge_pixel_fraction"]


def _empirical_exponent(reservoir, curve: pd.DataFrame, aoi_area_m2: float) -> float | None:
    """The exponent that best explains a reservoir's own empirical curve, anchored at
    (aoi_area_m2, capacity_af) -- least squares through the origin in log-log space."""
    valid = curve[(curve.area_m2 > 0) & (curve.volume_af > 0)]
    if valid.empty:
        return None
    logx = np.log(valid.area_m2 / aoi_area_m2)
    logy = np.log(valid.volume_af / reservoir.capacity_af)
    denominator = (logx**2).sum()
    return float((logx * logy).sum() / denominator) if denominator else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Infer capacity curves for reservoirs lacking an empirical one."
    )
    parser.add_argument("--project", default=None, help="Google Cloud project ID for Earth Engine.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be inferred without writing curves or updating ReservoirList.csv.",
    )
    args = parser.parse_args()

    gee_auth.initialize(project=args.project)

    reservoirs = config.load_reservoirs(include_excluded=True)
    reservoir_list = pd.read_csv(config.RESERVOIR_LIST_CSV)
    operators = reservoir_list.set_index("cdec_station_id")["operator_agency"].to_dict()

    print("Measuring AOI geometry for all reservoirs...")
    features = {}
    for reservoir in reservoirs:
        features[reservoir.cdec_station_id] = _reservoir_geometry_features(reservoir)

    # Split into reservoirs that already have an empirical curve (the training set) and those
    # that don't (the targets).
    training_rows, targets = [], []
    for reservoir in reservoirs:
        station = reservoir.cdec_station_id
        aoi_area_m2, edge = features[station]
        empirical_path = cc.capacity_curve_path(station)
        if empirical_path.exists():
            exponent = _empirical_exponent(reservoir, pd.read_csv(empirical_path), aoi_area_m2)
            if exponent is not None:
                training_rows.append(
                    {
                        "cdec_station_id": station,
                        "exponent": exponent,
                        "is_usace": icc.is_usace(operators.get(station)),
                        "edge_pixel_fraction": edge,
                    }
                )
        else:
            targets.append(reservoir)

    training = pd.DataFrame(training_rows)
    model = icc.fit_exponent_model(training)
    print(
        f"\nExponent model fit on {len(training)} empirically-curved reservoirs:\n"
        f"  n = {model['intercept']:.3f} + {model['usace']:.3f}*USACE "
        f"+ {model['edge_pixel_fraction']:.3f}*edge_pixel_fraction\n"
        f"  accepted exponent range: {model['exponent_min']:.3f} - {model['exponent_max']:.3f}\n"
    )

    # Operators with no representation in the training set can't inform a prediction -- those
    # reservoirs get their curve, but flagged low-confidence.
    trained_operators = {
        operators.get(row["cdec_station_id"]) for _, row in training.iterrows()
    }

    reservoir_list["capacity_curve_path"] = reservoir_list["capacity_curve_path"].astype(object)
    for column in ("capacity_curve_method", "capacity_curve_confidence"):
        if column not in reservoir_list.columns:
            reservoir_list[column] = None
        reservoir_list[column] = reservoir_list[column].astype(object)

    # Label the reservoirs that already have empirical curves.
    for _, row in training.iterrows():
        idx = reservoir_list.index[reservoir_list.cdec_station_id == row["cdec_station_id"]][0]
        reservoir_list.loc[idx, "capacity_curve_method"] = "empirical"
        reservoir_list.loc[idx, "capacity_curve_confidence"] = "high"

    reports = []
    for reservoir in targets:
        station = reservoir.cdec_station_id
        aoi_area_m2, edge = features[station]
        operator = operators.get(station)
        curve, report = icc.infer_capacity_curve(reservoir, operator, aoi_area_m2, edge, model)
        operator_known = operator in trained_operators
        report["operator_in_training"] = operator_known
        reports.append(report)

        if curve is None:
            print(
                f"{station}: REFUSED (predicted exponent {report['predicted_exponent']:.3f} "
                f"outside trained range) -- no curve written"
            )
            continue

        confidence = "standard" if operator_known else "low"
        report["confidence"] = confidence
        print(
            f"{station}: inferred n={report['predicted_exponent']:.3f} "
            f"usace={report['is_usace']} operator={operator!r} confidence={confidence}"
        )
        if args.dry_run:
            continue

        icc.save_inferred_curve(station, curve)
        idx = reservoir_list.index[reservoir_list.cdec_station_id == station][0]
        reservoir_list.loc[idx, "capacity_curve_path"] = f"capacity_curves_inferred/{station}.csv"
        reservoir_list.loc[idx, "capacity_curve_method"] = "inferred"
        reservoir_list.loc[idx, "capacity_curve_confidence"] = confidence

    if args.dry_run:
        print("\nDry run -- nothing written.")
        return

    reservoir_list.to_csv(config.RESERVOIR_LIST_CSV, index=False)
    written = sum(1 for r in reports if r["outcome"] == "inferred")
    print(f"\nWrote {written} inferred curve(s); {config.RESERVOIR_LIST_CSV} updated.")


if __name__ == "__main__":
    main()
