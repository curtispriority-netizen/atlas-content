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

if index["latest"] not in {e["id"] for e in index["editions"]}:
    problems.append("index.json: latest is not in editions")

if problems:
    print("FAILED")
    print("\n".join(problems))
    sys.exit(1)
print(f"OK: {len(index['editions'])} edition(s) valid")
