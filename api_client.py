"""Thin client for the natural-language -> Snowflake query Lambda (via API Gateway).

Kept separate from the UI so the request/response contract lives in one place
and can be exercised without Streamlit (see `python api_client.py --help`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

# API Gateway REST APIs cut the integration off at 29s by default (raisable via
# a service quota request; HTTP APIs are hard-capped at 30s). A client timeout
# above that is deliberate: it lets us distinguish "the gateway gave up" from
# "the network is wedged".
DEFAULT_TIMEOUT_SECONDS = 60
APIGW_TIMEOUT_HINT_SECONDS = 29

# Sent as DATABASE/SCHEMA to deliberately fail _resolve_target, which replies
# 400 with the trusted target list. Cheaper than adding a /targets endpoint.
_PROBE_SENTINEL = "__DISCOVER_TARGETS__"


@dataclass
class QueryResult:
    """Normalized outcome of one call. `ok` says whether to render a result
    table or an error; `elapsed` is always populated so the UI can surface how
    close a request ran to the gateway timeout."""

    ok: bool
    elapsed: float
    status_code: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    detail: str = ""

    # --- convenience accessors for the success path ---
    @property
    def answer(self) -> str:
        return self.payload.get("answer", "")

    @property
    def sql(self) -> str:
        return self.payload.get("sql", "")

    @property
    def columns(self) -> list[str]:
        return self.payload.get("columns") or []

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.payload.get("rows") or []

    @property
    def truncated(self) -> bool:
        return bool(self.payload.get("truncated"))


def _gateway_error_hint(status_code: int, message: str, headers: Any) -> str:
    """Turn an API Gateway rejection into something actionable.

    These responses never reach the Lambda, so there is no `detail` field to
    fall back on -- the cause has to be inferred from the status, the `message`
    string and the x-amzn-* headers.
    """
    msg = (message or "").strip()
    lowered = msg.lower()
    error_type = ""
    request_id = ""
    try:
        error_type = headers.get("x-amzn-ErrorType", "") or ""
        request_id = headers.get("x-amzn-RequestId", "") or headers.get("x-amz-apigw-id", "") or ""
    except Exception:  # noqa: BLE001 - headers is best-effort diagnostic data
        pass

    lines: list[str] = []

    if status_code == 403 and "missing authentication token" in lowered:
        lines.append(
            "API Gateway could not match the request to a route. Despite the wording, "
            "this usually means the URL path or HTTP method is wrong, not that a "
            "credential is missing. Check that the URL includes the stage AND the "
            "resource path (e.g. .../dev/query, not just .../dev), and that the "
            "resource has a POST method deployed."
        )
    elif status_code == 403 and "forbidden" in lowered:
        lines.append(
            "The route matched but the request was refused. Most likely one of: the "
            "method has 'API key required' enabled and no valid x-api-key was sent; "
            "an authorizer (Cognito/Lambda/IAM) is attached and rejected the call; or "
            "a resource policy / WAF rule blocked the caller's IP or VPC endpoint."
        )
    elif status_code == 403:
        lines.append(
            "API Gateway refused the request before it reached the Lambda. Check "
            "authorizers, API key requirements, and any resource policy or WAF rule."
        )
    elif status_code == 429:
        lines.append("Throttled by an API Gateway usage plan or account-level rate limit.")
    elif status_code == 502:
        lines.append(
            "Bad Gateway: the Lambda errored or returned a malformed proxy response. "
            "Check the Lambda's CloudWatch logs for a traceback."
        )
    elif status_code == 504:
        lines.append(
            "Gateway timeout: the Lambda exceeded the integration timeout (~29s by "
            "default). Try a Table hint to skip the table-selection Bedrock call."
        )

    if error_type:
        lines.append(f"x-amzn-ErrorType: {error_type}")
    if request_id:
        lines.append(f"Request ID (for CloudWatch): {request_id}")

    return "\n".join(lines)


class SnowflakeNLClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _post(self, payload: dict[str, Any]) -> QueryResult:
        started = time.monotonic()
        try:
            resp = requests.post(
                self.base_url, json=payload, headers=self._headers(), timeout=self.timeout
            )
        except requests.Timeout:
            return QueryResult(
                ok=False,
                elapsed=time.monotonic() - started,
                error=f"Request timed out after {self.timeout}s.",
                detail=(
                    "The Lambda makes two Bedrock calls plus several Snowflake round-trips, "
                    "so a cold invocation can exceed the API Gateway integration timeout "
                    f"(~{APIGW_TIMEOUT_HINT_SECONDS}s by default). Retry to hit a warm "
                    "container, or narrow the request with a Table hint."
                ),
            )
        except requests.RequestException as exc:
            return QueryResult(
                ok=False,
                elapsed=time.monotonic() - started,
                error="Could not reach the API.",
                detail=str(exc),
            )

        elapsed = time.monotonic() - started

        try:
            body = resp.json()
        except ValueError:
            return QueryResult(
                ok=False,
                elapsed=elapsed,
                status_code=resp.status_code,
                error=f"API returned non-JSON response (HTTP {resp.status_code}).",
                detail=resp.text[:2000],
            )

        # API Gateway proxy integrations occasionally surface the Lambda body as
        # a JSON string rather than an object; unwrap that case.
        if isinstance(body, str):
            try:
                import json

                body = json.loads(body)
            except ValueError:
                return QueryResult(
                    ok=False,
                    elapsed=elapsed,
                    status_code=resp.status_code,
                    error="API returned an unparseable body.",
                    detail=body[:2000],
                )

        if not isinstance(body, dict):
            return QueryResult(
                ok=False,
                elapsed=elapsed,
                status_code=resp.status_code,
                error="API returned an unexpected body shape.",
                detail=repr(body)[:2000],
            )

        if resp.status_code >= 400 or "error" in body:
            # Two different error shapes reach us. The Lambda uses {"error",
            # "detail"}; API Gateway's own rejections (403/429/502...) never
            # reach the Lambda and use {"message"} instead -- reading only
            # "error" turned those into a bare "HTTP 403" with no cause.
            gateway_message = body.get("message") or body.get("Message") or ""
            error = body.get("error") or gateway_message or f"HTTP {resp.status_code}"
            detail = body.get("detail") or ""

            if not detail:
                hint = _gateway_error_hint(resp.status_code, gateway_message, resp.headers)
                if hint:
                    detail = hint

            return QueryResult(
                ok=False,
                elapsed=elapsed,
                status_code=resp.status_code,
                payload=body,
                error=error,
                detail=detail,
            )

        return QueryResult(ok=True, elapsed=elapsed, status_code=resp.status_code, payload=body)

    def ask(
        self,
        query: str,
        table: str = "",
        database: str = "",
        schema: str = "",
    ) -> QueryResult:
        """Run one natural-language question. Optional hints are omitted when
        blank -- the Lambda treats a missing DATABASE/SCHEMA as 'use the default
        target', and requires the two to be supplied together or not at all."""
        payload: dict[str, Any] = {"query": query}
        if table.strip():
            payload["Table"] = table.strip()
        if database.strip() and schema.strip():
            payload["DATABASE"] = database.strip()
            payload["SCHEMA"] = schema.strip()
        return self._post(payload)

    def discover_targets(self) -> tuple[list[str], str]:
        """Ask the Lambda which DATABASE.SCHEMA pairs it accepts.

        There is no dedicated endpoint for this, so we send a DATABASE/SCHEMA
        pair we know is invalid: _resolve_target rejects it with 400 and echoes
        `valid_targets`. This runs before any Bedrock call or user query, so it
        costs one Snowflake connection check and nothing else.

        Returns (targets, error_message).
        """
        result = self._post(
            {
                "query": "discover valid targets",
                "DATABASE": _PROBE_SENTINEL,
                "SCHEMA": _PROBE_SENTINEL,
            }
        )
        targets = result.payload.get("valid_targets")
        if targets:
            return list(targets), ""
        if result.ok:
            # The sentinel was somehow accepted -- shouldn't happen, but don't
            # pretend we learned something.
            return [], "API accepted the probe target; could not infer the valid list."
        return [], result.detail or result.error


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Smoke-test the text-to-SQL API.")
    parser.add_argument("url", help="Full API Gateway invoke URL for the query resource")
    parser.add_argument("question", nargs="?", help="Question to ask (omit to just list targets)")
    parser.add_argument("--table", default="", help="Optional Table hint (comma-separated)")
    parser.add_argument("--database", default="")
    parser.add_argument("--schema", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    client = SnowflakeNLClient(args.url, args.api_key, args.timeout)

    if not args.question:
        found, err = client.discover_targets()
        print(f"valid targets: {found}" if found else f"could not discover targets: {err}")
        raise SystemExit(0)

    out = client.ask(args.question, args.table, args.database, args.schema)
    print(f"--- {'OK' if out.ok else 'FAILED'} in {out.elapsed:.1f}s (HTTP {out.status_code}) ---")
    print(json.dumps(out.payload, indent=2, default=str)[:4000])
