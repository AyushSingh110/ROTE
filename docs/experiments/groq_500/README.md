# Full-batch measurement — 500 cases, production configuration

Run on 29 August 2026. **Nothing was tuned for this run.** Prompt, confidence threshold, routing,
ambiguity rule and safety behaviour are exactly those the deployed application uses. The frozen V1
and V2 baselines in `docs/baselines/` were not touched.

| Setting | Value |
|---|---|
| Classifier | `llm` |
| Provider / model | Groq · `openai/gpt-oss-120b` |
| Evidence verification | **ON** |
| Batch | 500 synthetic settlement exceptions (`eval_seed=91`) |
| Compiled system | `fit_seed=5`, `count=1500` |

A deterministic arm was re-run **with verification ON** as the comparison. It is therefore *not*
identical to frozen V2, which ran with verification OFF.

## Headline

| Metric | Deterministic | **Groq `gpt-oss-120b`** |
|---|---|---|
| Records in batch | 500 | **500** |
| Automated — **match rate** | 184 · 36.8% | **174 · 34.8%** |
| Not automated | 316 | **326** |
| **Wrong automated actions** | **0** | **0** |
| **Precision of automated actions** | 184/184 · **100%** | 174/174 · **100%** |
| Model classification accuracy | 85.4% (411 scored) | **81.1%** (391 scored) |
| Plan lookups | 184 | 174 |
| Executions | 184 | 174 |
| Compiled steps executed | 518 | 484 |
| Provider failures | 0 | **20** |
| Untrusted blocks withheld | 0 (local) | **1000** |
| Ledger valid | true | **true** |
| Tokens in / out | — | 194,249 / 107,987 |

**Read the terminology carefully.** *Match rate* is how much of the batch was automated. *Precision
of automated actions* is how many of those automations were correct. *Classification accuracy* is
how often the model named the right category. These are three different things and the first is
never called accuracy.

## The exception list

| Reason | Deterministic | Groq | What it means |
|---|---|---|---|
| `ambiguous_evidence` | 227 | **201** | two or more procedures fit the same evidence |
| `evidence_unverifiable` | 89 | **89** | the evidence named a bank line the authoritative read could not associate |
| `low_classifier_confidence` | 0 | **16** | the model answered below the 700/1000 threshold |
| `classifier_unavailable` | 0 | **20** | the provider could not be reached; failed closed |
| `evidence_mismatch` | 0 | **0** | no clean case disagreed with the record |
| **Total not automated** | **316** | **326** | |

**Every one of those 326 refusals reached zero plan lookups and executed zero steps.**

## The result that matters

The model's most common error was `partial_payment → fee_mismatch`, **54 times**.

That is precisely the error that produced V1's 60 wrong money movements. A real hosted model,
given the same queue, makes the same mistake — and **not one of those 54 became a financial
action**, because those cases have two procedures fitting the same evidence and are refused before
the plan registry is consulted.

Full confusion matrix (scored cases only):

```
fee_mismatch      -> fee_mismatch        119   correct
timing_cutoff     -> timing_cutoff       106   correct
fx_rounding       -> fx_rounding          68   correct
partial_payment   -> fee_mismatch         54   WRONG - the V1 failure mode, contained
duplicate_entry   -> duplicate_entry      20   correct
duplicate_entry   -> timing_cutoff        20   WRONG
fx_rounding       -> unknown               7   rejected by the classifier boundary
fee_mismatch      -> unknown               5   rejected
partial_payment   -> partial_payment       4   correct
duplicate_entry   -> unknown               3   rejected
timing_cutoff     -> unknown               3   rejected
partial_payment   -> unknown               2   rejected
```

Confidence distribution: `900-1000: 210`, `800-900: 131`, `700-800: 34`, `600-700: 16`,
`0-200: 20`. The model is confident far more often than it is right.

## What refusing costs — computed from the frozen baselines

| | |
|---|---|
| Cases refused that V1 got **wrong** (prevented) | **60** |
| Cases refused that V1 got **right** (given up) | **256** |
| **Cost ratio** | **4.3 correct automations given up per wrong action prevented** |

All 60 of V1's errors fall inside the refused set; none survived into the automated set. This is
pinned by a test that reads only the immutable baseline files.

## Honest notes on these numbers

**The latency figure in `summary.json` is not model latency.** The runner paces itself to stay
inside the provider's rate limit, and that sleep sits inside the timed region, so
`median_classification_ms: 6393` measures the pacing, not the provider. Clean end-to-end latency,
measured against the deployed container without pacing, is **~1.0–1.5 s** for an automated case and
**~0.04 s** for a verification refusal (which never calls the model).

**20 provider failures are real.** The demo key is rate-limited to roughly nine calls a minute. Over
a 500-case batch, 4% of cases exhausted the retry budget and failed closed as
`classifier_unavailable` — no plan lookup, no execution. That is the designed behaviour under an
outage, and it is what a production deployment would see if the provider degraded.

**The deterministic accuracy here (85.4%) differs from V2's 88.0%** because verification runs first
and removes 89 cases from the scored set. Different denominator, same system.

**`evidence_unverifiable: 89` on clean data is expected, not a fault.** Those cases carry a second
candidate bank line the authoritative queries cannot associate, so the verifier abstains rather than
guessing. They were already being refused as ambiguous in V2 — verification simply runs first and
takes the attribution. **Coverage is unaffected: the deterministic arm still automates exactly 184.**

**Limitations.** One model, one provider, one seed, one synthetic benchmark. Hosted models are not
reproducible — the same case can receive different answers across runs. `research_grade: false`.

## Files

| File | Contents |
|---|---|
| `summary.json` | both arms, every metric, confusion matrix, configuration |
| `cases_deterministic.jsonl` | one row per case, deterministic arm |
| `cases_groq.jsonl` | one row per case, Groq arm |
| `SHA256SUMS.txt` | checksums |

Reproduce with `python run_groq_500.py 500` and `GROQ_API_KEY` set. Expect roughly two hours at the
free-tier rate limit.
