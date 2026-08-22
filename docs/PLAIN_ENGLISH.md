# Rote, explained in simple English

This file explains the whole project in short, easy sentences.
No difficult words. No assumed knowledge of Python or of payments.
If you can read this file, you can explain the project to anyone.

---

## Part 1 — The problem

### 1.1 What a payment company does every night

A payment company moves money. A customer pays. The money goes to a bank.
Later the bank sends back a file. The file is a list. Each line says:
*"On this day, this much money moved."*

The company also keeps its own list of the same payments.

Every night the company compares the two lists.
This job is called **reconciliation**. It means: *make the two lists agree.*

Most lines match easily. Line 1 in our list equals line 1 in the bank list. Good.
A computer program does this matching. It is fast and it never gets tired.

### 1.2 The lines that do not match

But some lines do not match. Maybe:

- The bank wrote the date in a different style.
- Somebody typed the reference number wrong. `4417` became `4471`.
- The customer paid only part of the amount, and wrote a note saying why.
- The bank took a small fee, and did not say what the fee was for.
- The payment happened on Friday but the bank recorded it on Monday.

A line that does not match is called an **exception**.
Think of it as a problem line. It needs a person to look at it.

In a good company, the computer matches about 85 to 95 lines out of 100.
The rest — 5 to 15 out of every 100 — become exceptions.

### 1.3 Why humans do this work

A human sits down. They open one exception. They look at it. They think:
*"Ah, this is a fee problem."* Then they fix it.

They fix it by doing a few steps, always the same steps:
look up the order, look up the fee table, calculate the difference, post a correction.

One difficult exception can take **two or three hours**.
A big company has thousands of exceptions every day.
So companies employ many people to do only this.

**Important:** this is not the main money system.
The main money system — taking the payment, sending it to the bank — is
normal computer code. It is exact. It never guesses. Nobody wants to change that,
and this project does **not** try to change it.

This project is only about the problem lines. The leftovers. The residue.

---

## Part 2 — Why the obvious solution does not work

### 2.1 The obvious solution

Today we have AI models. They are good at reading messy text.
So the obvious idea is: *let an AI agent read the exception and fix it.*

Companies are trying this now. And it partly works. The AI is quite good at it.

### 2.2 Why it gets stuck

But the AI never gets permission to act alone. It stays in "suggestion mode".
It suggests, and a human still checks every single one.
So nobody saves any time. Three reasons:

**Reason 1 — It is not consistent.**
Give the AI the same problem on Monday and on Tuesday.
It may fix it in two different ways. Both may be correct. But they are different.
In a company that handles money, that is a serious problem.
An auditor will find it and ask why.

**Reason 2 — It cannot explain itself.**
Somebody asks: *"Why did you move 317 rupees and 50 paise?"*
The answer *"the model decided"* is not an acceptable answer.
Not to the manager. Not to the auditor. Not to the merchant.

**Reason 3 — It has no fixed limit.**
Nothing stops the AI from doing something outside its job.
It reads text written by strangers. Someone can write tricky text to fool it.
Because nothing stops it, nobody is willing to let it act alone.

So the human still opens every ticket. The saving never arrives.

---

## Part 3 — The idea of Rote

### 3.1 The key observation

Look carefully at what a human does with one exception. There are two parts:

**Part A — deciding what kind of problem it is.**
This is hard. You must read messy text. You need judgement.
Only a human, or an AI, can do this well.

**Part B — fixing it, once you know what kind it is.**
This is **not** hard. It is the same few steps every single time.
Once you know it is a fee problem, the repair is boring and mechanical.

### 3.2 The idea, in one sentence

> **Let the AI do Part A. Let ordinary computer code do Part B.**

The AI only says: *"this is a fee problem."*
Nothing more. It does not touch money. It does not choose actions.

Then a small program takes over. That program has no AI inside it.
It does the same steps every time. It cannot be tricked, because
there is nothing inside it to trick — it is just plain code.

### 3.3 Where the program comes from

Here is the interesting part. Nobody writes that program by hand.

First, we let the AI agent work normally for a while. We watch it.
We write down everything it does. Every step. Every tool it used.
Every number it passed. We call one such recording a **trajectory**.
It is like a video recording of the work.

Then we look at, say, 300 recordings of fee problems.
We ask: *did the AI do the same steps every time?*

If yes, we can turn those recordings into a fixed program.
That is called **compiling a plan**.

Now the next fee problem does not need the AI at all — after the first step.
The AI says "fee problem". The fixed program does the rest.

