"""Check every edition against the schema and cross-check ids.

Run: python scripts/validate.py
Exits non-zero if anything is wrong, so the weekly task can refuse to commit.
Needs: pip install jsonschema
"""
import json
import pathlib
import sys

from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parent.parent
schema = json.loads((ROOT / "schema" / "edition.schema.json").read_text())
validator = Draft202012Validator(schema)
index = json.loads((ROOT / "index.json").read_text())
problems = []

for entry in index["editions"]:
    path = ROOT / entry["path"]
    if not path.exists():
        problems.append(f"{entry['id']}: file missing at {entry['path']}")
        continue
    data = json.loads(path.read_text())
    for err in validator.iter_errors(data):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        problems.append(f"{entry['id']}: {loc}: {err.message}")
    if data.get("edition_id") != entry["id"]:
        problems.append(f"{entry['id']}: edition_id inside file is {data.get('edition_id')}")
    source_ids = {s["id"] for s in data.get("sources", [])}
    cards = list(data.get("items", []))
    if data.get("measure_of_week"):
        cards.append(data["measure_of_week"])
    for card in cards:
        for sid in card.get("source_ids", []):
            if sid not in source_ids:
                problems.append(f"{entry['id']}: card cites unknown source {sid}")
    if len(data.get("items", [])) != entry.get("item_count"):
        problems.append(f"{entry['id']}: index item_count does not match")

# ---- Papers Curt added (papers/<id>.json) ----
import copy
study_schema = copy.deepcopy(schema["$defs"]["study"])
study_schema["properties"]["item_id"] = {"type": "string", "pattern": "^mine-[A-Za-z0-9-]+$"}
paper_schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "paper_id", "study", "sources"],
    "properties": {
        "schema_version": {"const": 1},
        "paper_id": {"type": "string"},
        "added_at": {"type": "string"},
        "study": study_schema,
        "sources": {"type": "array", "items": schema["$defs"]["source"]},
    },
    "$defs": schema["$defs"],
}
paper_validator = Draft202012Validator(paper_schema)
pidx_path = ROOT / "papers" / "index.json"
if pidx_path.exists():
    pidx = json.loads(pidx_path.read_text())
    for entry in pidx.get("papers", []):
        path = ROOT / entry["path"]
        if not path.exists():
            problems.append(f"paper {entry['id']}: file missing at {entry['path']}")
            continue
        data = json.loads(path.read_text())
        for err in paper_validator.iter_errors(data):
            loc = "/".join(str(p) for p in err.path) or "(root)"
            problems.append(f"paper {entry['id']}: {loc}: {err.message}")
        if data.get("paper_id") != entry["id"]:
            problems.append(f"paper {entry['id']}: paper_id inside file differs")
        if data.get("study", {}).get("item_id") != f"mine-{entry['id']}":
            problems.append(f"paper {entry['id']}: study.item_id must be mine-{entry['id']}")

if index["latest"] not in {e["id"] for e in index["editions"]}:
    problems.append("index.json: latest is not in editions")

if problems:
    print("FAILED")
    print("\n".join(problems))
    sys.exit(1)
print(f"OK: {len(index['editions'])} edition(s) valid")
