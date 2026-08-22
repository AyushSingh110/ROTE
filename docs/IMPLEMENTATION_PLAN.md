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

### Phase 9 — Align, bind, expectations, replay validation · `DONE` (2026-08-22)

**Approved amendment A4 — observational tools.** Verified against all eleven security invariants
before changing anything. Exposing a read does not weaken invariant 1 (every read still passes the
gate and is still audited), and §E1's allowlist bounds **actions**, not reads. One caveat changed
the rule's shape: threat T6 grows with read breadth, so the rule is *read-only tools returning
typed, non-sensitive fields*, encoded as a separately named `OBSERVATIONAL_TOOLS` group with the
reasoning above it rather than a quiet widening of `READ_TOOLS`. **Effect:** with decoys reachable
again, the same agent with detours on produces genuinely varied sequences and the probe correctly
refuses all six categories (0.16–0.28). The perfect score at zero detours is now measured, not
guaranteed.

**Measured result.** Compiled from 337 fit runs, validated on the 163 holdout runs untouched until
that moment.

```text
category                fit  steps  trunc   holdout  patheq  miss  validated
duplicate_entry          32      1   True        11      11     0  PASS
fee_mismatch             93      2   True        31      31     0  PASS
fx_rounding              46      2   True        29      29     0  PASS
partial_payment          39      2   True        21      21     0  PASS
timing_cutoff            68      1   True        41      41     0  PASS
transposed_reference     59      2   True        30      30     0  PASS

binding mix: {from_input: 11, literal: 4}
replay total: holdout 163  path-equal 163  playback misses 0
```

**163/163 unseen runs reproduced exactly, zero playback misses** — but **every category truncated**.
The compiled prefixes are perfect and nothing compiles all the way to the money.

457 tests passed (52 new) · ruff clean · `mypy --strict` clean over 79 files · import-linter 6/6.

**The finding: exactly two arguments in the whole system fail to bind.**

1. **`idempotency_key`** — in every unbound list, and it should never have been the plan's problem.
   §E1 already says the key is derived from `(exception_id, action_type, canonicalised_args)`, which
   is the **gate's** job; it became a tool argument the caller supplies, so the compiler is asked to
   learn something that should be computed. Moving it back would give:
   `duplicate_entry 3/3 · timing_cutoff 2/2 · transposed_reference 3/3` — **three of six categories
   compile end to end from one change.**
2. **`minor_units`** — the correction amount, internal minus bank. Not a field, not a constant:
   arithmetic. Exactly the gap tier-1 rule induction exists to fill.

Neither is mine to change — the first touches Phase 3 tool contracts and Phase 7's gate, the second
needs scikit-learn. **Both raised for approval, neither done.**

**Decisions.** (a) Alignment is by index over the modal group only, so step *i* is the same tool in
every run and no clever alignment is needed — the probe already did the hard part. (b) Binding asks
three questions cheapest-first: constant → task field → earlier result, earliest producing step
winning so the plan takes the shortest dependency. (c) **Types must match, not just values** — `5`
never binds to `"5"`. (d) **Ambiguity is recorded, never resolved silently**: if two fields both
always match, the shallowest wins *and the alternatives are stored*. (e) A **playback miss is a
failure, never a skip** — swallowing it would make every other number meaningless. (f) Expectations
store raw **and** widened numeric ranges, integer arithmetic throughout, so tolerance stays a
runtime knob and reports stay canonically comparable.

