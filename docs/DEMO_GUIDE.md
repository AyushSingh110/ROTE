# Rote — Demo Operations Guide

Everything you need to run, explain and defend the demo yourself.

> ⚠️ **This is an offline research prototype. `research grade: False`.** Synthetic world, stand-in
> agent, stand-in classifier. No payment rail, no bank, no external API, no real money.

---

## 1. Running it

### Prerequisites

- conda with the `rote` environment on Python 3.11
- a browser
- **no** network, no API key, no credentials — the demo is fully offline

### Verify the environment before you present

```bash
conda run -n rote python -c "import fastapi, jinja2, uvicorn, pydantic; print('ok')"
conda run -n rote python -m pytest -q        # expect 1093 passed
conda run -n rote lint-imports               # expect Contracts: 11 kept, 0 broken
```

If any of those fail, do not present — see §9.

### Start the server

```bash
cd /path/to/Rote-runtime
conda run -n rote python -m uvicorn rote.web.app:app --host 127.0.0.1 --port 8000
```

**Warmup takes roughly 50–130 seconds** depending on machine load. Plans are compiled at startup so
nothing compiles mid-demo. **The port does not accept connections until warmup finishes**, so *"it
answers"* is itself the readiness signal.

Watch the console for:

```
warmup_started    note=compiling plans; the server is not serving yet
warmup_complete   seconds=... note=READY - open http://127.0.0.1:8000/
```

### Confirm it is genuinely ready

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{"ready":true,"warmup_seconds":90.31,"scenarios":6,"backlog":500,
 "ledger_entries":0,"ledger_valid":true,"research_grade":false,"verify_evidence":false}
```

Check all four: `ready:true`, `backlog:500`, `ledger_valid:true`, `ledger_entries:0`.
`ledger_entries` must be **0** at the start of a run — if not, reset (below).

### Open it

**http://127.0.0.1:8000/**

### Reset between rehearsals — do this every time

Resolving mutates the world. Before each run-through:

```bash
curl -X POST http://127.0.0.1:8000/api/reset
```

```json
{"reset":true,"backlog":500,"ledger_entries":0,
 "world_hash":"8f267b12f4dc416e1a0b5e0c12d2bff0f257d14356e0606218b15a962ee77f10"}
```

That `world_hash` is the pristine value. If you see it, the world is clean. Reset is fast (no
recompilation) and preserves the verification setting.

### Optional: evidence verification on

```bash
ROTE_VERIFY_EVIDENCE=1 conda run -n rote python -m uvicorn rote.web.app:app --host 127.0.0.1 --port 8000
```

`/health` then reports `"verify_evidence":true`. **Leave it off for the standard demo** — the
five-minute script does not use it, and it roughly doubles per-case latency.

### Shut down

`Ctrl-C` in the server terminal. Nothing persists; there is nothing to clean up.

---

## 2. Rote in 60 seconds

**The problem.** A payment company's reconciliation system auto-matches most settlement lines
against bank statements. What it cannot match lands in an exception queue that people work by hand.
Today that work is binary: either a hand-written rule covers it and it is fully automatic, or it
does not and a human does the whole thing. There is no defensible middle — nobody can prove a
proposed automation is safe enough to run unattended.

**Concrete case.** Your record says the merchant is owed **₹2,705.09**. The bank credited
**₹2,700.00**. Short by ₹5.09. Why?

- the gateway deducted a **fee** → post a `fee` adjustment, mark `matched`
- the customer made a **partial payment** → post a `shortfall` adjustment, mark `partially_settled`

Different money, different final state. **From the numbers alone, both are equally consistent.**

**The user** is the reconciliation analyst working that queue. **The company** is a payment
processor. **The AI's role** is to read the case and propose the explanation — it is good at that.
**Rote's role** starts at the moment that proposal wants to become an action.

**What gets automated:** cases where exactly one procedure fits the evidence, after that procedure
has been compiled from many verified runs, validated on held-out cases, shadowed with no authority,
and signed off by a named human.

**What gets refused:** everything else — and Rote names the competing procedures for the human.

**Why refusal is a feature.** We measured the alternative. Automating whenever a plan existed gave
**500/500 automated, 60 confidently wrong**, and every safety layer passed those sixty. They were
wrong about *meaning*, and shape-and-range checks cannot see that. Refusing on ambiguity took
automation to 36.8% and confident errors to **zero**.

---

## 3. Architecture

```
        exception + structured evidence
                     │
      ┌──────────────▼───────────────┐
      │ Authoritative verification   │  (optional switch)
      │ re-read the record & lines   │
      └──────────────┬───────────────┘
             mismatch│unverifiable ──────────► REFUSE
                     │agreement
              ┌──────▼──────┐
              │ Classifier  │  returns a typed category, never an action
              └──────┬──────┘
              ┌──────▼──────┐
              │   Router    │  how many procedures fit this evidence?
              └──────┬──────┘
          2+ fit  ───┴───►  REFUSE   (registry never consulted)
                     │ exactly 1
             ┌───────▼────────┐
             │ Plan Registry  │  validated · shadowed · human-approved
             └───────┬────────┘
             ┌───────▼────────┐
             │     Guard      │  invariant veto, then result check
             └───────┬────────┘
             ┌───────▼────────┐
             │  Policy Gate   │  allowlist · cap · idempotency key
             └───────┬────────┘
             ┌───────▼────────┐
             │   Executor     │  two-phase commit
             └───┬────────┬───┘
       ┌─────────▼──┐  ┌──▼──────────────┐
       │   World    │  │ Ledger (chained)│
       └────────────┘  └─────────────────┘
