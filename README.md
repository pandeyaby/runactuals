# RunActuals

**Claims versus actuals, for every agent run.**

RunActuals is an open-source, self-hosted flight recorder for AI agents. It takes
OpenTelemetry traces and stops trusting what the agent says about itself. For every
run it answers four questions:

| | Question | How RunActuals answers it |
|---|---|---|
| 1 | **Did the run really succeed?** | Compares the claimed status with evidence from the outside world (a commit on the branch, a green CI run). It catches *false failures*, where a run was marked failed but actually shipped, and *false successes*, where a run says it's done but the evidence says otherwise. |
| 2 | **What did each result cost?** | Reports cost per **verified** outcome, not tokens per call. That's model cost plus recorded spend, divided by outcomes that were actually verified. |
| 3 | **Is it stuck or drifting?** | Flags repeated identical tool calls, retry storms, tool storms, and quality that keeps dropping across scheduled runs. |
| 4 | **What did it touch?** | Keeps an audit timeline of approvals, sends, writes, posts, deletes, and spend. Any sensitive action with no approval reference gets flagged. |

It speaks OTLP/HTTP (protobuf or JSON, gzip OK) and follows the
[OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai).
It can also send the same stream on to DataCat, Grafana, or any other OTLP backend.
RunActuals is meant to sit next to your APM, not replace it.

> Status: v0.1 pre-alpha. The Python package has no runtime dependencies (stdlib only).

## Why

Here's the case that started it. A scheduled lesson-building agent pushes a lesson
commit, then waits about 20 minutes for an end-to-end CI job. The scheduler times the
run out and marks it **failed/interrupted**, even though the commit already landed and
CI went green. Trace tools show a red span. RunActuals shows a **FALSE FAILURE** with
the commit SHA and the CI run ID as proof.

## Quickstart (5 minutes)

```bash
git clone https://github.com/pandeyaby/runactuals && cd runactuals
docker compose up --build
open http://localhost:4318/      # dashboard, pre-seeded with synthetic demo runs
```

Without Docker (Python 3.11+):

```bash
pip install .
runactuals --db demo.db demo     # load the synthetic lesson-builder runs and print the report
runactuals --db demo.db serve    # dashboard + OTLP receiver on http://127.0.0.1:4318
```

The demo output looks like this:

```
run                                    claimed      verified   verdict            cost  warnings
Build lesson: Thunder and Lightning    succeeded    succeeded  consistent     $ 0.0270
Build lesson: K-Pg Extinction          interrupted  succeeded  false_failure  $ 0.0270  repeated_tool_call
Build lesson: Mixed Numbers            failed       succeeded  false_failure  $ 0.0270
Build lesson: Telescopes               succeeded    failed     false_success  $ 0.0270
Build lesson: Tectonic Plates          failed       failed     consistent     $ 0.0370  repeated_tool_call, retry_storm
Build lesson: Area of Compound Shapes  succeeded    succeeded  consistent     $ 0.0270  quality_drift

runs=6 claimed_successes=3 verified_outcomes=4 false_failures=2 false_successes=1
total=$0.1720 cost_per_verified_outcome=$0.0430 (naive per claimed success $0.0573)
```

The demo data is synthetic (fake SHAs and CI run IDs, plus a fixture GitHub API), but
it runs through the real path: SDK → OTLP/HTTP → recorder → SQLite → evidence adapter
→ checks → dashboard.

## Instrument an agent

### Python SDK (zero dependencies)

```python
from runactuals.sdk import Recorder

rec = Recorder("http://localhost:4318", service_name="lesson-builder")
with rec.run("lesson-builder", title="Build lesson: Telescopes") as run:
    run.llm("grok-4", input_tokens=9000, output_tokens=3500, cost_usd=0.0089)
    run.tool("git_push", {"branch": "visual-draft"})
    run.claim("git_commit", branch="visual-draft", revision=sha)      # optional: adapters can find it anyway
    run.audit("write", "github:org/repo@visual-draft", approval_ref="standing rule: drafts only")
    run.audit("spend", "Guide eval", approval_ref="under 5c a run", spend_usd=0.015)
    run.quality(27 / 27)
# A normal exit ends the run as "succeeded". An exception ends it as "failed";
# KeyboardInterrupt/SystemExit end it as "interrupted". Or call run.end(status, message).
```

