"""Reservoir list and processed-scene ledger loading, per planning/ProjectPlan.docx."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
RESERVOIRS_DIR = REPO_ROOT / "reservoirs"
RESERVOIR_LIST_CSV = RESERVOIRS_DIR / "ReservoirList.csv"
PROCESSED_IMAGERY_CSV = RESERVOIRS_DIR / "ProcessedImagery.csv"
SAR_THRESHOLD_CALIBRATION_CSV = RESERVOIRS_DIR / "sar_threshold_calibration.csv"

PROCESSED_IMAGERY_COLUMNS = ["sensor", "scene_id", "cdec_station_id", "scene_date"]


@dataclass(frozen=True)
class Reservoir:
    name: str
    cdec_station_id: str
    aoi_geojson_path: Path
    capacity_curve_path: Path | None
    capacity_af: int
    dam_lat: float
    dam_lon: float

    def aoi_geometry(self) -> dict:
        """The AOI polygon as a GeoJSON geometry dict, suitable for ee.Geometry(...)."""
        data = json.loads(self.aoi_geojson_path.read_text())
        return data["features"][0]["geometry"]


def load_reservoirs(csv_path: Path = RESERVOIR_LIST_CSV) -> list[Reservoir]:
    df = pd.read_csv(csv_path)
    reservoirs = []
    for row in df.itertuples(index=False):
        capacity_curve = (
            RESERVOIRS_DIR / row.capacity_curve_path
            if isinstance(row.capacity_curve_path, str) and row.capacity_curve_path
            else None
        )
        reservoirs.append(
            Reservoir(
                name=row.reservoir_name,
                cdec_station_id=row.cdec_station_id,
                aoi_geojson_path=RESERVOIRS_DIR / row.aoi_geojson_path,
                capacity_curve_path=capacity_curve,
                capacity_af=row.capacity_af,
                dam_lat=row.dam_lat,
                dam_lon=row.dam_lon,
            )
        )
    return reservoirs


def load_processed_scene_ids(
    csv_path: Path = PROCESSED_IMAGERY_CSV,
) -> set[tuple[str, str]]:
    """Return the set of (sensor, scene_id) pairs already recorded, so Stage 1 can skip them.

    Scene IDs are only unique within a given sensor's collection (per ProjectPlan.docx),
    hence the (sensor, scene_id) pair rather than scene_id alone.
    """
    if not csv_path.exists():
        return set()
    df = pd.read_csv(csv_path)
    return set(zip(df["sensor"], df["scene_id"]))


def append_processed_scene(
    sensor: str,
    scene_id: str,
    cdec_station_id: str,
    scene_date: str,
    csv_path: Path = PROCESSED_IMAGERY_CSV,
) -> None:
    row = pd.DataFrame(
        [[sensor, scene_id, cdec_station_id, scene_date]],
        columns=PROCESSED_IMAGERY_COLUMNS,
    )
    if csv_path.exists():
        row.to_csv(csv_path, mode="a", header=False, index=False)
    else:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        row.to_csv(csv_path, mode="w", header=True, index=False)


def load_sar_thresholds(
    csv_path: Path = SAR_THRESHOLD_CALIBRATION_CSV,
) -> dict[str, dict]:
    """Return {cdec_station_id: {"band": ..., "threshold_db": ...}} for calibrated reservoirs.

    Empty until the SAR Threshold Calibration step (see ProjectPlan.docx, Stage 1) has been
    run; measure_water_area_sar() falls back to per-scene Otsu thresholding for any reservoir
    missing from this table.
    """
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path)
    return {
        row.cdec_station_id: {"band": row.band, "threshold_db": row.threshold_db}
        for row in df.itertuples(index=False)
    }
