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
`SENTINELOPS_REPLAY_PAUSE_SECONDS` for metered tiers, and the adapter retries
429/503 twice with short delays.

On the 7B model the prompt change moved one scenario from wrong to right and
none the other way. Its config-mismatch answer, "DNS resolution failure for
DB_HOST", passes the keyword grader by naming the variable and is still half
wrong: it calls a symptom the cause. The grader counts it; the reader should
not.

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
proves it: with nothing changed but `SENTINELOPS_LLM_PROVIDER`, a current
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

SENTINELOPS_LLM_PROVIDER=ollama \
SENTINELOPS_OLLAMA_MODEL=qwen2.5:7b \
SENTINELOPS_LLM_TIMEOUT_SECONDS=300 \
python -m app.replay scenarios/hard
```

Swap `scenarios/hard` for the default directory to run the easy set. For the
cloud run:

```bash
SENTINELOPS_LLM_PROVIDER=openai \
SENTINELOPS_OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/ \
SENTINELOPS_OPENAI_MODEL=gemini-3.6-flash \
SENTINELOPS_OPENAI_API_KEY=... \
python -m app.replay scenarios/hard
```

Note on the free tier: Google states that free-tier data may be used to improve
its products. Fine for a benchmark on synthetic scenarios; not a production
setting for real logs. The 2026-09-13 run was executed inside the cluster so
the key never left it.
