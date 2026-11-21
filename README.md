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

GitHub Actions runs only on the pushed head, not on every commit in a push. So the
CI verdict comes from the newest commit in the run's window, at or after the matched
commit, that has a workflow run. That's the run's final state. Set `auth = "gh"` to
use the GitHub CLI's login instead of a `GITHUB_TOKEN`.

```bash
runactuals --db runactuals.db verify --config examples/lesson-builder.toml
runactuals --db runactuals.db serve --config examples/lesson-builder.toml   # re-checks every 5 minutes
```

## Back-test on real history: `runactuals replay`

Before you instrument an agent, replay its past runs from a case file. Each case
gives a start time and the status the scheduler reported. The evidence adapter then
checks the real repository, read-only. See
[`examples/why-next-replay.toml`](examples/why-next-replay.toml): it covers 18 real
Why Next? lesson-builder runs from Nov 6–17, 2026. Eleven were marked failed or
interrupted, and seven were marked succeeded as controls.

```bash
runactuals --db replay.db replay examples/why-next-replay.toml
```

Result ([`examples/why-next-replay.out.txt`](examples/why-next-replay.out.txt)):
- All 11 runs marked failed were flagged as false failures, each with its lesson
  commits and the green `validate` run.
- All 7 runs marked succeeded stayed consistent.
- For all 14 cases with ground truth, the pushed head matched the green tip that the
  builder's own later report named.

## Fan-out to DataCat / Grafana

```bash
runactuals serve --forward http://datacat:4318/v1/traces --forward http://alloy:4318/v1/traces
```

Each request body is forwarded as-is, with the same content type and encoding.


## Record every LLM and MCP call, without per-bot hooks

Point your SDKs at a recording proxy instead of instrumenting each bot. The
proxy forwards the request to the real provider, then writes a span to the
same store the dashboard already uses: model (or MCP tool), latency, token
counts, and cost from a price table. API keys are forwarded upstream and
stripped from everything that is stored.

```bash
# terminal 1: dashboard + OTLP collector
runactuals --db runactuals.db serve --proxy-port 8787

# or run the proxy as its own process (ships spans to the collector by default)
runactuals proxy --port 8787
```

Then aim the client at the proxy, not the provider:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/openai/v1
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic
# Google GenAI (Gemini API):
#   client = genai.Client(http_options={"base_url": "http://127.0.0.1:8787/gemini"})
```

Paths the proxy accepts:

| Prefix | Default upstream | Typical client base URL |
|---|---|---|
| `/openai/...` | `https://api.openai.com` | `http://127.0.0.1:8787/openai/v1` |
| `/anthropic/...` | `https://api.anthropic.com` | `http://127.0.0.1:8787/anthropic` |
| `/gemini/...` | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:8787/gemini` |
| `/mcp/...` | none (set `--mcp-upstream`) | your MCP streamable-HTTP client |
| `/v1/chat/completions`, `/v1/embeddings`, `/v1/responses` | OpenAI upstream | `http://127.0.0.1:8787/v1` |
| `/v1/messages` | Anthropic upstream | |

Override an upstream with `--openai-upstream`, `--anthropic-upstream`,
`--gemini-upstream`, or `--mcp-upstream` (OpenAI-compatible hosts such as a
local vLLM work the same way). The proxy only talks to those configured
origins; it is not an open proxy.

Each call becomes a `chat` or `embeddings` span (`execute_tool` for MCP
`tools/call`) with:

- `gen_ai.request.model` / `gen_ai.tool.name`
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` when the provider reports them
- `runactuals.cost.usd` from the [price table](src/runactuals/prices.py) (USD per 1M tokens; unknown models leave cost unset)
- latency, HTTP status, and redacted request/response previews

If the request carries a W3C `traceparent` (or `x-runactuals-trace-id`), the
span joins that run. Otherwise the proxy opens a new run. Set
`x-runactuals-agent` to name the bot (default `proxy`). Streaming responses
are fully buffered so usage on the final chunk can be recorded; the client
still receives the complete stream.

Cost rates go stale. Override them with a JSON file:

```bash
export RUNACTUALS_PRICES=./my-prices.json   # {"gemini-2.5-flash": {"input": 0.30, "output": 2.50}}
```

### MCP stdio wrapper

For servers that speak MCP over stdio (Content-Length framing or NDJSON):

```bash
runactuals wrap --agent lesson-builder -- my-mcp-server --flag
```

The wrapper sits between the client and the child, records `tools/call` (and
other JSON-RPC methods), and exits with the child's status.

### Routing Why Next? lesson-builder Gemini calls

The lesson builder can keep its existing Gemini client and only change the
base URL (the API key stays in the environment and is never written to the
store):

```python
from google import genai
client = genai.Client(http_options={"base_url": "http://127.0.0.1:8787/gemini"})
```

Each `generateContent` / `embedContent` call then shows up on the RunActuals
dashboard with token counts and price-table cost, so reports no longer need a
per-run Gemini estimate. To attach those calls to the builder's own run,
propagate a `traceparent` header from the SDK span, or set
`x-runactuals-trace-id` and `x-runactuals-agent: lesson-builder`.

## API

- `POST /v1/traces`: OTLP/HTTP. Set `RUNACTUALS_INGEST_TOKEN` to require `Authorization: Bearer …`.
- `GET /`: dashboard. `GET /runs/<trace_id>`: one run.
- `GET /api/report`: the four checks as JSON. `GET /healthz`.
- Proxy (default `http://127.0.0.1:8787`, or `serve --proxy-port`): `GET /healthz`, plus the path prefixes in the proxy section above.

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
- True streamed (not buffered) proxy responses; Azure OpenAI path mapping; live price feeds.

## License

Apache-2.0
