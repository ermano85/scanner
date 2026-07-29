"""Assemble the daily review packet: prompt + scan + journal, in one file.

The daily loop has to survive contact with a half hour before the open. Four files to
locate and attach is enough friction to erode a habit; one file is not. This concatenates
the standing instructions, today's scan and the current journal state into a single
document to paste or attach.

The journal is included verbatim rather than summarised. It is small, and a summary would
be one more thing that can drift from what actually happened.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from qms import paths

PROMPT_FILE = paths.REPO_ROOT / "prompts" / "daily-review.md"
JOURNAL_DIR = paths.REPO_ROOT / "journal"
POSITIONS_FILE = JOURNAL_DIR / "positions.csv"
CLOSED_FILE = JOURNAL_DIR / "closed.csv"

# Everything below this marker in the prompt file is the prompt proper; above it is the
# note telling a human how to use it, which the packet does not need.
PROMPT_MARKER = "\n---\n"

# Finished trades carried into the packet. Enough to see how the test is going without
# pasting two months of history every morning.
RECENT_CLOSED = 20


def _prompt_body() -> str:
    if not PROMPT_FILE.exists():
        return "(prompts/daily-review.md is missing)"
    text = PROMPT_FILE.read_text(encoding="utf-8")
    _, separator, body = text.partition(PROMPT_MARKER)
    return (body if separator else text).strip()


def _csv_block(path: Path, title: str, limit: int | None = None) -> str:
    if not path.exists():
        return f"### {title}\n\n(no {path.name} yet)\n"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) <= 1:
        return f"### {title}\n\nNone.\n"
    header, rows = lines[0], lines[1:]
    if limit is not None and len(rows) > limit:
        rows = rows[-limit:]
    body = "\n".join([header, *rows])
    return f"### {title}\n\n```csv\n{body}\n```\n"


def build_packet(as_of: dt.date | None = None, out_dir: Path | None = None) -> Path:
    from qms.report.publish import latest_scan_date

    scan_date = as_of or latest_scan_date()
    if scan_date is None:
        raise RuntimeError("no scan output found — run `qms nightly` first")

    source = out_dir or paths.scan_out_dir(scan_date)
    brief_path = source / "claude-brief.md"
    if not brief_path.exists():
        raise RuntimeError(f"no brief at {brief_path} — run `qms report` first")

    parts = [
        _prompt_body(),
        "",
        "---",
        "",
        "# My journal",
        "",
        _csv_block(POSITIONS_FILE, "Open positions"),
        "",
        _csv_block(CLOSED_FILE, "Closed trades", limit=RECENT_CLOSED),
        "",
        "---",
        "",
        brief_path.read_text(encoding="utf-8"),
    ]

    packet_path = source / "daily-packet.md"
    packet_path.write_text("\n".join(parts), encoding="utf-8")
    size_kb = packet_path.stat().st_size / 1024
    print(f"[packet] {scan_date} -> {packet_path} ({size_kb:.0f} KB)")
    return packet_path
