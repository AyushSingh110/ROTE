# Rote — Implementation Plan

`docs/ARCHITECTURE.md` is the approved source of truth and is **not** re-opened here.
`docs/CLAUDE.md` holds the working rules. This file maps the approved architecture onto the
repository, records the amendments approved at the start of implementation, and tracks phases.

Every phase is **test-first** and ends in a **measurable result**, not a feature.

---

## 1. Amendments approved on 2026-08-22 (session 2)

These three corrections were given at implementation kick-off. They override the corresponding
text in `docs/ARCHITECTURE.md`. That file is left unedited on purpose; this section is the
amendment record.

### A1 — Tool results are quarantined until the Guard passes

A tool result does **not** become trusted executor state on return.

```text
Tool call -> Pending (quarantined) Result -> Guard -> PASS -> Commit trusted state
                                                   -> FAIL -> Handover / Escalation
```

A result that failed the Guard must never become readable by a `FROM_STEP` binding.

*Effect on the build:* `ExecutionState` gains an explicit two-phase commit. The executor holds
`pending: dict[int, Any]` separately from `committed: dict[str, Any]`, and only
`committed` is visible to argument resolution. Enforced by a test, not by convention.

*Consistency with the approved doc:* `ARCHITECTURE.md` §F/T3 already required the guard to run
"before the value is committed to executor state". A1 makes that normative and names the states.

### A2 — The live agent does not automatically outrank a compiled plan

`ARCHITECTURE.md` §E1 says *"compiled plans carry a strictly lower cap than the live agent."*
**That is withdrawn.** Monetary caps are explicit per `(path, category)` in configuration, with
no implicit ordering between the compiled path and the live agent. Any action above the safe
automatic threshold escalates to a human on **either** path.

*Effect on the build:* `PolicyRequirement` caps are looked up by `(path, category)`; the gate has
no notion of "the agent is allowed more". The default configuration sets the same automatic
threshold for both paths, and the difference between them — if any — becomes an explicit,
reviewable config value rather than an architectural assumption.

### A3 — Scope: what may be cut

Optional if the core research system falls behind: **shadow mode**, **the second domain
(dispute evidence)**, **UI polish**. Plus `CLAUDE.md`'s cut list: third domain, local model for
variable slots, live dashboard, automatic recompile-on-drift.

**Never cut:** compilability probe · replay validation · consistency measurement · policy gate ·
divergence evaluation · code-only outcome checking · audit trail · honest reporting of bad
results.

---

## 2. Resolved decisions

Both resolved on 2026-08-22. These are implementation clarifications of the approved design,
not architectural changes.

### Q1 — **APPROVED — one Guard component with two checkpoints**

One Guard component, two explicit entry points, sharing the same deterministic signal and
scoring machinery where appropriate.

| Entry point | Runs | Evaluates |
|---|---|---|
| `check_proposed_action()` | after argument resolution, **before** the Policy Gate and the tool call | deterministic invariants over the resolved arguments, e.g. `adjustment <= order_amount` |
| `check_result()` | after the tool returns, on the **quarantined** result, **before** it becomes trusted state | structural · numeric · categorical · behavioural |

```text
resolved arguments -> check_proposed_action() -> Policy Gate -> Tool
Tool -> pending / quarantined result -> check_result() -> PASS -> commit trusted state
                                                       -> FAIL -> handover / escalation
```

`check_proposed_action()` exists because an invariant checked only after the external action is
too late to prevent anything.

**Security invariant:** a result that failed `check_result()` must never become readable by a
`FROM_STEP` binding. Enforced by a test in Phase 11, not by convention.

### Q2 — **APPROVED — sequence grouping precedes the compilability decision**

Computational order:

```text
outcome-verified trajectories -> exact sequence grouping -> modal sequence
    -> support calculation -> compilability decision
```

So the pipeline reads `Outcome Checker -> Exact Sequence Grouping -> Compilability Probe`.
The probe is the GO/NO-GO decision taken on the evidence that grouping produced.
Clustering is **not** introduced as a replacement for grouping, in v1 or otherwise.

---

## 3. Architecture mapped onto the repository

