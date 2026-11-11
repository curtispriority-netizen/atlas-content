# Reviewing an Atlas card (about 5 minutes)

**How cards reach Atlas.** The weekly run screens new abstracts against your focus, appraises up to 8 papers a week (topping up to 4 with older, well-cited open-access papers on a quiet week), has a second AI model check each reading, and publishes them labelled **AI-checked, not clinician-reviewed**. Changes the second reader made and any open concerns are listed on the card under verification. Review is optional: use it for cards that will feed a playbook or handout. A card that breaks the style rules is not published; it waits in `reviews/` for you. PDFs you add to the Drive folder go through the same steps on the next run.

Each staged paper has a file in the private state repo: `reviews/<12-character id>.md`. It shows the card, the passages the model cited, and every judgement it made. Code checks that each number appears in the cited passages, that style limits are met, and that the bias tool's domains are complete. Failures of those checks no longer hold the paper: they are listed at the top under **⚠ Check these first**. Start there. Code has **not** checked that the judgements are right. That is your job, and it is the only thing between a model and the feed.

A style warning will make publishing fail until the text is shortened. Tell Claude the fix or reject the card.

## 1. Right study? (30 seconds)
Compare the title, PICO and design with the paper's abstract. Reject if the population, intervention or comparator is wrong.

## 2. Do the results mean what the card says? (1-2 minutes)
Under each result you can read the exact passage it came from. Check direction, group, timepoint, and whether it is a **between-group** effect or only a **within-group** change. "Both groups improved" is not a treatment effect. Check that a "no difference" result does not say "no effect".

## 3. Risk of bias, RoB 2 (2 minutes)
Judge each domain `low`, `some concerns` or `high`. Plain questions:

| Domain | Ask |
|---|---|
| Randomisation process | Was the sequence random and the allocation concealed? Any baseline imbalance that suggests it failed? |
| Deviations from intended interventions | Participants and therapists cannot be blinded in exercise trials, so ask instead: were non-adherence and co-interventions balanced, and was the analysis intention-to-treat? |
| Missing outcome data | How many were lost, and was it similar in both groups? Many or uneven losses with no sensitivity analysis is a concern. |
| Measurement of the outcome | Who measured it? A **self-reported** outcome from participants who know their group is at least `some concerns`, even if the assessor was blinded. |
| Selection of the reported result | Was there a registered protocol or analysis plan, and does the paper report the outcomes as registered? |

Overall risk of bias follows the worst domain. Any `high` means `high`. Any `some concerns` means at least `some concerns`. All `low` means `low`. When unsure, take the lower rating and say why.

## 4. GRADE certainty for a single trial (1 minute)
Start at **high**. Subtract one level for each `serious` row and two for each `very serious` row:
- **Risk of bias:** `some concerns` usually means serious or none depending on the domain, `high` usually means serious.
- **Imprecision:** the confidence interval crosses no effect *and* the smallest worthwhile difference, or the sample is small. If no CI is reported, say so; that is usually serious.
- **Indirectness:** the population, intervention or outcome differs from the question a clinician would ask.
- **Inconsistency and publication bias:** not applicable for one trial.

Floor is `very low`. If the card's certainty does not match this count, change it.

## 5. Decide
- **Approve:** run the *Atlas research* workflow with mode `promote` and `promote_ids` set to the 12-character id.
- **Reject or change:** leave it pending and tell Claude what to fix. Rejecting is cheap; publishing a wrong card is not.

Reject when the study is wrong, a result is misread, or the card advises action the evidence does not support. Nothing is published unless you promote it and `ATLAS_PUBLISH_ENABLED` is `true`.

## Editing a card before you publish it
Fill `card_edits` when you run `promote`. It's JSON, keyed by the 12-character id:

```json
{"b225e479d22f": {"appraisal.certainty": "low", "limits[4]": "New limit.", "limits[+]": "Appended limit.", "summary[0]": null}}
```

`[n]` replaces item n (counting from 0), `[+]` appends, and `null` deletes. Card metadata and verification can't be edited. validate.py runs before anything is published, so a bad edit stops the publish and changes nothing. The published card says it was edited by the clinician.
