# CLAUDE.md — Rote

Read `docs/ARCHITECTURE.md` before implementing anything.

`docs/ARCHITECTURE.md` is the approved architectural source of truth.
This file contains implementation non-negotiables and working rules.

If something here conflicts with `docs/ARCHITECTURE.md` or a direct instruction
from me, STOP and ask before making a change. Do not silently reconcile conflicts.

---

## What this is

Rote — deterministic execution for payment-operations agents.

Rote records how an agent resolves settlement exceptions, compiles repeated
resolution procedures into typed LLM-free execution plans, guards every step
against learned expectations, falls back to the live agent on divergence, gates
every money-moving action through a deterministic policy layer, and writes an
append-only audit ledger.

**Load-bearing thesis: the model classifies; compiled deterministic code executes.**

Core money movement — authorization, capture, settlement, ledger postings —
is deterministic hardcoded software and stays that way.

No language model goes near core money movement.

The target is the exception layer around that core: settlement lines that the
rules engine cannot match and that humans work today.

**What Rote is for:**

Rote is a mechanism for earning an agent the right to act unsupervised on the
routine head of the exception distribution while humans retain the tail.

The goal is not cost reduction as the primary claim.

Determinism, replay, bounded policy, and verified outcomes are how permission
to act autonomously is earned.

### Never write these claims in code, comments, docs, README, or demo text

- "agents process payments, we make that cheaper"
- "we replace the rules engine"
- "we remove humans from finance ops"
- "first of its kind"

These statements are false or unsupported by the project framing.

---

# HARD RULES

These rules are implementation non-negotiables.

Do not violate them without asking me first.

---

## 1. Architecture

`docs/ARCHITECTURE.md` is approved.

Do not redesign the architecture during implementation.

If implementation reveals that an architectural assumption is wrong:

1. Stop.
2. Explain the problem.
3. Show the evidence/test failure.
4. Propose the smallest change.
5. Wait for approval before changing the architecture.

Do not silently simplify, replace, or invent architectural components.

---

## 2. NO LANGGRAPH

Do not use LangGraph.

Do not add LangGraph as a dependency.

Do not create a LangGraph abstraction layer.

Do not reintroduce LangGraph because a component appears easier to implement
with it.

The live agent must use a small, explicit, hand-written Python execution loop.

Use ordinary Python control flow, explicit state, typed models, and small
functions/classes.

The project is not intended to demonstrate agent orchestration frameworks.

The important research boundary is:

    model
      ↓
    classification
      ↓
    deterministic routing
      ↓
    compiled execution
      ↓
    guard
      ↓
    policy gate
      ↓
    tool

The live agent is the fallback path when a suitable compiled plan does not
exist or when a compiled plan diverges.

---

## 3. TEST BEFORE CODE

For every major implementation component:

1. Inspect existing code.
2. Define the expected contract.
3. Write focused tests first.
4. Run the tests.
5. Confirm the failures are expected.
6. Implement the smallest solution.
7. Run the tests again.
8. Add regression/property tests where appropriate.
9. Only then move to the next component.

Do not write the entire system and test it at the end.

The development loop is:

    Understand
        ↓
    Define contract
        ↓
    Write tests
        ↓
    Run tests
        ↓
    Implement
        ↓
    Run tests
        ↓
    Review
        ↓
    Continue

If an assumption cannot yet be tested, state the assumption explicitly before
implementing around it.

Never claim a component works without actually running the relevant tests.

---

## 4. Git

Never:

- `git push`
- add a remote
- `git reset --hard`
- force
- rebase
- delete files
- perform destructive repository operations

You may:

- initialize the repository
- create branches if needed
- stage files
- inspect history
- propose commit messages

I review and push myself.

Ask before any destructive operation.

---

## 5. Frozen contracts

Once committed, the following are treated as stable contracts:

- contract models
- `TrajectoryStore` protocol
- serialized representations used across layers

Changing a frozen contract can ripple through the system.

If a contract change becomes necessary:

1. Explain why.
2. Show the affected layers/tests.
3. Propose the change.
4. Wait for approval.

Do not silently modify frozen fields.

---

## 6. Dependency direction

Dependency direction is one-way.

    contracts
        ↓
    safety
        ↓
    runtime
        ↓
    adapters/tools

Lower-level contract/safety modules must not import higher-level runtime
components.

In particular:

- `contracts` imports nothing from the application
- `safety` imports only contracts and permitted lower-level dependencies
- `runtime` may import `safety`
- `safety` must never import `runtime`
- concrete tool adapters must remain behind the tool boundary

If the policy gate needs to import runtime code, the design is wrong.

Stop and explain the dependency problem instead of creating a circular dependency.

Use import-linter to enforce the intended boundaries.

---

## 7. Dependencies

No new dependency without asking.

Before introducing a dependency, state:

- what problem it solves
- why the standard library is insufficient
- whether an existing dependency can already solve it
- the maintenance/complexity cost

Prefer the standard library when it is sufficient.

Pin dependencies in `environment.yml`.

Keep `pyproject.toml` and `environment.yml` consistent.

---

# SECURITY INVARIANTS

These are security invariants, not suggestions.

A change that violates one is a defect even if tests currently pass.

---

## 1. Policy gate is the tool boundary

The policy gate sits immediately before tool execution.

Both paths must pass through it:

    Compiled Plan
         ↓
    Policy Gate
         ↓
       Tool

    Live Agent
         ↓
    Policy Gate
         ↓
       Tool

No code path may directly call a money-moving tool without passing through
the policy gate.

Neither the compiled executor nor the live agent may bypass it.

---

## 2. A model never emits an action

The classifier returns a typed category only.

The classifier does not return:

- tool calls
- money-moving actions
- execution plans
- executable instructions

Every model output must be schema-validated before downstream code reads it.

A model output must never directly authorize an external action.

---

## 3. Untrusted text is quarantined

Merchant notes, statement narrations, ticket bodies, and similar free text are
untrusted input.

They must be placed in a clearly labelled, delimited data block.

Never concatenate untrusted text into model instructions.

The component that reads untrusted content must not itself hold write capability.

Treat prompt injection as a first-class threat.

Sensitive/untrusted text must not reach a hosted model when the approved
architecture requires local processing.

---

## 4. Only outcome-verified trajectories can teach the compiler

A trajectory can enter compilation only after the code-only Outcome Checker
has verified the final outcome.

The compiler must never learn from:

- failed runs
- unverified runs
- `UNKNOWN` runs
- incomplete runs

The model's confidence is not ground truth.

The agent's own claim of success is not ground truth.

Ground truth comes from code-only checking.

---

## 5. Validation is required before activation

A plan without a passing `ValidationReport` is never activatable.

No override flag may bypass this rule.

The lifecycle must remain explicit:

    DRAFT
      ↓
    SHADOW
      ↓
    ACTIVE
      ↓
    RETIRED

Activation must be auditable.

---

## 6. Tool results are untrusted until Guard approval

A tool result must NOT immediately become trusted executor state.

Use:

    Tool call
        ↓
    Pending / quarantined result
        ↓
    Guard
        ↓
      PASS
        ↓
    Commit trusted state

If Guard fails:

    Pending result
        ↓
    Handover / escalation

The failed result must not become available as a trusted `FROM_STEP` binding.

This ordering prevents a poisoned or unexpected tool result from contaminating
future execution state.

---

## 7. INTENT / OUTCOME / UNKNOWN

Every mutating action follows:

    INTENT
       ↓
    external call
       ↓
    OUTCOME

If execution crashes after INTENT but before a confirmed OUTCOME:

    UNKNOWN

`UNKNOWN` is not success.

`UNKNOWN` is not failure.

`UNKNOWN` must never be automatically retried as if the external outcome were
known.

It must enter the approved recovery/escalation path.

---

## 8. Idempotency

Every mutating call carries an idempotency key derived from the exception/action
identity.

The at-most-once record is written before the external call.