| Approved component (`ARCHITECTURE.md` §A) | Module | Phase |
|---|---|---|
| — (shared contracts, fingerprint, canonical JSON) | `rote/contracts/` | 1 |
| A12 Audit Ledger | `rote/safety/ledger.py` | 2 |
| — (synthetic exception generator) | `rote/domain/generators/` | 3 |
| A10 Outcome Checker | `rote/domain/checkers/` | 4 |
| A8 Typed Tool Layer | `rote/domain/tools/` | 3, 5 |
| Live Agent (fallback) | `rote/agent/loop.py` | 5 |
| A9 Recorder & Trajectory Store | `rote/recorder/` | 6 |
| A7 Policy Gate | `rote/safety/gate.py` | 7 |
| A1 Ingestion & Redaction Boundary | `rote/safety/boundary.py` | 7 |
| A11 Plan Compiler | `rote/compiler/` | 8, 9 |
| A4 Plan Registry | `rote/compiler/registry.py` | 10 |
| A5 Plan Executor | `rote/runtime/executor.py` | 11 |
| A6 Guard + invariant registry | `rote/runtime/guard.py`, `rote/runtime/invariants.py` | 12 |
| A2 Classifier | `rote/runtime/classifier.py` | 13 |
| A3 Router | `rote/runtime/router.py` | 13 |
| Handover / fallback | `rote/runtime/handover.py` | 13 |
| Evaluation harness | `rote/eval/` | 14, 16 |
| Service + minimal demo | `rote/service/` | 16 |

Dependency direction is enforced mechanically by `import-linter`, configured in
`pyproject.toml` with four contracts matching `ARCHITECTURE.md` §G.

---

## 4. Phases

Status: `TODO` · `IN PROGRESS` · `DONE`.
Each phase lists the tests written **before** the code, and the number that must exist at the end.

### Phase 1 — Contracts, canonical serialisation, fingerprint · `DONE` (2026-08-22)

**Measured result:** 56 tests passed · ruff clean · `mypy --strict` clean over 22 source files ·
`import-linter` 4 contracts kept, 0 broken. Property tests (Hypothesis) confirm key-reorder
invariance and value-independence over generated nested structures.

**Decision taken during implementation — canonical serialisation rejects `float` entirely.**
A float has no single stable rendering across platforms, so allowing one inside a hash would
quietly break the determinism claim that the ledger chain, `outcome_hash` and replay comparison
all rest on. Consequence, which constrains Phase 3: **money is integer minor units** (`31750`,
never `317.50`) and **FX rates are scaled integers or strings**. This is normal payments practice,
so it costs nothing and removes a whole class of bug.

Smallest foundational slice. Everything else imports it, so it is frozen early and carefully.

- `rote/contracts/canonical.py` — canonical JSON bytes: sorted keys, no whitespace ambiguity,
  UTF-8, integers only for money, explicit UTC ISO-8601 for datetimes, rejection of float NaN/Inf.
- `rote/contracts/fingerprint.py` — structural hash: sorted `(json_path, type_name)` pairs,
  lists contribute element schema not length, depth-capped, values excluded, SHA-256.
- `rote/contracts/errors.py` — the typed exception hierarchy root.

**Tests first:** canonical bytes are stable under key reordering; two dicts differing only in key
order share a hash; adding a key changes the fingerprint; changing a *value* does not; changing a
*type* does; list length does not affect the fingerprint but element type does; depth cap is
enforced; NaN/Inf/naive-datetime are rejected loudly. Property tests via Hypothesis for the
reorder-invariance and add-a-key-changes-it laws.

**Measurable result:** property suite green over generated nested structures; a deliberately
reordered payload produces a byte-identical canonical form.

### Phase 2 — Ledger + hash-chain verification · `DONE` (2026-08-22)

**Measured result — achieved.** `verify()` names the exact position of the first tampered entry:

```text
intact ledger          valid=True   first_broken_seq=None  reason=None
payload tampered @5    valid=False  first_broken_seq=5     reason=payload does not match payload_hash
resealed forge @3      valid=False  first_broken_seq=4     reason=prev_hash does not match the previous entry hash
entry deleted @2       valid=False  first_broken_seq=2     reason=sequence number is 3, expected 2
entries reordered      valid=False  first_broken_seq=1     reason=sequence number is 6, expected 1
```

105 tests passed (49 new) · ruff clean · `mypy --strict` clean over 27 files · import-linter 4/4.

**Decisions.** (a) `LedgerEvent` (caller-supplied) is separate from `LedgerEntry` (ledger-sealed),
so a caller can never supply `seq`, `prev_hash` or `entry_hash` — the same principle as the
recorder computing its own fingerprints. (b) `entry_hash` covers the payload via `payload_hash`
rather than inlining it, so a payload can be redacted later without breaking the chain.
(c) Chain mathematics are free functions over any sequence of entries, so persistence can be
swapped in later without touching the part that must be correct. (d) Three event types were added
that §B omitted but the §0.5 lifecycle requires: `PLAN_VALIDATED`, `PLAN_SHADOWED`,
`PLAN_DEACTIVATED`; extending an enum is backward-compatible.

**Known limitation, documented not hidden.** A hash chain cannot detect deletion from the *tail* —
truncating the last N entries leaves a self-consistent chain. Proven by
`test_removing_the_last_entry_is_not_detectable_by_the_chain_alone`. The production answer is an
externally recorded head hash (periodic anchor / WORM storage, `ARCHITECTURE.md` §H). Not solved
in the prototype.


- `rote/safety/ledger.py` — append-only, `prev_hash` chaining over canonical bytes, `verify()`
  walking the chain.

**Tests first:** appending N entries produces a valid chain; tampering with entry *k* makes
`verify()` report **exactly** `k`; no `update`/`delete` path exists on the store (asserted);
`LedgerEntry` rejects unknown fields.

**Measurable result:** `verify()` names the exact sequence number of a tampered entry.

### Phase 3 — Synthetic environment + deterministic mock tools · `DONE` (2026-08-22)

**Measured result — achieved.**

```text
generator: 500 exceptions, 1 distinct digest over 5 runs (target 1); a different seed differs
category mix: fee 24.8% · timing 21.8% · transposed 17.8% · fx 15.0% · partial 12.0% · dup 8.6%
untrusted: 2 blocks per exception; 13.6% carry an injection; 0/500 markers reach structured facts
tools: 9 read-only tools x 40 calls across 2 independently built worlds -> 1 distinct result each
```

166 tests passed (61 new) · ruff clean · `mypy --strict` clean over 36 files · import-linter 5/5.

**Decisions.** (a) Ground truth is end state only — enforced by a test asserting no tool name
appears anywhere in the serialised ground truth, and another asserting no procedure-shaped field
exists. (b) The tool set is a deliberate **superset**: 12 tools, of which 3 are working decoys, so
a recorded tool choice is a real choice (Risk R2). (c) Every case draws the same eight random
values regardless of category, so the random stream never depends on the category schedule.
(d) FX rates are integer millionths and money is minor units — no floats anywhere, per Phase 1.
(e) Idempotency lives in the world: same key + same args replays; same key + different args raises
rather than silently overwriting. (f) A test parses every domain module's imports and fails on any
network, model, framework, clustering or higher-layer import — "offline with no agent or compiler
logic" is checked, not promised.

**Contract refinement found while implementing.** `mark_settlement_matched` gained a **required**
`status` argument (`matched` | `partially_settled`). Without it the `PARTIALLY_SETTLED` end state
that ground truth demands was unreachable by any tool, so Phase 4 would have failed every partial
payment. No default, because a default would let a real decision be skipped by accident. This
argument is expected to compile to a `FROM_RULE` decision table in Phase 9.

**Deferred out of this phase.** The divergence-labelled generator was listed here, but injecting
corrupted tool results is only meaningful once the Guard exists to catch them. Moved to Phase 8/14
where it is consumed.


Six reconciliation categories, each carrying code-only ground truth. Generator knows the correct
**end state** only — it never encodes a tool sequence (Risk R2).

