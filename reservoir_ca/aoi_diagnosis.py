"""AOI diagnosis agent: for a reservoir whose SAR measurements stay unreliable even after
per-geometry recalibration (reservoir_ca/sar_geometry_evaluation.py), determine whether the
cause is fixable or intrinsic.

Motivated by the 2026-09-16 LBS investigation: none of LBS's three Sentinel-1 geometries
individually cleared the recalibration IoU bar, including the one with the most historical
coverage. Per-geometry recalibration alone can't explain *why* -- it could be that (a) our AOI
polygon doesn't match what Dynamic World (the calibration target) considers the reservoir's
water extent, which is fixable by redrawing the AOI, or (b) the reservoir's shape is
structurally hard to classify at Sentinel's 10m resolution (long and thin, mostly shoreline),
which no AOI edit or threshold choice fixes -- that reservoir becomes a candidate for removal
from the monitored set instead. This module checks for both, separately:

- Dynamic World agreement (`dynamic_world_agreement`): a temporal "ever water in this window"
  composite, checked both inside the AOI (pixels DW never once calls water suggest our AOI is
  too generous) and in a ring just outside it (pixels DW frequently calls water suggest our AOI
  is missing real water). Needs live Earth Engine calls, but only a couple of reduceRegions --
  no per-date pixel sampling.
- Shape difficulty (`shape_difficulty`): pure geometry, no imagery. Erodes the AOI polygon
  inward by one pixel-width and compares what's left to the original area -- a reservoir that
  loses most of its area to that erosion is mostly shoreline/mixed-pixel terrain by
  construction, independent of any satellite measurement.
"""

from __future__ import annotations

import datetime as _dt
import math

import ee

from reservoir_ca.config import Reservoir
from reservoir_ca.sar_threshold_calibration import DW_WATER_PROB_THRESHOLD, DYNAMIC_WORLD_COLLECTION

PIXEL_SCALE_M = 10  # Sentinel-1/2 pixel size
EROSION_DISTANCE_M = 10  # ~one pixel width, for the shape-difficulty erosion test
RING_BUFFER_M = 30  # how far outside the AOI to check for water DW sees that our AOI misses
# Every AOI in this project deliberately includes this buffer beyond the OSM water polygon
# (added 2026-09-14, see reservoir_ca_osm_aoi_lessons, to avoid clipping shoreline at low
# pool) -- that margin is *expected* to read as non-water most of the time by design, so the
# over-inclusion check below is run on the AOI eroded back by this distance (its approximate
# pre-buffer "core"), not the full buffered AOI, or every reservoir would show a large
# baseline "over-inclusion" that has nothing to do with a mismatched polygon. Confirmed this
# mattered empirically: an earlier version of this check, run against the full buffered AOI,
# flagged SHA (a known-good reservoir, calibration IoU 0.86) at 28% "over-inclusion" -- almost
# as high as LBS's 43% -- purely from this buffer margin.
AOI_BUFFER_M = 75

OVER_INCLUSION_THRESHOLD = 0.15  # >15% of the AOI's core never seen as water -> AOI too generous
UNDER_INCLUSION_THRESHOLD = 0.3  # >30% of the adjacent ring frequently water -> AOI too small
SHAPE_DIFFICULTY_THRESHOLD = 0.5  # >50% of AOI lost to a one-pixel erosion -> mostly shoreline


def shape_difficulty(reservoir: Reservoir) -> dict:
    """Pure-geometry signal for whether a reservoir's shape is structurally hard to classify
    at Sentinel's 10m resolution.

    Runs on the AOI as actually used for measurement (already includes the ~75m buffer added
    during AOI construction, per reservoir_ca_osm_aoi_lessons) rather than the raw OSM water
    polygon -- that's the geometry Stage 1 actually reduces pixels over, so it's the relevant
    one to ask "is this hard to classify." One caveat: that buffer can make an intrinsically
    narrow reservoir look wider than it really is, so a *low* edge_pixel_fraction here doesn't
    fully rule out shape difficulty -- but a *high* one is strong positive evidence, since it
    means even the padded AOI is still mostly shoreline.
    """
    aoi = ee.Geometry(reservoir.aoi_geometry())
    area_m2 = aoi.area(1).getInfo()
    perimeter_m = aoi.perimeter(1).getInfo()
    eroded_area_m2 = aoi.buffer(-EROSION_DISTANCE_M, 1).area(1).getInfo()
    edge_pixel_fraction = 1.0 - (eroded_area_m2 / area_m2 if area_m2 else 0.0)
    isoperimetric_quotient = (
        (4 * math.pi * area_m2 / perimeter_m**2) if perimeter_m else 0.0
    )
    return {
        "area_m2": area_m2,
        "perimeter_m": perimeter_m,
        "edge_pixel_fraction": edge_pixel_fraction,
        "isoperimetric_quotient": isoperimetric_quotient,
        "shape_difficulty_flagged": edge_pixel_fraction >= SHAPE_DIFFICULTY_THRESHOLD,
    }


