# atlas-content

Content feed for Atlas, Curt's personal physio knowledge app.

A weekly Claude scheduled task researches and writes one edition here. The Atlas Android app reads `index.json`, downloads any new editions, and keeps them on the phone. Reading logs and notes stay on the phone and never come here.

## Files

- `index.json`: list of editions; `latest` points to the newest.
- `editions/YYYY-Www.json`: one edition per week (0–3 studies, an optional measure of the week, on the radar, filtered out, sources).
- `schema/edition.schema.json`: the rules every edition must follow.
- `shown.json`: everything ever shown, so nothing repeats.
- `CARD_STYLE.md`: how every card is written (format 2: bullets, word caps, layers). All tasks follow it; the validator enforces it.
- `QUESTIONS.md`: how a question from the app becomes a reading list (`questions/<id>.json`), and how papers queued without a PDF get their checked read.
- `APPRAISAL.md`: how every study is critically appraised (design-matched risk-of-bias tool, GRADE, context). Both tasks follow it.
- `scripts/validate.py`: checks every edition and paper card against the schema and the CARD_STYLE.md caps. Every task must run it and only commit if it passes.

## Read-only preflight

Run `python scripts/preflight.py daily` with a JSON array of filenames from a complete, successful listing of the live Drive folder on standard input. Markers and PDFs without a matching checked full-text format-2 card require work. Supply every page of the listing and treat a listing failure as an error.

Run `python scripts/preflight.py weekly YYYY-MM-DD` with the date in Hobart and a current content checkout. An edition already recorded in both the index and its file can be skipped.

Skip the model session only on a successful `{"work_needed": false}` result. A nonzero exit means the check failed. The preflight needs only Python's standard library and does not modify Drive or the checkout. Run its tests with `python -m unittest discover -s tests`.

## Rules

- No patient-identifiable information, ever.
- No paywalled PDFs. Summaries in our own words only.
- Every number carries its population and source; `verified` is true only when the task checked the record itself.