**Tests first:** every generated exception validates against the contract; ground truth is
present and well-formed; the adversarial generator emits injection payloads in free-text fields
only; the divergence generator labels each injected divergence class; generation is
seed-reproducible.

**Measurable result:** 500 exceptions across 6 categories, seed-reproducible byte-for-byte.

### Phase 4 — Code-only Outcome Checker · `DONE` (2026-08-22)

**Measured result — achieved.**

```text
checker version: reconciliation-1        (500 exceptions, seed 13)
correctly resolved            pass 500   fail   0   undetermined   0
untouched (nothing done)      pass   0   fail   0   undetermined 500
corrupted (wrong bank line)   pass   0   fail 500   undetermined   0
unfinished (never closed)     pass   0   fail   0   undetermined 500
path independence: the same 500 endings reached by a different tool order -> identical verdicts
per category (correct): fee 124/124 · timing 109/109 · transposed 89/89 · fx 75/75 ·
                        partial 60/60 · duplicate 43/43
```

199 tests passed (33 new) · ruff clean · `mypy --strict` clean over 40 files · import-linter 5/5.

**Decisions.** (a) The third verdict is `UNDETERMINED`, the approved §A10 name — deliberately
**not** `UNKNOWN`, which is already the action state for "we sent a money instruction and then
crashed". Two different concepts must not share a name. (b) The verdict rule: an unclosed record
is `UNDETERMINED` regardless of side effects, because escalation is a safe outcome and must never
count as a wrong answer; mismatches are still listed, so nothing is hidden. `UNDETERMINED` runs are
ineligible for compilation, so an unfinished run can never teach a habit. (c) `check_outcome` takes
`ReconciliationFacts`, not `ReconciliationException` — that type has no free-text field, so the
checker **structurally cannot** read merchant notes. (d) Mismatches carry typed codes, so the
Phase 16 accuracy report can break failures down by cause rather than counting them.

**Risk R2 guard.** The reference resolver that produces correct endings for these tests lives in
`tests/domain/reference_resolver.py` and a test asserts no file under `rote/` mentions it. If that
hand-written correct procedure ever produced trajectories, the compiler would rediscover what was
written by hand and the central result would be void.

**Reading note for the report.** `skip_adjustment` and `double_post` corruptions show 241 pass /
259 fail. That is correct, not a gap: 241 exceptions (timing 109 + transposed 89 + duplicate 43)
need no adjustment at all, so those corruptions are no-ops for them. 124 + 75 + 60 = 259.


**Tests first:** returns `PASS` on a correct end state; `FAIL` on a corrupted one; `UNDETERMINED`
when the end state is incomplete; never reads the tool sequence or the agent's claims (asserted
by signature — it is not given them).

**Measurable result:** PASS on 500/500 ground truths, FAIL on 50/50 deliberately corrupted ones.

### Phase 5 — Hand-written live agent loop + trajectory recording · `DONE` (2026-08-22)

**Measured result — achieved.**

```text
500 exceptions, seed 5, offline, no API key
exploration 0.0   trajectories 500   outcomes {resolved: 500}   verdicts {pass: 500}
                  steps per run: min 2 / median 4 / max 4
exploration 0.35  trajectories 500   steps per run: min 4 / median 6 / max 6
determinism       same seed -> identical tool sequences across all 500 runs
tool variety      5 distinct sequences over the whole dataset, modal support 0.37
```

266 tests passed (67 new) · ruff clean · `mypy --strict` clean over 54 files · import-linter 5/5.

**⚠ The offline model scored 500/500. That is a warning, not a success.** Reported agent
implementations plateau at 85–92%; a stand-in that never errs is behaving like a hand-written
procedure, not an agent. **A compilability result computed only from `offline-heuristic-1`
trajectories is not a research result** — it would measure my own heuristic's self-consistency.
Phase 8 must run its probe on trajectories from a real model, and §I.8 skeleton agreement exists to
prove the discovered procedure belongs to the task rather than the model. Every trajectory records
`agent_model_id` so any later report can be split by producing model.

**Two forced deviations from the §B sketch**, recorded rather than made silently:

1. `category` / `category_confidence` are nullable. The classifier is Phase 13, so a Phase 5 run
   genuinely has no category. Side benefit: Phase 8 will group by the dataset's **true** category,
   which keeps "is the procedure stable?" separate from "can the classifier pick the right label?".
2. `GateVerdict` gains `UNGATED`. The gate is Phase 7; rather than leave the field empty, every step
   states out loud that no gate stood in its path. A Phase 7 test can then assert no `UNGATED` step
   remains. A visible gap is safer than an absent one.

**Decisions.** (a) The agent talks only to a `Toolbox` protocol in `contracts/tools.py`; the gate
implements it in Phase 7 and the agent will not notice. It can only see tools the boundary offers,
so a withheld tool is invisible rather than merely forbidden. (b) `run_agent` takes
`task_input`/`untrusted`, not a `ReconciliationException`, so `rote/agent` imports nothing from
`rote/domain`. (c) Three endings — `resolved`, `escalated`, `failed` — so a structurally broken
model (naming a tool never offered) cannot hide inside a normal-looking escalation. (d) The recorder
computes fingerprints itself and exposes no parameter to supply one; a test asserts the signature
never mentions "fingerprint". (e) Trajectory ids are derived from the correlation id, and the clock
is injected, so recordings are reproducible.

**Enforced by test:** the agent package imports no framework, never mentions ground truth or the
Phase 4 oracle, and never imports a tool adapter.


No LangGraph. Explicit `while` loop, hard step cap, token budget, wall-clock budget, offline fake
model for tests.

**Tests first:** the loop terminates at the step cap; it never calls a tool except through the
gate handle; a malformed model response is rejected loudly rather than parsed leniently; the fake
model makes the whole suite runnable offline with no API key.

**Measurable result:** N exceptions resolved end-to-end offline; a test proves `rote.agent`
cannot import a tool adapter.

### Phase 6 — Durable trajectory store · `DONE` (2026-08-22)

**Scope note.** The recorder, in-memory store and labelling were already delivered in Phase 5
(which was requested as "live agent + trajectory recording foundation"), and every test this phase
originally listed was already green. Rather than invent work, Phase 6 addressed the two things that
were genuinely missing: **nothing was persisted**, and **"round-tripped unchanged" had never been
tested through serialisation** — round-tripping through a list is trivially true and proves nothing.
The Phase 8 compiler is an offline batch job that must read recordings written by an earlier
process, which an in-memory store cannot support.

**Measured result — achieved.** Two separate interpreters, sharing only a filename:

```text
WRITER PROCESS   trajectories written 500   verdicts {pass: 500}
READER PROCESS   trajectories read    500   file on disk 2,088,960 bytes
                 select(verdict=PASS) 500   select(model=offline) 500
                 select(model=other)    0   select(outcome=escalated) 0
                 index columns match payload: True
in-suite         120 trajectories round-tripped BYTE-IDENTICAL, not merely equal
```

306 tests passed (40 new) · ruff clean · `mypy --strict` clean over 57 files · import-linter 5/5.

**`select(model=...)` is the isolation lever you asked for.** Every trajectory records
`agent_model_id`, so Phase 8 can be pointed at real-model recordings only and cannot be quietly
contaminated by `offline-heuristic-1`. The rule from Phase 5 is now enforceable with one argument
rather than remembered goodwill.

**Decisions.** (a) The canonical JSON payload is the source of truth; the indexed columns are a
projection used only to narrow a query, and a test rebuilds every row from its payload and asserts
the columns still agree, so the two can never drift. (b) SQLAlchemy **Core**, not the ORM — closer
to plain SQL and readable line by line. (c) One shared conformance test class runs against both
stores, so they are interchangeable. (d) Append-only: no update or delete method exists, and a
duplicate `trajectory_id` is rejected across process boundaries by a unique constraint.

**Contract finalised before first commit.** `select` was added to the `TrajectoryStore` protocol.
CLAUDE.md §5 freezes that protocol *once committed*, and nothing has been committed yet — so this
was the last safe moment to settle its shape rather than break it in Phase 8.