### 3.4 Learning what "normal" looks like

While we study the 300 recordings, we also learn what normal results look like.

For example, we might learn:
*"At step 3, the answer always contains a field called `fee_amount`.
That number was always between 5 and 400 rupees.
The currency was always INR or USD."*

We save this. We call it an **expectation**. It means "this is what normal looks like".

Later, when the fixed program runs, we check every step against the expectation.
If the answer looks wrong — a missing field, a strange number, a new currency —
we stop the program immediately and give the job back to the AI agent,
or to a human. The part that does this checking is called the **guard**.

This matters because the world changes. The bank may change its file format.
When that happens, the fixed program must not carry on blindly.
It must notice, and stop.

### 3.5 The gate — the rule that cannot be broken

Before any money moves, the request must pass through one door.
We call it the **policy gate**.

The gate does not think. It does not use AI. It checks fixed rules:

- Is this action allowed for this kind of problem? (A fee plan may correct a fee.
  It may never send a refund.)
- Is the amount below the limit?
- Have we already done this exact thing before? (So we never pay twice.)
- Are we in test mode?

The important part: **both paths go through the same gate**.
The fixed program goes through it. The AI agent goes through it.
Neither one can go around it. Not because we promised — because the code
is arranged so that only the gate can reach the money-moving tools.
Nothing else can even find them.

### 3.6 The ledger — the receipt book

Everything that happens gets written into a list that only grows.
You can add to it. You can never change or delete anything in it.

Each new line contains a fingerprint of the line before it.
So if somebody edits an old line, all the fingerprints after it stop matching.
We can then run one command and it tells us exactly which line was changed.

This is not a blockchain. It is much simpler. It is a chain of fingerprints.

Because of this list, we can answer, for any old case:
*"Why was this correction made?"*
The answer is in the list. Nobody has to read code to find it.

---

## Part 4 — The whole thing, as a picture in words

A problem line arrives.

1. **The boundary** — we open it, we check it is well-formed, and we split it into
   two piles: normal fields (dates, amounts, IDs) and free text written by a stranger.
   We treat the stranger's text as dangerous.
2. **The classifier** — an AI reads it and says only one thing: which kind of problem.
   It has no tools. It cannot do anything. It can only pick a label from a fixed list.
3. **The router** — plain code. It first checks: *do the normal fields agree with that
   label?* If the AI says "fee problem" but the numbers do not look like a fee problem,
   we do not trust the label, and we escalate. If they agree, it looks for a fixed program
   for that label.
4. **If a fixed program exists** — run it. Step by step. No AI. The **guard** checks
   every step against what normal looks like.
5. **If no fixed program exists, or the guard is unhappy** — hand the job to the
   AI agent, which works the old, slow, flexible way.
6. **Either way** — every action passes the **gate** before it happens.
7. **Everything** is written into the **ledger**.
8. **Later, offline** — successful runs are studied, and new fixed programs are made.
   This learning happens at night. It never touches a live request.

---

## Part 5 — Is this idea genuine? My honest assessment

You asked me to be direct. Here is my full opinion.

### 5.1 Is the problem real?

**Yes. Clearly yes.**

Reconciliation exception handling is a real job done by real teams at real cost.
Match rates in the 75–95% range are widely reported. The leftover is worked by hand.
This is not an invented problem.

The framing in your draft — *core money movement is deterministic and stays that way,
we target the human layer wrapped around it* — is correct, and it is the strongest
part of your document. Most AI-in-fintech pitches get this wrong and lose credibility
in the first minute. Yours does not. Do not change it.

### 5.2 Is the core claim true?

**Mostly yes, with one honest limit.**

The claim is: within one exception, deciding *what kind* needs judgement,
but the repair after that does not.

For the common categories — fee differences, FX rounding, timing gaps,
transposed digits, duplicates — this is true. The repair really is mechanical.

For the genuinely strange cases, it is not true, and no design can make it true.
But your architecture already says that. Humans keep the tail. The system is built
to *notice* when it is out of its depth and hand back. That is the correct answer,
and it is honest.

**However**, be ready for this: the claim is true for the categories you chose.
You chose those categories partly because they are compilable. That is fine —
it is what a sensible engineer does — but say it out loud before somebody says it
to you.

### 5.3 Is it new?

**The individual ideas are not new. The combination, in this place, is unusual.**

- Learning a program by watching demonstrations — old, well-studied idea.
- Caching or compiling agent behaviour — active research area right now.
- A policy gate at the tool boundary — standard security practice.
- An append-only tamper-evident log — standard audit practice.

