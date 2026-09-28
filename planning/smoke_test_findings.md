# Stage 1 smoke test findings (2026-09-14)

Full run: `python main.py --project reservoir-ca --since 2026-08-01` across all 48 reservoirs,
after rebuilding all AOIs as tight OSM water polygons (see `ProjectPlan.docx` and
`reservoirs/aoi_polygons/`). Ran cleanly end to end — no errors, no crashes.

## Summary

The tight-polygon AOI fix holds up broadly, not just for Shasta. 25 of 45 reservoirs with a
same-day SAR/optical pair show excellent agreement — average ratio 1.00x, using plain
per-scene Otsu thresholding, no calibration. That's a strong validation that tight AOIs were
the right call.

16 reservoirs still show real disagreement, and it's not one uniform cause:

- Most (LON, ICH, DON, TRM, INP, DNN, HID, LBS, UNV, SCC, BUC, CHV, BRD, FRD) are
  smaller/narrower reservoirs where SAR still runs moderately hot (1.1x-2.3x). Plausibly less
  histogram signal for Otsu to work with in a small AOI, and the fixed 75m buffer is
  proportionally larger relative to a small reservoir.
- **SNL (San Luis) runs the other direction** — SAR at 0.73x optical, i.e. SAR
  *under*-measures. San Luis is a big, wind-exposed valley reservoir — consistent with the
  wind-roughened-water failure mode flagged during planning (rough water reads brighter in
  SAR, gets missed as "land").
- **HTH (Hetch Hetchy) is the worst outlier (4.9x avg, 8.1x worst) — but SAR probably isn't
  the actual problem.** Its optical measurements swing wildly between nominally clear dates
  (6.4M m^2 -> 0.8M m^2 -> 5.1M m^2 -> 2.6M m^2 -> 1.4M m^2) while its SAR values stay
  comparatively steady (6.4-8.2M m^2). Hetch Hetchy is a narrow, steep-walled canyon
  reservoir — likely cloud/terrain *shadow* (not cloud itself) is contaminating the NDWI
  optical measurement, since s2cloudless masks clouds, not shadows, and shadowed rock can
  read as "water" under a naive NDWI threshold. If that's right, optical is the unreliable
  measurement there, not SAR.

Also: CLE, WHI, LEW had no new scenes in this window (nothing wrong, just no qualifying
imagery). SHA, FOL, KES, PRR didn't have a same-day S1/S2 pair to compare in this specific
run (not an error — SHA's scenes were mostly already logged from an earlier single-reservoir
test).

## Implication for next steps

This argues for building the SAR Threshold Calibration script next (it should directly help
the "small reservoir" pattern above). But it also suggests the calibration *target* itself
(Dynamic World / optical) may need a shadow-contamination check for narrow canyon reservoirs
like Hetch Hetchy, not just a cloud-cover check — otherwise the calibration step could end up
tuning SAR thresholds to match a broken optical signal for reservoirs like HTH.

**This is the starting point for the next session.**

## Addendum (2026-09-16): re-measured with calibrated SAR thresholds

`reservoir_ca/sar_threshold_calibration.py` calibrated 47/48 reservoirs (see
`reservoirs/sar_threshold_calibration.csv`). `compare_calibrated_thresholds.py` then
re-measured the *exact same* same-day S1/S2 scene pairs already logged in
`ProcessedImagery.csv` from the run above, using each reservoir's calibrated threshold in
place of plain Otsu, and wrote `planning/investigations/smoke_test_calibrated_comparison.csv` (169 pairs
across 42 reservoirs with same-day imagery; CLE/FOL/WHI/PRR/KES/LEW still have none, matching
the original run).

**Headline result: calibration worked, and better than it first looked.** Naively averaged
across all 169 pairs, several previously-flagged reservoirs still looked hot (LON 2.2x, ICH
1.77x, DON 1.45x, etc.) and one — UNV — looked *worse* (2.59x avg). But per-date inspection
showed something the naive average hid: **SAR area was rock-steady across dates for these
reservoirs while optical area cratered on specific dates** — e.g. LON's SAR area stayed
4.66-4.88M m² across three dates while its optical read dropped to 1.3M m² on 2026-09-07 alone
(a 3.6x mismatch on that one date, ~1.0x on the other two). The same three dates —
**2026-08-08, 2026-09-07, 2026-09-12** — account for 17 of the run's 169 pairs but 15 of its
17 worst outliers, hitting seven-plus otherwise-unrelated reservoirs (BRD, DNN, LON, DON, ICH,
CHV, HTH on 09-07 alone). That's a systematic, cross-reservoir optical-measurement problem on
specific S2 acquisition dates, not a per-reservoir SAR calibration problem — and September is
peak California wildfire season, so smoke/haze that s2cloudless's cloud-probability model
doesn't screen for (it detects clouds, not smoke) is the leading suspect. This is the same
failure mode already flagged for Hetch Hetchy, just broader than "canyon shadow" — it's
hitting flat, open reservoirs too, on the same handful of dates.

**Excluding those three dates, agreement is excellent: 32 of 36 reservoirs land within
0.85x-1.15x**, mean ratio 1.109x — a large jump from the original 25/45 under plain Otsu, and
strong confirmation the calibration step is doing its job. Two reservoirs remain genuinely
elevated even on clean dates — **SCC (1.94x)** and **LBS (1.38x)** — both among the weakest
calibration IoUs (0.68 and 0.62 respectively), so these look like real per-reservoir
calibration/AOI issues worth a closer look, not measurement noise. HTH has no clean-date pairs
at all in this sample (all three of its logged pairs fell on suspect dates), consistent with
it being unusually smoke/haze-prone, though its sample is too small to be conclusive alone.

**Implication:** the optical measurement path (`measure_water_area_optical`, and the
calibration target's cloud gate) likely needs a haze/smoke screen in addition to the existing
cloud-probability screen — not just for calibration, but for Stage 1 production measurements,
which would be equally fooled by a smoke-contaminated NDWI read on a live date. **This is the
starting point for the next session**, along with a closer look at SCC and LBS specifically
(narrow AOI geometry vs. genuine calibration difficulty) and why FOL still has insufficient
paired imagery to calibrate at all.