**Deliberately not done.** `FROM_RULE` (needs scikit-learn — dependency decision) and `FROM_SLOT`
(cut-list item #2). Invariants exist as a slot holding *names* of hand-written checks, never
expressions, and none are written yet — they arrive with the Guard. Every plan is emitted as
`DRAFT`; lifecycle, sign-off and kill switch are Phase 10, and passing validation is not the same as
being allowed to run.

**Standing caveat:** every number carries `research grade: False`.


**Tests first:** each `ArgBinding` hypothesis is inferred correctly from fixtures; hypothesis
ordering is respected (LITERAL before FROM_INPUT before FROM_STEP); ambiguous input paths are
recorded, not silently resolved; type mismatches never bind; a plan that asks for a
`(tool, args)` pair absent from the recording is a **playback miss and a failure**, never a skip.

**Measurable result:** replay pass rate on the 30% holdout per plan, with outcome / action / path
equality reported separately and playback misses counted.

### Phase 10 — Plan Registry, lifecycle, shadow mode, sign-off, kill switch · `DONE` (2026-08-22)

**Phase 9 contracts FROZEN** after three checks, all now tested: (1) an unknown formula name fails
closed — no fallback, no default, and no dynamic lookup anywhere in the compiler (`getattr`,
`importlib`, `__import__` all banned by test); a forged plan carrying an invented name *raises*
rather than quietly producing a number. (2) `MAX_ALTERNATIVES` cannot change the chosen formula —
compilation run at 0, 1, 3, 5 and 50 yields an identical winner. (3) `MAX_OPERANDS` rejects rather
than guesses — a cap that hides the true operands finds nothing and truncates, whatever survives any
cap still reproduces every run, and a narrower cap never invents a formula a wider cap rejected.

**Measured result.**

```text
six categories registered      -> all six landed in SHADOW, none active
20 agreeing shadow runs each   -> all six then activated by a named human
kill switch on one             -> no longer served, immediately

registry contents      : {active: 5, inactive: 1, shadow: 2}
ledger entries         : 23     ledger chain valid: True
every active validated : True   every active signed off by a human: True

lifecycle rebuilt from the ledger alone:
  duplicate_entry  v1:shadow(system:compiler) -> v1:active(human:ops-lead-42) -> v1:inactive(system:guard)
  fee_mismatch     v1:shadow(system:compiler) -> v1:active(human:ops-lead-42) -> v2:shadow(system:compiler)
```

530 tests passed (50 new) · ruff clean · `mypy --strict` clean over 85 files · import-linter 6/6.

**Every refusal, fired for the reason it claims:**

```text
system actor activating      -> refused: activation needs a named human actor, got 'system:auto'
activating with no sign-off  -> refused: activation needs a sign-off note on the diff
activating on thin evidence  -> refused: 1 agreeing shadow runs, 20 needed
registering unvalidated plan -> refused: has never been replay-validated
shadowing an active plan     -> refused: is active, not shadowing
```

**Decisions.** (a) An unvalidated plan cannot even be **registered** — refused outright, not stored
as inactive, because it is not a candidate for anything. (b) A plan that passes validation lands in
**SHADOW, never ACTIVE**: passing validation is a technical claim about held-out recordings, not
permission. (c) **There is no override parameter** — a test reads `activate`'s signature and fails
if force/override/skip/bypass/ignore appears. That is the difference between a rule and a policy; a
policy gets waived at 3am during an incident. (d) **Asymmetry is deliberate**: the system may
*remove* permission automatically (one shadow disagreement demotes; the kill switch needs no human)
but can never *grant* it — a test feeds 50 agreeing shadow runs and confirms the plan still waits.
(e) A killed plan cannot be switched back on; it must shadow and be signed off again.

**Deliberately not done.** Shadow mode **records** observations, it does not produce them — the
registry owns the rule (20 agreeing, 0 disagreeing) and accepts recorded outcomes; running a plan
beside the live agent needs the executor (Phase 11). The separation is deliberate: the permission
rule is testable today with no executor in existence. Nothing has yet executed a compiled plan
against the world — *permitted to run* and *running* remain two different things, and only the first
is built.


**Tests first:** a plan without a passing `ValidationReport` can never reach `ACTIVE`, with no
override flag; every transition writes a ledger entry naming the actor; the kill switch works.

**Measurable result:** lifecycle transitions reconstructable from the ledger alone.

### Phase 11 — Deterministic Plan Executor · `DONE` (2026-08-23)

**Prototype defaults confirmed:** 20 agreeing shadow runs is a configurable threshold;
`human:` is a prototype identity convention with no authentication built.

**Measured result — the project's headline claim, now a measurement.**

```text
exceptions run on the compiled path : 163   (the holdout, never seen during compilation)
outcomes                            : {resolved: 163}
checker verdicts                    : {pass: 163}
LLM calls made by the compiled path : 0

CONSISTENCY — 20 identical runs, a fresh world each time
  all six categories: 1 distinct outcome hash over 20 runs
```

**One distinct outcome for twenty identical runs — not "usually the same", one.** And the compiled
path is **as correct as the agent that taught it**: 163/163 pass the code-only checker with zero
model calls.

565 tests passed (35 new) · ruff clean · `mypy --strict` clean over 90 files · import-linter 7/7.

**Honest limit on that number.** It says *given the right plan, execution is perfectly repeatable
and correct*. It says nothing yet about **picking** the right plan — there is no classifier or
router, so each exception was handed straight to the plan for its true category. The deterministic
resolution rate needs Phase 13. **The mechanism is deterministic; the routing is untested.**

**Decisions.** (a) `outcome_hash` is defined **once**, in `contracts/execution.py`, and deliberately
**excludes** the plan identity so the same measurement can later compare compiled path against live
agent (metric §I.6). (b) Amendment A1's quarantine is real: call → hold aside → inspect → commit or
hand over. The Guard is Phase 12, so the inspector is injected and the default `AcceptEveryResult`
is named so nobody mistakes it for a check — **yet the quarantine rule is fully testable today** by
injecting a rejecting inspector, and the tests prove a rejected result is never committed, the
dependent step never runs, and the rejected value travels separately in `untrusted_result`.
(c) Any failure — refusal, cap breach, tool error, unresolved argument — returns *escalated*; there
is no path that returns resolved after something went wrong.

**Architecture conformance fix.** Wiring the executor, I imported the formula registry and path
resolver from `rote.compiler` — everything worked, and it was wrong: §G says runtime does not depend
on the offline compiler. The fix was not to copy but to notice where those belong. A plan saying
`difference` means nothing unless the compiler that wrote it and the executor that runs it agree
what `difference` is — that makes it a **contract**, exactly like `fingerprint.py`. Both moved to
`rote/contracts/`, and a **seventh import-linter contract** now forbids `runtime → compiler`.
*A boundary nobody wrote down is not a boundary.*

**Deliberately not done.** The Guard (Phase 12). No classifier, router, or handover consumer — when
the executor escalates it returns a serialisable handover package and stops; nothing picks it up yet
(Phase 13). Standing caveat: `research grade: False`.


Amendment A1 applies: two-phase state, quarantine then commit.

**Tests first:** a guard-failed result is **not** readable by a later `FROM_STEP` binding;
execution state stays flat and JSON-serialisable at every step; identical input produces an
identical `outcome_hash`; handover state serialises before the guard runs.

**Measurable result:** **exactly one distinct `outcome_hash` across 20 identical runs** of a
slot-free plan.

### Phase 12 — Guard + invariant registry · `DONE` (2026-08-23)

**Measured result — per-signal firing on a labelled divergence set** (529 checks per class):

```text
injected divergence       checks  aborted  abort %  struct  numeric  categ  behav  median div
none                         529        0     0.0%       0        0      0      0           0
schema_drift_missing         529        0     0.0%     499        0      0      0         350
schema_drift_added           529        0     0.0%     529        0      0      0         140
type_change                  529        0     0.0%      81        0      0      0           0
extreme_value                529        0     0.0%       0       81      0      0           0
unseen_enum                  529        0     0.0%       0        0    378      0         250
```

609 tests passed (44 new) · ruff clean · `mypy --strict` clean over 94 files · import-linter 7/7.

**⚠ FINDING: the Guard sees everything and stops nothing.** The signals are *correct* — clean
results fire nothing (zero false alarms), a vanished field fires structural on 499/529 and nothing
else, an unseen category fires categorical on 378/529 and nothing else. But §D2's approved settings
weight structural at 350 with an abort threshold of 500, so **a signal at full strength contributes
350, and no single signal can ever abort.** A bank changing its statement format scores 350 against
500 and sails through. Not a code bug — a property of the approved numbers, and reported rather than
quietly tuned. A test names it directly:
`test_no_single_signal_can_abort_under_the_approved_defaults`. **This is exactly what the Phase 14
sweep exists to settle**, and the table above is its input; picking a threshold by eye today to make
the number look better is the forbidden "tune until it looks good".

**Honest limit on the table.** `type_change` and `extreme_value` fire on only 81/529 because both
need an integer nested in the result and most steps return none — a weakness in my five hand-written
mutations, not in the Guard. Phase 14 needs a proper labelled divergence generator.

**The invariant veto works and outranks everything.** Posting 3× the record amount is vetoed; with
the threshold set so nothing could ever abort, it is *still* vetoed. Money safety must not be
adjustable by the same knob that controls sensitivity to cosmetic format drift. Invariants are named
functions in a closed registry — a plan refers to one and can never contain one; an unknown name
**raises** rather than being skipped; and a missing field makes an invariant **fail**, because
absence is not evidence of safety.

**Decisions.** (a) Two checkpoints: `check_proposed_action` runs on resolved arguments **before** the
gate (an invariant checked after the money moved prevents nothing); `check_result` runs on the
quarantined result before commit. (b) The Guard sits *beside* the gate and can only object — a test
asserts it holds no toolbox and imports neither an adapter nor the gate. (c) Every score is stored
as an integer per mille, so verdicts stay exactly comparable for the Phase 14 sweep. (d) The raw
per-signal vector is recorded on **every** check including passes.

**Contract change, additive and defaulted.** `StepExpectation` gained `schema_always` / `schema_ever`
so the structural signal can tell a new optional field (0.4) from one that vanished (1.0). Empty
defaults mean every previously compiled plan still validates and gets the older binary signal — same
safe category as Phase 10's activation fields.

**Deliberately not done.** No threshold chosen — the calibration finding stands open for Phase 14.
Behavioural is implemented and unit-tested but never fires in practice because nothing retries yet.
Invariants are not yet attached to any compiled plan: the registry and veto work, but the
category→invariant table is hand-written work still to do.


Subject to Q1 above.

**Tests first:** each of the five signals fires on its own fixture and on nothing else; the score
is a pure function of inputs; an invariant failure vetoes regardless of threshold; the raw
per-signal vector is logged on every step, not just the boolean.

**Measurable result:** per-signal firing table on the labelled divergence set.

### Phase 13 — Classifier, Router, boundary, handover · `DONE` (2026-08-23)

Also picks up the **ingestion/redaction boundary** deferred from Phase 7 — it protects the
classifier, which now exists.

**Measured result.**

```text
INGESTION BOUNDARY
  note in : "reach me at ops@merchant.example or card 4111111111111111"
  note out: "reach me at [redacted:email] or card [redacted:card]"   redactions: (card, email)

HANDOVER ON EVERY DIVERGENCE CLASS        quarantined   leaked into task input
  schema_drift_missing / schema_drift_added / type_change /
  extreme_value / unseen_enum / injected_text_in_a_result    True (6/6)      False (0/6)
```

667 tests passed (58 new) · ruff clean · `mypy --strict` clean over 105 files · import-linter 7/7.

**The number that looks bad and is the point.** A test-double classifier reading **only structured
fields** scores 101/163 (62%). Its errors are not random: every *transposed reference* and every
*duplicate entry* becomes *timing cut-off* (amounts match, bank posted later — all the numbers say),
and every *partial payment* becomes *fee mismatch* (identical from the numbers alone). Those three
confusions account for the 62. **This is the thesis as a measurement:** the structured fields cannot
tell a partial payment from a fee, because what distinguishes them is the merchant writing
"customer paid half now" in free text. The numbers say what to do; the note says what happened.

**⚠ Honest limit on the T2 injection defence.** A deliberately corruptible classifier that obeys any
note mentioning "duplicate" was caught 5/5 by the precondition check (`precondition_contradiction`
→ live agent). But the honest classifier's **62 misclassifications all routed to the compiled path
uncaught**. The rule:

> **The precondition check catches contradictions. It does not catch confusions.**

A note steering toward a category the data *contradicts* is refused; one steering toward a category
the data merely *permits* is not. That is the limit of any check on structured data. The remaining
defence is the per-category money cap — which is exactly why §F/T2 sizes the most text-dependent
categories lowest.

**Decisions.** (a) Classifier and router stay separate: they fail differently, they have different
trust levels, and **a component cannot cross-check itself** — merging them deletes the precondition
check. A test asserts the word "untrusted" never appears in the router's source. (b) A
`Classification` carries a typed category and nothing a model could act through; an answer outside
the allowed set becomes `UNKNOWN` (→ live agent) with the rejected text recorded, never swallowed.
(c) **Free text never reaches a hosted model** — the classifier refuses outright; structured redacted
fields may still go. (d) The classifier holds no tools at all, asserted by test. (e) The router takes
a `PlanSource` protocol so it never imports the offline compiler.

**A measurement I nearly reported as a defect.** My first handover run printed `leaked: True` for one
class. My own check was wrong — I used `"record"` as the marker and the task input legitimately
contains `record_id`, so a substring match found it. **A leak detector built on substring matching
finds its marker inside unrelated words**; fixed with distinctive sentinels, after which all six
classes report cleanly.

**Deliberately not done.** No real model — both classifiers used are test doubles, so every number
carries `research grade: False`. Nothing consumes the handoff yet (end-to-end wiring, not this
phase). **Guard thresholds untouched**, as instructed; the Phase 12 calibration finding stands open.


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
