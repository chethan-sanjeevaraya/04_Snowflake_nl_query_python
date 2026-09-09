# Snowflake NL Query — chat UI

Streamlit front end for the natural-language → Snowflake text-to-SQL Lambda
(behind API Gateway).

The UI calls the API **server-side**, from the Streamlit process. Nothing about
the endpoint reaches the user's browser, so there is no CORS configuration to do
and no API key embedded in client-side JavaScript.

## Two frontends

This Streamlit app (`frontend/`) and a React + TypeScript app (`frontend-react/`)
are both maintained, side by side, against the same Lambda -- pick whichever
fits how you want to run or share it:

| | `frontend/` (this one) | `frontend-react/` |
|---|---|---|
| Calls the Lambda | Server-side, from the Streamlit process | Directly from the browser |
| Requires | `streamlit run app.py` kept running | Any static file host (or none, for local dev) |
| CORS setup needed | No | Yes, once -- see `frontend-react/README.md` |
| API URL/key visible in the browser | No | Yes (see that README's security note) |

Both implement the same feature set: conversation memory, clarification
questions, table hints, target discovery, SQL/result rendering. See
`frontend-react/README.md` for that app's setup, build, and deploy steps.

## Run it

```bash
pip install -r requirements.txt
```

```bash
streamlit run app.py
```

Paste your API Gateway invoke URL into the sidebar, or set it once:

```bash
export SNOWFLAKE_NL_API_URL="https://abc123.execute-api.eu-central-1.amazonaws.com/dev/query"
```

On Windows PowerShell:

```bash
$env:SNOWFLAKE_NL_API_URL = "https://abc123.execute-api.eu-central-1.amazonaws.com/dev/query"
```

Alternatively create `.streamlit/secrets.toml`:

```toml
api_url = "https://abc123.execute-api.eu-central-1.amazonaws.com/dev/query"
api_key = ""   # only if the endpoint requires x-api-key
```

## Test the API without the UI

```bash
python api_client.py "https://.../dev/query" "how many events were created last month?"
```

Omit the question to just list the DATABASE.SCHEMA pairs the Lambda accepts.

To exercise conversation memory / clarification from the CLI, carry the printed
`session_id` forward on the next call:

```bash
python api_client.py "https://.../dev/query" "how many events were held?"
# -> needs_clarification: ... candidates: ['EM_EVENT', ...]; session_id: abc123

python api_client.py "https://.../dev/query" "EM_EVENT" --session-id abc123
# -> answers the ORIGINAL question ("how many events were held?") against EM_EVENT

python api_client.py "https://.../dev/query" "now just GB" --session-id abc123
# -> refines the previous turn's query using the same session
```

## ⚠️ This is a dev-testing setup — it has no authentication

You chose "just me / dev testing", so nothing here authenticates the caller. Two
things to be aware of before this goes anywhere near other people:

1. **The Streamlit app is unauthenticated.** Anyone who can reach the port can
   run queries against Snowflake. Bind it to localhost (Streamlit's default) and
   do not expose it publicly.
2. **The API Gateway endpoint is presumably unauthenticated too.** Anyone with
   the URL can query Snowflake directly, without this UI. Before sharing the app
   with anyone, add a Cognito JWT authorizer or an API key + usage plan on the
   API Gateway side — the sidebar already supports sending `x-api-key`.

## How the UI maps to the Lambda's contract

| Lambda response field | Rendered as |
|---|---|
| `answer` | Assistant message text |
| `sql` | Collapsible "SQL" code block |
| `rows` / `columns` | Dataframe + CSV download |
| `truncated`, `total_matching_rows` | Warning banner above the table |
| `note` | Caption (e.g. the column-narrowing note) |
| `database`, `schema`, `tables_used` | Caption line under the answer |
| `error`, `detail`, `reason`, `sql` | Error block, with the failing SQL expanded |
| `available_tables`, `valid_targets` | Expander / caption on the error |
| `session_id` | Stored client-side, sent back on the next request (conversation memory) |
| `needs_clarification`, `message`, `candidates` | Assistant message asking which table you meant, with a button per candidate |

Target discovery has no dedicated endpoint, so `discover_targets()` sends a
deliberately invalid `DATABASE`/`SCHEMA` pair; the Lambda's `_resolve_target`
rejects it with HTTP 400 and echoes `valid_targets`. That happens before any
Bedrock call, so the probe is cheap. If you'd rather not rely on an error path,
add a `GET /targets` route that returns `_valid_targets(db_info)`.

## Troubleshooting

### HTTP 403

A 403 comes from API Gateway itself — the request never reached your Lambda, so
there are no CloudWatch logs for it on the Lambda side. The cause is in the
response's `message` field, which the UI now surfaces under **Detail**:

| `message` | Cause |
|---|---|
| `Missing Authentication Token` | **No route matched.** Almost always a wrong URL path or method — not a missing credential, despite the wording. The URL needs the stage *and* the resource: `.../dev/query`, not `.../dev`. Also confirm a `POST` method exists and the stage has been redeployed since you added it. |
| `Forbidden` | Route matched, request refused. Either "API key required" is enabled on the method (send one via the sidebar), an authorizer rejected the call, or a resource policy / WAF rule blocked the caller. |
| `User ... not authorized` | IAM auth is on the method; the call needs SigV4 signing. |

Check it directly, bypassing the UI:

```bash
curl -i -X POST "https://<id>.execute-api.<region>.amazonaws.com/dev/query" -H "Content-Type: application/json" -d "{\"query\":\"test\"}"
```

Read the `x-amzn-ErrorType` response header — it names the exception precisely.
Note that an enterprise outbound proxy can also return its own 403 with an HTML
body; in that case the UI reports "non-JSON response" instead.

## Conversation memory and clarifying questions

The Lambda can now hold a real multi-turn conversation, backed by DynamoDB,
instead of treating every request as a one-off:

- **Session memory.** Every response includes a `session_id`; the UI stores it
  and sends it back on the next call. The Lambda uses it to remember prior
  turns (question, SQL, tables, row counts) and feeds that history into the
  SQL-generation prompt, so "now filter that to last quarter" or "same but for
  GB" refines the previous query instead of starting blind. A follow-up that
  refers to specific returned rows ("what was the second one about?") gets the
  previous SQL re-run fresh and those rows passed to the model for that one
  turn only — **no result rows are ever persisted** to DynamoDB; see the
  module docstring in `session_memory.py` for the exact data-governance
  boundary.
- **Ambiguous-table clarification.** When the Lambda can't confidently tell
  which table a question needs, it no longer just returns an HTTP error. It
  responds with `{"needs_clarification": true, "message": ..., "candidates":
  [...]}` (HTTP 200) and remembers, server-side, that it's waiting on an
  answer. Your next message — a table name, an alias, or just its number in
  the candidate list — resolves it and the Lambda answers the *original*
  question against that table. The UI renders this as an assistant message
  with clickable buttons for the suggested tables.
- **Turning it on**: set the `SESSION_TABLE` env var to a DynamoDB table name
  (partition key `session_id`, string; enable TTL on the `ttl` attribute).
  Leave it unset and the Lambda behaves exactly as before — stateless, no
  `session_id` in responses, every clarification failure is a plain error.
  Related tuning knobs: `SESSION_TTL_SECONDS`, `SESSION_MAX_TURNS`,
  `SESSION_MAX_ITEM_BYTES`, `REQUERY_ROW_PREVIEW` (all in `session_memory.py`).
- **DATABASE/SCHEMA stays restricted** to the same trusted pairs as before —
  conversation memory doesn't change what targets are queryable, only how
  follow-ups within one target are handled.

## Known rough edges (backend, not UI)

These are limits of the current Lambda that the UI can only surface, not fix:

- **Latency vs. the gateway timeout.** A cold request does `SELECT 1` → catalog
  query → Bedrock call #1 (table selection) → `APPROX_COUNT_DISTINCT` → up to N
  `SELECT DISTINCT` calls → Bedrock call #2 (SQL generation) → `COUNT(*)`
  wrapper → the real query. REST APIs default to a 29s integration timeout
  (raisable via a service quota request; HTTP APIs are hard-capped at 30s). The
  UI prints elapsed time on every response so you can see the headroom, and
  explains the likely cause on a timeout. Passing a **Table hint** skips the
  table-selection Bedrock call and scopes the metadata query — the single
  biggest win on a large schema.
- **No streaming.** The response arrives all at once, so the spinner is the only
  progress signal. Per-stage timing logs in the Lambda would tell you which
  stage actually dominates.
