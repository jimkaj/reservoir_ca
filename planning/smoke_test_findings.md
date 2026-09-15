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
