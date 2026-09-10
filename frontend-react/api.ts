// Thin client for the natural-language -> Snowflake query Lambda, called
// directly from the browser via API Gateway (see README.md for the CORS
// setup this requires on the API Gateway side -- the Lambda's own responses
// already carry Access-Control-Allow-Origin: *, see backend/lambda_function.py
// _respond(), but the OPTIONS preflight has to be handled by API Gateway
// itself before a request ever reaches the Lambda).
//
// This is a TypeScript port of frontend/api_client.py -- kept behaviourally
// identical (same error-shape handling, same 403/429/502/504 hints) so the
// two UIs give consistent troubleshooting information.

import type { LambdaPayload, QueryResult } from "./types";

export const DEFAULT_TIMEOUT_SECONDS = 60;
export const APIGW_TIMEOUT_HINT_SECONDS = 29;

// Sent as DATABASE/SCHEMA to deliberately fail the Lambda's _resolve_target,
// which replies 400 with the trusted target list. Cheaper than a dedicated
// /targets endpoint. Mirrors api_client.py's _PROBE_SENTINEL.
const PROBE_SENTINEL = "__DISCOVER_TARGETS__";

function gatewayErrorHint(statusCode: number, message: string, headers: Headers): string {
  const lowered = (message || "").trim().toLowerCase();
  const errorType = headers.get("x-amzn-errortype") || "";
  const requestId = headers.get("x-amzn-requestid") || headers.get("x-amz-apigw-id") || "";

  const lines: string[] = [];

  if (statusCode === 403 && lowered.includes("missing authentication token")) {
    lines.push(
      "API Gateway could not match the request to a route. Despite the wording, " +
        "this usually means the URL path or HTTP method is wrong, not that a " +
        "credential is missing. Check that the URL includes the stage AND the " +
        "resource path (e.g. .../dev/query, not just .../dev), and that the " +
        "resource has a POST method deployed."
    );
  } else if (statusCode === 403 && lowered.includes("forbidden")) {
    lines.push(
      "The route matched but the request was refused. Most likely one of: the " +
        "method has 'API key required' enabled and no valid x-api-key was sent; " +
        "an authorizer (Cognito/Lambda/IAM) is attached and rejected the call; or " +
        "a resource policy / WAF rule blocked the caller's IP. Could also be CORS " +
        "-- check the browser console for a CORS error rather than trusting this " +
        "message alone."
    );
  } else if (statusCode === 403) {
    lines.push(
      "API Gateway refused the request before it reached the Lambda. Check " +
        "authorizers, API key requirements, CORS configuration, and any resource " +
        "policy or WAF rule."
    );
  } else if (statusCode === 429) {
    lines.push("Throttled by an API Gateway usage plan or account-level rate limit.");
  } else if (statusCode === 502) {
    lines.push(
      "Bad Gateway: the Lambda errored or returned a malformed proxy response. " +
        "Check the Lambda's CloudWatch logs for a traceback."
    );
  } else if (statusCode === 504) {
    lines.push(
      `Gateway timeout: the Lambda exceeded the integration timeout (~${APIGW_TIMEOUT_HINT_SECONDS}s ` +
        "by default). Try a Table hint to skip the table-selection Bedrock call."
    );
  }

  if (errorType) lines.push(`x-amzn-ErrorType: ${errorType}`);
  if (requestId) lines.push(`Request ID (for CloudWatch): ${requestId}`);

  return lines.join("\n");
}

// A CORS-blocked request never reaches this code with a status at all --
// the browser refuses to expose ANYTHING about the response (not even the
// status code) and fetch() just rejects with a generic TypeError. That
// ambiguity ("could be CORS, could be the network, could be the server being
// down") is inherent to the browser's fetch API, not something this client
// can resolve -- so the message below names all three rather than guessing.
const NETWORK_OR_CORS_HINT =
  "Could not reach the API. This is almost always one of: (1) the URL is wrong, " +
  "(2) the API Gateway endpoint doesn't have CORS enabled for this origin yet -- " +
  "see README.md, or (3) the network/API is actually down. Open the browser " +
  "console: a CORS failure logs an explicit CORS error there even though this " +
  "message can't tell the difference.";

async function postWithTimeout(
  url: string,
  payload: Record<string, unknown>,
  apiKey: string,
  timeoutSeconds: number
): Promise<{ response: Response; elapsed: number } | { timedOut: true; elapsed: number } | { networkError: string; elapsed: number }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSeconds * 1000);
  const started = performance.now();
  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (apiKey) headers["x-api-key"] = apiKey;

    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    return { response, elapsed: (performance.now() - started) / 1000 };
  } catch (exc) {
    const elapsed = (performance.now() - started) / 1000;
    if (controller.signal.aborted) {
      return { timedOut: true, elapsed };
    }
    return { networkError: exc instanceof Error ? exc.message : String(exc), elapsed };
  } finally {
    clearTimeout(timer);
  }
}

