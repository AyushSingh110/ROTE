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

## 2. Two points that need your confirmation (not blocking yet)

Recorded here rather than decided silently, per `CLAUDE.md` line 8.

**Q1 — the Guard runs in two positions, not one.**
`ARCHITECTURE.md` §A6 describes the guard as running *after* every step, on the result.
Amendment A1 confirms that. But `CLAUDE.md`'s own chain reads
`Compiled Plan -> Guard -> Policy Gate -> Tool`, i.e. guard *before* the call — and it has to,
because an invariant like `adjustment <= order_amount` must be checked **before** money moves,
not after. Checking it afterwards is useless.

*Proposal:* one Guard component, two entry points, same signal machinery and same score:

| Entry point | Runs | Checks |
|---|---|---|
| `check_proposed_action()` | after argument resolution, **before** the Policy Gate | invariant registry over resolved arguments |
| `check_result()` | on the **quarantined** result, before commit | structural · numeric · categorical · behavioural |

Both are drawn in `docs/architecture.mmd`. Not needed until Phase 12 — flag it now, decide later.

**Q2 — offline pipeline ordering.**
Your kick-off message lists `Compilability Probe -> Sequence Grouping`. The probe *measures the
output of* grouping (it needs a modal sequence before it can compute support), so
`docs/architecture.mmd` and `ARCHITECTURE.md` §C both order it
`Outcome Checker -> Exact Sequence Grouping -> Compilability Probe`. Say if you want it drawn
the other way; I believe the listed order was shorthand rather than a design intent.

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

### Phase 2 — Ledger + hash-chain verification · `TODO`

- `rote/safety/ledger.py` — append-only, `prev_hash` chaining over canonical bytes, `verify()`
  walking the chain.

**Tests first:** appending N entries produces a valid chain; tampering with entry *k* makes
`verify()` report **exactly** `k`; no `update`/`delete` path exists on the store (asserted);
`LedgerEntry` rejects unknown fields.

**Measurable result:** `verify()` names the exact sequence number of a tampered entry.

### Phase 3 — Synthetic generator + tool layer stubs · `TODO`

Six reconciliation categories, each carrying code-only ground truth. Generator knows the correct
**end state** only — it never encodes a tool sequence (Risk R2).

**Tests first:** every generated exception validates against the contract; ground truth is
present and well-formed; the adversarial generator emits injection payloads in free-text fields
only; the divergence generator labels each injected divergence class; generation is
seed-reproducible.

**Measurable result:** 500 exceptions across 6 categories, seed-reproducible byte-for-byte.

### Phase 4 — Code-only Outcome Checker · `TODO`

**Tests first:** returns `PASS` on a correct end state; `FAIL` on a corrupted one; `UNDETERMINED`
when the end state is incomplete; never reads the tool sequence or the agent's claims (asserted
by signature — it is not given them).

**Measurable result:** PASS on 500/500 ground truths, FAIL on 50/50 deliberately corrupted ones.

### Phase 5 — Hand-written live agent loop · `TODO`

No LangGraph. Explicit `while` loop, hard step cap, token budget, wall-clock budget, offline fake
model for tests.

**Tests first:** the loop terminates at the step cap; it never calls a tool except through the
gate handle; a malformed model response is rejected loudly rather than parsed leniently; the fake
model makes the whole suite runnable offline with no API key.

**Measurable result:** N exceptions resolved end-to-end offline; a test proves `rote.agent`
cannot import a tool adapter.

### Phase 6 — Recorder + trajectory store · `TODO`

**Tests first:** every step is captured; the recorder computes fingerprints itself and **rejects**
a caller-supplied one; `agent_model_id` / `prompt_template_id` / `untrusted_text_paths` /
`dry_run` are always populated (the never-backfillable fields from §B).

**Measurable result:** 100 trajectories recorded and round-tripped through the store unchanged.

### Phase 7 — Policy Gate + ingestion boundary · `TODO`

Amendment A2 applies: caps by `(path, category)`, no implicit live-agent privilege.

**Tests first:** allowlist refusal; per-action cap; rolling aggregate cap; idempotency key
prevents a double-post; `INTENT` is written before the call and `OUTCOME` after; a forced crash
between them leaves `UNKNOWN`; `UNKNOWN` is never auto-retried; dry-run is the default;
`import-linter` proves `runtime` and `agent` cannot import adapters.

**Measurable result:** forced-crash test yields `UNKNOWN`, not a double-post. Zero tool calls in
the whole suite bypass the gate.

### Phase 8 — Compiler: select, group, compilability probe · `TODO`

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
