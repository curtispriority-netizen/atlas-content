"""Review packets: what a clinician reads before approving a staged card for publication."""

from datetime import datetime, timezone

from research_appraisal import cited_text, passage_map

AREAS = (("design", "Design"), ("population", "Population"),
         ("intervention_or_exposure", "Intervention or exposure"), ("comparator", "Comparator"),
         ("outcomes", "Outcomes"), ("results", "Results"), ("numbers", "Numbers"))


def review_entry(fingerprint, record, paper_id, card, citation, added_at, reading, article, now):
    """The private record of one staged card. Nothing here is published until a human approves it."""
    known = passage_map(article)
    evidence = {area: cited_text(refs, known)
                for area, refs in (reading.get("evidence") or {}).items() if refs}
    entries = {}
    for group in ("results", "key_numbers"):
        for index, row in enumerate((reading.get("card") or {}).get(group) or []):
            entries[f"{group}[{index}]"] = cited_text(row.get("evidence_refs") or [], known)
    return {
        "status": "pending", "staged_at": now.isoformat(), "fingerprint": fingerprint,
        "record": {"doi": record.doi, "pmid": record.pmid, "title": record.title,
                   "published": getattr(record, "published", None)},
        "paper_id": paper_id, "added_at": added_at, "card": card, "citation": citation,
        "evidence": evidence, "entry_evidence": entries,
    }


def bullets(items):
    items = items if isinstance(items, list) else [items]
    return "\n".join(f"- {item}" for item in items if item)


def passage_block(rows):
    return "\n".join(f"  > [{row['id']}] {row['text']}" for row in rows) or "  > (no passage cited)"


LABELS = {
    "unsupported_number": "Numbers not found in the passages the card cites. Check each against the paper.",
    "card_style_validation_failed": "Style rules broken. Publishing will fail until these are shortened "
                                    "(tell Claude the fix, or reject).",
    "missing_source_excerpt": "No supporting passage quoted for these areas.",
    "missing_bias_domains": "Risk-of-bias tool is missing required domains.",
    "unrecognised_bias_tool": "Risk-of-bias tool not recognised.",
    "unreported_bias_domain_overclaim": "A bias judgement was made where the paper reports nothing.",
    "single_trial_inconsistency_overclaim": "GRADE inconsistency should be 'not applicable' for one trial.",
    "abstract_overclaim": "Low risk of bias claimed from an abstract only.",
    "certainty_corrected": "Certainty did not match the GRADE rows, so code corrected it. Check the rows.",
}


def warning_block(warnings):
    if not warnings:
        return ["No automated warnings.", ""]
    out = ["## ⚠ Check these first", ""]
    for row in warnings:
        out.append(f"- **{row['check']}**: {LABELS.get(row['check'], '')}")
        details = row.get("details")
        if isinstance(details, dict):
            out += [f"  - `{field}`: {', '.join(map(str, values)) if isinstance(values, list) else values}"
                    for field, values in details.items()]
        elif isinstance(details, list):
            out += [f"  - {item}" for item in details]
        else:
            out += [f"  - `{field}`" for field in row.get("fields", [])]
    return out + [""]


def critic_block(critic):
    if critic.get("status") != "done":
        return ["Second-reader check: not run." if not critic else "Second-reader check: unavailable this run.", ""]
    out = ["## Second reader (AI critic)", ""]
    out += [f"- Changed `{edit['path']}`: {edit['before']} → {edit['after']}. Why: {edit['why']}"
            for edit in critic.get("kept", [])] or ["- No changes made."]
    out += [f"- Concern left open: {text}" for text in critic.get("concerns", [])]
    if critic.get("dropped"):
        out.append(f"- {critic['dropped']} proposed change(s) were discarded because they broke a check.")
    return out + [""]


