"""Shared checks for the Atlas format-2 card style."""

import re

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
