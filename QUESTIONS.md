# How Atlas answers a question

Curt writes a question in Atlas (e.g. "HSR vs eccentrics for mid-portion Achilles?"). When he taps **Find studies now**, the phone searches Europe PMC, saves the 6 best-evidence papers as draft cards linked to the question, and saves `question__<id>__<title>.txt` to the Drive "Atlas papers" folder. That one file holds the question, the drafts to check (`papers`), and every card already linked to it (`linked_items`).

The GitHub pipeline (`scripts/research.py`, daily evening run and the weekly run) then:
1. Gives every paper in `papers` the normal checked read (section 5): one reading plus the second-model critic, free full text only.
2. Writes a **reading list** of anything the quick search missed (sections 1–3). It never writes cards for those; Curt taps Add.
3. Writes the **answer** (section 6) from the CHECKED cards only.

All of it goes in `questions/<id>.json`.

## How the pipeline does it (`scripts/research_questions.py`)
- Question work runs only when publishing is on (`auto_publish`, `ATLAS_PUBLISH_ENABLED=true`, main branch). Up to `max_question_papers` (8) question papers are read per run, separate from the weekly cap.
- Papers without free full text are **not read** (no abstract-only cards). They are skipped without a failure notice, and the first `gaps` line says how many were not read.
- Reading list: one cheap model call writes the PICO and 2–3 Europe PMC queries. Code runs them and drops anything retracted, already in the library, or in the marker. A second cheap call only ranks those records. Every suggestion is a real Europe PMC record; title, citation, DOI, PMID, year and open access come from the record, never from the model.
- Answer: one call to the stronger model sees only the checked cards. Code refuses any item id that was not supplied, any number that is not on a card (comparison rows: on that row's own card), and any cap overrun. One repair is allowed; after that the answer is left out and the run fails so Curt is told. `risk_of_bias`, `certainty` and `title` in each comparison row are copied from the card.
- `search_summary` is written by code and ends "AI-checked, not clinician-reviewed."
- Drive is read-only, so markers are never trashed. Private state remembers each marker (file id + md5) and the cards it used. An unchanged question costs nothing. A changed or re-sent marker is searched and answered again. New checked cards for the same marker rebuild only the answer.
- A queue marker with `question_id` links its papers to that question; the answer is rebuilt once they have cards.
- `delete__<id>.txt` removes `papers/<id>.json`, its index entry and its rows in any answer. The id is remembered so the PDF or marker left in Drive never rebuilds the card.

Sections 1–3 and 6 below are the standard the output is held to. Where they describe steps the pipeline does not take (PubMed, Cochrane and guideline searches, abstract-level cards, trashing markers), the list above wins.

## 1. Search like a reviewer
- Turn the question into a PICO. Use the `context` and `region` Curt gave.
- Search PubMed and Europe PMC with synonyms and MeSH (e.g. Achilles tendinopathy OR tendinosis OR achillodynia; heavy slow resistance OR HSR OR eccentric loading OR Alfredson). Also check JOSPT/BJSM clinical practice guidelines and Cochrane.
- Order of preference: current guidelines → systematic reviews/meta-analyses (newest, best quality) → key RCTs (landmark and recent) → large cohorts → other designs only if nothing better exists.
- Skip anything in the marker's `already_in_library` list or in `papers/index.json`, but you may mention in a gap that the library already covers it.
- Check each record yourself (title, journal, year via DOI or PubMed). Never suggest a retracted paper.

## 2. Pick 5–10 papers
- `priority`: `core` (read these first; usually 2–4), `useful`, `background`.
- Prefer free full text where the paper is otherwise equal; set `open_access` honestly.
- `why`: one line, ≤20 words, on what this paper adds to *this* question (e.g. "Only RCT comparing HSR with eccentrics head to head; 12-month follow-up."). No summaries of results beyond what's needed to say why.
- `citation`: authors first ("Surname AB, Surname C, et al. Title. Journal Year.") so Atlas can search by author.
- `key`: the DOI, else PMID, else lower-case title.

## 3. Say what's missing (`gaps`, up to 4 × 20 words)
What the evidence can't yet answer for Curt's caseload: missing populations (athletes, insertional), no head-to-head trial, short follow-up, only low-certainty reviews, or a key guideline being out of date.

## 4. Write `questions/<id>.json`
```json
{
  "schema_version": 1,
  "question_id": "<id from the marker>",
  "question": "as Curt wrote it",
  "searched_at": "ISO 8601",
  "search_summary": "≤40 words: where you searched, the terms, and how many records you screened",
  "pico": {"p": "", "i": "", "c": "", "o": ""},
  "suggestions": [
    {"key": "", "title": "", "citation": "", "doi": "", "pmid": "", "year": "", "design": "Guideline | Meta-analysis | Systematic review | RCT | Cohort | Other", "priority": "core | useful | background", "open_access": true, "why": ""}
  ],
  "gaps": [""]
}
```
Then trash the marker. A re-sent question (same id) is refreshed: write the file again with a new `searched_at`, keeping or improving the answer.

## 5. Papers to check (in a question file, or in queue markers)
A question file's `papers` list has the same shape as a queue marker's. Check them exactly as below; a queue marker may also carry `question_id`, which means "rebuild that question's answer after checking".

### Queue markers (papers Curt added without a PDF)
`queue__<id>__<title>.txt` or `queue__batch__<time>.txt` contain `{"kind": "queue", "papers": [{id, title, citation, doi, pmid, url, added_at}]}`. For each paper: get the free full text yourself (Europe PMC full text for a PMCID, PMC, or the publisher's open-access page). If you can read it in full, run the normal first pass + independent second pass. If it's paywalled, run both passes on the abstract (`read_level` "abstract", `appraised_from` "abstract", `checked_by` "independent second pass (abstract)") and set `full_text.worth_getting` true with routes. Write `papers/<id>.json` as for any paper card, then trash the marker.

## 6. The answer (`answer` in `questions/<id>.json`)
Build it ONLY from cards that have `appraisal.verification` (checked): the question's `linked_items` and any `papers` you just checked. Read those cards (papers/<id>.json, or the edition item for ids like 2026-W45-1). Never use abstracts of unchecked suggestions, and never use a number that isn't on a checked card.

- `answer`: ≤50 words. Lead with the direct answer to Curt's question, then the single most important caveat.
- `certainty`: high / moderate / low / very low for the body of evidence on the main comparison: start from the best design present, then downgrade for risk of bias, inconsistency between the studies, indirectness to his caseload, imprecision and publication bias (as in APPRAISAL.md, but across studies).
- `provisional`: true if fewer than two checked studies address the main comparison, or none is a systematic review, guideline or RCT.
- `actions`: up to 6 × 22 words, one instruction each (dose, progression, what to say, what to stop). If the evidence doesn't justify a change, say what NOT to change.
- `comparison`: one row per checked study used: `item_id` (e.g. "mine-1a2b3c4d" or "2026-W45-1"), `title` (≤12 words), `design`, `n`, `finding` (≤20 words, the result that matters for this question), `verdict` (meaningful / below_mcid / no_difference / unclear / descriptive), `risk_of_bias`, `certainty` (from that card).
- `agreement` and `conflicts`: up to 4 × 22 words each: where the studies point the same way, and where they disagree and the likely reason (population, dose, outcome, follow-up, bias).
- `based_on`: the item_ids used. `updated_at`: now (ISO 8601).

Follow CARD_STYLE.md: bullets, numbers first, no provenance in these fields. If no linked study is checked yet, leave `answer` out.

`scripts/validate.py` checks every `questions/*.json`, including the answer caps and that every comparison `item_id` exists.
