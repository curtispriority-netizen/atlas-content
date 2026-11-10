"""Read-only checks to run before starting an Atlas model session."""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER_PREFIXES = ("queue__", "question__", "delete__")
PDF_NAME = re.compile(r"([^/\\]{8})__[^/\\]+\.pdf", re.IGNORECASE)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def daily_work(root, names):
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("expected a complete JSON array of Drive file names")
    if any(name.startswith(MARKER_PREFIXES) for name in names):
        return True

    ids = {match[1] for name in names if (match := PDF_NAME.fullmatch(name))}
    if not ids:
        return False

    indexed = {
        entry["id"]: entry["path"] for entry in read_json(root / "papers/index.json")["papers"]
    }
    for paper_id in sorted(ids):
        path = f"papers/{paper_id}.json"
        if indexed.get(paper_id) != path or not (root / path).is_file():
            return True
        paper = read_json(root / path)
        study = paper.get("study") or {}
        appraisal = study.get("appraisal") or {}
        verification = appraisal.get("verification") or {}
        if not (
            paper.get("paper_id") == paper_id
            and study.get("item_id") == f"mine-{paper_id}"
            and study.get("format") == 2
            and study.get("read_level") == "full_text"
            and appraisal.get("appraised_from") == "full_text"
            and verification.get("status") in ("agrees", "disagreements")
            and verification.get("checked_by") == "independent second pass (full text)"
            and verification.get("checked_at")
        ):
            return True
    return False


def weekly_work(root, hobart_date):
    year, week, _ = hobart_date.isocalendar()
    edition_id = f"{year}-W{week:02d}"
    path = f"editions/{edition_id}.json"
    entries = [entry for entry in read_json(root / "index.json")["editions"] if entry["id"] == edition_id]
    exists = (root / path).is_file()
    if not exists and not entries:
        return True
    if (
        not exists or len(entries) != 1 or entries[0]["path"] != path
        or read_json(root / path).get("edition_id") != edition_id
    ):
        raise ValueError(f"{edition_id} edition file and index disagree")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("daily", help="read a complete Drive filename listing from stdin as JSON")
    weekly = modes.add_parser("weekly", help="check the edition for the supplied Hobart date")
    weekly.add_argument("hobart_date", type=date.fromisoformat)
    args = parser.parse_args()
    try:
        if args.mode == "daily":
            needed = daily_work(ROOT, json.loads(sys.stdin.buffer.read()))
        else:
            needed = weekly_work(ROOT, args.hobart_date)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        parser.exit(2, f"Preflight could not determine work: {error}\n")
    print(json.dumps({"work_needed": needed}))


if __name__ == "__main__":
    main()
