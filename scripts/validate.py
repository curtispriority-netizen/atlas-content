"""Check every edition against the schema and cross-check ids.

Run: python scripts/validate.py
Exits non-zero if anything is wrong, so the tasks can refuse to commit.
Also enforces the CARD_STYLE.md caps on every format-2 card.
Needs: pip install jsonschema
"""
import json
import pathlib
import sys

from jsonschema import Draft202012Validator

from card_style import PROVENANCE, check_card, words

ROOT = pathlib.Path(__file__).resolve().parent.parent
schema = json.loads((ROOT / "schema" / "edition.schema.json").read_text(encoding="utf-8"))
validator = Draft202012Validator(schema)
index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
problems = []

for entry in index["editions"]:
    path = ROOT / entry["path"]
    if not path.exists():
        problems.append(f"{entry['id']}: file missing at {entry['path']}")
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
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
    pidx = json.loads(pidx_path.read_text(encoding="utf-8"))
    for entry in pidx.get("papers", []):
        path = ROOT / entry["path"]
        if not path.exists():
            problems.append(f"paper {entry['id']}: file missing at {entry['path']}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for err in paper_validator.iter_errors(data):
            loc = "/".join(str(p) for p in err.path) or "(root)"
            problems.append(f"paper {entry['id']}: {loc}: {err.message}")
        if data.get("paper_id") != entry["id"]:
            problems.append(f"paper {entry['id']}: paper_id inside file differs")
        if data.get("study", {}).get("item_id") != f"mine-{entry['id']}":
            problems.append(f"paper {entry['id']}: study.item_id must be mine-{entry['id']}")


# ---- Card format 2: CARD_STYLE.md caps ----
# Editions after FORMAT2_FROM and every paper card must be format 2, so the
# structure can't quietly slide back to paragraphs.
FORMAT2_FROM = "2026-W45"  # editions with a later id must use format 2


for entry in index["editions"]:
    path = ROOT / entry["path"]
    if not path.exists():
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    for n, card in enumerate(data.get("items", [])):
        where = f"{entry['id']} item {n + 1}"
        if card.get("format") == 2:
            problems.extend(check_card(where, card))
        elif entry["id"] > FORMAT2_FROM:
            problems.append(f"{where}: new editions must use card format 2 (see CARD_STYLE.md)")

if pidx_path.exists():
    for entry in json.loads(pidx_path.read_text(encoding="utf-8")).get("papers", []):
        path = ROOT / entry["path"]
        if not path.exists():
            continue
        card = json.loads(path.read_text(encoding="utf-8")).get("study", {})
        if card.get("format") != 2:
            problems.append(f"paper {entry['id']}: paper cards must use card format 2 (see CARD_STYLE.md)")
        else:
            problems.extend(check_card(f"paper {entry['id']}", card))


# ---- Question reading lists (questions/<id>.json, see QUESTIONS.md) ----
DESIGNS = {"Guideline", "Meta-analysis", "Systematic review", "RCT", "Cohort", "Other"}
for qpath in sorted((ROOT / "questions").glob("*.json")) if (ROOT / "questions").exists() else []:
    where = f"question {qpath.stem}"
    try:
        qd = json.loads(qpath.read_text(encoding="utf-8"))
    except Exception as e:
        problems.append(f"{where}: not valid JSON ({e})")
        continue
    for k in ("schema_version", "question_id", "question", "searched_at", "search_summary", "suggestions", "gaps"):
        if k not in qd:
            problems.append(f"{where}: {k} is required")
    if qd.get("question_id") != qpath.stem:
        problems.append(f"{where}: question_id must match the file name")
    if words(qd.get("search_summary", "")) > 40:
        problems.append(f"{where}: search_summary is over 40 words")
    sugs = qd.get("suggestions", [])
    if len(sugs) > 12:
        problems.append(f"{where}: {len(sugs)} suggestions (max 12)")
    keys = set()
    for i, sg in enumerate(sugs):
        w = f"{where} suggestion {i + 1}"
        for k in ("key", "title", "citation", "year", "priority", "why"):
            if not sg.get(k):
                problems.append(f"{w}: {k} is required")
        if sg.get("priority") not in ("core", "useful", "background"):
            problems.append(f"{w}: priority must be core, useful or background")
        if sg.get("design") and sg["design"] not in DESIGNS:
            problems.append(f"{w}: design must be one of {sorted(DESIGNS)}")
        if words(sg.get("why", "")) > 20:
            problems.append(f"{w}: why is over 20 words")
        if sg.get("key") in keys:
            problems.append(f"{w}: duplicate key {sg.get('key')}")
        keys.add(sg.get("key"))
    ans = qd.get("answer")
    if ans:
        known_items = {f"mine-{e['id']}" for e in (json.loads(pidx_path.read_text(encoding="utf-8")).get("papers", []) if pidx_path.exists() else [])}
        for e in index["editions"]:
            ep = ROOT / e["path"]
            if ep.exists():
                known_items |= {it.get("item_id") for it in json.loads(ep.read_text(encoding="utf-8")).get("items", [])}
        for k in ("answer", "certainty", "provisional", "actions", "comparison", "based_on", "updated_at"):
            if k not in ans:
                problems.append(f"{where}: answer.{k} is required")
        if words(ans.get("answer", "")) > 50:
            problems.append(f"{where}: answer.answer is over 50 words")
        if ans.get("certainty") not in (None, "high", "moderate", "low", "very low"):
            problems.append(f"{where}: answer.certainty must be high, moderate, low or very low")
        for f, mx, n in (("actions", 6, 22), ("agreement", 4, 22), ("conflicts", 4, 22)):
            items = ans.get(f, [])
            if len(items) > mx:
                problems.append(f"{where}: answer.{f} has more than {mx} items")
            for i, b in enumerate(items):
                if words(b) > n:
                    problems.append(f"{where}: answer.{f}[{i}] is over {n} words")
                if PROVENANCE.search(str(b)):
                    problems.append(f"{where}: answer.{f}[{i}] contains working notes")
        for i, r in enumerate(ans.get("comparison", [])):
            w = f"{where} answer.comparison[{i}]"
            if r.get("item_id") not in known_items:
                problems.append(f"{w}: item_id {r.get('item_id')} is not a card in this repo")
            if words(r.get("finding", "")) > 20:
                problems.append(f"{w}: finding is over 20 words")
            if words(r.get("title", "")) > 12:
                problems.append(f"{w}: title is over 12 words")
            if r.get("verdict") not in ("meaningful", "below_mcid", "no_difference", "unclear", "descriptive"):
                problems.append(f"{w}: verdict must be one of the CARD_STYLE.md verdicts")
    gaps = qd.get("gaps", [])
    if len(gaps) > 4:
        problems.append(f"{where}: more than 4 gaps")
    for i, g in enumerate(gaps):
        if words(g) > 20:
            problems.append(f"{where}: gaps[{i}] is over 20 words")

if index["editions"] and index["latest"] not in {e["id"] for e in index["editions"]}:
    problems.append("index.json: latest is not in editions")

if problems:
    print("FAILED")
    print("\n".join(problems))
    sys.exit(1)
print(f"OK: {len(index['editions'])} edition(s) valid")
