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


# ---- Card format 2: CARD_STYLE.md caps ----
# Editions after FORMAT2_FROM and every paper card must be format 2, so the
# structure can't quietly slide back to paragraphs.
import re

FORMAT2_FROM = "2026-W45"  # editions with a later id must use format 2
PROVENANCE = re.compile(r"\b(p\.\s?\d|I (did not|didn't|could not|couldn't|was unable)|not in the PDF)", re.I)


def words(t):
    return len(str(t).split())


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def check_card(where, c):
    out = []

    def cap(field, value, n):
        if value is not None and words(value) > n:
            out.append(f"{where}: {field} is {words(value)} words (max {n}): {str(value)[:60]}…")

    def bullets(field, value, max_items, n, min_items=0, must_be_list=True):
        if value is None:
            if min_items:
                out.append(f"{where}: {field} is required")
            return
        if must_be_list and not isinstance(value, list):
            out.append(f"{where}: {field} must be a list of bullets, not a paragraph")
            value = [value]
        if len(value) > max_items or len(value) < min_items:
            out.append(f"{where}: {field} has {len(value)} items (allowed {min_items}-{max_items})")
        for i, b in enumerate(value):
            cap(f"{field}[{i}]", b, n)

    def clinical(field, value):
        for b in as_list(value):
            if PROVENANCE.search(str(b)):
                out.append(f"{where}: {field} contains provenance/working notes (move to verification, support or detail): {str(b)[:60]}…")

    cap("title", c.get("title"), 14)
    cap("impact_line", c.get("impact_line"), 25)
    bullets("key_findings", c.get("key_findings"), 5, 20, min_items=3)
    if isinstance(c.get("bottom_line"), list):
        out.append(f"{where}: bottom_line should be one short string")
    cap("bottom_line", c.get("bottom_line"), 40)
    bullets("summary", c.get("summary"), 5, 25, min_items=3)
    for i, k in enumerate(c.get("key_numbers", [])):
        cap(f"key_numbers[{i}].value", k.get("value"), 4)
        cap(f"key_numbers[{i}].label", k.get("label"), 10)
    bullets("how_to_apply", c.get("how_to_apply"), 6, 22)
    for i, r in enumerate(c.get("results", [])):
        w = f"results[{i}]"
        if "verdict" not in r:
            out.append(f"{where}: {w}.verdict is required")
        for f, n in (("outcome", 6), ("timepoint", 8), ("result", 15), ("effect", 20), ("meaning", 12), ("detail", 60)):
            for b in as_list(r.get(f)):
                cap(f"{w}.{f}", b, n)
        for f in ("result", "meaning", "effect"):
            clinical(f"{w}.{f}", r.get(f))
        if re.search(r"MCID not applicable|not a comparative effect", " ".join(map(str, as_list(r.get("meaning")) + as_list(r.get("effect")))), re.I):
            out.append(f"{where}: {w} has a filler line; omit effect/meaning and use verdict 'descriptive'")
    bullets("applies_to", c.get("applies_to"), 4, 15)
    bullets("not_for", c.get("not_for"), 4, 15)
    bullets("limits", c.get("limits"), 5, 20)
    cap("patient_explanation", c.get("patient_explanation"), 45)
    bullets("open_questions", c.get("open_questions"), 4, 20, must_be_list=False)
    cap("confidence_note", " ".join(as_list(c.get("confidence_note"))), 30)
    cap("evidence_note", " ".join(as_list(c.get("evidence_note"))), 30)
    for f in ("key_findings", "summary", "how_to_apply", "applies_to", "not_for", "limits", "bottom_line", "impact_line"):
        clinical(f, c.get(f))
    a = c.get("appraisal") or {}
    bullets("appraisal.clinical_significance", a.get("clinical_significance"), 4, 25)
    for f in ("comparator_fairness", "alternative_explanations", "spin", "fit_with_evidence", "external_validity", "would_change_my_mind"):
        bullets(f"appraisal.{f}", a.get(f), 4, 22)
        clinical(f"appraisal.{f}", a.get(f))
    for group in ("domains", "grade"):
        for i, d in enumerate(a.get(group, [])):
            cap(f"appraisal.{group}[{i}].reason", d.get("reason"), 25)
            cap(f"appraisal.{group}[{i}].support", d.get("support"), 20)
    bullets("appraisal.strengths", a.get("strengths"), 6, 15)
    bullets("appraisal.weaknesses", a.get("weaknesses"), 6, 15)
    v = a.get("verification") or {}
    for i, d in enumerate(v.get("disagreements", [])):
        for f, n in (("item", 8), ("first", 12), ("second", 12), ("why", 25), ("resolved", 25)):
            cap(f"verification.disagreements[{i}].{f}", d.get(f), n)
    cap("verification.external_searched", v.get("external_searched"), 30)
    return out


for entry in index["editions"]:
    path = ROOT / entry["path"]
    if not path.exists():
        continue
    data = json.loads(path.read_text())
    for n, card in enumerate(data.get("items", [])):
        where = f"{entry['id']} item {n + 1}"
        if card.get("format") == 2:
            problems.extend(check_card(where, card))
        elif entry["id"] > FORMAT2_FROM:
            problems.append(f"{where}: new editions must use card format 2 (see CARD_STYLE.md)")

if pidx_path.exists():
    for entry in json.loads(pidx_path.read_text()).get("papers", []):
        path = ROOT / entry["path"]
        if not path.exists():
            continue
        card = json.loads(path.read_text()).get("study", {})
        if card.get("format") != 2:
            problems.append(f"paper {entry['id']}: paper cards must use card format 2 (see CARD_STYLE.md)")
        else:
            problems.extend(check_card(f"paper {entry['id']}", card))

if index["editions"] and index["latest"] not in {e["id"] for e in index["editions"]}:
    problems.append("index.json: latest is not in editions")

if problems:
    print("FAILED")
    print("\n".join(problems))
    sys.exit(1)
print(f"OK: {len(index['editions'])} edition(s) valid")
