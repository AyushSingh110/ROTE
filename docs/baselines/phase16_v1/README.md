# Phase 16 — Rote v1 baseline (immutable)

Captured 2026-08-23, **before** the router ambiguity check was added. This is the run log behind
every Phase 16 number in `docs/IMPLEMENTATION_PLAN.md` and `docs/JOURNAL.md`.

**Do not regenerate these files.** Once the router escalates on ambiguous evidence, `run_evaluation`
produces v2 and can no longer reproduce v1. This directory is the only remaining copy of the
evidence, which is why it is checked in rather than left in a scratch directory.

## What it records

| file | lines | contents |
|---|---|---|
| `runs.jsonl` | 1000 | one `RunRecord` per exception per path — 500 Rote, 500 live agent |
| `repeats.jsonl` | 400 | consistency cohort, 10 tasks x 20 repeats x 2 paths |
| `exploring.jsonl` | 200 | live agent with exploration on, 10 tasks x 20 repeats |
| `replays.jsonl` | 500 | audit replay of every compiled resolution |

Eval seed 91, n=500. Plans compiled from seed 5, n=1500. Agent `offline-heuristic-1`.

## The v1 numbers, recomputed from `runs.jsonl`

```
total exceptions      500
compiled (0 llm)      500       automation 100.0%
resolved live           0
escalated               0
failed                  0
checker pass          440       accuracy 88.0%
checker fail           60
live agent            500/500
only agent passed      60
terminal states       {'resolved_compiled': 500}
escalation reasons    none
```

All 60 failures were `partial_payment` routed to the `fee_mismatch` plan.

## Checking it

```
sha256sum -c SHA256SUMS.txt
```

Then recompute the metrics with the existing evaluator — no new code needed:

```python
from rote.contracts.evaluation import EvalPath, RunRecord
from rote.eval.report import accuracy, deterministic_resolution
from rote.eval.runlog import read_records

records = read_records("docs/baselines/phase16_v1/runs.jsonl", RunRecord)
rote = [r for r in records if r.path is EvalPath.ROTE]
print(deterministic_resolution(rote))
print(accuracy(records))
```

**Standing caveat:** the agent and classifier are stand-ins, so every number here is
`research grade: False` — a measurement of the mechanism, not evidence about language models.