**The risk that was specifically tested.** A UTC timestamp can serialise as `...10:00:00Z` or
`...10:00:00+00:00` — both correct, different text. Since the headline consistency metric compares
runs byte-for-byte, a reformatted timestamp would report a difference that does not exist. A test
asserts timestamps survive the round trip exactly.

**Standard still unmet, flagged for decision.** CLAUDE.md requires structured JSON logging with a
correlation id through every layer; six phases in there is no logging at all. Half-adding it to one
module is worse than adding it properly once. *Proposal:* a small `rote/observability/` configuring
structlog, with the policy gate as the first consumer in Phase 7, since its decisions most need to
be traceable. Not done unilaterally.


**Tests first:** every step is captured; the recorder computes fingerprints itself and **rejects**
a caller-supplied one; `agent_model_id` / `prompt_template_id` / `untrusted_text_paths` /
`dry_run` are always populated (the never-backfillable fields from §B).

**Measurable result:** 100 trajectories recorded and round-tripped through the store unchanged.

### Phase 7 — Policy Gate + tool-boundary enforcement · `DONE` (2026-08-22)

**Measured result — achieved.** 500 exceptions, offline:

```text
DEFAULT POLICY
  recorded steps 1650 · gate verdicts in ledger 1650 · adapter calls 1650 · BYPASSES 0
  step verdicts {permit: 1650} · INTENT == OUTCOME (802 each) · UNKNOWN left behind 0
  checker verdicts {pass: 500}  <- the gate costs no resolution quality
  ledger chain valid, 3,254 entries · 3,300 JSON log events, all with a correlation id
```

357 tests passed (51 new) · ruff clean · `mypy --strict` clean over 64 files · import-linter 6/6.

**A real defect the measurement found — and the fix.** With a deliberately tight cap the gate
refused 171 over-cap adjustments correctly, but the agent absorbed each refusal as an ordinary tool
error and closed the settlement anyway:

```text
BEFORE  step verdicts {escalate: 171, permit: 1479} · outcomes {resolved: 500}
        checker verdicts {fail: 171, pass: 329}      <- 171 confidently wrong answers
AFTER   step verdicts {escalate: 171, permit: 1308} · outcomes {escalated: 171, resolved: 329}
        checker verdicts {pass: 329, undetermined: 171}  <- 171 honest hand-offs
```

The gate was never broken; the **agent was routing around it**, and a system that can be told "no"
and continue is not bounded. Four lines in the loop now end the run on an `ESCALATE` verdict. The
system resolves exactly the same 329 cases — it simply stopped pretending about the other 171.
*Lesson recorded:* the gate was tested, and the agent was tested, but not the seam between them.

**Decisions.** (a) The gate **is** the `Toolbox` the agent already talked to, so nothing above it
changed shape; it holds the adapters, and a forbidden tool is filtered out of `available_tools()`
so it is invisible rather than merely refused. (b) Every decision is recorded, permits included —
a gate that logs only refusals cannot prove it was consulted, and "0 bypasses" is measurable only
because the yeses are recorded too. (c) Any failure after `INTENT` is treated as `UNKNOWN`, even a
tidy not-found error: the gate cannot tell from outside whether the instruction landed, and being
optimistic double-pays. (d) `GateVerdict.UNGATED` now never appears in a gated run, asserted by
test — the Phase 5 placeholder became evidence. (e) Secrets are scrubbed by the log processor, not
by call sites.

**Amendment A2 made concrete.** Both execution paths start from identical caps and neither may
exceed them; there is no code path where "it was the live agent" grants more authority. Per-category
rules implement §F/T2: categories leaning hardest on merchant free text carry the **lowest** caps, so
a nudged label reaches less rope, and a fee plan cannot void a bank line at all.

**Logging kept minimal as approved:** one ~30-line module, JSON renderer, correlation id on every
event, secret scrubbing. `rote.observability` is enforced as a leaf by a sixth import-linter
contract.

**Still open from this phase's original scope.** The **ingestion / redaction boundary** was listed
here but only becomes load-bearing when a hosted model is wired in, and there is no model yet.
Proposed for Phase 13 alongside the classifier — the component it actually protects.

