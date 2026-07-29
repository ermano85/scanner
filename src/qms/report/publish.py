"""Assemble a scan's output into a directory suitable for static hosting.

Deliberately a copy step rather than a git commit. The report inlines every chart as
base64 and runs to roughly 7 MB; committing that nightly would add a couple of gigabytes
of history a year and eventually make the repo painful to clone. GitHub Actions uploads
this directory as a Pages artifact instead, which leaves no trace in git history.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from qms import paths

# Everything a scan produces that is worth serving. Charts are copied as a directory.
SITE_FILES = (
    "index.html",
    "ranked.csv",
    "claude-brief.md",
    "claude-brief.json",
    "tradingview.txt",
)
SITE_DIRS = ("charts",)

# Tells GitHub Pages not to run the output through Jekyll, which would otherwise strip
# files and directories beginning with an underscore.
NOJEKYLL = ".nojekyll"


def latest_scan_date() -> dt.date | None:
    """Newest `out/<date>/` that actually contains a report."""
    if not paths.OUT_DIR.exists():
        return None
    dates = []
    for child in paths.OUT_DIR.iterdir():
        if not child.is_dir() or not (child / "index.html").exists():
            continue
        try:
            dates.append(dt.date.fromisoformat(child.name))
        except ValueError:
            continue
    return max(dates) if dates else None


def publish(as_of: dt.date | None = None, dest: Path | None = None) -> Path:
    """Copy one scan's output into `dest`, replacing whatever was there."""
    scan_date = as_of or latest_scan_date()
    if scan_date is None:
        raise RuntimeError("no scan output found — run `qms nightly` first")

    source = paths.scan_out_dir(scan_date)
    if not (source / "index.html").exists():
        raise RuntimeError(f"no report at {source}")

    destination = dest or (paths.REPO_ROOT / "site")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    copied = 0
    for name in SITE_FILES:
        candidate = source / name
        if candidate.exists():
            shutil.copy2(candidate, destination / name)
            copied += 1
    for name in SITE_DIRS:
        candidate = source / name
        if candidate.is_dir():
            shutil.copytree(candidate, destination / name)
            copied += 1

    (destination / NOJEKYLL).touch()

    print(f"[publish] scan {scan_date}: {copied} item(s) -> {destination}")
    return destination
