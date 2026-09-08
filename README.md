# Snowflake NL Query — chat UI

Streamlit front end for the natural-language → Snowflake text-to-SQL Lambda
(behind API Gateway).

The UI calls the API **server-side**, from the Streamlit process. Nothing about
the endpoint reaches the user's browser, so there is no CORS configuration to do
and no API key embedded in client-side JavaScript.

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
- **No conversation memory.** The Lambda is stateless. The sidebar's *Send
  previous question as context* toggle prepends your last question to the new
  one client-side. It's a workaround; real multi-turn needs the handler to accept
  a `history` array and feed prior Q/SQL pairs into `_generate_sql`.
- **No streaming.** The response arrives all at once, so the spinner is the only
  progress signal. Per-stage timing logs in the Lambda would tell you which
  stage actually dominates.