**Known limitation.** At-most-once survives a crash only within one process: the gate remembers
completed idempotency keys in memory. The ledger holds the durable record, so rebuilding that map
from the ledger at startup is the production answer. Not done.


Amendment A2 applies: caps by `(path, category)`, no implicit live-agent privilege.

**Tests first:** allowlist refusal; per-action cap; rolling aggregate cap; idempotency key
prevents a double-post; `INTENT` is written before the call and `OUTCOME` after; a forced crash
between them leaves `UNKNOWN`; `UNKNOWN` is never auto-retried; dry-run is the default;
`import-linter` proves `runtime` and `agent` cannot import adapters.

**Measurable result:** forced-crash test yields `UNKNOWN`, not a double-post. Zero tool calls in
the whole suite bypass the gate.

### Phase 8 — Compilability probe (Day-4 go/no-go) · `DONE` (2026-08-22)

**Measured result — the probe discriminates.** 500 exceptions, 70/30 hash split, fit = 337.
All three runs are `research grade: False` (offline test double), so these demonstrate the
**machinery**, not reconciliation.

```text
A. gate on, no detours            all six categories  support 1.00   COMPILABLE
B. ungated, detours 50%           all six categories  support 0.16-0.28  NON_COMPILABLE
C. ungated, detours 90%           all six categories  support 0.78-0.88  COMPILABLE
```

**Run B is the important one:** the probe says *no* to all six categories. A go/no-go that can only
say "go" is a rubber stamp. Run C shows support is **non-monotonic** in noise — worst in the middle,
because a detour taken almost always becomes part of the routine. The probe measures **consistency,
not quality**; a consistently wasteful procedure compiles fine, and judging sensibleness is the
human sign-off's job, not this number's.

405 tests passed (48 new) · ruff clean · `mypy --strict` clean over 71 files · import-linter 6/6.

**Decisions.** (a) The probe **may not know the tool set**: a test scans every compiler file and
fails if any of the twelve real tool names appears, another forbids importing the tools package, and
its own tests use invented names (`alpha`, `beta`, `gamma`). (b) It emits a **verdict, never a plan**
— keeping the decision separate from the construction stops a weak result quietly becoming a plan.
(c) A fourth verdict, `INSUFFICIENT_EVIDENCE`, fires below 20 eligible runs, so victory cannot be
declared on a sample of three. (d) The probe **refuses mixed producing models** — a skeleton across
two models conflates them, and §I.8 compares separate runs instead; every report carries its
`agent_model_id`. (e) Report ratios are stored as **integer counts and per-mille thresholds**, never
floats, so a report is canonically comparable — which §I.8's report-vs-report comparison needs.

**Two defects found and fixed.** (1) My measurement script built a fresh model per exception with the
same seed, replaying an identical random stream and manufacturing a fake support of 1.00 even under
heavy noise — *a suspiciously perfect number is a bug report*. (2) The offline test double asked for
`get_chargeback_history` without checking it had been offered; the loop correctly refused and 269 of
337 runs became ineligible. Fixed with a guard plus a test that runs at maximum exploration against a
narrowed toolbox and asserts no withheld tool is ever named.

**⚠ A finding needing your decision — two approved phases are in tension.**
Phase 3 deliberately gave the agent a **superset** of tools including three plausible-but-useless
ones, because "the agent chose these tools" is meaningless if there was nothing else to choose — that
superset is the Risk R2 defence. Phase 7's gate then excluded those three from the allowlist. The
result: **with the gate on, the agent cannot make a wrong tool choice, so support 1.00 is guaranteed
by construction rather than measured.** The three tools are read-only, move no money and carry no
authority, so refusing them buys no safety while allowing them restores the property the research
argument depends on. **Recommendation: allowlist read-only tools.** Not changed unilaterally.

**Honest headline: no research result has been produced.** The probe is ready; it needs trajectories
from a real model.


**This is the Day-4 go/no-go and the most important checkpoint in the build.**

