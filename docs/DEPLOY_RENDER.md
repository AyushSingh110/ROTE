# Deploying Rote to Render (free tier)

Public demo deployment. **No code change is required** — the existing Dockerfile already binds
`0.0.0.0` and honours `$PORT`, which is exactly what Render provides.

> **Read this first.** On Render's free instance (0.1 CPU) the one-time plan compilation takes
> **about 9–10 minutes**, measured on this exact image. Once warm the app is fast: pages under
> 100 ms, a full resolve about 3 seconds. Free services sleep after 15 minutes of inactivity, so
> **§6 (keep it awake) is not optional** if you want a link a judge can click.
>
> **Record your demo video from the local Docker container, not from Render.** The public URL is
> for judges who want to click something afterwards.

---

## 0. Before you start

Confirm the repository is safe to build from:

```bash
git ls-files --error-unmatch .env      # must fail: .env must NOT be tracked
git log --all --oneline -- .env        # must print nothing
git status                             # clean, pushed to origin/main
```

You need: a GitHub account with this repo pushed, and your Groq API key to hand. **Never put the
key in a file, a commit, or the Dockerfile.** It goes into Render's dashboard only.

---

## 1. Create the service

1. Sign in at **<https://dashboard.render.com>** with GitHub. No credit card is required.
2. **New +** → **Web Service**.
3. **Connect a repository** → authorise Render for GitHub → pick **`Rote-runtime`**.
   If it is not listed, use *Configure account* and grant access to that repo.

## 2. Configure it

| Field | Value |
|---|---|
| **Name** | `rote` (this becomes `https://rote.onrender.com`, or `rote-xxxx` if taken) |
| **Language** | **Docker** — Render detects the Dockerfile automatically |
| **Branch** | `main` |
| **Region** | **Singapore** (closest to India; any region works) |
| **Instance Type** | **Free** |
| **Health Check Path** | **`/health`** ← **do not skip this** |

**Why the health check path matters.** During the 9-minute warmup every normal route returns
`503` with a "warming up" page, but `/health` deliberately answers `200` immediately. If you leave
this blank or point it at `/`, Render can decide the deploy failed and kill it mid-warmup. Pointing
it at `/health` lets the container finish compiling.

## 3. Environment variables

Under **Advanced** → **Add Environment Variable**, add these five:

| Key | Value | Notes |
|---|---|---|
| `ROTE_CLASSIFIER` | `llm` | use the real model |
| `ROTE_LLM_PROVIDER` | `groq` | |
| `ROTE_LLM_MODEL` | `openai/gpt-oss-120b` | |
| `ROTE_VERIFY_EVIDENCE` | `1` | evidence verification on |
| `GROQ_API_KEY` | *paste your key* | **secret — never commit it** |

If Render offers a "secret" toggle, use it for `GROQ_API_KEY`.

**Do not set `PORT`.** Render provides it (default `10000`) and the Dockerfile picks it up. Only add
it manually if step 4 shows the wrong port.

Then click **Create Web Service**.

## 4. Watch the first deploy

Two stages, roughly **4–6 min build + 9–10 min warmup**. In the **Logs** tab, expect in order:

```
==> Building...
==> Deploying...
INFO:     Uvicorn running on http://0.0.0.0:10000      ← Render's PORT was picked up
[info] warmup_started   note=compiling plans; requests are held until this finishes
...
[info] warmup_complete  seconds=550.3
```

**If the port says `7860` instead of `10000`**, add `PORT` = `10000` as an environment variable and
redeploy.

**If you see `warmup_failed error=ClassifierError`**, the Groq key is missing or wrong. `/health`
will name the variable. This is Rote failing closed on purpose — it will not fall back to the
deterministic classifier and pretend everything is fine.

## 5. Verify it publicly

Replace `<url>` with your Render URL.

```bash
curl -s https://<url>/health
```

While warming (expected for the first ~10 minutes):

```json
{"ready": false, "warming_up": true, "warmup_error": null, ...}
```

When ready — check every one of these:

```json
{"ready": true, "warming_up": false, "warmup_seconds": 550.3,
 "backlog": 500, "ledger_valid": true, "research_grade": false,
 "verify_evidence": true, "classifier": "llm",
 "classifier_model_id": "groq:openai/gpt-oss-120b"}
```

| Field | Must be | Meaning |
|---|---|---|
| `ready` | `true` | compilation finished |
| `classifier` | `llm` | a real model, not the deterministic stand-in |
| `classifier_model_id` | `groq:openai/gpt-oss-120b` | which model |
| `verify_evidence` | `true` | evidence verification is on |
| `ledger_valid` | `true` | audit chain intact |
| `research_grade` | `false` | honest labelling, keep it |

Then open the site and run the curated suite:

```bash
conda run -n rote python <scratchpad>/smoke_http.py https://<url>
```

## 6. Keep it awake

Free services spin down after **15 minutes** idle, and waking costs the full ~10 minutes again.

Set up a free external ping — **<https://cron-job.org>** or **<https://uptimerobot.com>**:

- URL: `https://<url>/health`
- Interval: **every 10 minutes**

**Budget check.** Render's free tier gives **750 instance-hours per month** and a month is about
**744 hours**, so one continuously-awake service just fits — with almost no margin. Do not run a
second free web service on this account while the demo needs to stay up.

Start the pinger the day before judging, not weeks before.

## 7. Redeploying

Push to `main` and Render rebuilds automatically. **Every redeploy costs another ~10 minutes of
warmup**, so do not push during a judging window.

To deploy without a code change: **Manual Deploy** → *Clear build cache & deploy*.

---

## What to do when things go wrong

| Symptom | Cause | Fix |
|---|---|---|
| Blank / "warming up" page | cold start, 9–10 min | wait, or check `/health`; the pinger prevents it |
| Deploy killed mid-warmup | Health Check Path not `/health` | set it, redeploy |
| `warmup_failed ClassifierError` | Groq key missing or invalid | fix the env var; `/health` names it |
| Everything escalates with `classifier_unavailable` | Groq unreachable or rate-limited | expected fail-closed behaviour; wait a minute |
| Bound to the wrong port | image `ENV PORT` won | set `PORT=10000` explicitly |
| Service suspended | 750 monthly hours exhausted | stop the pinger, or wait for the reset |

### Known limitations of this deployment

- **9–10 minute cold start** on 0.1 CPU. Measured, not estimated.
- **Sleeps after 15 minutes** idle without a pinger.
- **No persistent storage** — the world and ledger are in memory. A restart resets the demo, which
  is correct for a synthetic prototype.
- **`/api/reset` is public.** Any visitor can reset demo state. Harmless here (synthetic data, no
  real money) but worth knowing before you show the URL to a room.
- **Groq rate limit** on the demo key is roughly nine model calls per minute.
- **Still `research_grade: false`.** A public URL does not make it production infrastructure.

## The guaranteed fallback

The local Docker container is fully validated (49/49 curated checks, real Groq, verification on).
It cannot sleep, rate-limit or cold-start during a recording:

```bat
docker build -t rote:demo .
docker run --rm -p 7860:7860 ^
  -e ROTE_CLASSIFIER=llm -e ROTE_LLM_PROVIDER=groq ^
  -e ROTE_LLM_MODEL=openai/gpt-oss-120b -e ROTE_VERIFY_EVIDENCE=1 ^
  -e GROQ_API_KEY=%GROQ_API_KEY% rote:demo
```

Warmup is about **48 seconds** locally. Then open <http://localhost:7860/>.

**Demo cases:** `EXC-000004` automates · `EXC-000000` refuses as ambiguous · corrupt `EXC-000011`
for an evidence mismatch.
