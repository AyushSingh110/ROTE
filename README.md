# Rote

**An authority layer that decides when an AI-derived financial procedure has earned deterministic
execution authority — and refuses to automate when the evidence is ambiguous.**

> ⚠️ **Offline research prototype. `research grade: False`.** The financial world, the agent and the
> classifier are deterministic stand-ins written for this project. No real payment rail, no bank, no
> external API, no credentials, no real money. Nothing here is production financial infrastructure.

---

## The problem

AI agents can reason about settlement exceptions perfectly well. The dangerous part is giving that
reasoning **direct authority to move money**, because the characteristic failure is not a crash — it
is being *confidently wrong while producing a completely plausible action*.

Take a real shape of this problem. A settlement line does not match, and the bank paid less than
expected. That single fact is consistent with several different explanations:

| Explanation | What the correct resolution does |
|---|---|
| **fee mismatch** | post a `fee` adjustment, mark the record `matched` |
| **partial payment** | post a `shortfall` adjustment, mark the record `partially_settled` |
| **duplicate entry** | void the duplicated bank line first |
| **timing cutoff** | post nothing at all, just match it |

These are *different financial procedures*. A system that picks the most likely explanation and acts
on it will sometimes post the wrong adjustment with the wrong reason and close the record in the
wrong state — and it will look completely normal in the logs.

**We measured exactly this.** An early version of Rote automated all 500 evaluated exceptions and got
**60 of them wrong**. Every safety layer passed those sixty: the policy gate permitted them, the guard
passed them, the precondition held, execution was deterministic, and the replay was byte-identical.
The error was in *meaning*, and none of those layers reasons about meaning.

## What Rote does

Rote sits between the agent's reasoning and any financial action.

```
AI Agent
   ↓
Structured evidence
   ↓
Rote
   ↓
Does exactly one procedure fit the evidence?
   ├── YES → validated plan → Guard → Gate → Execute → Ledger
   └── NO  → refuse automation → hand to the live agent / a human
```

**Rote does not replace the agent.** The agent reasons. Rote decides whether that reasoning has
earned the authority to be repeated deterministically, without a model in the loop and without a
human watching each time.

**The claim, stated narrowly:** Rote removes the human review step for the slice it can prove is
unambiguous — and tells you exactly how big that slice is. It is *level* with the agent on accuracy,
never better. What it changes is whether a person has to check each action.

Concretely, Rote records what the agent did across many verified runs, compiles the repeated
procedure into a typed plan with **no language model in it**, validates that plan against held-out
recordings, runs it in shadow with no authority, requires a named human to sign it off — and then, at
the moment of use, asks whether the evidence identifies *exactly one* procedure. If two fit, it
refuses.

## Why this matters

- **Ambiguity is the real risk.** Two procedures that both fit the same facts do different things
  with money. Guessing between them is indistinguishable from working correctly, right up until an
  auditor asks.
- **Deterministic execution is provable.** A compiled plan produces one outcome hash across twenty
  identical runs. An agent with any exploration produced up to thirteen different outcomes on the
  same input.
- **Safety boundaries are structural, not advisory.** No component can reach a tool directly; every
  call passes an allowlist, a per-category money cap, and a gate-derived idempotency key the caller
  cannot choose.
- **Auditability.** Every decision writes to a hash-chained append-only ledger. `intent` is recorded
  *before* the call, so a crash between the two leaves an `unknown` for a human rather than a silent
  double-post.
- **Controlled automation.** Authority can be removed automatically; it can only ever be *granted* by
  a named person, and there is no override flag (a test fails if one appears).

## Architecture

```mermaid
flowchart TD
    A[AI Agent] --> B[Structured Evidence]
    B --> C[Classifier<br/><i>returns a typed category, never an action</i>]
    C --> D[Router]
    D -->|2+ procedures fit| R["REFUSE AUTOMATION<br/><b>registry never consulted</b>"]
    D -->|exactly 1 fits| E[Plan Registry<br/><i>validated · shadowed · human-approved</i>]
    E --> F[Guard<br/><i>invariant veto + result check</i>]
    F --> G[Policy Gate<br/><i>allowlist · caps · idempotency</i>]
    G --> H[Executor<br/><i>two-phase commit</i>]
    H --> W[(Simulated World)]
    H --> L[(Hash-chained Ledger)]
    R --> Z[Live agent / human]
```

