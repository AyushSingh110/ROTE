# Rote — one-page cheat sheet

**Rote decides when an AI-derived financial procedure has earned deterministic execution authority —
and refuses to automate when the evidence is ambiguous.**

> Offline research prototype · `research grade: False` · synthetic world · no real money

---

### The problem in one case

Record says ₹2,705.09. Bank credited ₹2,700.00. Short by ₹5.09.
**Fee deducted?** → post `fee`, mark `matched`. **Partial payment?** → post `shortfall`, mark
`partially_settled`. Different money. **From the numbers alone, both fit equally.**

### Architecture

```
evidence → [verification] → classifier → router → registry → Guard → Gate → executor → world + ledger
                  │                        │
             mismatch                    2+ fit
                  └────────► REFUSE ◄───────┘   (registry never consulted)
```

### Three numbers

| | |
|---|---|
| **60 → 0** | confident wrong actions, v1 → v2 |
| **36.8%** | automation coverage, the price we paid |
| **345 → 0** | evidence-corruption escapes, after authoritative verification |

### Automate

exactly one procedure fits → validated + shadowed + human-signed plan → Guard checks proposal →
Gate checks allowlist/cap/idempotency → `intent` → tool → `outcome` → Guard checks result → commit.
**Zero model calls after classification. Replays to a byte-identical hash.**

### Refuse

two procedures fit → **plan lookups 0 · steps 0 · financial intents 0 · world hash unchanged** →
handed to a human with the competing procedures named.

### Biggest limitation

**We have never shown that a real exception queue contains a meaningful unambiguous slice.** 36.8%
is a property of six synthetic categories we wrote. If a real queue is mostly ambiguous, Rote
refuses nearly everything.

### Five-minute demo

`/` problem → `/queue` judge picks → resolve → **ambiguous case, `plan lookups: 0`** → schema drift
→ `/ledger` → v1/v2 table → close.

**Before starting:** `netstat -ano | grep ":8000 .*LISTENING"` — if occupied use `--port 8001`
(the bind error appears *after* the ~1 min warmup, so check first)
**Ready check:** `curl -s http://127.0.0.1:8000/health` → `ready:true, ledger_entries:0`
**Reset between runs:** `curl -X POST http://127.0.0.1:8000/api/reset`

### Ten hardest questions, one line each

| Q | A |
|---|---|
| Why would Razorpay need this? | Your rules engine covers the bulk; the residue is worked by hand because nobody can prove an automation is safe. Rote produces that proof. |
| Isn't this a rules engine? | A rule fires when it matches. It doesn't ask whether *another* rule also matches and disagrees about money. That question is the product. |
| Why not just an LLM? | We ran one: 500/500 automated, 60 confidently wrong, up to 13 different outcomes on the same input. |
| Why only 36.8%? | Only 2 of 6 categories are unambiguous. Five experiments failed to separate the rest, so we refuse them. |
| How do you know the classifier is right? | We don't — it scored 62%. The design assumes it's wrong; a wrong label can't acquire authority. |
| What if the evidence is wrong? | Our worst finding: 345 wrong actions. We now re-read the record through the Gate; it caught all 345, 0% false alarms. |
| How do you prevent double payment? | Gate-derived idempotency key, `intent` written before the call. Repeat resolve: world hash identical, no second intent. |
| What if the Guard fails? | Run stops, result never becomes state. An AST test fails the build if the Guard is ever omitted. |
| Can you deploy this? | No. No real data, no model, no auth, no durable storage, no concurrency. |
| Biggest limitation? | Nothing in the system reasons about *meaning* — which is exactly why we refuse instead of adding a sixth checker. |

### Never say

"production ready" · "safer than humans" · "100% accuracy" · "36.8% of real exceptions" · "tested on
real data" · "truly independent verification" · "zero errors" unqualified