def _ever_water_mask(geometry: ee.Geometry, since_date: str, until_date: str) -> ee.Image:
    """1 where Dynamic World called this pixel water at least once in the window.

    A temporal composite, not a single-date snapshot -- a reservoir drawn down for part of the
    window shouldn't look falsely over-included just because it wasn't full on every date. The
    window needs to be long enough to have caught the reservoir near full pool at least once
    (the 2-year default calibration window should, for most California reservoirs) or this
    under-states real over-inclusion and can even manufacture a false one.
    """
    return (
        ee.ImageCollection(DYNAMIC_WORLD_COLLECTION)
        .filterBounds(geometry)
        .filterDate(since_date, until_date)
        .select("water")
        .max()
        .gt(DW_WATER_PROB_THRESHOLD)
        .rename("ever_water")
    )


def _mean_fraction(image: ee.Image, geometry: ee.Geometry, band: str, scale: int = PIXEL_SCALE_M) -> float | None:
    stats = image.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geometry, scale=scale, bestEffort=True
    ).getInfo()
    return stats.get(band)


def dynamic_world_agreement(reservoir: Reservoir, since_date: str, until_date: str) -> dict:
    """Whether Dynamic World's own water record agrees with our AOI's footprint, in both
    directions -- see the module docstring.

    over_inclusion_fraction is checked against the AOI's core (eroded back by AOI_BUFFER_M),
    not the full buffered AOI, so the deliberate buffer margin doesn't register as a mismatch.
    If eroding by that much collapses the AOI to nothing, the reservoir's water body is
    narrower than twice the buffer distance -- `core_too_narrow_for_check` flags that case
    (over_inclusion_fraction is None then, not a false 0% or 100%), and it's itself a hint
    toward shape_difficulty rather than an AOI mismatch.
    """
    aoi = ee.Geometry(reservoir.aoi_geometry())
    core = aoi.buffer(-AOI_BUFFER_M, 1)
    core_area_m2 = core.area(1).getInfo()
    ring = aoi.buffer(RING_BUFFER_M).difference(aoi)

    core_too_narrow = not core_area_m2 or core_area_m2 <= 0
    if core_too_narrow:
        over_inclusion_fraction = None
    else:
        inside_fraction = _mean_fraction(_ever_water_mask(core, since_date, until_date), core, "ever_water")
        over_inclusion_fraction = (1.0 - inside_fraction) if inside_fraction is not None else None

    under_inclusion_fraction = _mean_fraction(
        _ever_water_mask(ring, since_date, until_date), ring, "ever_water"
    )

    return {
        "core_too_narrow_for_check": core_too_narrow,
        "over_inclusion_fraction": over_inclusion_fraction,
        "under_inclusion_fraction": under_inclusion_fraction,
        "over_inclusion_flagged": (over_inclusion_fraction or 0.0) >= OVER_INCLUSION_THRESHOLD,
        "under_inclusion_flagged": (under_inclusion_fraction or 0.0) >= UNDER_INCLUSION_THRESHOLD,
    }


def diagnose_reservoir(
    reservoir: Reservoir,
    since_date: str,
    until_date: str | None = None,
) -> dict:
    """Combined AOI-vs-shape diagnosis. Returns every raw metric plus a `primary_issue`
    classification -- AOI and shape problems aren't mutually exclusive (a reservoir can have
    both), so a reviewer should check every *_flagged field, not just primary_issue, before
    deciding whether to redraw an AOI or drop a reservoir from the monitored set.
    """
    until_date = until_date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    shape = shape_difficulty(reservoir)
    agreement = dynamic_world_agreement(reservoir, since_date, until_date)

    if agreement["over_inclusion_flagged"]:
        primary_issue = "aoi_over_inclusion"
    elif agreement["under_inclusion_flagged"]:
        primary_issue = "aoi_under_inclusion"
    elif shape["shape_difficulty_flagged"] or agreement["core_too_narrow_for_check"]:
        # A core too narrow to survive eroding back by the buffer distance is itself strong
        # evidence of shape difficulty (the water body is narrower than ~2x the buffer
        # distance everywhere), even when the coarser one-pixel erosion test doesn't trigger.
        primary_issue = "shape_difficulty"
    else:
        primary_issue = "unexplained"

    return {
        "cdec_station_id": reservoir.cdec_station_id,
        "primary_issue": primary_issue,
        **shape,
        **agreement,
    }