Your draft already handles this correctly and honestly. It says the loop exists in
research and describes your slice as: inducing it from an agent's own logs,
for tool-calling agents, with a divergence guard and a policy gate.
That is accurate. Keep saying exactly that. Never say "first of its kind".

**The genuinely fresh part is the framing, not the algorithm.**
Most people building this treat it as a *cost* problem — "make the agent cheaper".
You treat it as a *permission* problem — "how does an agent earn the right to act
alone?" That reframing is the strongest idea in the project, and it is the one that
will interest people who actually run operations teams. Lead with it.

### 5.4 The three weaknesses I would attack if I were on the panel

Be ready for these three. They are the real ones.

**Weakness 1 — "You only rediscovered your own data generator."**
You write the generator. You write the checker. You prompt the agent.
Then you find structure. Is it real structure, or is it your own structure
coming back to you?

*Your answer:* the generator only knows the correct final state. It does not know
the steps. The agent gets extra tools it does not need, so its choices are real
choices. And most importantly — compile the plan twice, from two different AI models.
If both produce the same steps, the steps belong to the task and not to the model.
That experiment is cheap and it settles the question. It is in the plan as §I.8.

**Weakness 2 — "Maybe the steps are never stable, so there is nothing to compile."**
Exceptions are irregular by nature. That is what makes them exceptions.
So maybe every case is different and no fixed program can be made at all.

*Your answer:* the design measures this on **day 4**, before building the executor.
It counts, for each category, how many recordings used the identical step sequence.
That single number decides whether to continue, to narrow the categories, or to
report non-compilability as the finding.

And note: if some categories do not compile, that is a **result**, not a failure.
"Three of six categories compile cleanly, two partly, one not at all" is a credible
engineering answer. A demo that hides which categories failed is not.

**Weakness 3 — "You proved a mechanism, not a business case."**
Synthetic data cannot prove money saved.

*Your answer:* do not claim it. Claim the mechanism. The strongest single number
in the whole project is this one: run the same exception 20 times through the
compiled path, and get **exactly one** distinct outcome every time — while the
AI agent gives a spread. That is fully demonstrated by the build, needs no real
data, and is exactly the property that unblocks the permission conversation.

### 5.5 My verdict

**The idea is genuine. Build it.**

Three specific reasons:

1. **The problem framing is honest and correct.** That alone puts it ahead of most
   projects of this type. Honesty about what you are *not* doing is credibility.
2. **The central claim is testable in 12 days**, and it fails loudly if it is wrong.
   That is the mark of a real engineering project rather than a demo.
3. **Its best evidence — perfect consistency — costs almost nothing to produce.**
   It needs no real dataset, no partner company, no expensive model. Run it 20 times
   and count. Very few 15-day projects have a headline result that is that cheap and
   that hard to argue with.

**One caution.** The biggest danger to this project is not that the idea is wrong.
It is that you build too much. Two domains, a compiler, a runtime, a guard, a gate,
a ledger, an evaluation harness and a web page in 12 days is a lot for one person.
The build plan therefore ends every day with a number, and it fixes the cut order in
advance, so that if you fall behind you already know what to drop. Follow it.

---

## Part 6 — Words used in this project

| Word | Simple meaning |
|---|---|
| **reconciliation** | comparing two lists of payments to make them agree |
| **exception** | one line that did not match, and needs attention |
| **category** | which *kind* of problem it is (fee, timing, typo, and so on) |
| **agent** | an AI that can use tools, in a loop, until the job is done |
| **trajectory** | a full recording of one agent run — every step it took |
| **plan** | a fixed program, built from many recordings, with no AI inside |
| **compile** | to turn many recordings into one fixed program |
| **skeleton** | the steps that appeared in *every* recording, in the same order |
| **binding** | where one input value came from — a constant, the task, or an earlier step |
| **expectation** | what a normal result looked like, learned from recordings |
| **guard** | the check that compares a real result against the expectation |
| **divergence** | reality no longer matches the expectation, so stop |
| **policy gate** | the one door every money action must pass through |
| **idempotency key** | a name for an action, so doing it twice only counts once |
| **ledger** | the append-only receipt book, protected by a chain of fingerprints |
| **replay** | running an old case again from the recording, to check it comes out the same |
| **prompt injection** | a stranger writing tricky text to fool the AI |
| **shadow mode** | the plan runs alongside the AI but is not allowed to act, so it can prove itself first |
| **escalate** | give up safely and hand the job to a human |