**Tests first:** only checker-`PASS` trajectories are selected; the 70/30 split is hash-stable
across runs; the modal sequence and its support are computed correctly on hand-built fixtures;
support below threshold produces a `NonCompilableReport` rather than a plan.

**Measurable result:** the per-category table — eligible trajectories, modal sequence, support,
common-prefix support, alternative sequences, compilable yes/no. **If there is no stable
skeleton, that is the reported result. The compiler is not forced to emit a plan.**

### Phase 9 — Compiler: align, bind, induce, expectations, replay validation · `TODO`

**Tests first:** each `ArgBinding` hypothesis is inferred correctly from fixtures; hypothesis
ordering is respected (LITERAL before FROM_INPUT before FROM_STEP); ambiguous input paths are
recorded, not silently resolved; type mismatches never bind; a plan that asks for a
`(tool, args)` pair absent from the recording is a **playback miss and a failure**, never a skip.

**Measurable result:** replay pass rate on the 30% holdout per plan, with outcome / action / path
equality reported separately and playback misses counted.

### Phase 10 — Plan Registry · `TODO`

**Tests first:** a plan without a passing `ValidationReport` can never reach `ACTIVE`, with no
override flag; every transition writes a ledger entry naming the actor; the kill switch works.

**Measurable result:** lifecycle transitions reconstructable from the ledger alone.

### Phase 11 — Plan Executor · `TODO`

Amendment A1 applies: two-phase state, quarantine then commit.

**Tests first:** a guard-failed result is **not** readable by a later `FROM_STEP` binding;
execution state stays flat and JSON-serialisable at every step; identical input produces an
identical `outcome_hash`; handover state serialises before the guard runs.

**Measurable result:** **exactly one distinct `outcome_hash` across 20 identical runs** of a
slot-free plan.

### Phase 12 — Guard + invariant registry · `TODO`

Subject to Q1 above.

**Tests first:** each of the five signals fires on its own fixture and on nothing else; the score
is a pure function of inputs; an invariant failure vetoes regardless of threshold; the raw
per-signal vector is logged on every step, not just the boolean.

**Measurable result:** per-signal firing table on the labelled divergence set.

### Phase 13 — Classifier, Router, handover/fallback · `TODO`

**Tests first:** the classifier returns an enum member or raises — never free text, never an
action; an injected merchant note cannot change the return **type**; the category precondition
escalates when structured data contradicts the label; mid-run handover resumes correctly and
passes the diverging result as untrusted data.

**Measurable result:** handover succeeds on every injected divergence class.

### Phase 14 — Divergence evaluation · `TODO`

**Measurable result:** the missed-divergence vs false-abort curve across thresholds, and the
chosen operating point with its justification in operations language.

### Phase 15 — Shadow mode · `TODO` *(optional per A3)*

### Phase 16 — Full evaluation + minimal demo · `TODO`

**Measurable result:** all seven metrics from `ARCHITECTURE.md` §I computed from one JSONL run
log, plus the skeleton-agreement-across-models result (§I.8).

---

## 5. Dependencies, and why each is here

Approved in `ARCHITECTURE.md` §H. Installed now, pinned in `environment.yml`:

| Package | Why | Why not the standard library |
|---|---|---|
| `pydantic` | validation at every external boundary | stdlib `dataclasses` does not validate or coerce |
| `structlog` | structured JSON logs with a correlation id | stdlib `logging` needs hand-built JSON plumbing |
| `tenacity` | bounded retry with backoff, so transient and divergent stay distinguishable | hand-rolled retry is easy to get subtly wrong |
| `SQLAlchemy` | trajectory and ledger persistence, SQLite now and Postgres later | raw `sqlite3` would hard-code one database |
| `pytest`, `hypothesis` | tests; property tests on fingerprint and executor | `unittest` has no property testing |
| `ruff`, `mypy` | required quality gates | — |
| `import-linter` | mechanically enforces §G dependency direction | no stdlib equivalent |

Deferred, to be requested when the phase needs them: `scikit-learn` (Phase 9, tier-1 rules only),
`fastapi` + `jinja2` (Phase 16), and any hosted/local model client (Phase 5 — the test suite uses
an offline fake and must keep working with no API key).