async function post(
  baseUrl: string,
  apiKey: string,
  timeoutSeconds: number,
  requestPayload: Record<string, unknown>
): Promise<QueryResult> {
  const outcome = await postWithTimeout(baseUrl, requestPayload, apiKey, timeoutSeconds);

  if ("timedOut" in outcome) {
    return {
      ok: false,
      elapsed: outcome.elapsed,
      statusCode: null,
      payload: {},
      error: `Request timed out after ${timeoutSeconds}s.`,
      detail:
        "The Lambda makes two Bedrock calls plus several Snowflake round-trips, " +
        "so a cold invocation can exceed the API Gateway integration timeout " +
        `(~${APIGW_TIMEOUT_HINT_SECONDS}s by default). Retry to hit a warm ` +
        "container, or narrow the request with a Table hint.",
    };
  }
  if ("networkError" in outcome) {
    return {
      ok: false,
      elapsed: outcome.elapsed,
      statusCode: null,
      payload: {},
      error: "Could not reach the API.",
      detail: NETWORK_OR_CORS_HINT,
    };
  }

  const { response, elapsed } = outcome;

  let body: unknown;
  const rawText = await response.text();
  try {
    body = rawText ? JSON.parse(rawText) : {};
  } catch {
    return {
      ok: false,
      elapsed,
      statusCode: response.status,
      payload: {},
      error: `API returned non-JSON response (HTTP ${response.status}).`,
      detail: rawText.slice(0, 2000),
    };
  }

  // API Gateway proxy integrations occasionally surface the Lambda body as a
  // JSON string rather than an object; unwrap that case (mirrors api_client.py).
  if (typeof body === "string") {
    const rawBodyString = body;
    try {
      body = JSON.parse(rawBodyString);
    } catch {
      return {
        ok: false,
        elapsed,
        statusCode: response.status,
        payload: {},
        error: "API returned an unparseable body.",
        detail: rawBodyString.slice(0, 2000),
      };
    }
  }

  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return {
      ok: false,
      elapsed,
      statusCode: response.status,
      payload: {},
      error: "API returned an unexpected body shape.",
      detail: JSON.stringify(body).slice(0, 2000),
    };
  }

  const payload = body as LambdaPayload;

  if (response.status >= 400 || "error" in payload) {
    // Two different error shapes reach us. The Lambda uses {"error","detail"};
    // API Gateway's own rejections (403/429/502...) never reach the Lambda
    // and use {"message"} instead.
    const gatewayMessage = (payload.message as string) || (payload["Message"] as string) || "";
    const error = payload.error || gatewayMessage || `HTTP ${response.status}`;
    let detail = payload.detail || "";
    if (!detail) {
      detail = gatewayErrorHint(response.status, gatewayMessage, response.headers);
    }
    return { ok: false, elapsed, statusCode: response.status, payload, error, detail };
  }

  return { ok: true, elapsed, statusCode: response.status, payload, error: "", detail: "" };
}

export interface AskOptions {
  table?: string;
  database?: string;
  schema?: string;
  sessionId?: string;
}

/** Run one natural-language question. `sessionId` carries conversation memory
 * across turns -- pass back whatever the previous QueryResult's session_id
 * was (blank on the first turn); the Lambda's response echoes the id to keep
 * using. If the Lambda has no SESSION_TABLE configured it ignores this and
 * never returns one, which degrades gracefully to stateless behaviour. */
export async function ask(
  baseUrl: string,
  apiKey: string,
  timeoutSeconds: number,
  query: string,
  options: AskOptions = {}
): Promise<QueryResult> {
  const payload: Record<string, unknown> = { query };
  if (options.table?.trim()) payload.Table = options.table.trim();
  if (options.database?.trim() && options.schema?.trim()) {
    payload.DATABASE = options.database.trim();
    payload.SCHEMA = options.schema.trim();
  }
  if (options.sessionId?.trim()) payload.session_id = options.sessionId.trim();
  return post(baseUrl.replace(/\/+$/, ""), apiKey, timeoutSeconds, payload);
}

/** Ask the Lambda which DATABASE.SCHEMA pairs it accepts, the same
 * deliberate-400-probe trick api_client.py uses (see PROBE_SENTINEL above).
 * Returns (targets, errorMessage). */
export async function discoverTargets(
  baseUrl: string,
  apiKey: string,
  timeoutSeconds: number
): Promise<{ targets: string[]; error: string }> {
  const result = await post(baseUrl.replace(/\/+$/, ""), apiKey, timeoutSeconds, {
    query: "discover valid targets",
    DATABASE: PROBE_SENTINEL,
    SCHEMA: PROBE_SENTINEL,
  });
  const targets = result.payload.valid_targets;
  if (targets && targets.length > 0) {
    return { targets, error: "" };
  }
  if (result.ok) {
    return { targets: [], error: "API accepted the probe target; could not infer the valid list." };
  }
  return { targets: [], error: result.detail || result.error };
}