| Component | Responsibility |
|---|---|
| **Classifier** | Reads structured fields, returns a typed enum member. Holds no tools; free text can only ever change *which enum member*, never produce an action. |
| **Router** | Counts how many categories' preconditions fit. **2+ → refuse, before any plan lookup.** |
| **Plan Registry** | Owns plan lifecycle: `DRAFT → SHADOW → ACTIVE`. No plan reaches `ACTIVE` without a passing replay validation, N agreeing shadow runs, and a named human. No override parameter exists. |
| **Guard** | Checks the proposed action *before* the gate (invariant veto) and the returned result *before* it becomes state. Threshold chosen by an evidence-based sweep, not by eye. |
| **Policy Gate** | The only path to a tool. Allowlist, per-category money caps, rolling spend window, and gate-owned idempotency keys. |
| **Executor** | Walks the compiled plan. Two-phase commit: a result is quarantined until the Guard passes it. |
| **Ledger** | Append-only, hash-chained. `intent` → `outcome`, plus every gate verdict including refusals. |

## The key design principle

> **Rote does not optimise for maximum automation. It optimises for automation that has earned
> authority.**

This is a deliberate trade, and it costs real coverage. When the evidence supports two incompatible
procedures, Rote gives up the case rather than guessing — even though guessing would have been right
much of the time.

## Research result

| Metric | v1 — automate whenever a plan exists | v2 — refuse when evidence is ambiguous |
|---|---|---|
| Automated | 500 / 500 | **184 / 500** |
| Coverage | 100% | **36.8%** |
| Accuracy | 88.0% | **100%** |
| **Confidently wrong** | **60** | **0** |

v1 automated everything and produced 60 wrong resolutions — all of them `partial_payment` cases
routed to the `fee_mismatch` procedure.

Five pre-registered experiments then investigated whether those errors could be separated
deterministically from evidence available *before acting*:

| Experiment | Result | What it eliminated |
|---|---|---|
| Fee-schedule distance | separated perfectly at n=500 | — (looked solved) |
| Margin stability, 5 seeds, n→5000 | margin collapsed **36 → 6 → 0** | any tolerance-based fee rule |
| Merchant notes, 3 seeds, 15,000 cases | independent of category | any text classifier, **including an LLM** |
| Settlement status | constant `unmatched` everywhere | the cheapest hypothesis |
| Shortfall fraction | 58.1% of partials inside the fee range | the last deterministic signal |

**The evidence did not support a safe deterministic discriminator.** So v2 introduced ambiguity
refusal instead: 184 cases remained safe to automate, 316 were refused, and **0 wrong actions
occurred in the evaluated workload**.

**Read this precisely.** v2 is not "better AI" — it is *level* with the agent on accuracy (500/500
each), never better. The finding is:

> Rote trades automation coverage for the elimination of confident errors in the evaluated workload.

Synthetic deterministic environment · offline stand-in agent and classifier · not production
accuracy · `research_grade = False`.

### Then we attacked our own result

Refusing on ambiguity handles a *wrong label*. It says nothing about *wrong evidence*. So we ran two
further pre-registered attacks on the system.

**Upstream classification errors — contained.** Five error classes (wrong category, plausible-wrong,
unknown, low-confidence, contradictory), 500 cases each, injected outside every safety mechanism.
**Zero acquired authority.** Not by luck: if exactly one category fits the evidence it is always the
true one, so a wrong label is either contradicted or ambiguous. There is no third door.

**Evidence corruption — it escaped.** When we corrupted the *evidence* rather than the label,
**345 wrong automated actions** got through: 75 from a one-minor-unit amount change, 43 from a
reference substitution, 227 from evidence rewritten to fit a different category. Every layer passed
them. The finding:

> **Rote validated the interpretation of evidence but had no mechanism to validate the evidence
> itself.**

**The fix was already in the building.** `get_settlement_record` runs as step 0 of every plan — its
authoritative result was fetched and then used for nothing, because the argument binder prefers
`FROM_INPUT` over `FROM_STEP` whenever both match. Re-reading the record and bank lines *through the
Policy Gate*, before routing, detected **345 of 345** with a **0% false-mismatch rate on clean data**
and clean coverage unchanged at 36.8%.

| corruption rate | wrong actions, verification **off** | **on** | coverage cost |
|---|---|---|---|
| 0% | 0 | **0** | 0.0 pp |
| 5% | 1 | **0** | 1.0 pp |
| 10% | 5 | **0** | 2.4 pp |
| 20% | 8 | **0** | 3.6 pp |

