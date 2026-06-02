// Mirrors the normalized shapes returned by the agent service's trace endpoints
// (app/traces.py). Kept loose (payload is per-kind) on purpose.

export type NodeKind = 'llm' | 'tool' | 'http' | 'business';

export type TraceNode = {
  span_id: string;
  parent_id: string | null;
  name: string;
  kind: NodeKind;
  label: string;
  service?: string | null;
  model?: string | null;
  provider?: string | null;
  operation?: string | null;
  duration_ms?: number | null;
  start_ns: number;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cost?: number | null;
  error: boolean;
  payload: LlmPayload | ToolPayload | GenericPayload;
  children: TraceNode[];
};

export type ChatPart = { type?: string; content?: string };
export type ChatMessage = {
  role?: string;
  content?: string;
  parts?: ChatPart[];
  finish_reason?: string;
};

export type LlmPayload = {
  input_messages?: ChatMessage[] | string;
  output_messages?: ChatMessage[] | string;
  system_instructions?: ChatPart[] | string;
  finish_reasons?: string[];
};

export type ToolPayload = {
  tool_name?: string;
  arguments?: unknown;
  result?: unknown;
};

export type GenericPayload = {
  attributes?: Record<string, unknown>;
};

export type Rollup = {
  span_count: number;
  llm_calls: number;
  tool_calls: number;
  error_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  total_tokens: number;
  cost: number | null;
  models: string[];
};

export type TraceDetail = {
  traceID: string;
  roots: TraceNode[];
  rollup: Rollup;
};

export type TraceSummary = {
  traceID: string;
  rootServiceName?: string;
  rootTraceName?: string;
  durationMs?: number | null;
  startTimeUnixNano?: string;
};
