# Rote — Build Journal

A running diary of the project, written in simple English.
Append only. Never rewrite an old entry. If an old entry turns out to be wrong,
say so in a *new* entry and explain what changed.

The error stories matter as much as the code. Read this before the interview.

---

## 2026-08-22 — Session 1: architecture review

### What we did today

No code. Only design.

I gave Claude my architecture draft (`docs/ARCHITECTURE_DRAFT.md`, version 0.2) and asked
it to attack the design rather than replace it. The result is `docs/ARCHITECTURE.md`,
which is now the specification I will build from.

Three documents exist now:

- `docs/ARCHITECTURE_DRAFT.md` — my original design. Kept so the two can be compared.
- `docs/ARCHITECTURE.md` — the reviewed design. This is the one I build.
- `docs/PLAIN_ENGLISH.md` — the whole project explained in easy English, plus an honest
  assessment of whether the idea holds up.

### What the project is, in one paragraph

A payment company's main money system is ordinary exact code, and it stays that way.
But every night some payment lines fail to match the bank's file. Those are called
exceptions and humans fix them one at a time, because the input is messy free text.
My idea: an AI decides *what kind* of problem it is, and then plain code — with no AI
in it — does the repair, because the repair is the same few steps every time. The plain
code is not written by hand. It is built automatically by watching many successful AI
runs and finding the steps they all shared.

### The nine things that changed from my draft, and why

Written in short form. The long reasoning is in section §D of `ARCHITECTURE.md`.

1. **No embeddings in the router.** My draft had the router compare the exception to a
   plan using a small language model that turns text into numbers. Claude argued that the
   classifier has *already* answered the question, so this is a second, blurrier opinion
   about a settled question — inside the one part of the system whose whole point is that
   it is explainable. It also drags in about 2 GB of extra libraries.
   **New rule: one active plan per (domain, category, currency). Routing is a dictionary
   lookup plus a few yes/no checks.** The field for the embedding stays in the data model
   but is left empty, so I can add it later without changing the schema.
   *I accepted this. The interview sentence "the router is a dictionary lookup and five
   boolean checks" is much stronger than anything about cosine distance.*

2. **No clustering. Just counting.** My draft used scikit-learn clustering to find groups
   of similar runs. Claude pointed out I am grouping short sequences of tool names, from a
   set of maybe twelve tools, already split by category. That is a counting problem, not a
   clustering problem. Counting gives me the number I actually want: *"241 of 300 verified
   runs used the identical tool sequence."* Clustering gives me a silhouette score, which
   proves nothing to anybody.
   *I accepted this. It is simpler and the output is directly the evidence I need.*

3. **Invariants are named functions, not text.** My draft had a field holding rules as
   text, like `adjustment <= order_amount`. To use them I would have to write something
   that runs text as code. Text coming out of a compiler that read AI-produced recordings,
   being executed, inside a system that moves money — that is a security hole and a week
   of work. **New rule: a plan refers to an invariant by name, and the invariant itself is
   a normal Python function I wrote and tested.** A plan can never contain code.
   *I accepted this immediately. This was the clearest mistake in my draft.*

4. **Write the agent loop by hand, do not use LangGraph.** Claude's argument: my own first
   rule is that I must explain every line in a panel, and LangGraph's state handling is
   hard to explain under pressure — especially if someone on the panel knows it better than
   I do. A tool-calling loop is about 150 lines. And the recorder has to intercept every
   step anyway, which is easy in my own loop and painful through a framework's callbacks.
   *I accepted this. It also removes a dependency I would have spent a day learning.*

5. **Free text never leaves the machine.** My draft split hosted model and local model by
   "sensitivity", which is a judgement I would make at each call site — and therefore get
   wrong once. **New rule: any field that can contain merchant-written or customer-written
   free text goes only to the local model.** The hosted model sees structured, redacted
   fields and nothing else. This is enforced by the type of the field, not by me
   remembering.
   *Accepted. Cost: my classifier quality on text-heavy categories is limited by the small
   local model. That is measurable, and reporting it is fine.*

6. **Three components I had left out.** The Outcome Checker (mentioned everywhere in my
   draft but never given a home, even though it decides what may be compiled *and*
   produces the headline accuracy number), the Plan Registry (nothing owned plan versions,
   activation, or the kill switch), and the Ingestion/Redaction Boundary (I had treated
   redaction as a security note, but it is really the first component in the request path).
   *Accepted. I should have noticed the checker myself — I referred to it eleven times and
   never gave it inputs or outputs.*