Verification costs coverage only in proportion to how much evidence is actually corrupted. It
roughly doubles per-case latency and triples audit-trail volume, and it writes **no financial intent
or outcome when automation is refused**. It ships behind `ROTE_VERIFY_EVIDENCE`, off by default, so
the frozen v2 result stays exactly reproducible.

## Live demo

The repository contains a complete local end-to-end demo.

```
Browser → FastAPI → SessionRuntime → Classifier / Router
                                   → Plan / Guard / Gate
                                   → Simulated financial world
                                   → Hash-chained ledger
```

**This demo does not connect to real payment rails and moves no real money.** That is intentional.
The purpose is to demonstrate the *control architecture* safely: one persistent world, one policy
gate, one ledger, and 500 real exceptions you can pick from and watch decide.

## Demo screenshots

### 1 — Understand the product

![Landing page](docs/screenshots/01-landing.png)
*The product in about 30 seconds: the danger, the authority layer, the decision branch, and the
research result that produced it.*

![Live queue](docs/screenshots/02-queue.png)
*500 exceptions are available for investigation. Nothing executes merely because the queue exists —
the "Would" column is a routing preview with no plan fetched.*

### 2 — See automation

![Investigation](docs/screenshots/03-investigation.png)
*Trusted structured facts on the left; the merchant's free text quarantined on the right. Every
recorded tool call carries its gate verdict.*

![Evidence, automation](docs/screenshots/04-evidence-automation.png)
*Exactly one procedure fits. The plan's provenance is shown argument by argument — 162/162 support,
63/63 held-out replay, signed off by a named human.*

![Decision, automation](docs/screenshots/05-decision-automation.png)
*AUTOMATE: guard and gate passed, four steps executed, zero model calls after classification, and
the run replays to a byte-identical outcome hash.*

### 3 — See refusal

![Evidence, refusal](docs/screenshots/06-evidence-refusal.png)
*The hero safety case. Two procedures are consistent with the same evidence, and the registry was
consulted **zero** times.*

![Decision, refusal](docs/screenshots/07-decision-refusal.png)
*REFUSE AUTOMATION: zero compiled steps, zero ledger entries, world hash unchanged — and the
competing procedures are named for the human who picks it up. "Refusing is not a failed attempt."*

### 4 — See safety

![Schema drift](docs/screenshots/08-schema-drift.png)
*A valid, human-approved plan meets a bank response that has changed shape. The Guard rejects the
result before it can become state; zero steps commit.*

![Ledger](docs/screenshots/09-ledger.png)
*Every decision is auditable rather than opaque: `intent`, `outcome` and `gate_verdict` entries in a
hash-chained log that verifies.*

### 5 — Research evidence