The system must protect against duplicate execution caused by:

- retries
- crashes
- repeated requests
- replay
- handover

Do not silently retry an action whose external outcome is UNKNOWN.

---

## 9. Dry-run by default

Writes are dry-run by default.

Real writes require an explicit opt-in flag.

The prototype must use safe mocks/in-memory adapters.

Do not connect the prototype to real financial systems.

---

## 10. Hosted-model boundary

Sensitive fields must be redacted before anything reaches a hosted model.

Sensitive/untrusted free text uses the approved local-model path.

Hosted-model calls must reject payloads containing data that the architecture
marks as local-only.

---

## 11. Secrets

Secrets come from environment variables only.

Use test credentials only.

Never put secrets in:

- source code
- logs
- traces
- fixtures
- documentation
- commit messages
- generated artifacts

---

# CODE STANDARDS

## Environment

- Python 3.11
- conda environment: `rote`
- never install packages into base
- pinned dependencies in `environment.yml`

## Quality gates

Keep these clean throughout development:

- `ruff`
- `mypy --strict`

Use tests alongside the code.

Everything important must run offline without an API key.

## Error handling

- Validate at every external boundary.
- Malformed data fails loudly at the edge.
- Use a typed exception hierarchy.
- No bare `except`.
- No swallowed errors.
- No silent fallback.
- Every fallback must be observable and logged.

External calls require:

- timeout
- bounded retry
- backoff

Transient failures and divergence must remain distinguishable.

## State

Execution state must be a flat serialisable dictionary of named values.

Do not store:

- closures
- live objects
- non-serialisable runtime state

Mid-run handover depends on serialisable state.

## Logging

Use structured JSON logging.

Every exception carries a correlation ID.

Carry that correlation ID through every relevant layer.

Do not log unnecessary sensitive data.

## Comments

Comments must be one line and explain only non-obvious reasons.

No block comments.

No long explanatory docstrings.

If a comment explains what code does rather than why it exists, improve the
function/variable name instead.

---

# SESSION PROTOCOL

## Before code

Before implementing a requested change:

1. State in approximately five lines:
   - what you understand
   - what you will implement
   - what you will test
   - what you will not touch
   - any assumption requiring approval

2. If the task is ambiguous, STOP and ask.

Do not invent requirements.

## After code

Always produce an EXECUTION REPORT containing:

- what was built, in plain English
- every file added/changed, one line each
- decisions made and why
- what I must review before pushing
- what is deliberately not done
- exact commands to verify the result
- actual test/lint/type-check output

Never say:

    "should work"

Run it and report what actually happened.

---

# JOURNAL

Always append to:

    docs/JOURNAL.md

The journal is written in simple English for a reader who is not a Python expert.

Each session entry must contain:

- date
- what we built and why
- important errors
- what broke
- the actual root cause
- how it was fixed
- what I should have noticed sooner
- design decisions that changed and why

Append only.

Never rewrite previous entries.

This document is interview revision material.

Error stories matter as much as successful implementation.

---

# SCOPE DISCIPLINE

This is a solo build with a 15-day deadline ending 4 September.

If something cannot be implemented well within the deadline:

1. Say so directly.
2. Explain why.
3. Propose the smallest version that still proves the research thesis.
4. Wait for approval if the scope change affects architecture.

Do not start features that cannot be finished properly.

## Cut order

Cut these first if schedule pressure appears:

1. third domain
2. local model for variable slots
3. live dashboard
4. automatic recompile-on-drift

Shadow mode may also be deferred if necessary.

## Never cut

Never cut:

- compilability probe
- divergence detection
- divergence fallback
- replay validation gate
- audit trail
- consistency experiment
- policy gate
- code-only outcome checking
- honest reporting of bad results

If the system fails an experiment, report the failure.

Do not tune the evaluation until the result looks good.

---

# UI

The UI is not the research contribution.

Target approximately:

- 10% of effort
- server-rendered
- two counters
- one chart

Do not build React.

Do not allow UI work to delay core evaluation.

