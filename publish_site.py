"""Entry point: publish the built site (./site, from build_site.py) to the repo's gh-pages branch.

Each publish replaces the branch with a single new commit that has no parent, then force-pushes
it, so the branch never accumulates history: only the current masks, pages and data ever exist
on it (old versions become unreachable and GitHub garbage-collects them). The code branch is
untouched -- this works in a throwaway repository in a temp directory, not in this working tree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import tempfile
from pathlib import Path

from reservoir_ca.config import REPO_ROOT
from reservoir_ca.stage3_site import DEFAULT_SITE_DIR

BRANCH = "gh-pages"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def publish(site_dir: Path, remote: str) -> str:
    if not (site_dir / "index.html").exists():
        raise SystemExit(f"{site_dir} has no index.html -- run build_site.py first")
    remote_url = _git("remote", "get-url", remote, cwd=REPO_ROOT)
    user_name = _git("config", "user.name", cwd=REPO_ROOT)
    user_email = _git("config", "user.email", cwd=REPO_ROOT)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    with tempfile.TemporaryDirectory(prefix="reservoir_site_") as tmp:
        work = Path(tmp) / "site"
        shutil.copytree(site_dir, work)
        _git("init", "--quiet", cwd=work)
        _git("checkout", "--quiet", "--orphan", BRANCH, cwd=work)
        _git("add", "--all", cwd=work)
        _git(
            "-c", f"user.name={user_name}", "-c", f"user.email={user_email}",
            "commit", "--quiet", "-m", f"Publish site ({stamp})",
            cwd=work,
        )
        commit = _git("rev-parse", "--short", "HEAD", cwd=work)
        _git("push", "--quiet", "--force", remote_url, f"{BRANCH}:{BRANCH}", cwd=work)
    return commit


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish ./site to the gh-pages branch.")
    parser.add_argument("--site", default=str(DEFAULT_SITE_DIR), help="Built site directory.")
    parser.add_argument("--remote", default="origin", help="Git remote to push to (default: origin).")
    args = parser.parse_args()
    commit = publish(Path(args.site), args.remote)
    print(f"Published {args.site} to {args.remote}/{BRANCH} as {commit}")


if __name__ == "__main__":
    main()