The v1 → v2 comparison is the research panel on the landing page above
([`01-landing.png`](docs/screenshots/01-landing.png)); the authoritative numbers are in the
[Research result](#research-result) table.

![Health](docs/screenshots/11-health.png)
*Readiness signal: `ready`, `ledger_valid`, `backlog=500`, and `research_grade: false` served by the
application itself.*

## Running locally

```bash
conda run -n rote python -m uvicorn rote.web.app:app --host 127.0.0.1 --port 8000
```

Warmup takes roughly **52 seconds** — plans are compiled at startup so nothing waits mid-demo. The
port does not accept connections until warmup finishes, so *"it answers"* is the readiness signal.
Watch for `warmup_complete … note="READY"` in the console, or poll:

```bash
curl -s http://127.0.0.1:8000/health
# {"ready":true,"warmup_seconds":46.22,"scenarios":6,"backlog":500,
#  "ledger_entries":0,"ledger_valid":true,"research_grade":false,"verify_evidence":false}
```

Then open **http://127.0.0.1:8000/**

Between rehearsals, restore a clean world, gate and ledger without recompiling:

```bash
curl -X POST http://127.0.0.1:8000/api/reset
```

## Demo flow (5 minutes)

1. **Problem** — the landing page: reasoning is fine, authority is the danger.
2. **Automation** — pick a case from the live queue and resolve it: one fitting procedure, guard and
   gate, zero model calls after classification.
3. **Ambiguity refusal** — pick an ambiguous case: two procedures fit, **zero plan lookups**, nothing
   executes, nothing is written.
4. **Adversarial safety** — schema drift: the Guard rejects a divergent result before commit.
5. **Ledger** — the chain verifies; `intent` is written before the call.
6. **v1 vs v2** — 60 wrong to 0, at the cost of 63% of coverage. That trade is the product.

## Safety properties

Each of these is enforced by code and pinned by tests:

- **Ambiguity stops before plan lookup.** The registry is never consulted for an ambiguous case —
  verified across all 316 refusals in a full live sweep.
- **The Guard rejects divergent results** before they become trusted state (two-phase commit).
- **The Gate is the only path to a tool.** Allowlist, per-category caps, rolling spend window.
- **Idempotent replay prevents duplicate action.** Keys are derived by the gate; callers cannot
  supply one. A replay returns the recorded result and writes no second `intent`.
- **The ledger records intent, outcome and every gate verdict**, hash-chained and verifiable.
- **Evaluation baselines are immutable**, checked by SHA-256 in the test suite.
- **The runtime cannot import evaluation code** — enforced by import-linter contracts, not
  convention.
- **Evidence can be re-read against the authoritative record before authority is granted**
  (optional), with every read passing the same Policy Gate under actor `system:verifier` — an AST
  test fails the build if any component calls an adapter directly.

## Evaluation

Two independent numbers, and they should not be conflated.

**Research evaluation** (offline, from a JSONL run log, `docs/baselines/`): the v1 → v2 comparison
above, computed by the evaluator over 500 exceptions.

**Live demo verification** (a full sweep through the real `SessionRuntime`, not the evaluation
harness):

```
cases              500        automated 184        refused 316        wrong 0
route reasons      {ambiguous_evidence: 316, plan_matched: 184}
plan lookups       184  (exactly the automated cases)
executed steps     518        guard inspections 1036
world changes      184        ledger 0 → 1036      ledger valid True
checker verdicts   {pass: 184, undetermined: 316}

ambiguous safety violations: none
automated safety violations: none
```

The live runtime reproduces the v2 result exactly through an entirely different code path.

## Honest limitations

- **Synthetic financial world.** Records, bank lines, fee schedules and FX rates are generated.
- **Deterministic stand-in agent and classifier.** No language model is involved anywhere.
- **`research_grade = False`.** No number in this repository is evidence about real reconciliation or
  about language models.
- **No real payment rail**, no bank connectivity, no external API, no credentials.
- **No authentication.** `human:ops-lead-42` is a demo naming convention for the approver, not an
  identity system.
- **Two compiled systems are held in memory** (normal and kill-switch) — a demo shape, not a
  deployment one.
- **Startup warmup of ~52 seconds.**
- **No claim of production-level accuracy.** Rote is level with the agent, never better.
- **The biggest unknown is coverage, not safety.** 36.8% is a property of six synthetic categories
  we wrote — only two of which are unambiguous. We have never established that a real exception
  queue (the residue *after* a rules engine) contains a meaningful unambiguous slice. If it is
  mostly ambiguous, Rote refuses nearly everything. That is a data question, and no further building
  answers it.
- **"Authoritative" means an independent *path*, not an independent *source*.** In this prototype the
  world and the evidence both come from one generator. A real deployment would read a genuinely
  separate system of record.
- **Nothing in the system reasons about meaning.** Every layer checks shape, range, allowlist or cap.
  That is precisely why the 60 errors got through, and why the answer was to refuse rather than to
  add a sixth checker.

## Project status

**Ready for buildathon demo.** Rote is demo-ready and experimentally validated in its synthetic
environment. **It is not production-ready financial infrastructure.**

## Roadmap

1. **A pre-deployment coverage report** — point it at an exception queue and get back "this
   fraction has exactly one fitting procedure." It answers the biggest open question and is the
   artifact a pilot would start from.
2. Real financial-system integration behind the Gate, with a genuinely separate system of record.
3. Real agent and model evaluation (the second-model skeleton-agreement experiment remains undone).
4. Durable ledger and trajectory storage; concurrency and durable idempotency.
5. Production authentication and approval workflows.
6. Larger and real-world datasets.

## Development

```bash
conda run -n rote python -m pytest          # 977 tests
conda run -n rote ruff check .
conda run -n rote ruff format --check .
conda run -n rote mypy --strict rote tests
conda run -n rote lint-imports              # 11 architectural contracts
```

**Running the demo yourself:** [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md) covers setup, warmup,
health checks, reset, troubleshooting, the five-minute script, the judge Q&A and the claims never to
make. [`docs/DEMO_CHEAT_SHEET.md`](docs/DEMO_CHEAT_SHEET.md) is the one-page version.

Design decisions and the full experimental record live in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
and [`docs/JOURNAL.md`](docs/JOURNAL.md), including the experiments that failed and the claims that
were retracted.

## License

No licence file is present in this repository. All rights reserved by the author unless a licence is
added.
