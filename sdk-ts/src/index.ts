// RunActuals TypeScript SDK: stub.
//
// TODO(v0.2): port src/runactuals/sdk.py. Same surface, same span attributes
// (see src/runactuals/conventions.py), OTLP/JSON over fetch(), zero dependencies.

export type ClaimedStatus = "succeeded" | "failed" | "interrupted";

export interface RunRecorder {
  llm(model: string, usage: { inputTokens?: number; outputTokens?: number; costUsd?: number }): void;
  tool(name: string, args?: unknown, opts?: { error?: string; retry?: boolean; durationMs?: number }): void;
  claim(kind: string, attrs?: { summary?: string; branch?: string; revision?: string }): void;
  audit(action: string, target: string, opts?: { actor?: string; approvalRef?: string; spendUsd?: number }): void;
  quality(score: number): void;
  end(status?: ClaimedStatus, message?: string): Promise<void>;
}

export interface Recorder {
  run(agent: string, opts?: { title?: string }): RunRecorder;
}

export function createRecorder(_endpoint = "http://localhost:4318"): Recorder {
  throw new Error("RunActuals TypeScript SDK is not implemented yet; use the Python SDK or raw OTLP.");
}
