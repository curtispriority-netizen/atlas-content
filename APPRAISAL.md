# How Atlas critically appraises a study

Both scheduled tasks (weekly edition, paper cards) must follow this for every study's `appraisal` object. The aim is not a score: it is to show Curt the reasoning, domain by domain, so he can interpret the result in context and disagree where he knows better.

## 1. Say what you appraised from
`appraised_from`: `full_text` or `abstract`. If abstract only, every risk-of-bias domain that needs the methods section is `unclear` with the reason "not reported in abstract". Never guess a domain as low from an abstract.

## 2. Pick the tool that matches the design (`tool`)
| Design | Tool | Domains to list in `domains` |
|---|---|---|
| RCT | RoB 2 | Randomisation process; Deviations from intended interventions; Missing outcome data; Measurement of the outcome; Selection of the reported result |
| Non-randomised intervention / cohort comparing treatments | ROBINS-I | Confounding; Selection of participants; Classification of interventions; Deviations from intended interventions; Missing data; Measurement of outcomes; Selection of the reported result |
| Systematic review / meta-analysis | AMSTAR 2 (critical domains) | Protocol registered before review; Adequate literature search; Justified exclusions; Risk of bias assessed in included studies; Appropriate meta-analytic methods; RoB considered when interpreting results; Publication bias assessed |
| Diagnostic accuracy | QUADAS-2 | Patient selection; Index test; Reference standard; Flow and timing (plus applicability concerns in the reason) |
| Prognosis / risk factor | QUIPS | Study participation; Study attrition; Prognostic factor measurement; Outcome measurement; Study confounding; Statistical analysis and reporting |
| Measurement properties (an outcome measure) | COSMIN | The properties tested (e.g. reliability, measurement error, construct validity, responsiveness) |
| Other (cross-sectional, case series, qualitative) | JBI checklist for that design | The checklist's key items |

Each domain: `judgement` (low / some concerns / high / unclear), a one-sentence `reason` that cites the specific fact, and `support`: where in the paper that comes from (section, page or table) and what it says, paraphrased, e.g. "Methods p.4: sealed envelopes, opacity not stated". Write "Not reported" when the paper is silent; a silent domain is `unclear`, never `low`. Reason example (e.g. "Assessors blinded, but 22% loss to follow-up in control vs 8% in intervention, no sensitivity analysis"). Then `overall_risk_of_bias` using the tool's own rule (worst domain usually drives it).

## 3. GRADE the primary outcome (`grade`)
Five rows: risk of bias, inconsistency, indirectness, imprecision, publication bias. Each `not serious` / `serious` / `very serious` / `not applicable` with a reason. For a single trial, inconsistency is `not applicable`. Imprecision: say whether the 95% CI crosses no effect and/or the MCID, and whether the sample meets an optimal information size. `certainty` (high / moderate / low / very low) must follow from these rows: start high for RCTs, low for observational, and downgrade one level per serious concern.

## 4. Interpret in context (all required)
- `clinical_significance`: compare the effect and its CI against the MCID or smallest worthwhile change for that outcome (name the value and its source). Give an NNT if it can be derived. Say plainly if the effect is statistically significant but clinically trivial, or if a meaningful effect can't be ruled out.
- `comparator_fairness`: was the control credible and matched for dose, contact time and expectation, or a straw man (wait-and-see, a sham nobody believes, under-dosed usual care)?
- `alternative_explanations`: natural history, regression to the mean, contextual or placebo effects, co-interventions, differential attrition, and baseline imbalance. Which of these could plausibly produce this result?
- `spin`: do the abstract and conclusions match the primary outcome? Flag a focus on secondary outcomes or within-group change, or claims bigger than the data.
- `fit_with_evidence`: how this sits against the best existing review or guideline you can verify (name it). Does it confirm, extend, or contradict? Consistent with the Atlas playbook for this condition?
- `external_validity`: how close the sample (age, sport/activity level, chronicity, severity), setting (country, health system) and intervention are to an Australian private sports-physio caseload, and what that means for transferring the result.
- `would_change_my_mind`: what result or study would overturn or meaningfully strengthen this conclusion.

## 5. Strengths, weaknesses, funding
Keep `strengths`, `weaknesses` and `funding_conflicts` (who funded it; any author conflicts relevant to the intervention).

## 6. Verification (full-text cards from Curt's PDFs)
A second, independent pass checks the first. It is a separate session that sees only the paper's text and the draft appraisal, never the first pass's reasoning process.
- It re-checks every risk-of-bias domain, every GRADE row, each results number (effect and 95% CI) and the clinical-significance call against the text, using each domain's `support` location.
- Each disagreement goes in `verification.disagreements` as {item, first, second, why}. The first pass then makes the final call in `resolved` (or "left open"), and the card is updated to the resolved judgement. Never silently drop a disagreement.
- External anchors: look up the same paper on PEDro (pedro.org.au; RCTs and systematic reviews in physiotherapy, rated by trained raters) and in Cochrane or other major systematic reviews that assessed its risk of bias. Record each in `verification.external` {source, rating, note, url, agrees}. Record where you searched in `external_searched`, so "none found" means something.
- If an external expert rating conflicts with ours, show both and explain the gap; don't overwrite either.
- `verification.status`: "agrees" (no material disagreements), "disagreements" (any remain or were resolved), or "not checked".

## Honesty rules
- Only use numbers you actually read. If a value isn't reported, say "not reported".
- When you are inferring (e.g. an MCID from a different population), say so in the text.
- The study's `confidence` tag reflects how sure you are of your own reading; the appraisal `certainty` reflects the evidence itself. They can differ.
