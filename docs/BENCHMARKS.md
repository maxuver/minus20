# Benchmarks

Measured, reproducible, and reported whether or not the number flatters the
project. A tool that tells you what broke at 3 AM has to be honest about how
often it is wrong.

Last runs: 2026-09-09 and 2026-09-14 with `qwen2.5:7b` via Ollama, CPU only
(no GPU); 2026-09-13/14 with `gemini-3.6-flash` through the OpenAI-compatible
adapter (free tier). Cost $0.00 per alert in both.

## Method

`python -m app.replay <dir>` replays recorded scenarios through the real
pipeline. Each scenario carries the alert plus the exact context a live cluster
would have produced, so a run is deterministic and needs no cluster.

Grading is by declared keywords, checked **against the stated root cause only**.
Not an LLM judge, so the score is reproducible and anyone can see what counted.

Grading deliberately ignores the evidence list. An earlier version accepted a
keyword appearing anywhere in the hypothesis, and it scored a pass for an answer
whose root cause was the misleading one the scenario was built to punish — the
right word merely appeared in a cited log line. That inflated the hard-set score
from 2/5 to 3/5. The engineer acts on the cause that is stated; if that is wrong,
they go the wrong way regardless of what the evidence contains.

## Results

| Set | Scenarios | qwen2.5:7b, CPU | gemini-3.6-flash, API |
|---|---|---|---|
| Easy — signal stated plainly in the context | 6 | **6/6**, 31.5 s avg | **6/6**, 7.2 s avg |
| Hard — the obvious signal points the wrong way | 5 | **2/5**, 30.3 s avg | **5/5**, 7.6 s avg |

Same pipeline, same prompt, same scenarios, one environment variable changed.

### 2026-09-14: the prompt asks for elimination first, and two scenarios are added

The system prompt now spells out a method: name the obvious explanation,
look for what contradicts it, discard it if contradicted; a symptom is not a
cause when the thing it points at is shown healthy; when two things mismatch,
suspect the one that changed. Two scenarios were added to the hard set:
pod IP exhaustion (AWS VPC CNI, node looks healthy) and a config mismatch
where both a wrong env var and a NetworkPolicy are visible and the error
message names the wrong one.

| Set | Scenarios | qwen2.5:7b, CPU (prompt v1) | qwen2.5:7b, CPU (prompt v2) | gemini-3.6-flash |
|---|---|---|---|---|
| Hard, original five | 5 | 2/5 | **3/5** (missing Secret now found) | 5/5 |
| Hard, all seven | 7 | | **5/7**, 46 s avg | 6 of 7 distinct passes across two runs; the seventh never served |

The Gemini column needs a caveat: the free tier answered 503 "high demand"
and then 429 rate limits for most of the afternoon, so the seven-scenario
run never completed in one pass. Across the two partial runs every scenario
it did answer was correct (missing Secret, NetworkPolicy, rollout, sidecar,
unrotated log, IP exhaustion); the config-mismatch scenario was never served.
Reported as such rather than as 7/7. The replay harness gained
`MINUS20_REPLAY_PAUSE_SECONDS` for metered tiers, and the adapter retries
429/503 twice with short delays.

On the 7B model the prompt change moved one scenario from wrong to right and
none the other way. Its config-mismatch answer, "DNS resolution failure for
DB_HOST", passes the keyword grader by naming the variable and is still half
wrong: it calls a symptom the cause. The grader counts it; the reader should
not.

### 2026-09-17: three AWS-shaped scenarios (hard set = 10)

Where the comparison with AWS DevOps Agent will be made is on EKS, so the
hard set gained three faults that only exist there, each with a plausible
wrong answer sitting in plain sight:

| Scenario | The bait | The cause | qwen2.5:7b, CPU | cloud model |
|---|---|---|---|---|
| Spot interruption storm | every app log says "lost connection to Redis"; Redis is healthy | the Spot node was reclaimed, Karpenter drained it, 30 pods restarted | ❌ "Redis connection issues", 69 s | pending (free-tier quota) |
| Pod Identity denied | `AccessDenied` on S3 right after a deploy | the deploy renamed the ServiceAccount; the Pod Identity association points at the old name, the pod runs as the node role | ❌ "S3 PutObject permission denied", 73 s | pending |
| EBS volume stuck | `Multi-Attach error`, looks like a storage bug | the managed node group replaced the node; the volume is still attached to the terminated instance's stale VolumeAttachment | ✅ "VolumeInUse by terminated instance", 87 s | pending |

Local model on the AWS three: **1/3**, and the two misses are the same
failure mode as before: the symptom in the application log is named as the
cause even though the context shows the named component healthy (Redis
answering, the policy unchanged and a different role in the error). Hard set
overall on `qwen2.5:7b`: 6/10. The cloud column is filled in when a key with
a usable quota is available; the scenarios are in `scenarios/hard/`.

### Hard set, case by case

| Scenario | qwen2.5:7b said | Verdict | gemini-3.6-flash said | Verdict |
|---|---|---|---|---|
| OOM caused by a sidecar | "Memory pressure due to log-shipper container consuming excessive memory" | ✅ | "The log-shipper sidecar container in pod checkout-api…" | ✅ |
| Volume full from an unrotated log | "Log files are filling up the PersistentVolume" | ✅ | "An unrotated application log file (/var/lib/postgresql/data/…)" | ✅ |
| CrashLoop from a missing Secret | "failed database connection attempts" | ❌ took the bait | "The pod 'orders-api-…' is failing to start because [the Secret]…" | ✅ |
| DNS failing because of a NetworkPolicy | "DNS resolution failure" | ❌ blamed DNS | "The newly applied NetworkPolicy 'demo-default-deny'…" | ✅ |
| 5xx caused by a rollout | "Database timeout causing high 5xx error rate" | ❌ blamed the database | "The deployment of payments-api version v2.4.0 (replicaset …)" | ✅ |