7. **Open question 1 answered: the classifier is always separate from the live agent.**
   And the reason is security, not cost. My own threat model says the step that reads
   untrusted text must hold no write ability. If the classifier *is* the agent, then the
   thing reading hostile merchant notes is the thing holding every tool. That is a direct
   contradiction inside my own document.
   *This is the answer I will give if asked, and I should have spotted the contradiction.*

8. **Open questions 2, 3, 5, 6 answered.** No branching in version 1 — a plan is a straight
   line, and a plan that does four of six steps and then hands over is still a real result.
   Number tolerance is learned per field but by one fixed formula, and I store both the raw
   observed range and the widened range so I can re-tune later without recompiling. State
   for handover is saved in the executor *before* the guard runs, so that if the guard
   itself crashes I still have a clean handover point. And keep the hash-chained ledger —
   it is about forty lines, and the hard part (canonical JSON) is needed anyway for the
   consistency measurement.

9. **New: SHADOW mode.** A plan that passes validation does not become active straight
   away. It first runs alongside the AI agent without permission to act, and its answers
   are compared. Only after it agrees enough times, and a named human signs off, does it
   become active. Claude's argument for adding this: my thesis is that determinism and
   replay are how an agent *earns* permission. Shadow mode **is** that earning, made real.
   Without it my story stops at "it validated on held-out data", which is a machine-learning
   claim. With it, the story ends at "it proved itself with no authority, then a named human
   granted it a bounded remit" — which is the operations claim I actually want.
   *Accepted, but marked as the second thing to cut if I run out of time.*

### Two additions to the security thinking that were not in my draft

**Classifier steering.** I had thought about prompt injection as *"the note tells the AI to
approve the payment."* Claude pointed out the version that actually works: the note does not
need to give any instruction at all. It only needs to nudge the AI toward whichever category
has the highest money limit. No instruction-following required — just a pushed label.

The defence is a **category precondition**: plain code in the router that checks whether the
structured numbers independently support the label the AI chose. If the AI says "fee problem"
but the amounts do not look like a fee problem, we do not trust it, and we escalate.
This is also the strongest reason to keep the classifier and the router as separate
components: **one component cannot cross-check itself.**

**At-most-once needs three states, not two.** My draft said "record the action before the
call, not after". That is only half right. If the process dies *between* writing "I am about
to do this" and writing "here is what happened", the action is in an unknown state. It might
have happened. It might not. So there must be a third state, `UNKNOWN`, and an `UNKNOWN`
action goes to a human. It is never retried automatically. Retrying an unknown money action
is how you pay somebody twice.

### The error we hit today

**What broke.** Writing `ARCHITECTURE.md` through the Bash tool with a shell here-document
failed with `unexpected EOF while looking for matching quote`.

**What the real cause was.** This is Git Bash on Windows. A large multi-line document
containing backticks, quotes and code fences, pushed through a shell here-document, is
fragile — the shell tries to parse content that was only ever meant to be text.

**How we fixed it.** Stopped using the shell for it and used a direct file-write instead.

**What I should have noticed sooner.** The shell is for running commands. It is not a text
editor. If I am writing a document, I should write the document — not build it out of shell
syntax. This is a small thing today, but the same mistake with a script that touches money
would not be small. **Lesson: use the tool that matches the job.**

### What I have to decide before the next session

Claude has stopped and is waiting. Before I say "approved" I must decide the nine
disagreements above. The ones I should think hardest about:

- **D1 (no embeddings)** — this is a real narrowing of what the router can do. Am I
  comfortable saying "one plan per category" out loud?
- **D4 (no LangGraph)** — using a known framework looks good on paper. Writing my own loop
  is better for the panel *only if* I can genuinely explain it. Can I?
- **D9 (shadow mode)** — this is real extra work. Is the story worth the day?

### What is deliberately not built yet

Nothing at all is built. No `environment.yml`, no `pyproject.toml`, no package, no tests.
That is on purpose — my rule 1 is architecture first, and implementation does not begin
until I say the word "approved".

---

## 2026-08-22 — Session 2: implementation begins (Phase 1)

### What we did today

The architecture was approved, so today we started writing real code.

We set up the project, and built the **first and smallest piece**: the two functions that
everything else in Rote will depend on.

Rule followed all day: **write the tests first, watch them fail, then write the code.**

### The two things we built

**1. Canonical serialisation** (`rote/contracts/canonical.py`)

"Canonical" means: turn data into bytes in *exactly one* way, always.

Why this matters. Later in the project I need to say things like *"this run produced the same
result as that run."* The only honest way to compare two results is to turn both into bytes and
compare the bytes. But normally the same data can become bytes in many different ways —
`{"a":1,"b":2}` and `{"b":2,"a":1}` hold the same information but look different. If I compared
those naively I would report a difference that does not exist.