```

### Every component

**Authoritative verification** *(behind `ROTE_VERIFY_EVIDENCE`)*
Receives the structured evidence. Decides whether it agrees with the settlement record and bank
lines re-read through the Gate. Cannot write anything, cannot reach a write tool (its context is
category-free, so only read tools are allowlisted). Exists because Rote validated *interpretation*
but never the *evidence*. Prevents an upstream extraction error becoming a correct-looking action —
we measured **345 wrong actions** from corrupted evidence before this existed.

**Classifier**
Receives structured fields plus quarantined free text. Decides which category. Cannot emit an
action — its return type is an enum, so the worst an injected note achieves is a wrong enum member.
Prevents free text becoming behaviour.

**Router**
Receives facts and a classification. Decides how many categories' preconditions fit. Cannot call a
tool or see the world. Exists because a component cannot cross-check itself. Prevents acting on a
label the evidence does not uniquely support — **the registry is not consulted at all when 2+ fit.**

**Plan Registry**
Receives compiled plans. Decides which may serve. Cannot activate anything without a passing replay
validation, N agreeing shadow runs and a named human — **there is no override parameter, and a test
fails if `force`/`override`/`bypass` ever appears.** Prevents an unproven procedure gaining
authority.

**Guard**
Receives the proposed arguments (before the Gate) and each returned result (before it becomes
state). Decides whether an invariant is violated or a result diverges from what was learned. Cannot
reach a tool or the Gate. Prevents a changed world silently corrupting execution.

**Policy Gate**
Receives every tool call from both paths. Decides allowlist, per-category money cap, rolling window,
and derives the idempotency key itself. Cannot be bypassed — no component holds an adapter.
Prevents unauthorised, oversized or duplicated actions.

**Executor**
Receives an active plan and the facts. Decides argument resolution and step order. Cannot run an
inactive or unvalidated plan, and cannot commit a result the Guard rejected. Prevents partial or
poisoned state.

**Ledger**
Receives every gate verdict, intent and outcome. Hash-chained and append-only. Cannot be edited.
`intent` is written **before** the call, so a crash between the two leaves an `unknown` for a human
rather than a silent double-post.

---

## 4. The two paths, step by step

### A. AUTOMATE

1. Exception arrives with structured evidence and a quarantined merchant note.
2. *(If enabled)* verification re-reads record and bank lines **through the Gate**, under actor
   `system:verifier`. Agreement → continue.
3. Classifier returns one typed category.
4. Router counts fitting procedures. **Exactly one** → proceed.
5. Registry serves the `ACTIVE` plan — validated, shadowed, human-signed.
6. Executor resolves each argument, then for each step:
   - Guard checks the proposed action **before** the Gate — an invariant checked after the money
     moved prevents nothing.
   - Gate checks allowlist and cap, derives the idempotency key, writes `intent`, calls the tool,
     writes `outcome`.
   - Guard inspects the returned result; only then does it become state (two-phase commit).
7. World changes. Ledger holds the full chain. The run replays to a byte-identical outcome hash.

**On failure:** any Guard rejection or Gate refusal stops the run and hands over to the live agent,
carrying the diverging result as *untrusted text*, never as state.

### B. REFUSE

1. Same arrival.
2. *(If enabled)* verification finds a mismatch or cannot confirm a field → **refuse here**, before
   anything else.
3. Otherwise classifier returns a category.
4. Router finds **two or more** procedures fit → refuse.
5. **The registry is never consulted.** Plan lookups: 0. Steps: 0. Financial intents: 0. World hash
   unchanged.
6. The case goes to the live agent or a human, with the competing procedures named.

### Why the order is what it is

- **Verification before routing** — routing itself reads the evidence; verifying after would mean
  deciding on facts you have not checked. It also keeps refusals at zero plan lookups.
- **Ambiguity before plan lookup** — this is what makes "no path to a plan" structural rather than a
  promise. Measured: 316/316 refusals, 0 lookups.
- **The plan is LLM-free** — a typed artifact with no model in it, so it is reviewable, replayable
  and identical every run.
- **Guard before *and* after** the tool — before for invariants, after for divergence.
- **Gate at the tool boundary** — the single place where authority is checked, for both paths.
- **Ledger writes `intent` first** — so a crash yields `unknown`, never an unrecorded action.

---

## 5. The "why" table

| Question | Answer |
|---|---|
| Why not just an LLM agent? | We ran one: 500/500 automated, **88.0% accuracy, 60 confidently wrong**, 2,150 unbounded tool-selection calls, up to 13 different outcomes on the same input. You cannot predict it, so a human must review every action — and the review is the cost. |
| Why not just rules? | Rules are excellent where you have written them, which is why the tail stays manual. Rote *discovers* the procedure from verified behaviour, and — unlike a typical rules engine — asks "does more than one rule fit, and do they disagree about money?" |
| Why not just humans? | Keep them for the tail. Rote's claim is narrow: for the slice where exactly one procedure fits, the review step can be removed. Twenty identical runs give one outcome hash. |
| Why does Rote refuse? | Because two procedures that fit the same facts do different things with money, and guessing is indistinguishable from working correctly — until an auditor asks. |
| Why is 36.8% acceptable? | Because the alternative measured 60 confident errors. We surrendered 63% of coverage to remove all of them. **Whether 36.8% is enough for a real queue is unproven.** |
| Why is zero wrong better than 100% automation? | A wrong posting closes a record in the wrong state with a clean audit trail. An escalation costs an analyst ten minutes. These are not symmetric. |
| Why verify evidence? | Because we measured **345 wrong actions** from corrupted evidence that every other layer passed. Rote validated interpretation, never the evidence itself. |
| Why verify before routing? | Routing reads the evidence. Verifying afterwards would mean routing on unchecked facts, and a refusal would already have touched the registry. |
| Why is the plan LLM-free? | A typed artifact is reviewable, replayable, and produces one outcome hash across twenty runs. A prompt does none of that. |
| Why the Guard? | The world changes underneath a plan. The Guard catches a result that no longer looks like what was learned, before it becomes state. |
| Why the Gate? | One place where authority is decided, for both the compiled path and the live agent, with caps and gate-owned idempotency keys the caller cannot choose. |
| Why the Ledger? | So any decision can be reconstructed, and so a crash mid-action leaves an `unknown` for a human instead of a silent double-post. |
| Why `research_grade: False`? | The world, the agent, the classifier, the preconditions and the corruption rules are all ours. It tests the architecture, not reconciliation. |
| Why isn't this production-ready? | No real data source, no real model, no authentication, no durable storage, no concurrency, single process. It is a research prototype with a working demo. |

---

## 6. Judge Q&A

Format: **S** = 15–30s answer · **D** = deeper · **E** = evidence · **✗** = do not claim.

### PRODUCT

**1. Why would Razorpay need this?**
**S** Your rules engine handles the bulk. What is left is an exception queue people work by hand,
and there is no safe middle ground between "a rule covers it" and "a human does everything." Rote
produces the proof that lets a slice of that queue run unattended.
**D** The blocker is not capability — an agent can resolve these. The blocker is that you cannot
prove in advance what it will do, so someone reviews every money-moving action. Rote removes the
review for the slice it can prove is unambiguous, and refuses the rest with the competing
explanations named.
**E** v1 → v2: 60 confident errors → 0. **✗** Never claim we know your queue's automatable fraction.

**2. Isn't this just a rules engine?**
**S** A rules engine fires when its condition matches. It does not ask whether *another* rule also
matches and disagrees about the money. That question is the product.
**D** Two differences. Discovery: Rote compiles procedures from observed verified behaviour rather
than requiring someone to write and maintain each one. And the refusal calculus: our 60 errors
happened because `fee_mismatch` and `partial_payment` share a precondition — the same function
object. A rules engine would have fired one of them.
**E** `_PRECONDITIONS[FEE_MISMATCH] is _PRECONDITIONS[PARTIAL_PAYMENT]`, pinned by test.
**✗** Do not say rules engines cannot do this. A well-built one could. Most do not.

**3. Where does the real ROI come from?**
**S** Removing the human review step for the automatable slice, plus faster audit reconstruction.
Not from headcount.
**D** The honest model: value = automatable fraction × analyst minutes per case, plus reduced
re-work from wrong resolutions, plus audit time. The first term is the one we have not measured on
real data. **✗** Never quote a savings figure.

**4. Who buys it?**
**S** Head of payment operations or the finance controller. Internal audit has to be satisfied
before it ships.

**5. What is the smallest useful deployment?**
**S** One PSP, one merchant segment, one exception type — shadow mode first, no authority.

### ARCHITECTURE

**6. Walk me through a case.**
**S** Evidence → verification → classifier → router → registry → Guard → Gate → executor → world +
ledger. Refusals exit at verification or the router, before any plan is fetched.

**7. Why verify before routing?**
**S** Routing reads the evidence. Checking afterwards means routing on unchecked facts, and a
refusal would already have touched the registry. **E** 316/316 refusals with 0 plan lookups.

**8. What stops a component reaching a tool directly?**
**S** An import-linter contract — 11 of them, enforced in CI. Runtime and agent cannot import an
adapter; an AST test fails the build if the service layer calls `_adapters.invoke`.

**9. How is the compiled plan built?**
**S** Group verified trajectories by exact tool sequence, take the modal one if support is high
enough, align the steps, bind each argument to a typed source with evidence counts, learn
expectations, then replay-validate on a 30% holdout.
**E** fx_rounding: 162/162 support, 63/63 holdout replay.

**10. What is in a plan?**
**S** Typed steps and argument bindings with provenance. No model, no prompt, no free text.

### AI

**11. Why don't you just use an LLM?**
**S** We did. It got 500/500 right when it did the work itself. The problem is that it is different
every time — up to 13 outcomes on the same input — so nothing can be granted standing authority.

**12. What if the model hallucinates?**
**S** The classifier returns an enum member. A hallucination can only be a wrong category, never an
action. The router then independently checks whether that category's precondition holds.
**✗** Do not claim injection is impossible. A *plausible* wrong label is not caught by
preconditions — that is exactly how our 60 errors happened.

**13. How do you know your classifier is correct?**
**S** We do not, and the design assumes it is not. It scored 62% on structured fields alone. The
system is built so a wrong classification cannot acquire authority.
**E** Five upstream error classes × 500 cases: 0 escaped.

**14. Is there a model in the loop at run time?**
**S** One bounded classification call that cannot select a tool. Zero after that. The agent baseline
made 2,150 tool-selection calls.

**15. Would a real LLM change your results?**
**S** Almost certainly. Our classifier is a deterministic stand-in. Its errors are not distributed
like a real model's. That is a headline limitation.

### SAFETY

**16. What if the evidence itself is wrong?**
**S** That was our worst finding. Corrupted evidence produced **345 wrong actions** — Rote validated
interpretation but never the evidence. We built a verifier that re-reads the record and bank lines
through the Gate; it detected all 345, with 0% false alarms on clean data.

**17. How do you prevent double payment?**
**S** The Gate derives the idempotency key from the action content — callers cannot supply one — and
writes `intent` before the call. A replay returns the recorded result and writes no second intent.
**E** Same case resolved twice: world hash identical, no duplicate intent or outcome.

**18. What happens if the Guard fails?**
**S** The run stops and hands over; the rejected result never becomes state. And the Guard cannot be
silently switched off — an AST test fails the build if `execute_plan` is ever called without an
inspector.
**✗** Do not claim the Guard catches everything. It missed 21.3% of labelled divergences and 0 of
our 60 semantic errors — it reasons about shape and range, not meaning.

**19. What if two workers process the same case?**
**S** Untested. Single process today. Durable idempotency and per-record locking are on the
production list. **✗** Never claim concurrency safety.

**20. What is your biggest safety limitation?**
**S** Nothing in the system reasons about *meaning*. Every layer checks shape, range, allowlist or
cap. That is precisely why the 60 errors got through, and why the answer was to refuse rather than
to add a sixth checker.

### RESEARCH

**21. Why only 36.8%?**
**S** Only two of our six categories are unambiguous from pre-action evidence. The other four come
in pairs that fit the same facts. We refuse those.
**D** We ran five pre-registered experiments trying to separate them — fee arithmetic, its stability
as data grew, merchant notes, settlement status, shortfall fraction. All failed, and one overturned
a claim I had made. So we changed the policy instead of guessing.
**✗** Never present 36.8% as a real-world coverage figure.

**22. How do you know 36.8% isn't just a bad classifier?**
**S** Because it is structural, not statistical. If exactly one category fits, it is always the true
one — a wrong label is either contradicted or ambiguous. Verified on all 500 cases.

**23. Did anything surprise you?**
**S** Three retractions. Merchant notes turned out to be statistically independent of the true
category, which killed a claim in my own write-up. And I predicted 316 automated / 184 refused —
it was exactly the other way round.

**24. What did you fail to find?**
**S** Any deterministic tie-breaker for the ambiguous pair. Five attempts, all negative. Every one is
in the journal with what it eliminated.

**25. Is your evaluation trustworthy?**
**S** It is offline from a JSONL run log the runner reads back before rendering, so a number not in
the log cannot appear in the report. Both baselines are checksummed and checked by the test suite.

### BUSINESS / PRODUCTION

**26. Can this be deployed?**
**S** No. No real data source, no model, no auth, no durable storage, no concurrency. The
architecture is production-*shaped* — gated tool boundary, chained ledger, approval lifecycle — but
no component is production-*ready*.

**27. What would a pilot measure?**
**S** Two gates. What fraction of your queue has exactly one fitting procedure, and would our shadow
decisions have matched your analysts. Fail either and there is no product for you.

**28. What is your single biggest risk?**
**S** That a real exception queue — the residue after your rules engine — may not contain a
meaningful unambiguous slice. If it is 90% ambiguous we refuse nearly everything. That is a data
question, and no amount of further building answers it.

**29. How long to production?**
**S** I would not estimate. The gating item is data access for a pilot, not engineering.

**30. What would you build next?**
**S** A pre-deployment coverage report: point it at a queue and get back "this fraction has exactly
one fitting procedure." It is the sales artifact and it answers the biggest risk.

---

## 7. The five-minute demo

Reset first. Speak plainly.

| Time | Screen | Do | Point at | Say | Why it matters |
|---|---|---|---|---|---|
| **0:00** | `/` | Land on the overview | The opening line | "AI can reason about financial exceptions. The dangerous part is giving that reasoning direct authority to move money." | Frames the problem as authority, not capability |
| **0:20** | `/` | Scroll to the flow | The YES/NO branch | "One question decides everything: does exactly one procedure fit this evidence?" | The whole product in one diagram |
| **0:30** | `/queue` | Click **Live queue** | The 500 rows and the *Would* column | "500 real exceptions. You pick one — nothing here is pre-selected. The classifier and router have already run, but nothing has executed." | Kills "is this a recording?" |
| **1:00** | `/live/<pick>` | Open an **AUTOMATE** row | Facts left, quarantined note right | "Trusted structured evidence on the left. The merchant's free text is quarantined — it can never become an instruction." | Injection boundary, visibly |
| **1:45** | same | Click **Resolve** | steps, guard inspections, model calls, outcome hash | "Four steps, guard inspected twice per step, **zero model calls after classification**, and it replays to a byte-identical hash." | Determinism as a measurement |
| **2:30** | `/live/<ambiguous>` | Open a **REFUSE** row, then Resolve | `plan lookups: 0`, world before/after | "Two procedures fit the same evidence and they do different things with money. **The plan registry was consulted zero times.** Nothing ran, nothing was written." | The hero moment — refusal is structural |
| **3:15** | `/s/schema_drift/decision` | Open scenario C | the guard objection, `steps 0` | "A valid, human-approved plan meets a bank response that changed shape. The Guard rejected the result before it could become state." | Defence in depth, shown by running it |
| **4:00** | `/ledger` | Click **Ledger** | `intent` before `outcome`, chain valid | "Every decision is here, hash-chained. Intent is written *before* the call, so a crash leaves an `unknown` for a human — never a silent double-post." | Auditability, not opacity |
| **4:30** | `/` | Scroll to the research panel | the v1/v2 table | "Version one automated all 500 and was wrong 60 times — past every safety layer. Five experiments failed to separate those cases. So we gave up 63% of coverage to remove every confident wrong action." | The regression is the credibility |
| **5:00** | — | Stop | — | "Rote doesn't ask whether an agent can produce an answer. It asks whether the evidence is strong enough to grant that behaviour deterministic authority." | The close |

---

## 8. Do not say

| ✗ Never say | ✓ Say instead |
|---|---|
| "Rote is production ready" | "This is a research prototype with a working offline demo. The architecture is production-shaped; no component is production-ready." |
| "Rote is safer than humans" | "For the slice where exactly one procedure fits, it is more *consistent* than a human — one outcome hash across twenty runs." |
| "100% accuracy" | "100% on our synthetic 500-case workload after refusing the ambiguous cases. Level with the agent, never better." |
| "36.8% of real PSP exceptions can be automated" | "36.8% of *our synthetic* workload. The real fraction is our biggest unknown." |
| "We tested on real payment data" | "Entirely synthetic. No real data has ever touched this." |
| "The verification source is truly independent" | "An independent *path*, not an independent *source* — our world and evidence share one generator." |
| "Our corruption rates are realistic" | "We chose those rates to test the mechanism. Real frequency is unknown." |
| "Rote makes the AI correct" | "It prevents some upstream mistakes from acquiring authority. The AI is exactly as wrong as it was." |
| "It's fully auditable" | "Every decision is in a hash-chained ledger. Note the ledger can *verify* a replay but not *reconstruct* one — it stores a result hash, not the arguments." |
| "Zero errors" (unqualified) | "Zero confident wrong actions in the evaluated synthetic workload." |
| "The Guard catches divergence" | "It caught 78.7% of labelled divergences and none of our 60 semantic errors — it reasons about shape, not meaning." |
| "Human approval is enforced" | "A named human is recorded and there is no override flag — but there is no authentication behind the name." |

---

## 9. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Server won't start | `conda run -n rote python -c "import fastapi"` | Wrong env — re-run with `-n rote` |
| **Port already in use** | `netstat -ano \| grep ":8000 "` | Use `--port 8001` and open that instead. **Do not kill a PID you don't recognise.** |
| Warmup seems stuck | Look for `warmup_started` in the console | Normal for 50–130s. The port is closed until it finishes — that is expected, not a hang. |
| `/health` refuses connection | Is warmup finished? | Wait for `warmup_complete`, then retry |
| `ready` is false or missing | You may be hitting a different service | Confirm the JSON has `"research_grade":false` — that is *our* health payload |
| Page won't load | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/` | 200 = server fine, browser cache issue. Hard-refresh. |
| **Scenario already resolved** | The case shows a decision instead of a Resolve button | `curl -X POST http://127.0.0.1:8000/api/reset` |
| **Ledger isn't empty** | `curl -s http://127.0.0.1:8000/api/ledger` | Reset |
| Reset didn't seem to work | Compare `world_hash` to `8f267b12f4dc...` | If it differs, restart the server |
| Wrong scenario opened | — | The six are `automated`, `ambiguous`, `injected_note`, `schema_drift`, `cap_breach`, `kill_switch` |
| Everything is refusing | `curl -s .../health \| grep verify` | `verify_evidence` may be on — restart without `ROTE_VERIFY_EVIDENCE` |
| Total loss of confidence | — | `Ctrl-C`, restart, wait for `warmup_complete`, reset. Two minutes. |

**If the live demo fails entirely:** the six scripted scenarios at `/s/<id>/decision` do not touch
the live session and will still work. Fall back to those.