## What the pattern says

The two passes and the three failures split cleanly:

- **It succeeds when the answer is present in the context as text.** `log-shipper`
  appears in the metrics; `app.log is 8.4GiB` appears in the logs. The model
  reads it and names it.
- **It fails when the answer requires reasoning by elimination.** "The database
  is answering normally, *therefore* the database is not the cause." "CoreDNS is
  healthy, *therefore* something else is blocking resolution." In all three
  failures the disproving evidence was in the context and was not used.

This was a limit of a 7B model, not of the pipeline, and the second run
proves it: with nothing changed but `MINUS20_LLM_PROVIDER`, a current
cloud model gets all five, in a quarter of the time. The three cases the 7B
model failed were exactly the ones needing elimination ("the database answers
normally, so it is not the database"), and the larger model does that
reasoning unprompted.

What that means in practice: the local, zero-egress backend is the right
default for privacy and cost, and it handles the plainly-stated majority; for
misleading incidents a cloud model is measurably better, and switching is one
value. Prompt work for the small model ("rule out the obvious first") is still
worth doing, and is the next item.

## How to read these numbers

- Real incidents are mostly the easy kind: the signal is there and the cost is
  the twenty minutes of assembling it. That is what this automates.
- On genuinely misleading incidents it is right about half the time, so it is an
  assistant, not an oracle. That is why every hypothesis ships with its evidence
  and the cheapest way to disprove it: checking a wrong answer takes seconds.
- Nothing here has been measured against real production incidents. These
  scenarios were authored for this benchmark, and an author writing their own
  exam is a real limitation.

## Reproduce

```bash
cd services/analyzer-worker
pip install -r requirements-dev.txt

MINUS20_LLM_PROVIDER=ollama \
MINUS20_OLLAMA_MODEL=qwen2.5:7b \
MINUS20_LLM_TIMEOUT_SECONDS=300 \
python -m app.replay scenarios/hard
```

Swap `scenarios/hard` for the default directory to run the easy set. For the
cloud run:

```bash
MINUS20_LLM_PROVIDER=openai \
MINUS20_OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/ \
MINUS20_OPENAI_MODEL=gemini-3.6-flash \
MINUS20_OPENAI_API_KEY=... \
python -m app.replay scenarios/hard
```

Any OpenAI-compatible endpoint works the same way: `https://api.mistral.ai/v1`
(EU, free Experiment tier), `https://api.deepseek.com` (the adapter's default),
Groq, vLLM. Only the base URL, the model name and the key change.

Note on the free tier: Google states that free-tier data may be used to improve
its products. Fine for a benchmark on synthetic scenarios; not a production
setting for real logs. The 2026-09-13 run was executed inside the cluster so
the key never left it.

## Growing the set from real incidents

"An author writing their own exam" is the limitation above. The way past it
is the engineer's verdict: `/ok <id>` and `/wrong <id> <real cause>` in the
bot record what actually happened, and every such incident already holds the
redacted context the model saw. So the store is an eval set:

```bash
MINUS20_POSTGRES_DSN=postgres://... \
python -m app.replay --from-store --days 90
```

replays every incident with a verdict through the current model and grades
the new hypothesis against the verdict (`/ok` makes the recorded hypothesis
the expectation, `/wrong` makes the engineer's cause the expectation). Add
`--export scenarios/from-verdicts` to write them out as scenario files, the
same shape as `scenarios/hard/`, for the ones worth committing. No fixture is
written by hand, and the set grows one verdict at a time. Incidents without a
verdict are history, not a test, and are skipped.

### 2026-09-17: first run from the store

Three incidents on the kind cluster carried a verdict (two `/ok`, one
`/wrong`), replayed against `qwen2.5:7b` on CPU:

| Incident | Context recorded at the time | Verdict | Replay | Grade |
|---|---|---|---|---|
| `9e95b7f1` billing-api crash loop, 2026-09-14 18:21 | events only (238 chars, before pod logs were collected by default) | wrong: `postgres:5432 unreachable` | "Image pull failure" | FAIL |
| `f77f5269` billing-api crash loop, 19:13 | events + logs, the log names `postgres:5432` | ok | "Postgres database is unreachable or misconfigured" | PASS |
| `85509212` orders-1 storm leader, 21:38 | events + logs, `FATAL secret db-credentials not found` | ok | "Secret 'db-credentials' not found in the pod" | PASS |

2/3, average 36 s per hypothesis. Two things the first run exposed:

- The FAIL is a **collector gap, not a model gap**: with no log line in the
  context, "image pull" is a reasonable guess for a busybox pod restarting.
  The same alert with pod logs collected (the next row) was answered right.
  Pod logs are collected by default since 2026-09-15.
- The first pass scored 3/3, and one PASS was false. The `/wrong` text was
  "postgres:5432 unreachable, no secret involved", the grader extracted
  `secret` from it, and a repeat of the wrong "Secret not found" hypothesis
  matched. Keywords now come from the first clause only (name the cause
  first, commentary after a semicolon) and words every hypothesis contains
  (`error`, `pod`, `container`, `log`) never count. The corrected run is the
  table above.

One of the three verdicts was also entered wrongly by the author and
corrected before this table was written: an eval set built from verdicts is
only as good as the engineer's attention when writing them.
