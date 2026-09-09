// Shapes returned by the Lambda, and the local chat-message model built on
// top of them. Mirrors the Python side's contract (see backend/lambda_function.py
// and README.md's "How the UI maps to the Lambda's contract" table) -- kept
// intentionally loose (most fields optional) since API Gateway's own
// rejections (403/429/502/504) never reach the Lambda and arrive in a
// different shape ({"message": ...}) than the Lambda's own responses do.
export interface LambdaPayload {
  // Success
  answer?: string;
  database?: string;
  schema?: string;
  tables_used?: string[];
  sql?: string;
  columns?: string[];
  rows?: Record<string, unknown>[];
  row_count?: number;
  total_matching_rows?: number | null;
  truncated?: boolean;
  note?: string;
  session_id?: string;

  // Clarification (HTTP 200, but not a completed answer)
  needs_clarification?: boolean;
  message?: string;
  candidates?: string[];

  // Error shapes (Lambda's own {"error","detail",...} or API Gateway's {"message"})
  error?: string;
  detail?: string;
  reason?: string;
  message_from_gateway?: string; // set locally; API Gateway's raw field is "message"
  available_tables?: string[];
  valid_targets?: string[];
  table_count?: number;
  repair_attempts?: number;

  [key: string]: unknown;
}

export interface QueryResult {
  ok: boolean;
  elapsed: number;
  statusCode: number | null;
  payload: LambdaPayload;
  error: string;
  detail: string;
}

export function isTruncated(r: QueryResult): boolean {
  return Boolean(r.payload.truncated);
}

export function needsClarification(r: QueryResult): boolean {
  return Boolean(r.payload.needs_clarification);
}

// --------------------------------------------------------------------------
// Chat transcript model (local to the UI, not sent to/from the Lambda as-is)
// --------------------------------------------------------------------------

export interface UserMessage {
  role: "user";
  id: string;
  content: string;
}

export interface AssistantMessage {
  role: "assistant";
  id: string;
  result: QueryResult;
}

export type ChatMessage = UserMessage | AssistantMessage;
