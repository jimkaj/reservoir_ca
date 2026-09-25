"""Entry point: run Stage 3 (Reporting) -- refresh reservoir imagery, then build the static site.

Imagery step (needs Earth Engine; skip with --skip-imagery): generates a representative image for
any monitored reservoir that doesn't have one yet (one-time), and regenerates the water mask for
any reservoir whose latest measured scene is newer than its current mask. See
reservoir_ca/stage3_imagery.py.

Site step (no Earth Engine): writes the site into ./site (or --out), replacing it. Run
run_stage2.py first so the time series are current. Publish with publish_site.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from reservoir_ca import config, gee_auth
from reservoir_ca import stage3_imagery as im
from reservoir_ca import stage3_site as site


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Stage 3 reporting website.")
    parser.add_argument("--project", default=None, help="Google Cloud project ID for Earth Engine.")
    parser.add_argument("--out", default=str(site.DEFAULT_SITE_DIR), help="Output directory (replaced).")
    parser.add_argument(
        "--skip-imagery", action="store_true",
        help="Don't touch Earth Engine; build from the representative images and masks already on disk.",
    )
    parser.add_argument(
        "--regenerate-representative", action="store_true",
        help="Re-render every reservoir's representative image, not just missing ones.",
    )
    parser.add_argument(
        "--force-masks", action="store_true",
        help="Re-render every reservoir's water mask, even if its latest scene hasn't changed.",
    )
    args = parser.parse_args()

    reservoirs = config.load_reservoirs()

    if not args.skip_imagery:
        gee_auth.initialize(project=args.project)
        im.ensure_representative_images(reservoirs, regenerate=args.regenerate_representative)
        processed = pd.read_csv(config.PROCESSED_IMAGERY_CSV)
        im.update_water_masks(reservoirs, processed, force=args.force_masks)

    summary = site.build_site(reservoirs, site_dir=Path(args.out))
    print(
        f"Site written to {summary['site_dir']}: {summary['n_reservoirs']} reservoirs, "
        f"total {summary['total_volume_af'] / 1e6:.2f}M of {summary['total_capacity_af'] / 1e6:.2f}M AF"
    )


if __name__ == "__main__":
    main()
