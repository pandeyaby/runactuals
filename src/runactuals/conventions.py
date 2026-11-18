"""Attribute names RunActuals reads and writes.

``gen_ai.*`` names follow the OpenTelemetry GenAI semantic conventions
(open-telemetry/semantic-conventions-genai). As of November 2026 every one of
them is still marked Development, so expect renames. ``vcs.*`` names follow the
OpenTelemetry VCS conventions. ``runactuals.*`` is this project's own namespace
for things the upstream conventions do not cover yet: claims, evidence, audit.
"""

# OpenTelemetry GenAI
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ARGS = "gen_ai.tool.call.arguments"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

OP_INVOKE_AGENT = "invoke_agent"
OP_EXECUTE_TOOL = "execute_tool"
OP_CHAT = "chat"

# OpenTelemetry VCS
VCS_REF_HEAD_NAME = "vcs.ref.head.name"
VCS_REF_HEAD_REVISION = "vcs.ref.head.revision"
VCS_REPOSITORY_NAME = "vcs.repository.name"

# RunActuals
RA_CLAIMED_STATUS = "runactuals.run.claimed_status"  # succeeded | failed | interrupted
RA_STATUS_MESSAGE = "runactuals.run.status_message"
RA_COST_USD = "runactuals.cost.usd"
RA_QUALITY = "runactuals.quality.score"  # 0..1, e.g. eval pass rate
RA_RETRY = "runactuals.retry"
RA_CLAIM_KIND = "runactuals.claim.kind"
RA_CLAIM_SUMMARY = "runactuals.claim.summary"
RA_AUDIT_ACTION = "runactuals.audit.action"  # approval | send | write | spend | post | delete
RA_AUDIT_TARGET = "runactuals.audit.target"
RA_AUDIT_ACTOR = "runactuals.audit.actor"
RA_AUDIT_APPROVAL = "runactuals.audit.approval_ref"
RA_SPEND_USD = "runactuals.spend.usd"

SPAN_CLAIM = "runactuals.claim"
SPAN_AUDIT = "runactuals.audit"

CLAIMED_SUCCEEDED = "succeeded"
CLAIMED_FAILED = "failed"
CLAIMED_INTERRUPTED = "interrupted"

# Actions that should always carry an approval reference.
SENSITIVE_ACTIONS = frozenset({"send", "spend", "post", "delete", "merge", "publish"})