So this function sorts the keys, removes all spacing, and always uses UTF-8. Same data in,
identical bytes out, every time.

This one function is the foundation of three separate claims in the project:
the hash chain in the ledger, the `outcome_hash` used for the consistency measurement, and the
replay comparison. If this function is wrong, all three are wrong. That is why it was built
first and tested hard.

**2. Structural fingerprint** (`rote/contracts/fingerprint.py`)

This answers: *"what SHAPE does this result have?"* — not what it contains.

It walks the result and writes down every field path and its type, ignoring the actual values.
So `{"fee": 100}` and `{"fee": 999}` have the same fingerprint, because they are the same shape.
But `{"fee": 100}` and `{"fee": "100"}` have different fingerprints, because one is a number and
the other is text.

Why this matters. The Guard uses it later. During compilation we record the shapes that a normal
result had. At run time, if a bank suddenly changes its file format and a field disappears, the
shape changes, the fingerprint stops matching, and the Guard stops the plan. That is how the
system notices the world changed without anybody telling it.

I also built `structural_schema()`, which returns the readable list of paths and types *before*
it gets hashed. A hash tells you *that* something changed. The schema tells you *what* changed.
The Guard has to explain itself, so it needs the second one.

### A design decision I made today that will affect later work

**Canonical serialisation refuses floats completely.**

A float (a number with a decimal point, like `12.5`) is stored in a computer as an
approximation. Two computers, or two versions of Python, can render the same float slightly
differently. If that happens inside a hash, the hash changes for no real reason, and my
determinism claim quietly breaks.

So the rule is: no floats anywhere in canonical data.

This means:
- **Money is always a whole number of the smallest unit.** Not `317.50` rupees — `31750` paise.
  This is normal practice in payment systems, so it is a good rule anyway.
- **Exchange rates must be scaled whole numbers or text**, not floats.

This constrains the synthetic generator I build in Phase 3. I am writing it down now so I do not
rediscover it painfully later.

### Errors we hit today, and what actually caused them

**Error 1 — creating the conda environment failed halfway.**

*What broke:* `conda env create` ran for several minutes and then printed
`CondaEnvException: Pip failed`.

*The real cause:* not conda. Looking at the log, pip was downloading mypy (9.6 MB) and the
download stalled. It was a network timeout, nothing more.

*How we fixed it:* the environment itself had been created correctly with Python 3.11.9 — only
the package step failed. So we re-ran just the package install, with more retries and a longer
timeout. It worked.

*What I should have noticed sooner:* the word "failed" at the end of a long log is not the error.
The actual error was further up. **Read the whole log, not the last line.**

**Error 2 — `import-linter` said `Module 'rote.safety' does not exist`.**

*What broke:* I had written the rules that enforce the one-way dependency direction
(contracts → safety → runtime, never backwards) into `pyproject.toml`. But I had only created
the `contracts` folder. The rules pointed at folders that did not exist yet.

*The real cause:* I wrote the enforcement before creating the thing being enforced.

*How we fixed it:* created the whole approved folder layout from `ARCHITECTURE.md` §G as empty
packages. They are empty, but the boundaries are now enforced from day one, so a rule can never
be broken silently and discovered in week three.

*What I should have noticed sooner:* a rule that points at nothing does not protect anything.

**Error 3 — `mypy --strict` rejected a test.**

*What broke:* `Need type annotation for "payload"`.

*The real cause:* I wrote a dictionary containing mixed types — `None`, `True`, `-3`, `"x"`,
a list and a dictionary. Python could not work out one single type for it, and `--strict` mode
refuses to guess.

*How we fixed it:* said the type out loud — `payload: dict[str, object]`.

*What I should have noticed sooner:* `--strict` does not guess. If I mix types in a container,
I have to name the type myself. This will happen again and now I know the fix.

**Error 4 — `ruff` line too long, and formatting.**

Small. One line was 104 characters and the limit is 100. Fixed by running `ruff format`, which
rewrites the file automatically. Not really an error — the tool doing its job.

### Where we are

| Gate | Result |
|---|---|
| tests | 56 passed |
| ruff | all checks passed |
| mypy --strict | no issues in 22 source files |
| import-linter | 4 contracts kept, 0 broken |

Baseline before today was: no code, no tests, `no tests ran`.

### What is deliberately not done yet

Everything else. No ledger, no generator, no checker, no agent, no gate, no compiler.
Phase 2 is the ledger and its hash chain, and it comes next.