---

# RESEARCH CHECKPOINTS

Every major phase ends in a measurable result, not simply a feature.

Bad:

    "Divergence detection works."

Good:

    "At threshold 0.40, the guard caught 87% of injected divergences with
    a 4% false-abort rate."

Never invent the number.

The actual measured result must be reported.

---

## Day-4 Compilability Probe

This is a major research checkpoint.

Before spending significant time on the compiled executor, measure whether
verified trajectories actually contain stable resolution skeletons.

For each category report:

- eligible trajectories
- modal tool sequence
- support
- common-prefix support
- alternative sequences
- compilable / non-compilable

If exceptions are too irregular to produce a stable skeleton:

- do not force compilation
- narrow the category
- compile a stable common prefix if justified
- or report the category as non-compilable

Non-compilability is a valid research result.

---

# EVALUATION

Measure in this order:

1. Deterministic resolution rate
2. Consistency
3. Escalation rate by guard signal
4. Divergence curve
5. Audit replay fidelity
6. Accuracy against the code-only checker
7. Cost and latency

Cost and latency are supporting evidence, not the headline.

## Deterministic resolution rate

Measure:

    percentage of cases closed with zero LLM calls after classification

## Consistency

For repeated identical inputs, measure outcome/action/path consistency.

For a fully slot-free compiled deterministic plan, the target is:

    exactly one outcome hash

Do not incorrectly claim zero variance for:

- live-agent runs
- slot-bearing plans that invoke a model
- components that intentionally contain probabilistic behavior

Report those separately.

## Guard escalation

Break escalation down by the signal that fired:

- structural
- numeric
- categorical
- behavioural
- invariant

## Divergence curve

Sweep guard thresholds and report:

- missed divergences
- false aborts

Do not collapse the entire result into one accuracy number.

## Audit replay

Verify that the recorded audit trail can reconstruct:

- plan version
- execution path
- tool calls
- policy decisions
- guard results
- terminal outcome

## Accuracy

Compare Rote and the live-agent baseline against the code-only Outcome Checker.

Report:

- both pass
- both fail
- only Rote passes
- only live agent passes

Never hide cases where the live agent performs better.

---

# ARCHITECTURE DIAGRAM

Maintain:

    docs/architecture.mmd

The Mermaid diagram must reflect the approved architecture.

It must show:

    Exception
        ↓
    Tool-free Classifier
        ↓
    Deterministic Router
        ↓
    Plan Registry
        ↓
    Compiled Plan OR Live Agent
        ↓
    Guard
        ↓
    Policy Gate
        ↓
    Tool Layer
        ↓
    INTENT / OUTCOME / UNKNOWN
        ↓
    Ledger

And the offline path:

    Recorded Trajectories
        ↓
    Outcome Checker
        ↓
    Compilability Probe
        ↓
    Exact Sequence Grouping
        ↓
    Align
        ↓
    Bind
        ↓
    Induce
        ↓
    Expectations
        ↓
    Replay
        ↓
    SHADOW
        ↓
    Human Sign-off
        ↓
    ACTIVE Plan

Also show:

    Guard → Handover / Escalation

    Policy Gate → Reject / Escalate

The diagram must make clear that:

- Classifier = probabilistic
- Router = deterministic
- Compiler = offline
- Compiled Executor = deterministic
- Live Agent = fallback
- Guard = deterministic
- Policy Gate = deterministic
- Ledger = append-only/auditable

Do not put LangGraph anywhere in the architecture diagram.

---

# FINAL IMPLEMENTATION PRINCIPLE

Build the smallest system that can convincingly demonstrate:

    repeated verified agent behaviour
            ↓
    stable enough to compile
            ↓
    typed deterministic plan
            ↓
    replay validation
            ↓
    guarded execution
            ↓
    policy-bounded autonomy

The goal is NOT to build the largest agent platform.

The goal is to produce rigorous evidence for the Rote thesis.

If the evidence contradicts the thesis, report it honestly.

Start with tests and measurable checkpoints.