def render_review(entry):
    card, record = entry["card"], entry["record"]
    app = card["appraisal"]
    out = [f"# Review: {record['title']}", "",
           f"- Status: **{entry['status']}**  ·  staged {entry['staged_at'][:10]}",
           f"- Source: {entry['citation'].get('url', '')}  ·  fingerprint `{entry['fingerprint'][:12]}`",
           f"- Appraised from: {app['appraised_from']}  ·  tool: **{app['tool']}**", "",
           "This reading was produced by AI. Code checked that every number in the card appears in "
           "the passages it cites. Nobody has checked whether the judgements below are *right*. That is your call. "
           "See REVIEW.md for a 5-minute method.", "",
           *warning_block(entry.get("warnings") or []),
           *critic_block(entry.get("critic") or {}),
           "## What the card says", "",
           f"**{card['title']}**", "", card.get("impact_line", ""), "", card.get("bottom_line", ""), "",
           "Key findings:", bullets(card.get("key_findings")), "", "Limits:", bullets(card.get("limits")), "",
           "## 1. Does the card describe the right study? (PICO and methods)", ""]
    pico, methods = card.get("pico", {}), card.get("methods", {})
    out += [f"- P: {pico.get('p')}", f"- I: {pico.get('i')}", f"- C: {pico.get('c')}", f"- O: {pico.get('o')}",
            f"- Design: {methods.get('design')}", f"- Participants: {methods.get('participants')}",
            f"- Intervention: {methods.get('intervention')}", f"- Comparator: {methods.get('comparator')}",
            f"- Primary outcome: {methods.get('primary_outcome')}", "",
            "Passages the model cited for design and population:"]
    for area in ("design", "population", "intervention_or_exposure", "comparator", "outcomes"):
        if entry["evidence"].get(area):
            out += [f"- {dict(AREAS)[area]}:", passage_block(entry["evidence"][area])]
    out += ["", "## 2. Do the results match the paper?", ""]
    for index, row in enumerate(card.get("results") or []):
        result = row.get("result")
        result = "; ".join(result) if isinstance(result, list) else result
        out += [f"- **{row.get('outcome')}** ({row.get('timepoint', 'timepoint not stated')}): {result}",
                f"  Effect: {row.get('effect', 'not given')}  ·  verdict: {row.get('verdict')}",
                passage_block(entry["entry_evidence"].get(f"results[{index}]", []))]
    out += ["", "## 3. Risk of bias (" + app["tool"] + "). Agree, or change?", "",
            "| Domain | Model's call | Why | Where in the paper |", "|---|---|---|---|"]
    out += [f"| {row['domain']} | **{row['judgement']}** | {row['reason']} | {row['support']} |"
            for row in app["domains"]]
    out += ["", f"Overall: **{app['overall_risk_of_bias']}**", "",
            "## 4. GRADE and certainty", "", "| Row | Model's call | Why |", "|---|---|---|"]
    out += [f"| {row['domain']} | **{row['judgement']}** | {row['reason']} |" for row in app["grade"]]
    out += ["", f"Certainty: **{app['certainty']}**. Count the downgrades: start high for an RCT, "
            "minus one per `serious`, minus two per `very serious`.", "",
            "## 5. Clinical context", ""]
    for name in ("clinical_significance", "comparator_fairness", "alternative_explanations", "spin",
                 "fit_with_evidence", "external_validity", "would_change_my_mind"):
        out += [f"**{name.replace('_', ' ').capitalize()}**", bullets(app.get(name)), ""]
    out += ["Strengths:", bullets(app.get("strengths")), "", "Weaknesses:", bullets(app.get("weaknesses")), "",
            f"Funding and conflicts: {app.get('funding_conflicts')}", "",
            "## Decide", "",
            "Approve: run the *Atlas research* workflow with mode `promote` and "
            f"`promote_ids` = `{entry['fingerprint'][:12]}`. To fix wording or a judgement first, also fill "
            f"`card_edits`, e.g. `{{\"{entry['fingerprint'][:12]}\": {{\"appraisal.certainty\": \"low\", "
            "\"limits[+]\": \"New limit.\"}}}}` (see REVIEW.md). Reject: leave it, or tell Claude what to change.",
            "", f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}._", ""]
    return "\n".join(out)
