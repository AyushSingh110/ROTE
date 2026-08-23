# Evaluation baselines

Two immutable run logs. Neither is regenerated: the router change means `run_evaluation` produces
v2 and can no longer reproduce v1, so these directories are the only remaining evidence for the
comparison the project turns on.

| | `phase16_v1/` | `phase16_v2/` |
|---|---|---|
| router | before the ambiguity check | with the ambiguity check |
| compiled | 500 | **184** |
| refused to automate | 0 | **316** |
| correct | 440 | **500** |
| **wrong** | **60** | **0** |
| automation | 100% | **36.8%** |
| accuracy | 88.0% | **100%** |
| replay fidelity | 500/500 | 184/184 |

Everything else is identical between the two runs: same generator, eval seed 91 at n=500, plans
compiled from seed 5 at n=1500, same classifier, preconditions, guard, compiler, executor, gate,
ledger and harness. **The only behavioural difference is the router rule.**

## V1 — the baseline that found the regression

All 500 cases were automated and 60 were wrong: `partial_payment` routed to the `fee_mismatch`
plan. Every safety layer passed them — the gate permitted, the guard passed, the precondition held,
execution was deterministic and the replay was byte-identical. The error was in *meaning*, and
nothing in the system reasons about meaning.

## V2 — the ambiguity policy

Five experiments failed to find a way to separate the two categories from pre-action evidence
(fee-schedule distance, margin stability, merchant notes, settlement status, shortfall fraction).
Rather than guess, the router now refuses the compiled path whenever more than one category's
precondition fits the evidence:

```
0 fitting categories → existing fallback
1 fitting category   → compiled plan
2+ fitting categories → live agent, reason AMBIGUOUS_EVIDENCE
```

The rule counts; it names no category. The three collision classes it catches were **measured, not
hard-coded**:

```
fee_mismatch + partial_payment          184
timing_cutoff + transposed_reference      89
timing_cutoff + duplicate_entry           43
timing_cutoff alone (compiled)           109
fx_rounding alone (compiled)              75
```

**The trade is deliberate:** 63.2% of automation coverage given up to eliminate 60 confident wrong
actions. 132 of the 316 refusals are cases v1 resolved *correctly* — surrendered anyway, because
from the evidence alone they could genuinely be something else.

## ⚠ Research grade: False

**Both baselines are offline measurements of the mechanism, not evidence about language models or
about real reconciliation.** The agent (`offline-heuristic-1`) and the classifier
(`structured-fields-double-1`) are stand-ins written for this project, and the world is synthetic.
No number in either directory should be quoted as real-world performance.

## Verifying

```
cd docs/baselines/phase16_v1 && sha256sum -c SHA256SUMS.txt
cd docs/baselines/phase16_v2 && sha256sum -c SHA256SUMS.txt
```

Then recompute with the existing evaluator — see `phase16_v1/README.md` for the snippet.
