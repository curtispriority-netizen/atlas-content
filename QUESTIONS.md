# How Atlas builds a reading list for a question

Curt writes a question in Atlas (e.g. "HSR vs eccentrics for mid-portion Achilles?"). The app saves `question__<id>__<title>.txt` to the Drive "Atlas papers" folder. The daily paper-cards task turns it into `questions/<id>.json`: **a reading list, not an answer.** It suggests what to add to his library and why. It never writes study cards; Curt taps Add in the app, and added papers come back through the normal checked read.

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
Then trash the marker. A re-sent question (same id) is refreshed: write the file again with a new `searched_at`.

## 5. Queue markers (papers Curt added without a PDF)
`queue__<id>__<title>.txt` or `queue__batch__<time>.txt` contain `{"kind": "queue", "papers": [{id, title, citation, doi, pmid, url, added_at}]}`. For each paper: get the free full text yourself (Europe PMC full text for a PMCID, PMC, or the publisher's open-access page). If you can read it in full, run the normal first pass + independent second pass. If it's paywalled, run both passes on the abstract (`read_level` "abstract", `appraised_from` "abstract", `checked_by` "independent second pass (abstract)") and set `full_text.worth_getting` true with routes. Write `papers/<id>.json` as for any paper card, then trash the marker.

`scripts/validate.py` checks every `questions/*.json`.