### Any OpenTelemetry SDK

Point your OTLP/HTTP exporter at `http://localhost:4318/v1/traces`. One trace is one
run. The root span is the agent invocation. RunActuals reads these attributes:

| Attribute | Where | Meaning |
|---|---|---|
| `gen_ai.operation.name` | all | `invoke_agent` (root), `execute_tool`, `chat` |
| `gen_ai.agent.name` | root | agent name, which is matched against config |
| `gen_ai.tool.name`, `gen_ai.tool.call.arguments` | tool spans | loop detection |
| `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | chat spans | usage |
| `runactuals.run.claimed_status` | root | `succeeded` / `failed` / `interrupted` (falls back to span status) |
| `runactuals.cost.usd` | any | model cost |
| `runactuals.quality.score` | root | 0..1, used for drift |
| `runactuals.retry` | tool spans | marks a retry |
| `runactuals.claim.kind`, `vcs.ref.head.name`, `vcs.ref.head.revision` | claim spans | what the agent says it produced |
| `runactuals.audit.action`, `.target`, `.actor`, `.approval_ref`, `runactuals.spend.usd` | audit spans | what it touched |

Every `gen_ai.*` attribute upstream is still marked *Development*, so names may change.
See `src/runactuals/conventions.py`.

## Evidence adapters

Adapters are configured per agent in TOML. See
[`examples/lesson-builder.toml`](examples/lesson-builder.toml):

```toml
[[agents]]
name = "lesson-builder"

[agents.evidence]
adapter = "github_commit_ci"
repo = "pandeyaby/why-next"
branch = "visual-draft"
message_regex = "^lesson: "
workflows = ["CI"]
grace_minutes = 45
```

**`github_commit_ci`** verifies that a commit landed on `branch` and that its GitHub
Actions runs finished green. If the run claimed a SHA, the adapter checks that the SHA
is on the branch. If the run didn't claim one (the usual case when a run gets killed),
it looks for a matching commit inside the run's time window plus `grace_minutes`. It
reads `GITHUB_TOKEN` from the environment for private repos. Results:

- `verified`: the commit is on the branch and CI is green.
- `refuted`: no commit was found after the grace window, the commit isn't on the branch, or CI failed.
- `pending`: CI is still running, or the window is still open.
- `unknown`: the API call failed.

```bash
runactuals --db runactuals.db verify --config examples/lesson-builder.toml
runactuals --db runactuals.db serve --config examples/lesson-builder.toml   # re-checks every 5 minutes
```

## Fan-out to DataCat / Grafana

```bash
runactuals serve --forward http://datacat:4318/v1/traces --forward http://alloy:4318/v1/traces
```

Each request body is forwarded as-is, with the same content type and encoding.

## API

- `POST /v1/traces`: OTLP/HTTP. Set `RUNACTUALS_INGEST_TOKEN` to require `Authorization: Bearer …`.
- `GET /`: dashboard. `GET /runs/<trace_id>`: one run.
- `GET /api/report`: the four checks as JSON. `GET /healthz`.

## Development

```bash
cd tests && PYTHONPATH=../src python -m unittest -v
```

The TypeScript SDK in `sdk-ts/` is a stub for now.

## Roadmap

- More evidence adapters: Vercel deployments, email/Slack send receipts, card spend, files on disk.
- Drift and false-failure alerts, sent out as OTLP metrics or webhooks.
- TypeScript SDK, plus auto-instrumentation for common agent frameworks.
- Optional Postgres storage.

## License

Apache-2.0
