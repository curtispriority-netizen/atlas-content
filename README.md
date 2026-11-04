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

## Rules

- No patient-identifiable information, ever.
- No paywalled PDFs. Summaries in our own words only.
- Every number carries its population and source; `verified` is true only when the task checked the record itself.
