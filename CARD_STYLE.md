# How an Atlas card is written (card format 2)

Every study card, whether from the weekly edition, the daily paper-cards task, the verification pass or the in-app "Send to Claude" prompt, follows this. `scripts/validate.py` enforces the caps on any card with `"format": 2` and refuses the commit if one breaks.

**The rule behind all of it:** Curt is an expert reading on his phone. Lead with what changes practice, one idea per bullet, numbers first. Detail is kept, but one tap away.

## 1. Layers (what the app shows, in order)

| Layer | Fields | Shown |
|---|---|---|
| Glance | `title`, `impact_line`, `key_findings`, `key_numbers` | Always |
| Use it | `how_to_apply`, `results` (short form) | Open |
| Detail | `bottom_line`, `summary`, `applies_to`/`not_for`, `methods`, `pico`, `appraisal`, `limits`, `patient_explanation`, `open_questions` | Folded, one tap |
| How it was checked | `confidence_note`, `evidence_note`, `full_text`, `funding_conflicts`, `verification` | Folded, one line |

## 2. Writing rules

- **Bullets, not paragraphs.** These fields are arrays of strings: `key_findings`, `summary`, `limits`, `applies_to`, `not_for`, and appraisal `clinical_significance`, `comparator_fairness`, `alternative_explanations`, `spin`, `fit_with_evidence`, `external_validity`, `would_change_my_mind`. Methods fields may be a string or a short array.
- **One idea per bullet.** If a bullet has "and" joining two instructions, split it.
- **Numbers first, words second.** "65% still have some pain at 12 mo", not "By 12 months around two-thirds of people…".
- **No caveats inside the main sentence.** Put the caveat in its own bullet (✓ / ✗ pattern for transfer: what transfers, then what doesn't).
- **Provenance lives in one place.** Page numbers, "I couldn't open…", "the appendix wasn't in the PDF", YLD-vs-DALY labelling notes, search logs: only in `confidence_note`, `full_text.why`, a domain's `support`, a result's `detail`, or `verification`. Never in clinical fields.
- **No filler rows.** Don't write "MCID not applicable" or "Not a comparative effect". Leave `effect`/`meaning` out and set `verdict` to `descriptive`.
- **Drop the "On Monday:" prefix**; the app adds the label.
- Australian English, plain, direct, for an expert clinician. Own words only. Honesty rules from `APPRAISAL.md` still apply.

## 3. Caps (words, unless stated)

| Field | Cap |
|---|---|
| `title` | 14 |
| `impact_line` | 25 |
| `key_findings` | 3–5 items, 20 each (required) |
| `bottom_line` | 40 (string) |
| `summary` | 3–5 items, 25 each |
| `key_numbers` | up to 4; `value` 4, `label` 10 |
| `how_to_apply` | up to 6 items, 22 each |
| `results[]` | `outcome` 6, `timepoint` 8, `result` 15, `effect` 20, `meaning` 12, `detail` 60, `verdict` required |
| `applies_to`, `not_for` | up to 4 items, 15 each |
| `limits` | up to 5 items, 20 each |
| Appraisal context fields | up to 4 items, 22 each |
| `clinical_significance` | up to 4 items, 25 each |
| `domains[]`/`grade[]` `reason` | 25; `support` 20 |
| `strengths`, `weaknesses` | up to 6 items, 15 each |
| `patient_explanation` | 45 (string, plain language) |
| `open_questions` | up to 4 items, 20 each |
| `confidence_note`, `evidence_note` | 30 |
| `verification.disagreements[]` | `item` 8, `first`/`second` 12, `why` 25, `resolved` 25 |
| `verification.external_searched` | 30 |

## 4. Result verdicts

`verdict` on every result row:
- `meaningful`: effect and CI clear the MCID / smallest worthwhile change
- `below_mcid`: statistically different but below the MCID
- `no_difference`: CI includes no effect and excludes a meaningful one, or the paper reports no difference
- `unclear`: CI too wide, or numbers not read
- `descriptive`: not a comparison (prevalence, natural history, burden)

## 5. Before and after

Before (`external_validity`): *"Reasonable for an Australian private sports-physio caseload on the topics of natural history, imaging stance and psychosocial screening, with Australian data on red flag prevalence (n=1,172 primary care) and Australian authors contributing. Caveats: the pooled data are mixed-age general populations…"*

After:
```json
"external_validity": [
  "✓ Natural history, imaging stance, psychosocial screening",
  "✓ Australian red-flag data (n=1,172)",
  "✗ General populations, not athletes",
  "✗ MRI associations are under-50s only"
]
```

Before (results row): 492 characters mixing figures, page-number provenance and "MCID not applicable".

After:
```json
{"outcome": "Global burden", "timepoint": "GBD 2015", "result": "60.1M YLD, up 54% since 1990", "meaning": "Minority carries most disability", "verdict": "descriptive", "detail": "Under 28% of cases are severe but carry 77% of disability. Body text uses YLD; key messages and Fig 3 say DALYs. Card follows the GBD source (YLD)."}
```

## 6. Verification pass

The second pass checks the content **and** this format. A card that breaks a cap fails, even if the appraisal is right. When the second pass corrects something, the corrected wording must still meet the caps; the reasoning goes in `verification`, never back into the clinical fields.
